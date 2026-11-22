"""What the first run on 1.3.3 (2026-11-19) showed, fixed. Made-up jobs; no browser, no network.

* Starting hundreds of jobs failed: "Access is denied: jobs.tmp -> jobs.json" (Windows refuses to
  replace a file the app's window is reading), after one rewrite of the job list per job.
* The ChatGPT limit: the run stopped starting jobs in time by its measure, yet a job still running
  used the rest and was cut off; one cut off right after a "Next"-type click was left "may have been sent".
* A careers site's sign-up dialog ended with a bare "Submit": the job was marked "may have been
  sent" before any application form existed.
* A site asked the ZIP code of every past employer, and the run waited five minutes for the user.
"""
import asyncio
import json
from types import SimpleNamespace

import pytest

import main
from cli import apply_jobs
from core import shared_config, submit_guard, usage_meter
from core.submit_guard import Control, SubmitGuard
from tests.backend.core.test_usage_meter import headers
from tests.backend.test_main_api import _isolate_state  # noqa: F401 (isolated job list)


# ── the job list is written even while something reads it ─────────────────────
def test_a_job_list_write_refused_for_a_moment_is_tried_again(tmp_path, monkeypatch):
    target = tmp_path / "jobs.json"
    target.write_text("{}")
    real_replace, refusals = type(target).replace, []

    def replace(self, other):
        if len(refusals) < 3:                                  # the window is reading the file right now
            refusals.append(other)
            raise PermissionError(13, "Access is denied")
        return real_replace(self, other)

    monkeypatch.setattr(type(target), "replace", replace)
    monkeypatch.setattr(shared_config, "REPLACE_WAIT_SECONDS", 0.001)
    shared_config.save_json(target, {"https://jobs.example/1": {"status": "pending"}})
    assert len(refusals) == 3 and json.loads(target.read_text()) == {"https://jobs.example/1": {"status": "pending"}}


def test_control_a_write_that_never_gets_through_still_says_so(tmp_path, monkeypatch):
    target = tmp_path / "jobs.json"

    def replace(self, other):
        raise PermissionError(13, "Access is denied")

    monkeypatch.setattr(type(target), "replace", replace)
    monkeypatch.setattr(shared_config, "REPLACE_WAIT_SECONDS", 0.001)
    with pytest.raises(PermissionError):
        shared_config.save_json(target, {})


def test_starting_500_jobs_resets_them_in_one_write(data_dir, monkeypatch):
    jobs = {f"https://jobs.example/{n}": {"title": "Analyst", "company": "Example Works",
                                          "status": ("failed" if n % 3 else "needs_you"), "error": "old"}
            for n in range(500)}
    jobs["https://jobs.example/applied"] = {"status": "applied"}
    jobs["https://jobs.example/sent"] = {"status": "needs_you", "submit_attempted_at": "2026-11-15T10:00:00+00:00"}
    shared_config.write_jobs(jobs)
    writes = []
    real_save = shared_config.save_json
    monkeypatch.setattr(shared_config, "save_json", lambda path, data: writes.append(path) or real_save(path, data))
    urls = list(jobs)
    chosen = main._reset_all_for_apply(urls)
    assert len(writes) == 1                                    # was 500 separate rewrites
    saved = shared_config.read_jobs()
    assert len(chosen) == 501 and all(j["url"] in jobs for j in chosen)
    assert all(saved[j["url"]]["status"] == "pending" and saved[j["url"]]["error"] is None for j in chosen)
    assert saved["https://jobs.example/applied"]["status"] == "applied"           # never started again
    # Chosen by hand like before: a job that may have been sent is started only after the user's
    # explicit acknowledgement, which the start endpoint asks for before this point.
    assert saved["https://jobs.example/sent"]["submit_attempted_at"] is None


# ── the ChatGPT limit: running jobs keep room for a whole job ─────────────────
def test_the_run_stops_starting_jobs_early_enough_for_the_running_ones_to_finish():
    """Made-up numbers of the same kind: about a third of a percent of the 5-hour limit per request,
    jobs of 10-30 requests. At 70% used with three jobs running the next job doesn't start any more."""
    usage_meter.note_reply(headers(primary=10), "s")
    for n in range(1, 120):                                      # 0.36% per request on average
        usage_meter.note_reply(headers(primary=10 + n * 0.36 // 1), "s")
    for size in (10, 12, 16, 16, 20, 26, 30, 30):
        for _ in range(size):
            usage_meter.note_reply({}, f"job-{size}-{_ % 1}")
        usage_meter.job_finished(f"job-{size}-0")
    per_job = usage_meter.job_percent("primary")
    assert 9 <= per_job <= 12                                    # the larger jobs, not the middle one (about 5.7%)
    usage_meter._state["meters"]["primary"]["used"] = 70.0
    assert usage_meter.stop_reason(running_jobs=0) is None       # alone, one more job fits
    assert usage_meter.stop_reason(running_jobs=3) is not None   # with three running: no new job


async def test_a_job_cut_off_after_a_next_click_is_looked_at_and_put_back_to_ready(monkeypatch):
    guard = SubmitGuard(mode="auto")
    guard.attempts = 1
    ref = {"submit_guard": guard, "tentative_submits": 1, "url": "https://jobs.example/1"}
    updates = []
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: updates.append(fields) or True)

    async def page(browser):
        return {"url": "https://jobs.example/apply/profile", "text": 900, "controls": 12}

    async def no_receipt(browser):
        return False

    monkeypatch.setattr(apply_jobs.run_watch, "page_state", page)
    monkeypatch.setattr(apply_jobs, "page_confirms_submission", no_receipt)
    await apply_jobs.settle_after_cut(ref, object())
    assert guard.attempts == 0 and not apply_jobs.submit_attempted(ref)
    assert {"submit_attempted_at": None} in updates


@pytest.mark.parametrize("ref_changes,confirms", [({"firm_submit": True}, False), ({}, True)])
async def test_control_a_real_submit_or_a_receipt_keeps_the_mark(monkeypatch, ref_changes, confirms):
    guard = SubmitGuard(mode="auto")
    guard.attempts = 1
    ref = {"submit_guard": guard, "tentative_submits": 1, "url": "https://jobs.example/1", **ref_changes}
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **fields: True)

    async def page(browser):
        return {"url": "https://jobs.example/done", "text": 900, "controls": 3}

    async def receipt(browser):
        return confirms

    monkeypatch.setattr(apply_jobs.run_watch, "page_state", page)
    monkeypatch.setattr(apply_jobs, "page_confirms_submission", receipt)
    await apply_jobs.settle_after_cut(ref, object())
    assert guard.attempts == 1 and apply_jobs.submit_attempted(ref)


async def test_control_a_page_that_never_shows_anything_keeps_the_mark(monkeypatch):
    guard = SubmitGuard(mode="auto")
    guard.attempts = 1
    ref = {"submit_guard": guard, "tentative_submits": 1, "url": "https://jobs.example/1"}
    monkeypatch.setattr(apply_jobs, "CUT_SETTLE_SECONDS", 0.2)

    async def blank(browser):
        return None

    monkeypatch.setattr(apply_jobs.run_watch, "page_state", blank)
    await apply_jobs.settle_after_cut(ref, object())
    assert guard.attempts == 1


# ── a sign-up dialog's bare "Submit" ───────────────────────────────────────────
@pytest.mark.parametrize("label", ["Submit", "Continue", "Next", "Done", "Create"])
@pytest.mark.parametrize("mode,live", [("auto", False), ("trial", True)])
def test_a_bare_submit_in_a_sign_up_dialog_is_an_account_step(label, mode, live):
    control = Control(text=label, tag="button", type="button", in_form=False, account_context=True)
    decision = SubmitGuard(mode=mode, live_steps=live).decide("click", control)
    assert decision.allowed and not decision.submit


@pytest.mark.parametrize("control", [
    Control(text="Submit", tag="button", type="button", in_form=False),                          # no sign-up context
    Control(text="Submit application", tag="button", account_context=True),                     # says application
    Control(text="Submit", tag="button", account_context=True, final_context=True),              # a final step
    Control(text="Submit", tag="button", account_context=True, application_review=True),
    Control(text="Send", tag="button", type="submit", in_form=True, account_context=True),       # not a bare step word
])
def test_control_without_that_context_or_with_an_application_heading_it_stays_final(control):
    decision = SubmitGuard(mode="trial", live_steps=True).decide("click", control)
    assert decision.submit and not decision.allowed


def test_the_page_reader_names_sign_up_dialogs_and_never_work_authorization():
    assert "auth(?:entication|enticate)?" in submit_guard._FORM_CONTEXT_JS       # the same words the page reader uses
    import re
    words = re.compile(r"(?:^|[^a-z])(?:auth(?:entication|enticate)?|log[-_ ]?in|sign[-_ ]?(?:in|up)|regist(?:er|ration)|"
                       r"create[-_ ]?(?:an?[-_ ]?|your[-_ ]?|new[-_ ]?)?account)(?![a-z])", re.I)
    for said, expected in [("candidate-auth-v2-modal Authentication", True), ("signupForm", False), ("sign-up-dialog", True),
                           ("loginModal", False), ("login-modal", True), ("registration-form", True),
                           ("work-authorization", False), ("authorization", False), ("author-bio", False),
                           ("application-form", False)]:
        assert bool(words.search(said)) is expected, said


# ── ZIP codes ──────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("question,section,expected", [
    ("Zip/Postal Code", "Professional Experience — Example Works", "that city's main ZIP code"),
    ("Postal code", "Education — State University", "that city's main ZIP code"),
    ("Employer ZIP", "", "that city's main ZIP code"),
    ("ZIP code", "Personal information", "62701 (profile)"),
    ("What is your favourite colour?", "About you", None),
])
def test_zip_codes_are_never_put_to_the_user(question, section, expected):
    told = apply_jobs.address_answer(question, section, {"address": {"zip": "62701"}})
    assert (told is None) if expected is None else (expected in told)


async def test_the_employer_zip_question_is_answered_without_a_wait(tmp_path, monkeypatch):
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    waits = []

    async def no_reply(*args, **kwargs):
        waits.append(args)

    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: store)
    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", no_reply)
    tools = apply_jobs.build_tools({"url": "https://jobs.example/1", "title": "Intern", "company": "Example"},
                                   "jobs.example", False, {}, 1)
    ask = tools.registry.registry.actions["ask_for_answers"].function
    result = await ask(params=apply_jobs.UnansweredQuestions(questions=[apply_jobs.UnansweredQuestion(
        question="Zip/Postal Code", section="Professional Experience — Example Works")]), browser_session=object())
    assert waits == [] and not result.is_done and "main ZIP code" in result.extracted_content
