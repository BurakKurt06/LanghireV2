"""What the user's second supervised run (version 1.3.2, 2026-11) showed, fixed.

Many jobs failed only because the AI plan's limit ran out. Each test here is one of the things
that went wrong, with made-up jobs and pages:

* jobs the limit cut off were marked Failed, and each remaining job was started just to fail;
* one site was opened again and again while every job on it ended the same way;
* a job the site said was already applied to was marked Failed and tried again;
* a question the user had answered, or one the run had already waited on, cost another wait;
* after "Create Account" a site went back to its sign-in page (it had emailed a verification
  link) and the agent registered four more times, then gave up;
* an application the site confirmed was called "outcome unknown".
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from cli import apply_jobs
from core import inbox, job_screenshots, submit_guard, usage_meter
from memory.store import MemoryStore
from tests.backend.cli.test_apply_jobs import FakeHistory, GREENHOUSE, _run_fake_agent, run  # noqa: F401 (fixture)
from tests.backend.core.test_usage_meter import headers

LIMIT_ERROR = ("Your ChatGPT plan's usage limit is reached. It resets in about 220 minutes. "
               "(The usage limit has been reached)")
ACME = "https://jobs.acme.example/apply/1"
JOB = {"url": ACME, "title": "Intern", "company": "Acme"}


# ── the AI plan's limit ────────────────────────────────────────────────────────
def test_a_job_the_limit_cut_off_goes_back_to_ready_not_failed(run):
    status, _, saved = run(history=FakeHistory(success=False, step_errors=[None, None, LIMIT_ERROR]))
    assert status == "limit" and saved[-1] == ("pending", apply_jobs.LIMIT_NOTE)
    assert "ready again" in apply_jobs.LIMIT_NOTE and not any(s[0] == "failed" for s in saved)


def test_control_another_error_still_fails_the_job(run):
    status, _, saved = run(history=FakeHistory(success=False, step_errors=[None, None, "form broken"]))
    assert status == "failed" and saved[-1][0] == "failed"


def test_a_limit_error_raised_through_the_agent_goes_back_to_ready_too(run, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_crash_retries", {})
    _run_fake_agent[0].crash = RuntimeError(LIMIT_ERROR)
    status, _, saved = run()
    assert status == "limit" and saved[-1] == ("pending", apply_jobs.LIMIT_NOTE)
    assert apply_jobs._crash_retries == {}                  # not the one-off crash retry either


def test_a_job_is_not_even_opened_once_the_limit_is_reached(run):
    usage_meter.limit_reached(3600)
    status, kwargs, saved = run()
    assert status == "limit" and saved[-1] == ("pending", apply_jobs.LIMIT_NOTE)
    assert run.agents == []                                 # no browser, no agent, no AI request


def test_a_submitted_application_cut_off_by_the_limit_stays_possibly_sent(run, monkeypatch):
    """Submit was pressed: the limit doesn't make that job ready again (it may have gone out)."""
    monkeypatch.setattr(apply_jobs, "submit_attempted", lambda agent_ref: True)
    run.browser.page_text = "Submitting your application..."
    try:
        status, _, saved = run(history=FakeHistory(success=False, step_errors=[None, LIMIT_ERROR]))
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."
    assert status == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")


def _worker(monkeypatch, urls, outcomes=None, errors=None):
    """Run one worker over `urls`; returns (the urls it started, its stats)."""
    started, stats = [], {}
    outcomes = outcomes or {}

    async def apply(job, *a, **k):
        started.append(job["url"])
        return outcomes.get(job["url"], "applied")

    monkeypatch.setattr(apply_jobs, "apply_to_job", apply)
    monkeypatch.setattr(apply_jobs, "load_json", lambda path, default=None: default)
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {u: {"error": e} for u, e in (errors or {}).items()})
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: True)
    monkeypatch.setattr(apply_jobs, "_busy_sites", {})
    monkeypatch.setattr(apply_jobs.config, "maybe_human_delay", lambda *a: asyncio.sleep(0))
    queue = asyncio.Queue()
    for url in urls:
        queue.put_nowait({"url": url, "title": "Intern", "company": "Acme"})
    asyncio.run(apply_jobs.worker("W1", 1, queue, {}, {}, [], False, stats))
    return started, stats, queue.qsize()


def test_no_job_starts_when_the_next_one_would_not_fit_and_the_rest_stay_ready(monkeypatch):
    apply_jobs.new_run()
    usage_meter.note_reply(headers(primary=98, secondary=30, primary_reset=7200))
    started, stats, left = _worker(monkeypatch, [f"https://site{i}.example/1" for i in range(3)])
    assert started == [] and stats == {} and left == 3      # never claimed: still ready, nothing marked failed
    notice = apply_jobs.run_notice()
    assert notice["kind"] == "limit_stop"
    assert notice["text"].startswith("Stopped: ChatGPT limit: 98% of the 5-hour limit is used")
    assert "resets in about 2 hours" in notice["text"]


def test_control_with_room_on_the_plan_every_job_starts(monkeypatch):
    apply_jobs.new_run()
    usage_meter.note_reply(headers(primary=20, secondary=30))
    started, stats, left = _worker(monkeypatch, [f"https://site{i}.example/1" for i in range(3)])
    assert len(started) == 3 and stats == {"applied": 3} and left == 0
    assert apply_jobs.run_notice() == {"text": "", "kind": ""}


def test_the_run_stops_starting_jobs_as_soon_as_one_is_cut_off(monkeypatch):
    apply_jobs.new_run()
    urls = [f"https://site{i}.example/1" for i in range(4)]

    async def apply(job, *a, **k):
        seen.append(job["url"])
        if len(seen) == 2:
            usage_meter.limit_reached(5400)                  # the second job's request was refused
            return "limit"
        return "applied"

    seen = []
    monkeypatch.setattr(apply_jobs, "load_json", lambda path, default=None: default)
    monkeypatch.setattr(apply_jobs, "apply_to_job", apply)
    monkeypatch.setattr(apply_jobs, "_busy_sites", {})
    monkeypatch.setattr(apply_jobs.config, "maybe_human_delay", lambda *a: asyncio.sleep(0))
    queue, stats = asyncio.Queue(), {}
    for url in urls:
        queue.put_nowait({"url": url, "title": "Intern"})
    asyncio.run(apply_jobs.worker("W1", 1, queue, {}, {}, [], False, stats))
    assert seen == urls[:2] and stats == {"applied": 1, "limit": 1} and queue.qsize() == 2
    assert "resets in about 90 minutes" in apply_jobs.run_notice()["text"]


def test_a_limit_that_resets_within_minutes_is_waited_for(monkeypatch):
    apply_jobs.new_run()
    monkeypatch.setattr(apply_jobs, "LIMIT_POLL_SECONDS", 0.05)
    usage_meter.note_reply(headers(primary=99, secondary=30, primary_reset=600))
    notices = []

    async def sleep_then_reset(seconds):
        notices.append(apply_jobs.run_notice())
        usage_meter.reset()                                  # the window reset while the run waited
        usage_meter._state["loaded"] = True

    monkeypatch.setattr(apply_jobs.asyncio, "sleep", sleep_then_reset)
    assert asyncio.run(apply_jobs._hold_for_limit("W1", None)) is True
    assert notices[0]["kind"] == "limit_wait" and notices[0]["text"].startswith("Waiting: ChatGPT limit")
    assert apply_jobs.run_notice() == {"text": "", "kind": ""}


# ── a site where every job ends the same way ────────────────────────────────────
SIGN_IN = "sign-in failed on jobs.acme.example"


def test_two_jobs_ending_the_same_way_stop_the_sites_other_jobs_for_this_run(run, monkeypatch):
    apply_jobs.new_run()
    apply_jobs.note_site_outcome("https://jobs.acme.example/a", "needs_you", SIGN_IN)
    assert apply_jobs._site_trouble == {}                    # once is not yet a pattern
    apply_jobs.note_site_outcome("https://jobs.acme.example/b", "needs_you", SIGN_IN)
    status, _, saved = run(url="https://jobs.acme.example/c")
    assert status == "skipped" and run.agents == []
    assert saved[-1][0] == "pending"                         # back to ready for the next run, never Failed
    assert saved[-1][1] == apply_jobs.SITE_TROUBLE_NOTE.format(site="jobs.acme.example", reason=SIGN_IN)
    assert "two other jobs on jobs.acme.example already ended the same way" in saved[-1][1]


def test_control_another_site_and_a_new_run_are_tried(run):
    apply_jobs.new_run()
    for path in "ab":
        apply_jobs.note_site_outcome(f"https://jobs.acme.example/{path}", "needs_you", SIGN_IN)
    assert run(url="https://jobs.other.example/c")[0] == "applied"
    apply_jobs.new_run()
    assert run(url="https://jobs.acme.example/c")[0] == "applied"


@pytest.mark.parametrize("first,second", [
    (("needs_you", SIGN_IN), ("failed", "The page didn't load\nThe application page didn't load.")),   # two different ways
    (("failed", "Stopped at Review: Field of Study\nNo Statistics option."),
     ("failed", "Upload failed\nThe transcript upload was refused.")),
    (("needs_you", "Your answer is needed: notice period"), ("needs_you", "Your answer is needed: notice period")),
    (("needs_you", "Submission outcome unknown\nIt may have been sent."),
     ("needs_you", "Submission outcome unknown\nIt may have been sent.")),
    (("needs_you", "CAPTCHA the solver couldn't pass\nx"), ("needs_you", "CAPTCHA the solver couldn't pass\nx")),
    (("applied", ""), ("applied", "")),
    (("pending", apply_jobs.LIMIT_NOTE), ("pending", apply_jobs.LIMIT_NOTE)),
])
def test_endings_that_say_nothing_about_the_sites_other_jobs_do_not_stop_them(first, second):
    apply_jobs.new_run()
    apply_jobs.note_site_outcome("https://jobs.acme.example/a", *first)
    apply_jobs.note_site_outcome("https://jobs.acme.example/b", *second)
    assert apply_jobs._site_trouble == {}


@pytest.mark.parametrize("status,error,kind", [
    ("failed", "The page didn't load\nIt stayed blank.", "page"),
    ("failed", "Reached maximum of 70 steps — stopping to avoid wasting tokens", "steps"),
    ("needs_you", "complete account access\nI couldn't get past the registration step.", "account"),
    ("failed", "Stopped at Review: Field of Study", "other"),
])
def test_how_a_job_ended_is_sorted_into_a_kind(status, error, kind):
    assert apply_jobs.failure_kind(status, error)[0] == kind


def test_the_worker_notes_each_ending_and_skips_the_rest_of_the_site(monkeypatch):
    apply_jobs.new_run()
    urls = [f"https://jobs.acme.example/{n}" for n in "abc"]
    started, stats, _ = _worker(monkeypatch, urls, outcomes={u: "needs_you" for u in urls[:2]},
                                errors={u: SIGN_IN for u in urls})
    assert started == urls and apply_jobs._site_trouble == {"jobs.acme.example": SIGN_IN}


# ── the site says this application is already in ────────────────────────────────
def test_already_applied_on_the_sites_word_is_applied_and_never_retried(run, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *args: None)
    _run_fake_agent[0].call_step = True
    run.browser.page_text = "You have already applied to this job on November 14."
    updates = []
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append(fields) or True)
    try:
        status, _, saved = run(history=FakeHistory(
            success=False, done_text="ALREADY APPLIED: The site shows 'You have already applied to this job'."))
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."
    assert status == "applied" and saved[-1] == ("applied", None)
    assert any((u.get("submission_evidence") or {}).get("kind") == "site_says_already_applied" for u in updates)


def test_control_the_agents_word_alone_is_not_enough(run, monkeypatch):
    """The page never said so (the agent read it off the app's own list of earlier applications)."""
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *args: None)
    _run_fake_agent[0].call_step = True
    run.browser.page_text = "Sign in to start your application."
    try:
        status, _, saved = run(history=FakeHistory(success=False, done_text="ALREADY APPLIED: this job is on my list."))
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."
    assert status == "needs_you" and saved[-1] == ("needs_you", apply_jobs.ALREADY_APPLIED_UNSEEN_NOTE)


def test_an_already_applied_report_is_never_sent_back_to_keep_trying():
    assert apply_jobs.is_hard_stop("ALREADY APPLIED: the site lists it as received")


@pytest.mark.parametrize("text,expected", [
    ("You have already applied to this job.", True), ("You've already applied", True),
    ("Status: Application Received", True), ("Application submitted on 11/14/2026", True),
    ("Under consideration", True), ("You applied on November 14", True), ("Duplicate application", True),
    ("Apply now to join our team", False), ("Create an account to apply", False), ("", False),
])
def test_what_counts_as_the_site_saying_already_applied(text, expected):
    assert apply_jobs.says_already_applied(text) is expected


def test_the_agent_is_told_only_the_site_can_say_already_applied(run):
    _, kwargs, _ = run()
    task = kwargs["task"]
    assert "ALREADY APPLIED: only the SITE can say that" in task and "'ALREADY APPLIED:'" in task
    assert "A posting with the same title as an earlier application is a separate job" in task
    assert "NEVER PRINT: never click Print, Print Application, Download PDF or Save as PDF" in task
    assert "SMART GUESSES (fill these in yourself; never stop or ask for them)" in task
    assert "DATES (never ask the user for a date that follows from the resume or the profile)" in task
    assert "a date in work or education history that the resume and profile don't give" not in task


# ── the employer's confirmation email ───────────────────────────────────────────
def _submitted_then(monkeypatch, run, find):
    """A real Submit click, then a last page that confirms nothing."""
    monkeypatch.setattr(apply_jobs, "submit_attempted", lambda agent_ref: True)
    monkeypatch.setattr(inbox, "find_confirmation", find)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: True)

    async def firm(agent):
        agent._langhire_ref["firm_submit"] = True

    _run_fake_agent[0].during_run = firm
    run.browser.page_text = "Candidate Home"
    try:
        return run(history=FakeHistory(success=True))
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."


def test_the_employers_confirmation_email_makes_an_unconfirmed_application_applied(run, monkeypatch):
    asked = []

    async def find(senders, since, kwargs, wait, **ties):
        asked.append((sorted(senders), ties))
        return {"confirmed": True, "subject": "Thank you for applying to Acme"}

    status, _, saved = _submitted_then(monkeypatch, run, find)
    assert status == "applied" and saved[-1] == ("applied", None)
    senders, ties = asked[0]
    assert "greenhouse.io" in senders                                  # the site the job is on
    assert ties["recipients"] == ["ada@example.com", "login@example.com"] and "Acme" in ties["employer_names"]


def test_control_without_that_email_it_stays_outcome_unknown(run, monkeypatch):
    async def find(*args, **ties):
        return {"error": "No confirmation email from this employer arrived within 180 seconds."}

    status, _, saved = _submitted_then(monkeypatch, run, find)
    assert status == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")


def test_no_email_is_looked_for_when_only_a_next_button_was_pressed(run, monkeypatch):
    """A tentative click (Next, Save and Continue) is no Submit: nothing to confirm by email."""
    asked = []

    async def find(*args, **ties):
        asked.append(1)
        return {"confirmed": True}

    monkeypatch.setattr(apply_jobs, "submit_attempted", lambda agent_ref: True)
    monkeypatch.setattr(inbox, "find_confirmation", find)
    run.browser.page_text = "Candidate Home"
    try:
        status, _, saved = run(history=FakeHistory(success=True))
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."
    assert status == "needs_you" and asked == []


@pytest.mark.parametrize("subject,text,expected", [
    ("Thank you for applying to Acme", "We have received your application for Analyst.", True),
    ("Application received", "Your application was successfully submitted.", True),
    ("Acme careers", "Thanks for your interest. We're reviewing your application.", True),
    ("Finish your application", "You haven't finished your application for Analyst. Complete your application today.", False),
    ("Verify your email", "Your verification code is 482913.", False),
    ("Your application is incomplete", "Thank you for your interest. Your application is incomplete.", False),
    ("", "", False),
])
def test_which_emails_say_the_application_was_received(subject, text, expected):
    assert inbox.is_confirmation(subject, text) is expected


# ── questions ───────────────────────────────────────────────────────────────────
def _ask(monkeypatch, store, questions, agent_ref=None, waits=None):
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: store)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: True)
    waits = waits if waits is not None else []

    async def no_reply(*args, **kwargs):
        waits.append(args)
        return None

    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", no_reply)
    tools = apply_jobs.build_tools(dict(JOB), "jobs.acme.example", False, agent_ref if agent_ref is not None else {}, 1)
    ask = tools.registry.registry.actions["ask_for_answers"].function
    params = apply_jobs.UnansweredQuestions(questions=[apply_jobs.UnansweredQuestion(**q) for q in questions])
    return asyncio.run(ask(params=params, browser_session=object())), waits


def test_a_question_the_user_answered_before_is_never_asked_again(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    store.qa_set_answer("Which high school did you attend?", "Springfield High School", source="user")
    result, waits = _ask(monkeypatch, store, [{"question": "Which high school did you attend?"}])
    assert waits == [] and not result.is_done
    assert "the user answered this before: Springfield High School" in result.extracted_content


def test_control_an_answer_the_agent_guessed_is_not_the_users(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    store.qa_set_answer("Which high school did you attend?", "Unknown High", source="guess")
    result, waits = _ask(monkeypatch, store, [{"question": "Which high school did you attend?"}])
    assert len(waits) == 1 and result.is_done


def test_the_graduation_date_is_worked_out_not_asked(tmp_path, monkeypatch):
    profile = {"education": {"graduation": "May 2028"}}
    result, waits = _ask(monkeypatch, MemoryStore(tmp_path / "m.db"),
                         [{"question": "Please input your anticipated graduation date", "field_type": "date"}],
                         agent_ref={"profile": profile})
    assert waits == [] and not result.is_done and "05/15/2028" in result.extracted_content


def test_a_question_already_waited_on_in_this_run_is_not_waited_on_again(tmp_path, monkeypatch):
    """Three jobs on one site each waited five minutes on the same question."""
    apply_jobs.new_run()
    store = MemoryStore(tmp_path / "m.db")
    question = [{"question": "What pre-university education system did you follow?"}]
    first, waits = _ask(monkeypatch, store, question)
    assert len(waits) == 1 and first.is_done and first.extracted_content.startswith(apply_jobs.NEEDS_YOU_PREFIX)
    second, waits = _ask(monkeypatch, store, question, waits=waits)          # the next job on that site
    assert len(waits) == 1                                                    # no second wait
    assert second.is_done and second.extracted_content.startswith(apply_jobs.NEEDS_YOU_PREFIX)
    apply_jobs.new_run()
    _, waits = _ask(monkeypatch, store, question, waits=waits)               # a new run asks once again
    assert len(waits) == 2


def test_a_guess_is_saved_with_what_it_was_worked_out_from(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "m.db")
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: store)
    tools = apply_jobs.build_tools(dict(JOB), "jobs.acme.example", False, {}, 1)
    record = tools.registry.registry.actions["record_guesses"].function
    asyncio.run(record(params=apply_jobs.GuessedAnswers(guesses=[apply_jobs.GuessedAnswer(
        question="Graduation date", answer="05/15/2028", field_type="date", source="profile: May 2028")])))
    row = store.qa_list()[0]
    assert (row["answer"], row["source"], row["basis"]) == ("05/15/2028", "guess", "profile: May 2028")
    store.qa_update(row["id"], "05/20/2028")                 # the user corrects it: theirs now, no "guessed from"
    row = store.qa_list()[0]
    assert (row["answer"], row["source"], row["basis"]) == ("05/20/2028", "user", "")


# ── Create Account, then back at Sign In ────────────────────────────────────────
class _Page:
    """A browser session standing on one page (for the account note)."""
    def __init__(self, url, password_field=True):
        self.url, self.password_field = url, password_field

    async def get_current_page_url(self):
        return self.url


def _note(monkeypatch, data_dir, agent_ref, password_field=True):
    from core import site_access, site_accounts
    trail = site_access.SiteTrail(ACME)
    ref = {"trail": trail, "secrets": {"account_email": "ada.accounts@example.com"}, **agent_ref}

    async def evaluate(session, js):
        return password_field

    monkeypatch.setattr(apply_jobs.browser_policy, "evaluate", evaluate)
    monkeypatch.setattr(site_accounts, "known", lambda url, email: None)
    agent = SimpleNamespace(_langhire_ref=ref, browser_session=_Page(ACME))
    return asyncio.run(apply_jobs.account_memory_note(agent))


def test_back_at_sign_in_after_create_account_the_agent_is_sent_to_the_verification_email(monkeypatch, data_dir):
    note = _note(monkeypatch, data_dir, {"registered_at": datetime.now(timezone.utc)})
    assert note.startswith("ACCOUNT MEMORY: you pressed Create Account on this site (jobs.acme.example)")
    assert "Do NOT fill the Create Account form again" in note and "get_email_verification with kind 'link'" in note


def test_control_before_any_registration_the_note_says_to_create_one(monkeypatch, data_dir):
    note = _note(monkeypatch, data_dir, {})
    assert "has no account on this site" in note and "Create one" in note


def test_once_the_verification_was_fetched_the_note_is_the_usual_one(monkeypatch, data_dir):
    note = _note(monkeypatch, data_dir, {"registered_at": datetime.now(timezone.utc), "verification_tried": True})
    assert "you pressed Create Account" not in note


def test_giving_up_on_the_account_without_the_verification_email_is_sent_back_once(tmp_path, monkeypatch):
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.tools.views import DoneAction
    agent = SimpleNamespace(state=SimpleNamespace(n_steps=40))
    ref = {"agent": agent, "max_steps": 70, "registered_at": datetime.now(timezone.utc)}
    waits = []

    async def no_reply(*args, **kwargs):
        waits.append(args)

    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", no_reply)
    tools = apply_jobs.build_tools(dict(JOB), "jobs.acme.example", False, ref, 1)
    done = tools.registry.registry.actions["done"].function
    text = "NEEDS YOU: complete account access\nI couldn't get past the registration/sign-in step."
    first = asyncio.run(done(params=DoneAction(text=text, success=False), file_system=FileSystem(tmp_path)))
    assert not first.is_done and waits == []
    assert "get_email_verification with kind 'link'" in first.extracted_content
    second = asyncio.run(done(params=DoneAction(text=text, success=False), file_system=FileSystem(tmp_path)))
    assert second.is_done and len(waits) == 1                # then the usual wait for the user, and the end


def test_control_without_a_registration_nothing_is_sent_back(tmp_path, monkeypatch):
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.tools.views import DoneAction
    agent = SimpleNamespace(state=SimpleNamespace(n_steps=40))
    waits = []

    async def no_reply(*args, **kwargs):
        waits.append(args)

    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", no_reply)
    tools = apply_jobs.build_tools(dict(JOB), "jobs.acme.example", False, {"agent": agent, "max_steps": 70}, 1)
    done = tools.registry.registry.actions["done"].function
    result = asyncio.run(done(params=DoneAction(text="NEEDS YOU: sign-in failed on jobs.acme.example", success=False),
                              file_system=FileSystem(tmp_path)))
    assert result.is_done and len(waits) == 1


@pytest.mark.parametrize("label,creates", [("Create Account", True), ("Submit Registration", True), ("Register", True),
                                           ("Sign up", True), ("Sign In", False), ("Log in", False), ("Next", False)])
def test_which_account_buttons_register(label, creates):
    control = submit_guard.Control(text=label, tag="button", type="submit", in_form=True)
    assert submit_guard.is_account_creation(control) is creates


def test_the_agent_is_told_what_a_return_to_sign_in_means():
    text = apply_jobs.accounts_instructions(True)
    assert "PRESS the button that creates the account once the form is filled" in text
    assert "goes back to its Sign In page" in text and "get_email_verification (kind 'link'" in text


# ── the picture of the last page ────────────────────────────────────────────────
JPEG = b"\xff\xd8\xff\xe0" + b"made-up picture bytes" * 20


@pytest.mark.parametrize("status,kept", [("failed", True), ("needs_you", True), ("applied", False),
                                         ("closed", False), ("limit", False), ("skipped", False)])
def test_the_last_page_is_kept_for_a_job_that_did_not_finish(data_dir, monkeypatch, status, kept):
    updates = []
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append(fields) or True)
    asyncio.run(apply_jobs._keep_last_page(ACME, status, {"agent_ref": {"last_picture": JPEG}}))
    assert job_screenshots.exists(ACME) is kept
    assert bool(updates and updates[-1].get("screenshot_at")) is kept


def test_an_applied_job_loses_the_picture_of_its_earlier_failure(data_dir, monkeypatch):
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: True)
    asyncio.run(apply_jobs._keep_last_page(ACME, "failed", {"agent_ref": {"last_picture": JPEG}}))
    assert job_screenshots.exists(ACME)
    asyncio.run(apply_jobs._keep_last_page(ACME, "applied", {"agent_ref": {}}))
    assert not job_screenshots.exists(ACME)
