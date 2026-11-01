"""No duplicate or false "applied": unknown submit outcomes are never retried automatically.

Uses the `run` fixture from test_apply_jobs.py (fake agent, fake browser) to drive
apply_to_job end to end, and the worker loop for setup failures and retries.
"""
import asyncio

import pytest

from cli import apply_jobs
from tests.backend.cli.test_apply_jobs import FakeHistory, GREENHOUSE, _run_fake_agent, run  # noqa: F401 (fixture)


@pytest.fixture
def submitted(monkeypatch):
    """The agent clicked a final Submit during the run (the guard recorded an attempt)."""
    monkeypatch.setattr(apply_jobs, "submit_attempted", lambda agent_ref: True)


def test_a_failure_after_submit_is_outcome_unknown_and_not_retried(run, submitted):
    status, _, saved = run(history=FakeHistory(success=False, done_text="The confirmation page didn't load."))
    # needs_you, not failed: the worker only self-retries "failed" jobs.
    assert status == "needs_you"
    assert saved[-1][1].startswith("Submission outcome unknown")


@pytest.mark.parametrize("error", [apply_jobs._captcha.HumanCheckNeeded(),
                                  apply_jobs._captcha.CaptchaUnsolved("hcaptcha", "synthetic failure")])
def test_a_captcha_after_submit_reports_an_unknown_outcome(run, submitted, monkeypatch, error):
    async def blocked(browser, settings):
        raise error
    monkeypatch.setattr(apply_jobs._captcha, "handle", blocked)
    monkeypatch.setattr(apply_jobs, "_agent_on_step", lambda *args: None)
    _run_fake_agent[0].call_step = True
    status, _, saved = run(history=FakeHistory(success=False, done_text="Stopped at a check."))
    assert status == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")


def test_a_crash_after_submit_is_outcome_unknown_and_not_retried(run, submitted, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_crash_retries", {})
    _run_fake_agent[0].crash = TypeError("browser went away")
    status, _, saved = run()
    assert status == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")
    assert apply_jobs._crash_retries == {}  # the one-off crash retry was not used


def test_a_skip_right_after_submit_is_outcome_unknown(run, submitted, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_skip_requested", {GREENHOUSE})
    status, _, saved = run(history=FakeHistory(success=False, done_text="Stopped."))
    assert status == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")


def _saved_on_stop(run, monkeypatch):
    """Run until a Stop cancels the agent; the statuses written on the way out."""
    saved = []

    async def save(url, status, error=None, agent_words=False):
        saved.append((status, error))

    _run_fake_agent[0].crash = asyncio.CancelledError()
    monkeypatch.setattr(apply_jobs, "save_job_status", save)
    with pytest.raises(asyncio.CancelledError):  # the stop still propagates to the worker
        run()
    return saved


def test_stopping_mid_application_leaves_an_accurate_state(run, monkeypatch):
    assert _saved_on_stop(run, monkeypatch)[-1] == ("pending", apply_jobs.INTERRUPTED_NOTE)


def test_stopping_after_submit_is_outcome_unknown(run, submitted, monkeypatch):
    status, note = _saved_on_stop(run, monkeypatch)[-1]
    assert status == "needs_you" and note.startswith("Submission outcome unknown")


def test_success_without_a_submit_or_a_confirmation_page_is_not_applied(run):
    run.browser.page_text = "Application form - Step 3 of 4"
    try:
        status, _, saved = run()
    finally:
        run.browser.page_text = "Thank you for applying! Your application has been received."
    assert status == "needs_you" and saved[-1][1].startswith("Not confirmed as applied")


def test_success_with_a_confirmation_page_is_applied(run):
    status, _, saved = run()
    assert status == "applied" and saved[-1] == ("applied", None)


def test_credential_errors_are_retried_only_once(run, monkeypatch):
    monkeypatch.setattr(apply_jobs, "_credential_retries", {})
    monkeypatch.setattr(apply_jobs, "_crash_retries", {GREENHOUSE: 1})
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: True)
    _run_fake_agent[0].crash = RuntimeError("The security token included in the request is expired")
    first, _, _ = run()
    _run_fake_agent[0].crash = RuntimeError("The security token included in the request is expired")
    second, _, saved = run()
    assert first == "retry" and second == "failed" and saved[-1][0] == "failed"


def test_a_setup_failure_marks_the_job_failed_and_the_worker_goes_on(monkeypatch):
    calls, saved = [], []

    async def apply(job, *a, **k):
        calls.append(job["url"])
        if job["url"] == "https://a.example/1":
            raise RuntimeError("jobs.json is locked")
        return "applied"

    async def save(url, status, error=None, agent_words=False):
        saved.append((url, status, error))

    monkeypatch.setattr(apply_jobs, "apply_to_job", apply)
    monkeypatch.setattr(apply_jobs, "save_job_status", save)
    monkeypatch.setattr(apply_jobs, "load_json", lambda path, default=None: default)
    monkeypatch.setattr(apply_jobs, "_busy_sites", {})
    monkeypatch.setattr(apply_jobs.config, "maybe_human_delay", lambda *a: asyncio.sleep(0))
    queue = asyncio.Queue()
    for url in ("https://a.example/1", "https://b.example/1"):
        queue.put_nowait({"url": url, "title": "Intern"})
    stats = {}
    asyncio.run(apply_jobs.worker("W1", 1, queue, {}, {}, [], False, stats))
    assert calls == ["https://a.example/1", "https://b.example/1"]  # the worker survived
    assert saved[0][:2] == ("https://a.example/1", "failed") and "Couldn't start" in saved[0][2]
    assert stats == {"failed": 1, "applied": 1} and apply_jobs._busy_sites == {}


def test_startup_recovery_never_resets_a_possibly_sent_application(data_dir, monkeypatch):
    import main
    from core import shared_config
    monkeypatch.setattr(shared_config, "JOBS_FILE", data_dir / "jobs.json")
    monkeypatch.setattr(shared_config, "JOBS_LOCK", data_dir / "jobs.json.lock")
    shared_config.write_jobs({
        "https://a.example/1": {"status": "in_progress", "submit_attempted_at": "2026-10-11T10:00:00+00:00"},
        "https://b.example/1": {"status": "in_progress"},
    })
    main._recover_stale_jobs()
    jobs = shared_config.read_jobs()
    assert jobs["https://a.example/1"]["status"] == "needs_you"
    assert jobs["https://a.example/1"]["error"].startswith("Submission outcome unknown")
    assert jobs["https://b.example/1"]["status"] == "pending"


# ── Codex's reproductions ────────────────────────────────────────────────────

def test_a_submit_click_and_a_success_report_are_not_proof(run, submitted):
    """The final page says it was NOT submitted: possibly sent or not, so needs you (never 'applied')."""
    run.browser.page_text = "Your application has NOT been submitted. Please fix the validation errors."
    status, _, saved = run(history=FakeHistory(success=True))
    assert status == "needs_you"
    assert saved[-1][0] == "needs_you" and saved[-1][1].startswith("Submission outcome unknown")


def test_a_trial_without_an_approved_submit_is_never_applied(run):
    """The page says "thank you", but the user approved no Submit in this trial run: they check it."""
    apply_jobs.set_run_mode("trial")
    try:
        status, _, saved = run()
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "practised" and not any(s[0] == 'applied' for s in saved)


def test_a_trial_with_its_approved_submit_and_a_confirmation_page_is_applied(run, submitted):
    apply_jobs.set_run_mode("trial")
    try:
        status, _, saved = run()
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "needs_you" and not any(s[0] == 'applied' for s in saved)
    assert saved[-1][1].startswith('Submission outcome unknown')


def test_trial_preserves_an_older_live_submit_marker(run, monkeypatch):
    monkeypatch.setattr(apply_jobs, 'read_jobs', lambda: {GREENHOUSE: {
        'submit_attempted_at':'2026-10-19T01:00:00Z','title':'Intern','company':'Acme'}})
    apply_jobs.set_run_mode('trial')
    try:
        status, _, saved = run()
    finally:
        apply_jobs.set_run_mode('auto')
    assert status == 'needs_you' and saved[-1][1].startswith('Submission outcome unknown')


def test_a_submit_and_a_confirmation_page_is_applied(run, submitted):
    run.browser.page_text = "Thank you for applying! Your application has been received."
    status, _, saved = run(history=FakeHistory(success=True))
    assert status == "applied" and saved[-1] == ("applied", None)


@pytest.mark.parametrize("text, confirmed", [
    ("Thank you for applying!", True),
    ("Your application was sent to Acme", True),
    ("Your application has not been submitted.", False),
    ("Thank you for applying. There was an error with your submission.", False),
    ("Please fix the errors below to complete your application", False),
    ("Application form", False),
])
def test_what_counts_as_a_confirmation_page(text, confirmed):
    assert apply_jobs.confirms_submission(text) is confirmed


def test_a_stop_during_the_fit_check_puts_the_job_back(run, monkeypatch):
    """Cancelled after the claim but before the agent started: the job must not stay 'in progress'."""
    async def fit(*a, **k):
        raise asyncio.CancelledError()

    monkeypatch.setattr(apply_jobs, "check_fit", fit)
    with pytest.raises(asyncio.CancelledError):
        run()
    assert run.saved[-1] == ("pending", apply_jobs.INTERRUPTED_NOTE)
    assert run.agents == []   # it never got as far as the agent


def test_a_malformed_profile_doesnt_stop_the_worker(monkeypatch):
    applied, saved = [], []

    def broken(*a, **k):
        raise ValueError("malformed profile")

    async def apply(job, *a, **k):
        applied.append(job["url"])
        return "applied"

    async def save(url, status, error=None, agent_words=False):
        saved.append((url, status))

    monkeypatch.setattr(apply_jobs, "load_json", broken)
    monkeypatch.setattr(apply_jobs, "apply_to_job", apply)
    monkeypatch.setattr(apply_jobs, "save_job_status", save)
    monkeypatch.setattr(apply_jobs, "_busy_sites", {})
    queue = asyncio.Queue()
    for url in ("https://a.example/1", "https://b.example/1"):
        queue.put_nowait({"url": url, "title": "Intern"})
    asyncio.run(apply_jobs.worker("W1", 1, queue, {"name": "Ada"}, {}, [], False, {}))
    assert applied == ["https://a.example/1", "https://b.example/1"]


def test_page_loads_and_step_saves_never_count_as_a_submit_attempt():
    """Every page request goes through the gate now: only the guard's recorded submit counts."""
    from types import SimpleNamespace
    ref = {"request_gate": SimpleNamespace(sent=40), "submit_guard": SimpleNamespace(attempts=0)}
    assert not apply_jobs.submit_attempted(ref)
    ref["submit_guard"].attempts = 1
    assert apply_jobs.submit_attempted(ref)
