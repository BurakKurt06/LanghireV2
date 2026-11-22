"""What the user's first supervised run on 1.3.0 showed (2026-11-14), each as a failing case first.

- A rejected "Sign In" left the job marked "may have been sent": the guard counted the sign-in
  form's button as a possible application submit. "Submit Registration" would have been refused
  in a Trial run. Account buttons never send the application.
- The site emailed a link, the agent asked for a "code", and the user was asked after 90 seconds.
  The reader also opened old, unrelated emails again on every retry.
- "You stepped in, so the agent is waiting" stayed in the run panel while the agent was working.
- The agent's own link click counted as the user's when the site started the navigation late.
- Every log line with an emoji raised an encoding error whose trace flooded the run log.
"""
import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone

import browser_use
import pytest

from core import inbox, run_watch, submit_guard
from core.run_watch import RunWatch
from core.submit_guard import Control, SubmitGuard


# ── account buttons are never the application's submit ──────────────────────

@pytest.mark.parametrize("label", ["Sign In", "Log in", "Create Account", "Register", "Submit Registration", "Sign up"])
@pytest.mark.parametrize("mode, live", [("auto", False), ("trial", True)])
def test_sign_in_and_registration_buttons_are_not_submit_attempts(label, mode, live):
    guard = SubmitGuard(mode=mode, job_url="https://jobs.example/1", live_steps=live)
    decision = guard.decide("click", Control(text=label, tag="button", type="submit", in_form=True))
    assert decision.allowed and not decision.submit and not decision.tentative


def test_enter_in_the_password_field_of_a_sign_in_form_is_not_a_submit_attempt():
    guard = SubmitGuard(mode="auto", job_url="https://jobs.example/1")
    field = Control(text="Password", tag="input", type="password", in_form=True, submitter_text="Sign In")
    decision = guard.decide("send_keys", field, text="Enter")
    assert decision.allowed and not decision.submit and not decision.tentative


@pytest.mark.parametrize("label", ["Submit Application", "Submit", "Submit registration and application"])
def test_the_real_submit_is_still_stopped_in_trial(label):
    guard = SubmitGuard(mode="trial", job_url="https://jobs.example/1", live_steps=True)
    decision = guard.decide("click", Control(text=label, tag="button", type="submit", in_form=True))
    assert not decision.allowed and decision.submit


def test_an_account_word_on_the_final_review_page_doesnt_open_the_gate():
    guard = SubmitGuard(mode="trial", job_url="https://jobs.example/1", live_steps=True)
    final = Control(text="Register", tag="button", type="submit", in_form=True, final_context=True)
    assert not guard.decide("click", final).allowed


# ── the email reader: a code or a link, recent emails only, each opened once ─

class _Mail:
    """A Gmail search listing; `opened` records which emails were opened."""
    rows: list = []
    messages: dict = {}
    opened: list = []

    def __init__(self, **kwargs):
        self.url = ""

    async def start(self):
        pass

    async def kill(self):
        pass

    async def navigate_to(self, url):
        self.url = url
        if "#all/" in url:
            _Mail.opened.append(url.rsplit("/", 1)[-1])

    async def get_current_page(self):
        browser = self

        class Page:
            async def evaluate(self, js, *args):
                import json
                if "#all/" in browser.url:
                    return json.dumps(_Mail.messages[browser.url.rsplit("/", 1)[-1]])
                return json.dumps({"rows": _Mail.rows, "ready": True})
        return Page()


def _stamp(when: datetime) -> str:
    return when.astimezone().strftime("%a, %b %d, %Y, %I:%M %p")


def _find(monkeypatch, kind, rows, messages, **kwargs):
    monkeypatch.setattr(_Mail, "rows", rows)
    monkeypatch.setattr(_Mail, "messages", messages)
    monkeypatch.setattr(_Mail, "opened", [])
    monkeypatch.setattr(browser_use, "BrowserSession", _Mail)
    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    return asyncio.run(inbox.find_verification(
        kind, {"campus.example.com"}, kwargs.pop("since", datetime.now(timezone.utc) - timedelta(minutes=3)), {},
        employer_names=["Northwind Bank"], recipient="ada.accounts@example.com",
        link_domains={"careers.example.net"}, **kwargs))


_real_sleep = asyncio.sleep


async def _no_sleep(seconds, *args, **kwargs):
    await _real_sleep(0)


LINK_MAIL = {"subject": "Account Registration", "ready": True, "to": ["ada.accounts@example.com"],
             "text": "Thank you for your interest in Northwind Bank. Please click here to validate your email.",
             "links": ["https://careers.example.net/account/validate?token=made-up"]}


def test_a_link_is_returned_when_the_agent_asked_for_a_code(monkeypatch):
    rows = [{"id": "m1", "sender_email": "noreply@campus.example.com", "subject": "Account Registration",
             "date": _stamp(datetime.now())}]
    found = _find(monkeypatch, "code", rows, {"m1": LINK_MAIL}, wait_seconds=0)
    assert found == {"link": "https://careers.example.net/account/validate?token=made-up"}


def test_a_code_is_returned_when_the_agent_asked_for_a_link(monkeypatch):
    mail = {**LINK_MAIL, "text": "Northwind Bank: your verification code is 482913", "links": []}
    rows = [{"id": "m1", "sender_email": "noreply@campus.example.com", "subject": "Your code", "date": _stamp(datetime.now())}]
    assert _find(monkeypatch, "link", rows, {"m1": mail}, wait_seconds=0) == {"code": "482913"}


def test_emails_older_than_the_application_are_never_opened_and_no_email_is_opened_twice(monkeypatch):
    now = datetime.now()
    other = {"subject": "Verify your order", "ready": True, "to": ["ada.accounts@example.com"],
             "text": "Some shop: confirm your order", "links": []}
    rows = [{"id": "new", "sender_email": "shop@example.org", "subject": "Verify your order", "date": _stamp(now)},
            {"id": "old1", "sender_email": "x@example.org", "subject": "Your code", "date": _stamp(now - timedelta(hours=3))},
            {"id": "old2", "sender_email": "y@example.org", "subject": "Confirm", "date": _stamp(now - timedelta(hours=7))}]
    found = _find(monkeypatch, "code", rows, {"new": other, "old1": other, "old2": other}, wait_seconds=0.05)
    assert "error" in found and "code email" not in found["error"]      # "No verification email ...": code or link
    assert _Mail.opened == ["new"]            # the hours-old ones not at all; the new one once, not on every retry


def test_the_reader_never_looks_back_more_than_an_hour(monkeypatch):
    seen = {}
    real = inbox._poll

    async def spy(page, js, *args, **kwargs):
        if args and str(args[0]).startswith("after:"):
            seen["after"] = int(str(args[0]).split(":")[1])
        return await real(page, js, *args, **kwargs)
    monkeypatch.setattr(inbox, "_poll", spy)
    _find(monkeypatch, "code", [], {}, wait_seconds=0, since=datetime.now(timezone.utc) - timedelta(hours=9))
    assert time.time() - seen["after"] <= 3600 + 5


@pytest.mark.parametrize("text", ["Sat, Nov 14, 2026, 11:49 PM", "Sat, Nov 14, 2026, 11:49 PM"])
def test_gmails_date_tooltip_is_read(text):
    when = inbox._row_time(text)
    assert (when.year, when.month, when.day, when.hour, when.minute) == (2026, 11, 14, 23, 49)
    assert inbox._row_time("yesterday") is None


# ── the run panel's note, and whose link click it was ───────────────────────

def test_the_waiting_note_exists_only_while_the_agent_waits():
    watch = RunWatch(browser_session=None, idle_seconds=30)
    assert watch.status() == {"user_active": False, "note": "", "edits_kept": 0}
    watch._last_user_event = time.monotonic()                 # the user clicked in the browser
    assert watch.status()["note"] == run_watch.WAITING_NOTE and watch.status()["user_active"]
    watch.resume_now()                                        # Resume pressed
    assert watch.status() == {"user_active": False, "note": "", "edits_kept": 0}


def test_what_the_agent_kept_is_shown_for_a_short_while_only(monkeypatch):
    watch = RunWatch(browser_session=None)
    watch.note, watch._note_at = "Continuing, keeping your 2 edits.", time.monotonic()
    assert watch.status()["note"] == "Continuing, keeping your 2 edits."
    watch._note_at = time.monotonic() - run_watch.NOTE_SECONDS - 1
    assert watch.status()["note"] == ""


def test_a_link_the_agent_clicked_is_its_own_even_when_the_site_moves_on_late():
    watch = RunWatch(browser_session=None)
    with watch.acting("click"):
        pass                                                   # the agent's click on "Apply now"
    ended = time.time() * 1000
    watch._count({"t": ended + 2000, "type": "navigation", "url": "https://jobs.example/apply"})
    assert not watch.user_stepped_in                           # announced 2 s later: still the agent's
    watch._count({"t": ended + 2500, "type": "navigation", "url": "https://jobs.example/other"})
    assert watch.user_stepped_in                               # a second one is the user's: one click, one navigation


@pytest.mark.parametrize("action, moved_already, after_ms", [
    ("input", False, 1500),        # the agent typed: a link clicked right after is the user's
    ("click", True, 1500),         # the agent's click already moved the page: the next link click is the user's
    ("click", False, 20_000),      # long after the agent's click
])
def test_a_link_the_user_clicks_is_the_users(action, moved_already, after_ms):
    watch = RunWatch(browser_session=None)
    with watch.acting(action):
        if moved_already:
            watch._count({"t": time.time() * 1000, "type": "navigation", "url": "https://jobs.example/apply"})
    assert not watch.user_stepped_in
    watch._count({"t": time.time() * 1000 + after_ms, "type": "navigation", "url": "https://jobs.example/other"})
    assert watch.user_stepped_in


def test_a_click_right_after_the_agents_action_is_the_users():
    watch = RunWatch(browser_session=None)
    with watch.acting("click"):
        pass
    watch._count({"t": time.time() * 1000 + 3000, "type": "pointerdown"})
    assert watch.user_stepped_in


# ── the log file takes every character ──────────────────────────────────────

def test_the_backend_log_takes_emoji_without_a_logging_error(tmp_path, monkeypatch):
    import main
    monkeypatch.setattr(main, "get_data_dir", lambda: tmp_path)
    handler = main._backend_log_handler()
    failures = []
    monkeypatch.setattr(handler, "handleError", lambda record: failures.append(record))
    try:
        handler.emit(logging.LogRecord("Agent\U0001f170 c28a", logging.INFO, __file__, 1, "\U0001f3af Task: apply", (), None))
    finally:
        handler.close()
    assert failures == []
    assert "\U0001f3af Task: apply" in (tmp_path / "backend.log").read_text(encoding="utf-8")
