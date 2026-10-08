"""Tests for cli/apply_jobs.py: what one application sends to the agent, and what it saves.

The browser-use Agent is replaced by a fake that records its constructor
arguments, so these run without a browser or an LLM.
"""
import asyncio
from types import SimpleNamespace

import pytest

from cli import apply_jobs

GREENHOUSE = "https://boards.greenhouse.io/acme/jobs/123"
LINKEDIN = "https://www.linkedin.com/jobs/view/4242/"
GOOD_PROFILE = {"name": "Ada Lovelace", "email": "ada@example.com", "address": {"city": "Boston", "state": "MA", "zip": "02110"}}


# ── build_agent_secrets ─────────────────────────────────────────────────────

class TestBuildAgentSecrets:
    def test_valid_profile_and_account_emails(self):
        s = apply_jobs.build_agent_secrets(GOOD_PROFILE, {"email": "login@example.com", "password": "hunter2hunter2"})
        assert s == {"email": "ada@example.com", "account_email": "login@example.com", "password": "hunter2hunter2"}

    def test_invalid_profile_email_falls_back_to_account_email(self):
        """A 4-letter typo must never be typed into forms or masked across page text."""
        s = apply_jobs.build_agent_secrets({"email": "Zork"}, {"email": "login@example.com", "password": "x" * 12})
        assert s["email"] == "login@example.com"
        assert "Zork" not in s.values()

    def test_no_valid_email_at_all_leaves_email_out(self):
        s = apply_jobs.build_agent_secrets({"email": "Zork"}, {"email": "", "password": "x" * 12})
        assert "email" not in s and "account_email" not in s

    def test_invalid_account_email_uses_the_form_email_for_accounts(self):
        s = apply_jobs.build_agent_secrets(GOOD_PROFILE, {"email": "nope", "password": ""})
        assert s["account_email"] == "ada@example.com"

    @pytest.mark.parametrize("password,kept", [("", False), ("abc", False), ("abcdef", True)])
    def test_short_or_empty_password_is_dropped(self, password, kept):
        s = apply_jobs.build_agent_secrets(GOOD_PROFILE, {"email": "", "password": password})
        assert ("password" in s) is kept


@pytest.mark.parametrize("value,expected", [
    ("a@b.co", True), ("ada.lovelace+jobs@example.org", True),
    ("Zork", False), ("a@b", False), ("a b@c.d", False), ("", False),
])
def test_looks_like_email(value, expected):
    assert apply_jobs._looks_like_email(value) is expected


@pytest.mark.parametrize("stored,expected", [(None, 8), (3, 3), (0, 1), (99, 50), ("x", 8)])
def test_max_failures_reads_the_settings_value(monkeypatch, stored, expected):
    settings = {} if stored is None else {"max_failures": stored}
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings", lambda: settings)
    assert apply_jobs._max_failures() == expected


# ── apply_to_job with a fake Agent ──────────────────────────────────────────

class FakeHistory:
    """Like browser-use's AgentHistoryList: errors() has a None per step without one."""

    def __init__(self, success=True, steps=3, done_text=None, step_errors=None):
        self._success = success
        self._done_text = done_text
        self._errors = step_errors if step_errors is not None else ([None] * steps if success else [None] * (steps - 1) + ["form broken"])
        self.history = [object()] * steps

    def is_successful(self):
        return self._success

    def is_done(self):
        return self._done_text is not None

    def final_result(self):
        return self._done_text

    def errors(self):
        return self._errors


_run_fake_agent = [None]


@pytest.fixture
def run(data_dir, monkeypatch):
    """Run apply_to_job with everything external faked; returns (status, agent_kwargs, saved)."""
    created, saved = [], []

    class FakeAgent:
        def __init__(self, **kwargs):
            created.append(kwargs)
            self.kwargs = kwargs
            self.paused = False
            FakeAgent.instances.append(self)

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

        async def run(self, **kwargs):
            self.run_kwargs = kwargs
            if FakeAgent.crash:
                raise FakeAgent.crash
            if FakeAgent.call_step:
                await self.kwargs["register_new_step_callback"](None, None, 1)
            return self.history

        def stop(self):
            pass

    class FakeBrowser:
        def __init__(self, **kwargs):
            pass

        async def close(self):
            pass

    async def fake_save(url, status, error=None, agent_words=False):
        saved.append((status, error))

    async def no_stealth(browser):
        return False

    async def no_qa(*args, **kwargs):
        return None

    fake_store = SimpleNamespace(
        extract_domain=lambda url: "greenhouse.io",
        get_domain_memories=lambda url, limit=50: [],
        detect_ats_platform=lambda domain: None,
    )
    FakeAgent.instances = []
    FakeAgent.crash = None
    FakeAgent.call_step = False
    _run_fake_agent[0] = FakeAgent
    monkeypatch.setattr(apply_jobs, "Agent", FakeAgent)
    monkeypatch.setattr(apply_jobs, "_pause_state", {"paused": False})
    monkeypatch.setattr(apply_jobs, "BrowserSession", FakeBrowser)
    monkeypatch.setattr(apply_jobs, "claim_job", lambda url: True)
    monkeypatch.setattr(apply_jobs, "save_job_status", fake_save)
    monkeypatch.setattr(apply_jobs, "save_new_qa", no_qa)
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: fake_store)
    monkeypatch.setattr(apply_jobs, "extract_from_history", lambda result: ({}, {}))
    monkeypatch.setattr(apply_jobs, "extract_learnings_from_markers", lambda result: [])
    monkeypatch.setattr(apply_jobs, "MetricsStore", lambda: SimpleNamespace(record_run=lambda **kw: None))
    monkeypatch.setattr(apply_jobs, "BLOCKED_DOMAINS", [])
    monkeypatch.setattr(apply_jobs, "RESUME_PATH", "C:/resumes/ada.pdf")
    monkeypatch.setattr(apply_jobs, "SENSITIVE_DATA", {"email": "login@example.com", "password": "correct-horse-battery"})
    monkeypatch.setattr(apply_jobs.config, "get_llm", lambda: object())
    monkeypatch.setattr(apply_jobs.config, "maybe_apply_stealth", no_stealth)
    monkeypatch.setattr(apply_jobs.config, "build_browser_kwargs", lambda: {})
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings", lambda: {"max_failures": 6})

    def _run(url=GREENHOUSE, profile=GOOD_PROFILE, easy_apply=False, history=None):
        FakeAgent.history = history or FakeHistory()
        job = {"url": url, "title": "Intern", "company": "Acme", "location": "Boston"}
        status = asyncio.run(apply_jobs.apply_to_job(job, profile, {}, [], easy_apply=easy_apply, worker_id=1))
        return status, (created[-1] if created else None), saved

    _run.agents = FakeAgent.instances
    return _run


class TestAgentSetup:
    def test_opens_the_job_before_the_first_llm_step(self, run):
        _, kwargs, _ = run()
        assert kwargs["initial_actions"] == [{"navigate": {"url": GREENHOUSE, "new_tab": False}}]

    def test_judge_is_off_and_settings_are_wired(self, run):
        _, kwargs, _ = run()
        assert kwargs["use_judge"] is False
        assert kwargs["max_actions_per_step"] == 10
        assert kwargs["max_failures"] == 6          # Settings -> Max Failures Per Job
        assert kwargs["llm_timeout"] == 180         # was an ignored "llm_call_timeout"
        assert "llm_call_timeout" not in kwargs

    @pytest.mark.parametrize("url,easy", [(GREENHOUSE, False), (LINKEDIN, False), (LINKEDIN, True)])
    def test_no_up_front_login_checks(self, run, url, easy):
        _, kwargs, _ = run(url=url, easy_apply=easy)
        task = kwargs["task"]
        assert "LOGIN CHECK" not in task
        assert "linkedin.com/feed" not in task
        assert "mail.google.com/mail/u/0/#inbox" not in task
        assert "Wait up to 5 minutes" not in task
        assert "at most 4 times" in task            # bounded wait, only when needed

    def test_linkedin_sign_in_rule_only_for_linkedin(self, run):
        _, gh, _ = run(url=GREENHOUSE)
        assert "LINKEDIN SIGN-IN" not in gh["task"]
        _, li, _ = run(url=LINKEDIN)
        assert "LINKEDIN SIGN-IN" in li["task"]

    def test_secrets_use_the_valid_email(self, run):
        _, kwargs, _ = run(profile={**GOOD_PROFILE, "email": "Zork"})
        assert kwargs["sensitive_data"]["email"] == "login@example.com"
        assert "Zork" not in kwargs["extend_system_message"]


class TestNoValidEmail:
    def test_fails_fast_without_starting_the_agent(self, run, monkeypatch):
        monkeypatch.setattr(apply_jobs, "SENSITIVE_DATA", {"email": "", "password": ""})
        status, kwargs, saved = run(profile={**GOOD_PROFILE, "email": "Zork"})
        assert status == "failed"
        assert kwargs is None
        assert saved == [("failed", apply_jobs.NO_VALID_EMAIL_ERROR)]


class TestLearningExtraction:
    """The LLM summary used to run an event loop inside the running one and always failed."""

    def _capture(self, monkeypatch):
        calls = []

        class FakeLLM:
            async def ainvoke(self, messages, **kwargs):
                return SimpleNamespace(completion='[{"category": "navigation", "content": "x"}]')

        def fake_extract(result, job_url, job_title, success, llm_call):
            calls.append(llm_call("prompt"))
            return []

        monkeypatch.setattr(apply_jobs, "extract_learnings_via_llm", fake_extract)
        monkeypatch.setattr(apply_jobs.config, "get_llm", lambda: FakeLLM())
        return calls

    def test_failed_run_gets_a_working_llm_summary(self, run, monkeypatch):
        calls = self._capture(monkeypatch)
        status, _, _ = run(history=FakeHistory(success=False, steps=4))
        assert status == "failed"
        assert calls == ['[{"category": "navigation", "content": "x"}]']

    def test_long_successful_run_is_summarised(self, run, monkeypatch):
        calls = self._capture(monkeypatch)
        run(history=FakeHistory(success=True, steps=apply_jobs.LEARN_FROM_RUNS_OVER_STEPS))
        assert len(calls) == 1

    def test_short_successful_run_spends_no_request(self, run, monkeypatch):
        calls = self._capture(monkeypatch)
        status, _, _ = run(history=FakeHistory(success=True, steps=3))
        assert status == "applied"
        assert calls == []


@pytest.mark.parametrize("company,expected", [
    ("Acme", ["Intern at Acme", "Engineer at ACME"]),
    ("Unknown", []),
    ("", []),
    ("Globex", []),
])
def test_same_company_applications(company, expected):
    labels = ["Intern at Acme", "Engineer at ACME", "Intern at Initech", "Acme fan at Initech"]
    assert apply_jobs.same_company_applications(labels, company) == expected


def test_prompt_lists_only_same_company_applications(run, monkeypatch):
    captured = {}
    real_build = apply_jobs.build_memory_context

    def spy(profile, qa, applied_labels=None, job_url=None, learnings=True):
        captured["labels"] = applied_labels
        return real_build(profile, qa, applied_labels, job_url=job_url, learnings=learnings)

    monkeypatch.setattr(apply_jobs, "build_memory_context", spy)
    job = {"url": GREENHOUSE, "title": "Intern", "company": "Acme", "location": "Boston"}
    asyncio.run(apply_jobs.apply_to_job(job, GOOD_PROFILE, {}, ["Old role at Acme", "Other at Initech"], False, 1))
    assert captured["labels"] == ["Old role at Acme"]


# ── Resume text in the task ─────────────────────────────────────────────────

def _make_resume(path):
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "EXPERIENCE\nCORNER MARKET | Cashier    06/2023 - 08/2023\n"
                               "PROJECTS\nWeather App    01/2024 - 06/2024")
    doc.save(str(path))
    doc.close()
    return str(path)


def test_resume_text_reads_the_pdf(tmp_path):
    text = apply_jobs.resume_text(_make_resume(tmp_path / "r.pdf"))
    assert "CORNER MARKET | Cashier" in text and "Weather App" in text


def test_resume_text_is_capped(tmp_path):
    assert len(apply_jobs.resume_text(_make_resume(tmp_path / "r.pdf"), limit=20)) == 20


def test_unreadable_resume_gives_no_text(tmp_path):
    assert apply_jobs.resume_text(str(tmp_path / "missing.pdf")) == ""
    assert apply_jobs.resume_instructions("") == ""


def test_task_carries_the_resume_and_work_history_rules(run, monkeypatch, tmp_path):
    monkeypatch.setattr(apply_jobs, "RESUME_PATH", _make_resume(tmp_path / "r.pdf"))
    _, kwargs, _ = run()
    task = kwargs["task"]
    assert "<resume>" in task and "CORNER MARKET | Cashier" in task
    assert "Projects, coursework and clubs are NOT jobs" in task
    assert "Company = Corner Market, Job Title = Cashier" in task


def test_task_without_a_readable_resume_has_no_resume_block(run):
    _, kwargs, _ = run()  # the fixture's resume path doesn't exist
    assert "<resume>" not in kwargs["task"]


# ── Failure reasons, form rules, pause ──────────────────────────────────────

def test_failed_run_saves_the_agents_own_explanation(run):
    """errors()[-1] is None on a step without an error; that used to crash the save."""
    history = FakeHistory(success=False, steps=5, done_text="Salary field only takes digits.",
                          step_errors=[None] * 5)
    status, _, saved = run(history=history)
    assert status == "failed"
    assert saved[-1] == ("failed", "Salary field only takes digits.")


@pytest.mark.parametrize("history,expected", [
    (FakeHistory(success=False, done_text="Could not submit: form broken."), "Could not submit: form broken."),
    (FakeHistory(success=False, steps=3, step_errors=["timeout", None, None]), "timeout"),
    (FakeHistory(success=False, steps=2, step_errors=[None, None]), "Agent reported failure"),
])
def test_failure_reason(history, expected):
    assert apply_jobs.failure_reason(history) == expected


def test_task_carries_the_form_rules(run):
    _, kwargs, _ = run()
    task = kwargs["task"]
    assert "How did you hear about us" in task and "LinkedIn. Never an employee referral" in task
    assert "type a keyword of the option you want" in task
    assert "hourly x 2080 = yearly" in task
    assert "Address Line 1 is the street address" in task
    assert "Never change a field that already holds a valid, sensible value" in task


def test_pause_reaches_running_agents_and_new_ones_start_paused(run):
    class Running:
        paused = False

        def pause(self):
            self.paused = True

        def resume(self):
            self.paused = False

    running = Running()
    apply_jobs._running_agents.add(running)
    try:
        apply_jobs.set_paused(True)
        assert running.paused and apply_jobs.is_paused()
        run()  # an application that starts while paused
        assert run.agents[-1].paused
        apply_jobs.set_paused(False)
        assert not running.paused
    finally:
        apply_jobs._running_agents.discard(running)
    assert run.agents[-1] not in apply_jobs._running_agents  # removed when its run ended


# ── Settings are read for every job, not once per app start ────────────────

def test_settings_saved_after_startup_are_used(run, monkeypatch, tmp_path):
    """Resume, account login and blocked sites used to be frozen at import time."""
    new_resume = _make_resume(tmp_path / "new.pdf")
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings", lambda: {
        "max_failures": 6,
        "resume_path": new_resume,
        "sensitive_data": {"email": "new-login@example.com", "password": "brand-new-password"},
        "blocked_domains": ["spam.example"],
    })
    _, kwargs, _ = run()
    assert kwargs["available_file_paths"] == [new_resume]
    assert kwargs["sensitive_data"]["account_email"] == "new-login@example.com"
    assert kwargs["sensitive_data"]["password"] == "brand-new-password"
    assert "spam.example" in kwargs["task"]


def test_newly_blocked_domain_blocks_the_job(run, monkeypatch):
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings",
                        lambda: {"blocked_domains": ["greenhouse.io"]})
    status, kwargs, saved = run()
    assert status == "blocked" and kwargs is None
    assert saved == [("blocked", "Blocked domain")]


def test_worker_rereads_the_profile_before_each_job(monkeypatch, tmp_path):
    import json
    profile_file = tmp_path / "candidate_profile.json"
    monkeypatch.setattr(apply_jobs, "CANDIDATE_PROFILE", profile_file)
    seen = []

    async def fake_apply(job, profile, *args, **kwargs):
        seen.append(profile["address"]["street"])
        profile_file.write_text(json.dumps({"address": {"street": "12 Example St"}}))
        return "applied"

    async def no_delay(label=""):
        return 0

    monkeypatch.setattr(apply_jobs, "apply_to_job", fake_apply)
    monkeypatch.setattr(apply_jobs.config, "maybe_human_delay", no_delay)

    async def go():
        queue = asyncio.Queue()
        for i in range(2):
            queue.put_nowait({"url": f"https://x.example/{i}"})
        await apply_jobs.worker("W1", 1, queue, {"address": {"street": "old"}}, {}, [], False, {})

    asyncio.run(go())
    assert seen == ["old", "12 Example St"]


# ── the task text ───────────────────────────────────────────────────────────

def test_page_check_rules_always_and_review_rules_only_in_trial_runs(run):
    apply_jobs.set_trial_run(False)
    _, kwargs, _ = run()
    assert "PAGE BY PAGE" in kwargs["task"] and "record_guesses" in kwargs["task"]
    assert "never stop the application over it" in kwargs["task"]
    assert "referred by an employee, worked or applied here before" in kwargs["task"]
    assert "REVIEW BEFORE SUBMITTING" not in kwargs["task"]
    assert kwargs["step_timeout"] == 300
    apply_jobs.set_trial_run(True)
    try:
        _, kwargs, _ = run()
    finally:
        apply_jobs.set_trial_run(False)
    assert "REVIEW BEFORE SUBMITTING" in kwargs["task"]
    assert kwargs["step_timeout"] == apply_jobs.REVIEW_STEP_TIMEOUT
    assert "request_submit_review" in kwargs["tools"].registry.registry.actions


def test_a_note_on_the_job_reaches_the_task(run, monkeypatch):
    original = apply_jobs.apply_to_job

    async def with_note(job, *args, **kwargs):
        return await original({**job, "user_note": "Tick the terms box first"}, *args, **kwargs)

    monkeypatch.setattr(apply_jobs, "apply_to_job", with_note)
    _, kwargs, _ = run()
    assert "NOTE FROM THE USER ABOUT THIS JOB" in kwargs["task"] and "Tick the terms box first" in kwargs["task"]


def test_trial_runs_get_more_steps(run):
    apply_jobs.set_trial_run(True)
    try:
        _, kwargs, _ = run()
    finally:
        apply_jobs.set_trial_run(False)
    assert "maximum of 150 steps" in kwargs["task"]
    _, kwargs, _ = run()
    assert "maximum of 70 steps" in kwargs["task"]


@pytest.mark.parametrize("agent_words,expected", [
    (True, ("The page shows “Error: we could not save your application” after a timeout. " * 3).strip()),
    (False, "Operation timed out. The page took too long to respond."),
])
def test_agents_own_explanation_is_saved_as_written(monkeypatch, agent_words, expected):
    """Raw exceptions get friendlier text; the agent's explanation doesn't get rewritten."""
    saved = {}
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: saved.update(fields))
    text = "The page shows “Error: we could not save your application” after a timeout. " * 3
    asyncio.run(apply_jobs.save_job_status("u", "failed", text, agent_words=agent_words))
    assert saved["error"] == expected



# ── training runs: fill in, never submit ────────────────────────────────────

def test_training_run_never_submits_and_leaves_the_job_pending(run, monkeypatch):
    updates = []
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append(fields))
    apply_jobs.set_run_mode("training")
    try:
        status, kwargs, saved = run(history=FakeHistory(success=True))
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "practised"
    assert "TRAINING RUN (practice)" in kwargs["task"] and "NEVER click the final Submit" in kwargs["task"]
    assert "REVIEW BEFORE SUBMITTING" not in kwargs["task"]
    assert saved == []                                   # never marked applied
    assert updates[-1]["status"] == "pending" and updates[-1]["practised_at"]


def test_failed_training_run_is_a_normal_failure(run):
    apply_jobs.set_run_mode("training")
    try:
        status, _, saved = run(history=FakeHistory(success=False, done_text="Couldn't open the form."))
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "failed" and saved[-1] == ("failed", "Couldn't open the form.")


@pytest.mark.parametrize("mode,trial,training", [
    ("auto", False, False), ("trial", True, False), ("training", False, True), ("nonsense", False, False),
])
def test_run_modes(mode, trial, training):
    apply_jobs.set_run_mode(mode)
    try:
        assert (apply_jobs.is_trial_run(), apply_jobs.is_training_run()) == (trial, training)
    finally:
        apply_jobs.set_run_mode("auto")


# ── closed postings, human checks, crashes ──────────────────────────────────

def _posting(**kw):
    from core.job_page import Posting
    return Posting(**kw)


def test_a_closed_posting_ends_before_any_browser(run, monkeypatch):
    monkeypatch.setattr(apply_jobs.config, "refresh_posting_details",
                        lambda url, posting=None, mark_closed=True: _posting(closed=True, reason="The posting is gone (HTTP 404)."))
    status, kwargs, saved = run()
    assert status == "closed" and kwargs is None          # no agent was created
    assert saved == [("closed", "The posting is gone (HTTP 404).")]


def test_a_placeholder_title_gets_the_real_one(run, monkeypatch):
    monkeypatch.setattr(apply_jobs.config, "refresh_posting_details",
                        lambda url, posting=None, mark_closed=True: _posting(title="Data Intern", company="Northwind"))
    job_title = {}
    real = apply_jobs.apply_to_job

    async def spy(job, *a, **k):
        job["title"], job["company"] = "Pasted Link", ""
        result = await real(job, *a, **k)
        job_title.update(job)
        return result

    monkeypatch.setattr(apply_jobs, "apply_to_job", spy)
    _run_fake_agent[0].history = FakeHistory()
    asyncio.run(apply_jobs.apply_to_job({"url": GREENHOUSE}, GOOD_PROFILE, {}, [], easy_apply=False, worker_id=1))
    assert (job_title["title"], job_title["company"]) == ("Data Intern", "Northwind")
    task = _run_fake_agent[0].instances[-1].kwargs["task"]
    assert '"title": "Data Intern", "company": "Northwind"' in task


def test_agent_reporting_a_closed_posting_marks_it_closed(run):
    status, _, saved = run(history=FakeHistory(success=False, done_text="POSTING CLOSED: Workday says the job is no longer available."))
    assert status == "closed"
    assert saved[-1] == ("closed", "Workday says the job is no longer available.")


def test_a_sites_human_check_is_left_for_the_user(run, monkeypatch):
    async def human_check(browser, settings):
        raise apply_jobs._captcha.HumanCheckNeeded("https://soylent.example")

    monkeypatch.setattr(apply_jobs._captcha, "handle", human_check)
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *a: None)
    _run_fake_agent[0].call_step = True
    status, _, saved = run(history=FakeHistory(success=False, done_text="Blocked by a check."))
    assert status == "needs_you"
    assert saved[-1][0] == "needs_you" and "Trial mode" in saved[-1][1]


def test_in_a_trial_run_the_agent_asks_for_help_with_the_human_check(run, monkeypatch):
    async def human_check(browser, settings):
        raise apply_jobs._captcha.HumanCheckNeeded()

    monkeypatch.setattr(apply_jobs._captcha, "handle", human_check)
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *a: None)
    _run_fake_agent[0].call_step = True
    apply_jobs.set_run_mode("trial")
    try:
        status, kwargs, _ = run(history=FakeHistory(success=True))
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "applied"
    assert "the app ticks it for you; if it's still there, ask the user" in kwargs["task"]


def test_an_unexpected_crash_is_retried_once(run, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_crash_retries", {})
    _run_fake_agent[0].crash = TypeError("int() argument must be a string, a bytes-like object or a real number, not 'NoneType'")
    first, _, saved = run()
    assert first == "retry" and saved[-1] == ("pending", None)
    _run_fake_agent[0].crash = TypeError("again")
    second, _, saved = run()
    assert second == "failed" and saved[-1] == ("failed", "again")


def test_the_run_gets_our_step_limit_and_page_wait(run):
    run()
    agent = _run_fake_agent[0].instances[-1]
    assert agent.run_kwargs == {"max_steps": 70, "on_step_start": apply_jobs.before_step}


# ── batch 1: fit check, needs-you, self-retry, naming, run controls ─────────

def _settings(monkeypatch, **values):
    base = {"max_failures": 6}
    base.update(values)
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings", lambda: base)


def _posting_with(monkeypatch, **kw):
    monkeypatch.setattr(apply_jobs.config, "refresh_posting_details",
                        lambda url, posting=None, mark_closed=True: _posting(**kw))


def test_outside_the_search_area_is_skipped_before_any_browser(run, monkeypatch):
    _posting_with(monkeypatch, title="Co-Op", locations=["Worcester, MA"])
    profile = dict(GOOD_PROFILE, address={"zip": "10001"}, search_area={"center_zip": "", "radius": "50"})
    status, kwargs, saved = run(profile=profile)
    assert status == "skipped" and kwargs is None
    assert saved[-1][1].startswith("Outside your search area\n") and "Worcester, MA" in saved[-1][1]


def test_ai_fit_check_skips_a_hard_mismatch(run, monkeypatch):
    _posting_with(monkeypatch, title="MBA Associate", description="Open to first-year MBA students only.")

    async def verdict(llm, title, company, description, profile, location_note=""):
        return apply_jobs.ai_extras.FitVerdict(skip=True, title="Requires an MBA", reason="The posting is for MBA students only.")

    monkeypatch.setattr(apply_jobs.ai_extras, "fit_check", verdict)
    status, kwargs, saved = run()
    assert status == "skipped" and saved[-1] == ("skipped", "Requires an MBA\nThe posting is for MBA students only.")


def test_performance_mode_or_the_switch_turns_the_ai_fit_check_off(run, monkeypatch):
    _posting_with(monkeypatch, title="MBA Associate", description="MBA only.")

    async def boom(*a, **k):
        raise AssertionError("fit check must not run")

    monkeypatch.setattr(apply_jobs.ai_extras, "fit_check", boom)
    for settings in ({"performance_mode": True}, {"fit_check_enabled": False}):
        _settings(monkeypatch, **settings)
        assert run()[0] == "applied"


def test_applying_to_a_skipped_job_by_hand_overrides_the_checks(run, monkeypatch):
    _posting_with(monkeypatch, title="Co-Op", locations=["Worcester, MA"])
    profile = dict(GOOD_PROFILE, address={"zip": "10001"}, search_area={"radius": "25"})
    _run_fake_agent[0].history = FakeHistory()
    status = asyncio.run(apply_jobs.apply_to_job(
        {"url": GREENHOUSE, "title": "Co-Op", "company": "Acme", "fit_override": True}, profile, {}, [],
        easy_apply=False, worker_id=1))
    assert status == "applied"


def test_needs_you_report_marks_the_job(run):
    status, _, saved = run(history=FakeHistory(success=False, done_text="NEEDS YOU: missing document: unofficial transcript"))
    assert status == "needs_you" and saved[-1] == ("needs_you", "missing document: unofficial transcript")


def _apply_once(history):
    job = {"url": GREENHOUSE, "title": "Intern", "company": "Acme"}
    _run_fake_agent[0].history = history
    status = asyncio.run(apply_jobs.apply_to_job(job, GOOD_PROFILE, {}, [], easy_apply=False, worker_id=1))
    return status, job


def test_a_fixable_failure_is_marked_for_one_retry_with_its_report(run):
    status, job = _apply_once(FakeHistory(success=False, done_text="Stopped at Review: Field of Study\nNo Statistics option."))
    assert status == "failed" and job["_retry_with"].startswith("Stopped at Review")


def test_hard_stops_are_not_retried(run):
    _, job = _apply_once(FakeHistory(success=False, done_text="Gmail sign-in required to read the verification code."))
    assert "_retry_with" not in job


def _worker_run(monkeypatch, statuses, settings=None):
    """Run worker() over one job whose attempts return `statuses` in order; returns the jobs it was given."""
    _settings(monkeypatch, **(settings or {}))
    seen = []

    async def fake_apply(job, *a, **k):
        seen.append(dict(job))
        status = statuses[len(seen) - 1]
        if status == "failed" and "previous_failure" not in job:
            job["_retry_with"] = "Couldn't select the start date."
        return status

    async def no_delay(*a, **k):
        return None

    monkeypatch.setattr(apply_jobs, "apply_to_job", fake_apply)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **f: None)
    monkeypatch.setattr(apply_jobs.config, "maybe_human_delay", no_delay)
    monkeypatch.setattr(apply_jobs, "load_json", lambda path, default=None: default)

    async def go():
        queue = asyncio.Queue()
        queue.put_nowait({"url": GREENHOUSE, "title": "Intern"})
        await apply_jobs.worker("W1", 1, queue, GOOD_PROFILE, {}, [], False, {})

    asyncio.run(go())
    return seen


def test_worker_retries_a_failure_once_with_its_report(monkeypatch):
    seen = _worker_run(monkeypatch, ["failed", "failed"])
    assert len(seen) == 2 and seen[1]["previous_failure"] == "Couldn't select the start date."


def test_no_self_retry_in_performance_mode(monkeypatch):
    assert len(_worker_run(monkeypatch, ["failed"], {"performance_mode": True})) == 1


def test_previous_failure_reaches_the_agent():
    text = apply_jobs.previous_failure_instructions("Stopped at Review: Field of Study")
    assert "THE LAST ATTEMPT AT THIS JOB FAILED" in text and "Field of Study" in text
    assert apply_jobs.previous_failure_instructions("") == ""


def test_naming_action_only_outside_performance_mode(monkeypatch):
    job = {"url": GREENHOUSE, "title": "Pasted Link", "company": ""}
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {})
    _settings(monkeypatch)
    assert "set_job_details" in apply_jobs.build_tools(job, "x", False, {}, 1).registry.registry.actions
    _settings(monkeypatch, performance_mode=True)
    assert "set_job_details" not in apply_jobs.build_tools(job, "x", False, {}, 1).registry.registry.actions
    _settings(monkeypatch)
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {GREENHOUSE: {"name_source": "user"}})
    assert "set_job_details" not in apply_jobs.build_tools(job, "x", False, {}, 1).registry.registry.actions


def test_set_job_details_names_the_job(monkeypatch):
    calls = []
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {})
    monkeypatch.setattr(apply_jobs.config, "set_job_names", lambda *a: calls.append(a) or True)
    _settings(monkeypatch)
    tools = apply_jobs.build_tools({"url": GREENHOUSE, "title": "Pasted Link"}, "x", False, {}, 1)
    action = tools.registry.registry.actions["set_job_details"].function
    result = asyncio.run(action(params=apply_jobs.JobDetails(role="Data Intern", company="Contoso")))
    assert calls == [(GREENHOUSE, "Data Intern", "Contoso", "agent")] and result.extracted_content == "Noted."


def test_cover_letter_action_only_when_turned_on(monkeypatch, tmp_path):
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {})
    _settings(monkeypatch)
    assert "write_cover_letter" not in apply_jobs.build_tools({"url": GREENHOUSE}, "x", False, {}, 1).registry.registry.actions
    _settings(monkeypatch, cover_letter_boxes=True)
    ref = {"profile": {"name": "Ada Lovelace"}, "resume_text": "", "posting_description": "", "cover_letter_pdf": tmp_path / "cl.pdf"}
    tools = apply_jobs.build_tools({"url": GREENHOUSE, "title": "Intern", "company": "Acme"}, "x", False, ref, 1)
    calls = []

    async def letter(*a, **k):
        calls.append(1)
        return "Dear Hiring Manager,\n\nI built things.\n\nAda Lovelace"

    monkeypatch.setattr(apply_jobs.ai_extras, "write_cover_letter", letter)
    monkeypatch.setattr(apply_jobs.config, "get_llm", lambda: object())
    out = asyncio.run(tools.registry.registry.actions["write_cover_letter"].function())
    assert "I built things." in out.extracted_content and (tmp_path / "cl.pdf").exists()
    again = asyncio.run(tools.registry.registry.actions["write_cover_letter"].function())
    assert again.extracted_content == out.extracted_content and calls == [1]   # written once per job


def test_documents_and_workday_step_limit(run, monkeypatch, tmp_path):
    doc = tmp_path / "transcript.pdf"
    doc.write_bytes(b"%PDF-1.4")
    _settings(monkeypatch, other_documents=[{"label": "Transcript", "path": str(doc)}, {"label": "Gone", "path": "C:/nope.pdf"}])
    _, kwargs, _ = run(url="https://acme.wd1.myworkdayjobs.com/Acme/job/X/Intern_1")
    assert str(doc) in kwargs["available_file_paths"] and "C:/nope.pdf" not in kwargs["available_file_paths"]
    assert "- Transcript: " in kwargs["task"] and "maximum of 100 steps" in kwargs["task"]


@pytest.mark.parametrize("name,safe", [("apply__Early Careers: Health", "apply__Early_Careers__Health"),
                                       ('a<b>c"d|e?f*g/h\\i', "a_b_c_d_e_f_g_h_i")])
def test_safe_folder_name(name, safe):
    assert apply_jobs.safe_folder_name(name) == safe


def test_current_job_is_reported_and_cleared(run):
    seen = {}
    real_run = _run_fake_agent[0].run

    async def peek(self, **kwargs):
        seen.update(apply_jobs.current_jobs()[0])
        return await real_run(self, **kwargs)

    _run_fake_agent[0].run = peek
    run()
    assert seen["title"] == "Intern" and seen["step"] == 0 and seen["max_steps"] == 70
    assert apply_jobs.current_jobs() == []


def test_skip_job_stops_that_agent_and_keeps_the_job(run):
    real_run = _run_fake_agent[0].run
    stopped = []

    async def skip_mid_run(self, **kwargs):
        self.stop = lambda: stopped.append(True)
        assert apply_jobs.skip_job(GREENHOUSE) is True
        return await real_run(self, **kwargs)

    _run_fake_agent[0].run = skip_mid_run
    status, _, saved = run()
    assert status == "skipped" and stopped == [True]
    assert saved[-1] == ("pending", apply_jobs.SKIPPED_BY_YOU_NOTE)
    assert apply_jobs.skip_job("https://not-running.example") is False


def test_no_naming_action_when_the_page_already_named_the_job(monkeypatch):
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {})
    _settings(monkeypatch)
    named = {"url": GREENHOUSE, "title": "Data Intern", "company": "Contoso"}
    assert "set_job_details" not in apply_jobs.build_tools(named, "x", False, {}, 1).registry.registry.actions
