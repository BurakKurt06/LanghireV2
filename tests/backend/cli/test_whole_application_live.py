"""Whole applications through the real engine: real browser, local fake hiring site, scripted AI.

What the user asked the app to do again: open the link, follow Apply into a new tab, create the
account the site wants (with the job-site password, fitted to the site's rules), fetch the
emailed code, fill the form and submit. And what must never hold a run: a page that stays blank
or stops responding is retried up to "Max Failures Per Job" and then given up as "The page
didn't load", without spending AI requests on it.
"""
import asyncio
import os
import time

import pytest

from core import inbox, run_watch, secret_store
from tests.backend import e2e_harness as lab
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

PASSWORD = "Made-Up-Pass-77"       # 15 characters: the fake site wants 18
POSTING = f"""<!doctype html><title>Test Analyst at Example Works</title><h1>Test Analyst</h1><p>{lab.FILLER}</p>
<a id="apply" href="/account" target="_blank">Apply Now</a>"""


def _account(error=""):
    return f"""<!doctype html><title>Create your account</title><h1>Create your account</h1><p>{lab.FILLER}</p>
<p id="error">{error}</p>
<form method="post" action="/account">
  <label for="email">Email address</label><input id="email" name="email" type="email">
  <label for="pw">Password</label><input id="pw" name="password" type="password">
  <label for="pw2">Confirm password</label><input id="pw2" name="confirm" type="password">
  <button id="create" type="submit">Create Account</button>
</form>"""


VERIFY = f"""<!doctype html><title>Check your email</title><h1>Enter the code we emailed you</h1><p>{lab.FILLER}</p>
<form method="post" action="/verify"><label for="code">Verification code</label><input id="code" name="code">
<button id="verify" type="submit">Verify</button></form>"""
FORM = f"""<!doctype html><title>Application form</title><h1>Application</h1><p>{lab.FILLER}</p>
<form method="post" action="/submit">
  <label for="first">First name</label><input id="first" name="first">
  <label for="salary">Desired hourly pay (number only)</label><input id="salary" name="salary">
  <label for="certify"><input type="checkbox" id="certify" name="certify" value="yes"> I certify that all information is true</label>
  <label for="news"><input type="checkbox" id="news" name="news" value="yes"> Send me marketing emails</label>
  <button id="send" type="submit">Submit application</button>
</form>"""
THANKS = "<!doctype html><title>Thank you</title><h1>Thank you for applying! Your application has been received.</h1>"


def hiring_site():
    def account(method, data):
        if method == "GET":
            return _account()
        if len(data.get("password", "")) < 18 or data.get("password") != data.get("confirm"):
            return _account("Password must be at least 18 characters.")
        return 303, {"Location": "/verify"}, ""

    def verify(method, data):
        if method == "POST" and data.get("code") == "482913":
            return 303, {"Location": "/form"}, ""
        return VERIFY

    return lab.Site({"/job": POSTING, "/account": account, "/verify": verify, "/form": FORM,
                     "/submit": lambda method, data: THANKS})


def applicant(state):
    """A scripted agent that does what the instructions say, page by page."""
    def at(text, *snippets):
        for snippet in snippets:        # an attribute, or (for buttons and links) the visible text
            index = lab.index_of(text, snippet)
            if index is not None:
                return index
        raise AssertionError(f"{snippets} not on the page the agent sees:\n{lab.browser_state(text)[-1500:]}")

    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Thank you for applying" in page:
            return {"done": {"text": "The application was submitted.", "success": True}}
        if "Application form" in page or "Desired hourly pay" in page:
            if state.get("filled"):
                return {"click": {"index": at(text, "id=send", "Submit application")}}
            state["filled"] = True
            return [{"input": {"index": at(text, "id=first"), "text": "Ada"}},
                    {"input": {"index": at(text, "id=salary"), "text": "20"}},
                    {"click": {"index": at(text, "id=certify")}}]
        if "Enter the code we emailed you" in page:
            if not state.get("code_asked"):
                state["code_asked"] = True
                return {"get_email_verification": {"kind": "code"}}
            return [{"input": {"index": at(text, "id=code"), "text": "482913"}}, {"click": {"index": at(text, "id=verify", "Verify")}}]
        if "Create your account" in page:
            if "at least 18 characters" in page and not state.get("rules_told"):
                state["rules_told"] = True
                return {"password_rejected": {"message": "Password must be at least 18 characters."}}
            return [{"input": {"index": at(text, "id=email"), "text": "<secret>account_email</secret>"}},
                    {"input": {"index": at(text, "id=pw ", "id=pw"), "text": "<secret>password</secret>"}},
                    {"input": {"index": at(text, "id=pw2"), "text": "<secret>password</secret>"}},
                    {"click": {"index": at(text, "id=create", "Create Account")}}]
        if "Apply Now" in page:
            return {"click": {"index": at(text, "id=apply", "Apply Now")}}
        return {"wait": {"seconds": 1}}
    return decide


async def _finish(run, site, state, seconds=150):
    try:
        return await asyncio.wait_for(run, seconds)
    except asyncio.TimeoutError:
        raise AssertionError(f"the application didn't finish; last requests: {site.requests[-6:]}; "
                             f"last pages: {state.get('pages', [])[-3:]}") from None


@pytest.fixture
def fake_inbox(monkeypatch):
    asked = []

    async def find(kind, senders, since, kwargs, **binding):
        asked.append((kind, binding.get("recipient")))
        return {"code": "482913"}

    monkeypatch.setattr(inbox, "find_verification", find)
    return asked


async def test_full_auto_follows_apply_into_a_new_tab_creates_the_account_and_submits(data_dir, monkeypatch, fake_inbox):
    site, state = hiring_site(), {}
    script = lab.Script(applicant(state))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script, password=PASSWORD)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await _finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    assert status == "applied", (status, shared_config.read_jobs()[job["url"]].get("error"), state.get("pages"))
    accounts = site.posted("/account")
    assert accounts[0]["email"] == "ada.accounts@example.com" and accounts[0]["password"] == PASSWORD
    # Rejected for its length: the same password with characters added to the end, saved for that site.
    assert accounts[1]["password"].startswith(PASSWORD) and len(accounts[1]["password"]) >= 18
    assert [e["kind"] for e in secret_store.site_password_entries()] == ["changed"]
    assert fake_inbox == [("code", "ada.accounts@example.com")] and site.posted("/verify") == [{"code": "482913"}]
    sent = site.posted("/submit")
    assert sent == [{"first": "Ada", "salary": "20", "certify": "yes"}]          # sent once; marketing left unticked
    saved = shared_config.read_jobs()[job["url"]]
    assert saved["status"] == "applied" and not saved.get("submit_attempted_at")   # confirmed: no "possibly sent" mark left
    assert PASSWORD not in "\n".join(script.seen)                                  # the model never saw the password


async def test_trial_goes_through_the_same_pages_and_never_presses_the_final_submit(data_dir, monkeypatch, fake_inbox):
    site, state = hiring_site(), {}
    inner = applicant(state)

    def decide(text, step):
        if state.get("refused"):
            return {"request_submit_review": {"summary": "Everything is filled in."}}
        if "final submission is disabled" in text:
            state["refused"] = True
            return {"request_submit_review": {"summary": "Everything is filled in."}}
        return inner(text, step)

    script = lab.Script(decide)
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script, password=PASSWORD, mode="trial")
        job = lab.add_job(shared_config, site.url("/job"))
        status = await _finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        apply_jobs.set_run_mode("auto")
        site.close()
    assert status == "practised" and state.get("refused")
    assert len(site.posted("/account")) == 2 and site.posted("/verify")       # the live steps really ran
    assert site.posted("/submit") == []                                        # the final Submit never did
    saved = shared_config.read_jobs()[job["url"]]
    assert saved["status"] == "pending" and saved.get("practised_at") and not saved.get("submit_attempted_at")


async def test_a_page_that_stays_blank_is_reloaded_then_given_up_without_an_ai_request(data_dir, monkeypatch):
    site = lab.Site({"/job": "<!doctype html><title></title><body></body>"})
    script = lab.Script(lambda text, step: {"wait": {"seconds": 1}})
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script, settings={"max_failures": 3})
        monkeypatch.setattr(apply_jobs, "PAGE_WAIT_SECONDS", 1)
        monkeypatch.setattr(apply_jobs, "BLANK_RELOAD_WAIT_SECONDS", 1)
        job = lab.add_job(shared_config, site.url("/job"))
        started = time.monotonic()
        status = await asyncio.wait_for(lab.run_job(apply_jobs, job), 120)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "failed" and saved["status"] == "failed"
    assert saved["error"].startswith("The page didn't load\n") and "Nothing was submitted" in saved["error"]
    assert "Traceback" not in saved["error"] and "PageStalled" not in saved["error"]
    assert script.calls == 0                                    # no AI request was spent on a blank page
    assert 2 <= len([r for r in site.requests if r[1] == "/job"]) <= 4   # opened, then reloaded up to the limit
    assert time.monotonic() - started < 90


async def test_a_page_that_stops_responding_is_started_again_then_given_up(data_dir, monkeypatch):
    """The 8-hour night: one job sat on a dead page with the whole queue behind it. A page that
    doesn't change for the stall time (5 minutes in the app; seconds here) ends the attempt."""
    site = lab.Site({"/job": POSTING})
    monkeypatch.setattr(run_watch, "STALL_SECONDS", 4.0)
    monkeypatch.setattr(run_watch, "SIGNATURE_SECONDS", 1)
    script = lab.Script(lambda text, step: {"wait": {"seconds": 30}})     # the agent sits on a page where nothing happens
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script, settings={"max_failures": 2})
        job = lab.add_job(shared_config, site.url("/job"))
        started = time.monotonic()
        status = await asyncio.wait_for(lab.run_job(apply_jobs, job), 180)
        took = time.monotonic() - started
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "failed" and saved["error"].startswith("The page didn't load\n")
    assert "2 times in a row" in saved["error"] and "moved on to the next job" in saved["error"]
    assert len([r for r in site.requests if r[1] == "/job"]) == 2       # the job was started again once (the limit is 2)
    assert took < 120
