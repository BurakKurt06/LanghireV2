"""What Start checks and asks before the first job, and one answer for every job waiting on a question.

Made-up jobs, sites and answers; the CAPTCHA solver's balance call is a stand-in (no network).
"""
import pytest

from core import captcha, run_checks, usage_meter
from memory.store import MemoryStore
from tests.backend.core.test_usage_meter import headers

SETTINGS = {"sensitive_data": {"email": "ada@example.com", "password": "Made-Up-Pass-77"},
            "captcha_api_key": "made-up-key", "captcha_solver_enabled": True}
PROFILE = {"name": "Ada Test", "education": {"graduation": "May 2028"}}


def problems(**changes):
    given = dict(llm_ready=True, provider="chatgpt", settings=SETTINGS, profile=PROFILE, resume_ok=True,
                 gmail_signed_in=True, balance=lambda key: 4.2)
    given.update(changes)
    return run_checks.start_problems(**given)


def ids(found):
    return [p["id"] for p in found]


def test_nothing_missing_means_nothing_to_say():
    assert problems() == []


@pytest.mark.parametrize("changes,ident,words,required", [
    (dict(llm_ready=False), "ai", "ChatGPT isn't signed in", True),
    (dict(llm_ready=False, provider="gemini"), "ai", "No AI model is set up", True),
    (dict(resume_ok=False), "resume", "No resume is chosen", True),
    (dict(profile={}), "name", "Your name is missing", True),
    (dict(gmail_signed_in=False), "gmail", "Gmail isn't connected", False),
    (dict(settings={**SETTINGS, "sensitive_data": {}}), "password", "No password for job sites is saved", False),
    (dict(settings={**SETTINGS, "captcha_api_key": ""}), "captcha", "No 2Captcha key is saved", False),
    (dict(balance=lambda key: 0.0), "captcha", "Your 2Captcha balance is empty", False),
])
def test_each_missing_thing_is_named_once(changes, ident, words, required):
    found = problems(**changes)
    assert ids(found) == [ident] and words in found[0]["message"] and found[0]["required"] is required


def test_everything_missing_is_listed_together():
    found = problems(llm_ready=False, resume_ok=False, profile={}, gmail_signed_in=False,
                     settings={"sensitive_data": {}}, balance=lambda key: None)
    assert ids(found) == ["ai", "resume", "name", "gmail", "password", "captcha"]


def test_a_key_2captcha_refuses_is_said_in_plain_words():
    def refused(key):
        raise captcha.SolverRejected("ERROR_WRONG_USER_KEY")
    found = problems(balance=refused)
    assert ids(found) == ["captcha"] and "doesn't accept the saved API key" in found[0]["message"]


@pytest.mark.parametrize("balance", [lambda key: None, lambda key: (_ for _ in ()).throw(OSError("offline"))])
def test_a_balance_that_cannot_be_read_is_not_a_problem(balance):
    assert problems(balance=balance) == []


def test_the_solver_switched_off_is_not_checked():
    calls = []
    found = problems(settings={**SETTINGS, "captcha_solver_enabled": False},
                     balance=lambda key: calls.append(key) or 0.0)
    assert found == [] and calls == []


def test_no_room_on_the_ai_plan_is_said_but_the_run_waits_for_the_reset():
    usage_meter.note_reply(headers(primary=97, secondary=20, primary_reset=7200))
    found = problems()
    assert ids(found) == ["ai_limit"] and not found[0]["required"]
    assert "97% of the 5-hour limit is used" in found[0]["message"] and "resets in about 2 hours" in found[0]["message"]
    assert "The run will wait and start by itself when it resets" in found[0]["message"]


def test_a_limit_that_resets_within_minutes_does_not_stop_a_start():
    """The run waits those minutes itself."""
    usage_meter.note_reply(headers(primary=97, secondary=20, primary_reset=300))
    assert problems() == []


# ── the questions ──────────────────────────────────────────────────────────────
def job(url, status="pending", **fields):
    return {"url": url, "title": "Analyst", "company": "Example Works", "status": status, **fields}


QUESTION = {"question": "What is your notice period in weeks?", "section": "About you", "options": [], "field_type": "text"}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "m.db")


def test_a_site_family_known_need_is_asked_before_the_run(store):
    queued = [job("https://acme.tal.net/vx/opp/1"), job("https://other.tal.net/vx/opp/2"),
              job("https://boards.greenhouse.io/acme/jobs/3")]
    asked = run_checks.start_questions(queued, {j["url"]: j for j in queued}, store, PROFILE)
    assert len(asked) == 1 and asked[0]["jobs"] == 2
    assert asked[0]["question"].startswith("Your school before university (high school)")
    assert "tal.net sites ask every applicant" in asked[0]["why"]


def test_control_no_such_site_queued_nothing_is_asked(store):
    queued = [job("https://boards.greenhouse.io/acme/jobs/3")]
    assert run_checks.start_questions(queued, {j["url"]: j for j in queued}, store, PROFILE) == []


def test_once_answered_it_is_never_asked_again_on_any_site(store):
    queued = [job("https://acme.tal.net/vx/opp/1")]
    question = run_checks.start_questions(queued, {}, store, PROFILE)[0]["question"]
    store.qa_set_answer(question, "Springfield High School, US High School Diploma, GPA 3.8, June 2024", source="user")
    assert run_checks.start_questions(queued, {}, store, PROFILE) == []
    assert run_checks.start_questions([job("https://third.tal.net/vx/opp/9")], {}, store, PROFILE) == []


def test_a_question_another_job_on_the_site_waits_on_is_asked_before_the_run(store):
    waiting = job("https://acme.example/jobs/1", "needs_you", pending_questions=[QUESTION])
    queued = [job("https://acme.example/jobs/2"), job("https://acme.example/jobs/3"), job("https://else.example/jobs/4")]
    asked = run_checks.start_questions(queued, {waiting["url"]: waiting, **{j["url"]: j for j in queued}}, store, PROFILE)
    assert [(a["question"], a["jobs"]) for a in asked] == [("What is your notice period in weeks?", 2)]


def test_a_question_a_site_asked_in_an_earlier_run_is_asked_before_the_run(store):
    store.qa_add("How many hours a week can you work?", answer="", source_domain="acme.example")
    store.qa_add("Favourite colour?", answer="", source_domain="unrelated.example")
    queued = [job("https://acme.example/jobs/2")]
    asked = run_checks.start_questions(queued, {}, store, PROFILE)
    assert [a["question"] for a in asked] == ["How many hours a week can you work?"]


def test_a_date_the_app_works_out_itself_is_not_asked(store):
    waiting = job("https://acme.example/jobs/1", "needs_you", pending_questions=[
        {"question": "Please input your anticipated graduation date", "field_type": "date"}])
    queued = [job("https://acme.example/jobs/2")]
    assert run_checks.start_questions(queued, {waiting["url"]: waiting}, store, PROFILE) == []


def test_waiting_questions_are_grouped_with_the_jobs_that_wait_on_them(store):
    jobs = {
        "https://a.example/1": job("https://a.example/1", "needs_you", pending_questions=[QUESTION]),
        "https://a.example/2": job("https://a.example/2", "needs_you", pending_questions=[
            dict(QUESTION, question="What is your notice period, in weeks?"), {"question": "Your T-shirt size?"}]),
        "https://a.example/3": job("https://a.example/3", "needs_you", pending_questions=[QUESTION],
                                   submit_attempted_at="2026-11-15T10:00:00+00:00"),       # may have been sent
        "https://a.example/4": job("https://a.example/4", "pending"),
    }
    groups = run_checks.group_questions(jobs, store)
    assert [(g["question"], [j["url"] for j in g["jobs"]]) for g in groups] == [
        ("What is your notice period in weeks?", ["https://a.example/1", "https://a.example/2"]),
        ("Your T-shirt size?", ["https://a.example/2"])]


def test_one_answer_frees_every_job_that_only_waited_on_it(store):
    jobs = {
        "https://a.example/1": job("https://a.example/1", "needs_you", pending_questions=[QUESTION]),
        "https://a.example/2": job("https://a.example/2", "needs_you", pending_questions=[
            QUESTION, {"question": "Your T-shirt size?"}]),
        "https://a.example/3": job("https://a.example/3", "needs_you", pending_questions=[QUESTION],
                                   submit_attempted_at="2026-11-15T10:00:00+00:00"),
    }
    changed = run_checks.answer_everywhere(jobs, "What is your notice period in weeks?", store)
    assert changed == {"https://a.example/1": None, "https://a.example/2": [{"question": "Your T-shirt size?"}]}
