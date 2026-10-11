"""Unit tests for backend/core/shared_config.py.

Focuses on the pure / easily-isolated helpers: SSRF URL validation, question
normalisation, JSON load/save, jobs CRUD with file locking, credential refresh
behaviour, the memory-store singleton, the agent-history extractor, and the
memory-context builder. External boundaries (subprocess, MemoryStore) are
mocked; all file I/O lands in tmp dirs via the ``data_dir`` fixture.
"""
import json
import types

import pytest

from core import shared_config as sc


# ── validate_job_url ─────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.example.com/apply",
        "http://careers.acme.io",
        "https://www.linkedin.com/jobs/view/123",
    ],
)
def test_validate_job_url_allows_public_http(url):
    """Public http(s) URLs are accepted."""
    assert sc.validate_job_url(url) is True


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/x",
        "http://127.0.0.1/x",
        "http://10.0.0.5/x",
        "http://192.168.1.1/x",
        "http://172.16.0.1/x",
        "http://169.254.1.1/x",
        "http://0.0.0.0/x",
        "ftp://example.com/x",  # non-http scheme
        "not a url",
        "",
        "https://",  # no host
    ],
)
def test_validate_job_url_rejects_private_or_invalid(url):
    """Private/internal hosts, non-http schemes, and garbage are rejected."""
    assert sc.validate_job_url(url) is False


@pytest.mark.parametrize("url", [
    "https://[::1]/x", "http://[::ffff:127.0.0.1]/", "http://[fd00::1]/", "http://2130706433/",
    "http://127.1/", "http://169.254.169.254/latest", "http://app.localhost/",
])
def test_validate_job_url_refuses_every_form_of_a_local_address(url):
    assert sc.validate_job_url(url, resolve=False) is False


def test_validate_job_url_refuses_names_that_resolve_to_the_local_network(monkeypatch):
    import socket
    addresses = {"jobs.example.com": "93.184.216.34", "router.example.com": "192.168.1.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (addresses[host], 0))])
    assert sc.validate_job_url("https://jobs.example.com/1") is True
    assert sc.validate_job_url("http://router.example.com/") is False


def test_secrets_are_redacted(tmp_path, monkeypatch):
    import json
    monkeypatch.setattr(sc, "_secret_cache", {"key": None, "values": ()})
    (tmp_path / "settings.json").write_text(json.dumps(
        {"sensitive_data": {"email": "ada@example.com", "password": "Hunter2-Secret!"}, "captcha_api_key": "abcdef123456"}))
    (tmp_path / "llm_settings.json").write_text(json.dumps({"openai": {"api_key": "sk-test-000111"}}))
    import core.config as cfg
    monkeypatch.setattr(cfg, "get_data_dir", lambda: tmp_path)
    text = "used Hunter2-Secret! with key abcdef123456 and sk-test-000111 for ada@example.com"
    assert sc.redact_secrets(text) == "used *** with key *** and *** for ada@example.com"
    assert sc.redact_secrets(None) is None


# ── normalize_question ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Hello, World!", "hello world"),
        ("  What's your name?  ", "whats your name"),
        ("Years   of   experience???", "years   of   experience"),
        ("UPPER", "upper"),
        ("", ""),
    ],
)
def test_normalize_question(raw, expected):
    """Punctuation is stripped, case lowered, and edges trimmed."""
    assert sc.normalize_question(raw) == expected


# ── load_json / save_json ────────────────────────────────────────────────────

def test_load_json_missing_default_list(tmp_path):
    """Missing file with no default returns an empty list (not dict)."""
    assert sc.load_json(tmp_path / "x.json") == []


def test_load_json_missing_explicit_default(tmp_path):
    """An explicit default is returned when the file is absent."""
    assert sc.load_json(tmp_path / "x.json", {"a": 1}) == {"a": 1}


def test_save_then_load_json_roundtrip(tmp_path):
    """Saved JSON is read back verbatim and no temp file lingers."""
    p = tmp_path / "nested" / "data.json"
    sc.save_json(p, {"k": [1, 2, 3]})
    assert sc.load_json(p) == {"k": [1, 2, 3]}
    assert not p.with_suffix(".tmp").exists()


# ── jobs CRUD (read/write/update/claim) ──────────────────────────────────────

@pytest.fixture
def jobs_paths(tmp_path, monkeypatch):
    """Point the jobs file/lock at a tmp location for isolation."""
    jf = tmp_path / "jobs.json"
    monkeypatch.setattr(sc, "JOBS_FILE", jf)
    monkeypatch.setattr(sc, "JOBS_LOCK", tmp_path / "jobs.json.lock")
    return jf


def test_read_jobs_empty(jobs_paths):
    """Reading before any write yields an empty dict."""
    assert sc.read_jobs() == {}


def test_write_then_read_jobs(jobs_paths):
    """write_jobs persists and read_jobs returns it."""
    sc.write_jobs({"u1": {"status": "pending"}})
    assert sc.read_jobs() == {"u1": {"status": "pending"}}


def test_update_job_existing(jobs_paths):
    """update_job merges fields into an existing entry."""
    sc.write_jobs({"u1": {"status": "pending", "title": "Eng"}})
    sc.update_job("u1", status="done", company="Acme")
    jobs = sc.read_jobs()
    assert jobs["u1"] == {"status": "done", "title": "Eng", "company": "Acme"}


def test_update_job_missing_is_noop(jobs_paths):
    """Updating an unknown URL leaves the store unchanged."""
    sc.write_jobs({"u1": {"status": "pending"}})
    sc.update_job("ghost", status="done")
    assert sc.read_jobs() == {"u1": {"status": "pending"}}


def test_claim_job_success(jobs_paths):
    """A pending job is claimed and flipped to in_progress."""
    sc.write_jobs({"u1": {"status": "pending"}})
    assert sc.claim_job("u1") is True
    assert sc.read_jobs()["u1"]["status"] == "in_progress"


def test_claim_job_already_taken(jobs_paths):
    """A non-pending job cannot be claimed."""
    sc.write_jobs({"u1": {"status": "in_progress"}})
    assert sc.claim_job("u1") is False


def test_claim_job_unknown(jobs_paths):
    """Claiming an unknown URL returns False."""
    sc.write_jobs({})
    assert sc.claim_job("nope") is False


# ── refresh_credentials ──────────────────────────────────────────────────────

def test_refresh_credentials_no_cmd_returns_true(monkeypatch):
    """With no ADA_CMD configured, refresh is a silent success."""
    monkeypatch.setattr(sc, "ADA_CMD", ())
    assert sc.refresh_credentials() is True


def test_refresh_credentials_success(monkeypatch):
    """A successful subprocess run returns True."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))
    calls = {}

    def fake_run(cmd, **kwargs):
        calls["cmd"] = cmd
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is True
    assert calls["cmd"] == ("ada", "creds")


def test_refresh_credentials_failure(monkeypatch):
    """A CalledProcessError is caught and reported as False."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))

    def fake_run(cmd, **kwargs):
        raise sc.subprocess.CalledProcessError(1, cmd)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is False


def test_refresh_credentials_timeout(monkeypatch):
    """A TimeoutExpired is caught and reported as False."""
    monkeypatch.setattr(sc, "ADA_CMD", ("ada", "creds"))

    def fake_run(cmd, **kwargs):
        raise sc.subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(sc.subprocess, "run", fake_run)
    assert sc.refresh_credentials() is False


# ── get_memory_store singleton ───────────────────────────────────────────────

def test_get_memory_store_is_singleton(monkeypatch):
    """get_memory_store constructs once and caches the instance."""
    instances = []

    class FakeStore:
        def __init__(self):
            instances.append(self)

    monkeypatch.setattr(sc, "MemoryStore", FakeStore)
    monkeypatch.setattr(sc, "_memory_store", None)
    first = sc.get_memory_store()
    second = sc.get_memory_store()
    assert first is second
    assert len(instances) == 1


# ── extract_from_history ─────────────────────────────────────────────────────

def _history_item(memory_text):
    """Build a fake agent-history item exposing model_output.memory."""
    model_output = types.SimpleNamespace(memory=memory_text)
    return types.SimpleNamespace(model_output=model_output)


def _result(items):
    return types.SimpleNamespace(history=items)


def test_extract_from_history_jobs_and_questions():
    """Tagged JOB_APPLIED and QUESTION markers are parsed out."""
    mem = (
        '@@JOB_APPLIED: {"title": "Engineer", "company": "Acme", "location": "NYC"}\n'
        '@@QUESTION: {"question": "Visa?", "answer": "No", "type": "radio"}'
    )
    jobs, questions = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["Engineer at Acme - NYC"]
    assert questions == {"Visa?": "No"}


def test_extract_from_history_dedupes_questions():
    """Questions that normalise identically are deduplicated."""
    mem = (
        '@@QUESTION: {"question": "Your Name?", "answer": "A"}\n'
        '@@QUESTION: {"question": "your name", "answer": "B"}'
    )
    _, questions = sc.extract_from_history(_result([_history_item(mem)]))
    assert questions == {"Your Name?": "A"}


def test_extract_from_history_skips_empty_and_bad_json():
    """Items with no output and malformed JSON are ignored gracefully."""
    items = [
        types.SimpleNamespace(model_output=None),
        _history_item('@@JOB_APPLIED: {bad json}'),
        _history_item('no markers here'),
    ]
    jobs, questions = sc.extract_from_history(_result(items))
    assert jobs == []
    assert questions == {}


def test_extract_from_history_fallback_phrase():
    """Free-text 'applied to X via' is captured when no JSON marker exists."""
    mem = "Successfully applied to Senior Dev via the company site."
    jobs, _ = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["Senior Dev"]


def test_extract_from_history_deduplicates_jobs():
    """Duplicate job strings collapse to one (order preserved)."""
    mem = (
        '@@JOB_APPLIED: {"title": "X", "company": "Y", "location": "Z"}\n'
        '@@JOB_APPLIED: {"title": "X", "company": "Y", "location": "Z"}'
    )
    jobs, _ = sc.extract_from_history(_result([_history_item(mem)]))
    assert jobs == ["X at Y - Z"]


# ── build_memory_context ─────────────────────────────────────────────────────

@pytest.fixture
def base_profile():
    """A minimal-but-complete profile satisfying build_memory_context."""
    return {
        "name": "Ada Lovelace",
        "email": "ada@example.com",
        "phone": "5551234",
        "phone_country_code": "+1",
        "address": {"city": "London", "state": "", "zip": "", "country": "UK"},
        "work_authorization": "Citizen",
        "visa_sponsorship_needed": False,
        "willing_to_relocate": True,
        "preferred_work_mode": "remote",
        "years_of_experience": 5,
        "education": {"degree": "BSc", "school": "Cambridge", "graduation": "1850"},
        "current_role": "Mathematician",
        "target_locations": ["London", "Remote"],
        "languages": ["English"],
        "skills": ["Python", "Math"],
        "salary_expectation": {"min": 80000, "max": 120000, "currency": "GBP", "period": "annual"},
        "notice_period": "1 month",
        "nationality": "British",
        "cover_letter": "Dear Hiring Manager",
        "notes": "Pioneer",
        "date_format": "DD/MM/YYYY",
    }


@pytest.fixture
def no_op_store(monkeypatch):
    """Stub the memory store so build_memory_context skips DB/website memory."""
    class FakeStore:
        def qa_get_all_for_prompt(self, domain="", limit=None):
            return {}

        def get_domain_memories(self, url, limit=20):
            return []

        def extract_domain(self, url):
            return "example.com"

        def format_for_prompt(self, memories):
            return "MEM"

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    return FakeStore


def test_build_memory_context_basic(base_profile, no_op_store):
    """The context includes profile, salary, country instructions, and tracking."""
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "CANDIDATE PROFILE:" in ctx
    assert "Name: Ada Lovelace" in ctx
    assert "GBP 80,000-120,000 (annual)" in ctx
    assert "DATE FORMAT: When filling date fields, use DD/MM/YYYY" in ctx
    assert "NOTICE PERIOD: If asked about notice period" in ctx
    assert "NATIONALITY:" in ctx
    assert "COVER LETTER:" in ctx
    assert "TRACKING INSTRUCTIONS:" in ctx


def test_build_memory_context_includes_qa(base_profile, no_op_store):
    """A passed-in Q&A dict (with answers) is rendered into the prompt."""
    ctx = sc.build_memory_context(base_profile, qa={"Q1": "A1", "blank": ""})
    assert "Q: Q1\nA: A1" in ctx
    assert "blank" not in ctx  # empty answers are filtered out


def test_build_memory_context_applied_labels(base_profile, no_op_store):
    """applied_labels produce a SKIP section."""
    ctx = sc.build_memory_context(base_profile, qa={}, applied_labels=["Job A", "Job B"])
    assert "Already applied — SKIP:" in ctx
    assert "- Job A" in ctx


def test_build_memory_context_no_salary(base_profile, no_op_store):
    """No minimum and no maximum renders 'Not specified'; a maximum alone is 'up to'."""
    base_profile["salary_expectation"]["min"] = 0
    assert "Salary: GBP up to 120,000 (annual)" in sc.build_memory_context(base_profile, qa={})
    base_profile["salary_expectation"]["max"] = 0
    assert "Salary: Not specified" in sc.build_memory_context(base_profile, qa={})


def test_build_memory_context_default_date_format(base_profile, no_op_store):
    """A blank date_format falls back to MM/DD/YYYY."""
    base_profile["date_format"] = ""
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "use MM/DD/YYYY format" in ctx


def test_build_memory_context_injects_website_memory(base_profile, monkeypatch):
    """When a job_url is given, per-website memories are appended."""
    class FakeStore:
        def qa_get_all_for_prompt(self, domain="", limit=None):
            return {}

        def get_domain_memories(self, url, limit=20):
            return ["m1", "m2"]

        def extract_domain(self, url):
            return "acme.io"

        def format_for_prompt(self, memories):
            return "WEBSITE-MEMORY-BLOCK"

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    ctx = sc.build_memory_context(base_profile, qa={}, job_url="https://acme.io/job")
    assert "WEBSITE-MEMORY-BLOCK" in ctx


def test_build_memory_context_qa_db_overrides_passed_in(base_profile, monkeypatch):
    """SQLite Q&A takes precedence over the passed-in qa dict."""
    class FakeStore:
        def qa_get_all_for_prompt(self, domain="", limit=None):
            return {"DBQ": "DBA"}

        def get_domain_memories(self, url, limit=20):
            return []

        def extract_domain(self, url):
            return "x"

        def format_for_prompt(self, m):
            return ""

    monkeypatch.setattr(sc, "_memory_store", FakeStore())
    ctx = sc.build_memory_context(base_profile, qa={"OLDQ": "OLDA"})
    assert "Q: DBQ\nA: DBA" in ctx
    assert "OLDQ" not in ctx


def test_street_address_reaches_the_agent(base_profile, no_op_store):
    """Only city/state/zip used to be sent, so the town ended up in Address Line 1."""
    base_profile["address"] = {"street": "12 Example St", "city": "Springfield", "state": "NJ",
                               "zip": "12345", "country": "USA"}
    ctx = sc.build_memory_context(base_profile, qa={})
    assert ("Address: Street address (Address Line 1): 12 Example St | City: Springfield | "
            "State: NJ | ZIP / postal code: 12345 | Country: USA") in ctx


def test_empty_address_parts_are_left_out(base_profile, no_op_store):
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "Address: City: London | Country: UK" in ctx


def test_date_of_birth_only_when_set(base_profile, no_op_store):
    assert "Date of Birth" not in sc.build_memory_context({**base_profile, "date_of_birth": ""}, qa={})
    assert "Date of Birth: 06/07/2005" in sc.build_memory_context({**base_profile, "date_of_birth": "06/07/2005"}, qa={})


def test_profile_instructions_are_sent_as_rules(base_profile, no_op_store):
    ctx = sc.build_memory_context({**base_profile, "notes": "  Always click Apply with resume first.  "}, qa={})
    assert "USER'S INSTRUCTIONS" in ctx
    assert "Always click Apply with resume first." in ctx
    assert "Notes:" not in ctx
    assert "USER'S INSTRUCTIONS" not in sc.build_memory_context({**base_profile, "notes": "  "}, qa={})


@pytest.mark.parametrize("low,high,period,expected", [
    (20, 30, "hourly", "USD 20-30 (hourly)"),
    (50000, 0, "annual", "USD 50,000+ (no maximum) (annual)"),
    (50000, 40000, "annual", "USD 50,000+ (no maximum) (annual)"),
    (25, 25, "hourly", "USD 25 (hourly)"),
    (0, 90000, "annual", "USD up to 90,000 (annual)"),
    (0, 0, "annual", "Not specified"),
    (22.5, 30, "hourly", "USD 22.50-30 (hourly)"),
])
def test_describe_salary(low, high, period, expected):
    """A maximum of 0 used to read 'USD 50,000-0'."""
    assert sc._describe_salary("USD", low, high, period) == expected


def test_target_roles_reach_the_agent(base_profile, no_op_store):
    ctx = sc.build_memory_context({**base_profile, "target_job_titles": ["Software Intern", "Data Intern"]}, qa={})
    assert "Target Roles: Software Intern, Data Intern" in ctx


def test_users_answers_and_earlier_guesses_are_labelled(base_profile, monkeypatch):
    class Store:
        def qa_get_all_for_prompt(self, domain="", limit=None):
            return {"How did you hear about us?": "Company Website", "Do you smoke?": "No"}

        def qa_user_confirmed(self):
            return {"How did you hear about us?"}

        def get_domain_memories(self, url, limit=20):
            return []

        def extract_domain(self, url):
            return ""

    monkeypatch.setattr(sc, "get_memory_store", lambda: Store())
    ctx = sc.build_memory_context(base_profile, qa={})
    users = ctx.index("ANSWERS THE USER GAVE (always use these):\nQ: How did you hear about us?\nA: Company Website")
    earlier = ctx.index("Answers recorded in earlier applications (use one only when it agrees")
    assert users < earlier and "Q: Do you smoke?\nA: No" in ctx[earlier:]


def test_screening_answers_reach_the_agent(base_profile, no_op_store):
    base_profile["screening"] = {"start_date": "May 2027", "gpa": "3.6", "gender": "", "linkedin_url": "https://linkedin.com/in/ada"}
    ctx = sc.build_memory_context(base_profile, qa={})
    assert "SCREENING ANSWERS (the candidate's own answers):" in ctx
    assert "Start date: May 2027\nGPA: 3.6\nLinkedIn profile URL: https://linkedin.com/in/ada" in ctx
    assert "Gender" not in ctx.split("SCREENING ANSWERS")[1].split("\n\n")[0]


def test_no_screening_section_when_empty(base_profile, no_op_store):
    assert "SCREENING ANSWERS" not in sc.build_memory_context(base_profile, qa={})


@pytest.mark.parametrize("edu,expected", [
    ({"degree": "B.S.", "major": "Computer Science", "school": "State University", "graduation": "May 2028"},
     "B.S. in Computer Science from State University (May 2028)"),
    ({"degree": "B.S.", "major": "Computer Science", "school": "State University", "start_date": "September 2024", "graduation": "May 2028"},
     "B.S. in Computer Science from State University (started September 2024, graduation May 2028)"),
    ({"degree": "B.S.", "school": "State University", "start_date": "September 2024"},
     "B.S. from State University (started September 2024)"),
])
def test_education_includes_the_start_date(edu, expected):
    assert sc._describe_education(edu) == expected


def test_default_profile_has_an_education_start_date():
    from core.config import DEFAULT_PROFILE
    assert "start_date" in DEFAULT_PROFILE["education"]


def test_learning_instructions_ask_for_few_new_learnings():
    from core import shared_config
    import inspect
    source = inspect.getsource(shared_config.build_memory_context)
    assert "at most 3 per run" in source and "at least 2-3" not in source


@pytest.mark.parametrize("name,expected", [
    ("Ada Lovelace", "Name: Ada Lovelace (First name: Ada | Last name: Lovelace)"),
    ("Mary Ann Evans", "Name: Mary Ann Evans (First name: Mary Ann | Last name: Evans)"),
    ("Cher", "Name: Cher"),
    ("", "Name: "),
])
def test_name_is_split_for_first_and_last_name_fields(name, expected):
    assert sc._describe_name(name) == expected



# ── batch 1 ────────────────────────────────────────────────────────────────
import datetime as _dt


@pytest.mark.parametrize("value,expected", [
    ("Immediately", "Summer -> 05/15/YEAR; Fall -> 08/15/YEAR; Spring/Winter -> 01/02/YEAR. For any other job, today: 10/08/2026."),
    ("ASAP", "today: 10/08/2026"),
    ("June 1, 2027", "Start date: June 1, 2027"),
])
def test_start_date_rule(value, expected):
    assert expected in sc.describe_start_date(value, "MM/DD/YYYY", _dt.date(2026, 10, 8))


def test_empty_start_date_says_nothing():
    assert "Start date" not in sc._describe_screening({"start_date": "", "gpa": "3.0"})


def test_major_alternative_reaches_the_agent(base_profile, no_op_store):
    base_profile["education"]["major_alternative"] = "Finance"
    assert "Field of study if yours isn't listed: Finance" in sc.build_memory_context(base_profile, {})


def test_performance_mode_drops_per_step_learning_notes(base_profile, no_op_store):
    with_notes = sc.build_memory_context(base_profile, {})
    without = sc.build_memory_context(base_profile, {}, learnings=False)
    assert "SELF-LEARNING" in with_notes and "SELF-LEARNING" not in without
    assert "@@QUESTION" in without                                   # Q&A saving stays


def _jobs_file(data_dir, monkeypatch, jobs):
    import json as _json
    path = data_dir / "jobs.json"
    path.write_text(_json.dumps(jobs), encoding="utf-8")
    monkeypatch.setattr(sc, "JOBS_FILE", path)
    monkeypatch.setattr(sc, "JOBS_LOCK", str(path) + ".lock")
    return path


def test_set_job_names_never_touches_user_names(data_dir, monkeypatch):
    import json as _json
    path = _jobs_file(data_dir, monkeypatch, {"u1": {"title": "Pasted Link", "company": ""},
                                              "u2": {"title": "Mine", "company": "My Co", "name_source": "user"}})
    assert sc.set_job_names("u1", "Data Intern", "Contoso Brokerage Services LLC") is True
    assert sc.set_job_names("u2", "Other", "Other Co") is False
    jobs = _json.loads(path.read_text(encoding="utf-8"))
    assert (jobs["u1"]["title"], jobs["u1"]["company"], jobs["u1"]["name_source"]) == ("Data Intern", "Contoso Brokerage Services", "agent")
    assert (jobs["u2"]["title"], jobs["u2"]["company"]) == ("Mine", "My Co")


def test_refresh_renames_page_names_but_not_agent_names(data_dir, monkeypatch):
    import json as _json
    from core.job_page import Posting
    path = _jobs_file(data_dir, monkeypatch, {
        "u1": {"title": "About the Firm", "company": "NWND Cooperative Northwind", "source": "pasted"},
        "u2": {"title": "Quant Intern", "company": "Stark Global", "name_source": "agent"},
    })
    sc.refresh_posting_details("u1", Posting(title="Analyst Intern", company="Northwind", locations=["New York, NY"]))
    sc.refresh_posting_details("u2", Posting(title="Something else", company="Stark Global, LLC"))
    jobs = _json.loads(path.read_text(encoding="utf-8"))
    assert (jobs["u1"]["title"], jobs["u1"]["company"], jobs["u1"]["location"]) == ("Analyst Intern", "Northwind", "New York, NY")
    assert (jobs["u2"]["title"], jobs["u2"]["company"]) == ("Quant Intern", "Stark Global")


def test_a_field_the_user_typed_is_kept_and_the_other_filled_in(jobs_paths):
    from core import job_page
    url = "https://jobs.example/1"
    sc.write_jobs({url: {"title": "My Title", "company": "", "status": "pending", "user_named": ["title"]}})
    sc.refresh_posting_details(url, job_page.Posting(title="Page Title", company="Acme"))
    job = sc.read_jobs()[url]
    assert (job["title"], job["company"]) == ("My Title", "Acme")
    sc.refresh_posting_details(url, job_page.Posting(title="Page Title 2", company="Acme"))
    sc.set_job_names(url, "Agent Title", "Acme Labs")
    job = sc.read_jobs()[url]
    assert job["title"] == "My Title" and job["company"] == "Acme Labs"


def test_browser_use_telemetry_is_off():
    import os
    import core  # noqa: F401  (the package switches it off on import)
    from browser_use.telemetry.service import ProductTelemetry
    assert os.environ["ANONYMIZED_TELEMETRY"] == "false"
    assert ProductTelemetry()._posthog_client is None
