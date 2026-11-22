"""What the user's second supervised run showed, on local fake hiring sites: real browser, scripted AI.

Each test replays, with made-up pages, something that went wrong in the user's runs of 2026-11:

* a hiring system whose posting has its "Apply" as a form's submit button (a form of hidden
  values): every job there was marked "may have been sent" from its first click, a Trial run
  could not start one, and its "Print Application" link froze the browser;
* a site that showed "Application Submitted" and was left for its home page: called unconfirmed;
* a site saying the job was already applied to: marked Failed and tried again;
* an invisible hCaptcha: an answer bought before the first try was refused by the site;
* "Create Account", then the sign-in page again (the account waited for its emailed link): the
  agent registered again and again, then gave up;
* the AI plan's limit running out mid-application: "outcome unknown" instead of ready again.

No AI provider, no real site and no email is touched: pages come from this computer, the
scripted AI plays the agent, the email reader and the CAPTCHA solver are stand-ins.
"""
import asyncio
import os

import pytest

from core import inbox, job_screenshots, usage_meter
from tests.backend import e2e_harness as lab
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

THANKS = "<!doctype html><title>Thank you</title><h1>Thank you for applying! Your application has been received.</h1>"
REFUSAL = "printing or downloading the application isn't part of applying"


def at(text, *snippets):
    for snippet in snippets:
        index = lab.index_of(text, snippet)
        if index is not None:
            return index
    raise AssertionError(f"{snippets} not on the page the agent sees:\n{lab.browser_state(text)[-1500:]}")


async def finish(run, site, state, seconds=180):
    try:
        return await asyncio.wait_for(run, seconds)
    except asyncio.TimeoutError:
        raise AssertionError(f"the application didn't finish; last requests: {site.requests[-6:]}; "
                             f"last pages: {state.get('pages', [])[-3:]}") from None


def paths(site, method="POST"):
    return [where for how, where, _ in site.requests if how == method]


# ── a hiring system whose posting's Apply is a form's submit button ────────────
POSTING = f"""<!doctype html><title>Graduate Analyst at Example Works</title><h1>Graduate Analyst posting</h1><p>{lab.FILLER}</p>
<form method="post" action="/start"><input type="hidden" name="opp" value="1">
<input type="submit" id="submit_button" name="submit_button" value="Apply"></form>"""
SIGN_IN = f"""<!doctype html><title>Sign in</title><h1>Sign in to continue</h1><p>{lab.FILLER}</p>
<form method="post" action="/login"><label for="email">Email address</label><input id="email" name="email" type="email">
<label for="pw">Password</label><input id="pw" name="password" type="password">
<button id="login" type="submit">Login</button></form>"""
SECTION = f"""<!doctype html><title>Personal details</title><h1>Personal details section</h1><p>{lab.FILLER}</p>
<form method="post" action="/section"><label for="first">First name</label><input id="first" name="first">
<button id="continue_button" name="continue_button" type="submit">Save and Continue</button></form>"""
DECLARATION = f"""<!doctype html><title>Declaration</title><h1>Declaration and final step</h1><p>{lab.FILLER}</p>
<a id="print" href="/print" target="_blank">Print Application</a>
<form method="post" action="/submit">
<label for="agree"><input type="checkbox" id="agree" name="agree" value="yes"> I declare that this is true</label>
<button id="final" type="submit">Submit Application</button></form>"""
PRINTABLE = "<!doctype html><title>Printable application</title><h1>Printable application</h1><script>window.print()</script>"


def sections_site():
    return lab.Site({
        "/job": POSTING, "/start": lambda method, data: (303, {"Location": "/signin"}, ""), "/signin": SIGN_IN,
        "/login": lambda method, data: (303, {"Location": "/details"}, ""), "/details": SECTION,
        "/section": lambda method, data: (303, {"Location": "/declaration"}, ""), "/declaration": DECLARATION,
        "/print": PRINTABLE, "/submit": lambda method, data: THANKS})


def sections_agent(state, *, stop_at_section=False, trial=False, limit_after_section=False):
    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Thank you for applying" in page:
            return {"done": {"text": "The application was submitted.", "success": True}}
        if "Declaration and final step" in page:
            if limit_after_section:
                from browser_use.llm.exceptions import ModelRateLimitError
                usage_meter.limit_reached(3600)
                raise ModelRateLimitError(message="Your ChatGPT plan's usage limit is reached. It resets in about 60 "
                                                  "minutes. (The usage limit has been reached)", model="test")
            if not state.get("print_tried"):
                state["print_tried"] = True
                return {"click": {"index": at(text, "id=print", "Print Application")}}
            if not state.get("agreed"):
                state["agreed"] = True
                return {"click": {"index": at(text, "id=agree")}}
            if trial and state.get("final_tried"):
                return {"request_submit_review": {"summary": "Everything is filled in; the final Submit is next."}}
            state["final_tried"] = True
            return {"click": {"index": at(text, "id=final", "Submit Application")}}
        if "Personal details section" in page:
            if stop_at_section:
                return {"done": {"success": False, "text": "NEEDS YOU: Pre-university education details\n"
                                                           "The form needs your school before university."}}
            return [{"input": {"index": at(text, "id=first"), "text": "Ada"}},
                    {"click": {"index": at(text, "id=continue_button", "Save and Continue")}}]
        if "Sign in to continue" in page:
            return [{"input": {"index": at(text, "id=email"), "text": "<secret>account_email</secret>"}},
                    {"input": {"index": at(text, "id=pw"), "text": "<secret>password</secret>"}},
                    {"click": {"index": at(text, "id=login", "Login")}}]
        if "Graduate Analyst posting" in page:
            return {"click": {"index": at(text, "id=submit_button", "name=submit_button", "value=Apply")}}
        return {"wait": {"seconds": 1}}
    return decide


async def test_a_posting_whose_apply_is_a_form_button_is_applied_to_and_print_is_never_opened(data_dir, monkeypatch):
    site, state = sections_site(), {}
    script = lab.Script(sections_agent(state))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert saved["status"] == "applied" and not saved.get("submit_attempted_at")
    assert paths(site) == ["/start", "/login", "/section", "/submit"]                # each once; sent exactly once
    assert "/print" not in paths(site, "GET")                                         # the print page was never opened
    assert any(REFUSAL in seen for seen in script.seen)                               # the agent was told why
    assert not job_screenshots.exists(job["url"])                                     # an applied job keeps no picture


async def test_a_job_that_stops_before_the_final_submit_is_not_possibly_sent(data_dir, monkeypatch):
    """It used to end "Submission outcome unknown" on every job of such a site: the posting's Apply
    button counted as the application's final Submit, for good."""
    site, state = sections_site(), {}
    script = lab.Script(sections_agent(state, stop_at_section=True))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "needs_you", (status, saved.get("error"))
    assert saved["error"].startswith("Pre-university education details")
    assert "Submission outcome unknown" not in saved["error"] and not saved.get("submit_attempted_at")
    assert paths(site) == ["/start", "/login"] and "/submit" not in paths(site)
    # The last page is kept as a picture for the job's card (taken by the browser, no AI request).
    assert job_screenshots.exists(job["url"]) and saved.get("screenshot_at")
    assert job_screenshots.path_for(job["url"]).read_bytes().startswith(b"\xff\xd8")


async def test_trial_starts_such_an_application_and_still_never_presses_the_final_submit(data_dir, monkeypatch):
    site, state = sections_site(), {}
    script = lab.Script(sections_agent(state, trial=True))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script, mode="trial")
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        apply_jobs.set_run_mode("auto")
        site.close()
    assert status == "practised", (status, shared_config.read_jobs()[job["url"]].get("error"), state.get("pages"))
    assert paths(site) == ["/start", "/login", "/section"]            # every step on the way ran; the final one didn't
    assert any("final submission is disabled" in seen for seen in script.seen)
    assert "/print" not in paths(site, "GET")


async def test_the_ai_plans_limit_mid_application_puts_the_job_back_to_ready(data_dir, monkeypatch):
    """Right after "Save and Continue": it used to end "Submission outcome unknown"."""
    site, state = sections_site(), {}
    script = lab.Script(sections_agent(state, limit_after_section=True))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "limit", (status, saved.get("error"))
    assert saved["status"] == "pending" and saved["error"] == apply_jobs.LIMIT_NOTE
    assert not saved.get("submit_attempted_at") and "/submit" not in paths(site)


# ── a progress tracker: the unfinished section is only reached through it ──────
def _tracker(declaration_done=False):
    rows = [("Personal Information", "Mandatory item completed", "~~NON_MANDATORY_ICON~~"),
            ("Diversity", "Item completed", "Green Tick"),
            ("Declaration", "Item completed" if declaration_done else "Item incomplete",
             "Green Tick" if declaration_done else "Red Cross")]
    items = "".join(f'<li><span aria-label="{icon}" title="{icon}"></span>'
                    f'<a href="/eform/1/{name.split()[0].lower()}" aria-label="Section Header: {name}  {state}">{name}</a></li>'
                    for name, state, icon in rows)
    return (f'<aside><h2>Progress Tracker</h2><ul id="tracker">{items}</ul><h3>Symbol Key</h3>'
            '<ul><li><span aria-label="Green Tick"></span>Completed</li><li><span aria-label="Red Cross"></span>Incomplete</li></ul></aside>'
            '<p><a id="print" href="/eform/1/print" target="_blank">Print Application</a></p>')


TRACKED_SECTION = f"""<!doctype html><title>Diversity</title>{_tracker()}<h1>Diversity section</h1><p>{lab.FILLER}</p>
<form method="post" action="/eform/1/diversity"><label for="first">Optional note</label><input id="first" name="first">
<button id="continue_button" type="submit">Save and Continue</button></form>"""
TRACKED_DECLARATION = f"""<!doctype html><title>Declaration</title>{_tracker()}<h1>Declaration</h1><p>{lab.FILLER}</p>
<form method="post" action="/submit"><label for="agree"><input type="checkbox" id="agree" name="agree" value="yes"> I agree with the above</label>
<button id="back" type="button">Back</button><button id="final" type="submit">Submit</button></form>"""
PRINT_COPY = (f"<!doctype html><title>Print</title>{_tracker()}<h1>Printable copy of your application</h1>"
              f"<p>{lab.FILLER}</p><script>window.print(); document.title = 'Print view opened';</script>")


async def test_the_unfinished_section_is_found_through_the_progress_tracker_and_print_is_never_used(data_dir, monkeypatch):
    """Seen 2026-11: the tracker marked Declaration incomplete; the agent opened Print Application
    instead (a read-only copy) and the print window froze the browser."""
    site = lab.Site({"/job": PLAIN_POSTING.replace('href="/apply"', 'href="/eform/1/diversity"'),
                     "/eform/1/diversity": lambda method, data: (303, {"Location": "/eform/1/print"}, "")
                     if method == "POST" else TRACKED_SECTION,
                     "/eform/1/print": PRINT_COPY, "/eform/1/declaration": TRACKED_DECLARATION,
                     "/submit": lambda method, data: THANKS})
    state = {}

    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        note = text[text.rfind("PROGRESS TRACKER:"):] if "PROGRESS TRACKER:" in text else ""
        if "Thank you for applying" in page:
            return {"done": {"text": "The application was submitted.", "success": True}}
        if "Printable copy of your application" in page:
            state["saw_print_copy"] = True
            return {"wait": {"seconds": 1}}
        if "I agree with the above" in page and "Declaration" in page and "Diversity section" not in page:
            return [{"click": {"index": at(text, "id=agree")}}, {"click": {"index": at(text, "id=final", "Submit")}}]
        if "Diversity section" in page:
            if not state.get("saved"):
                state["saved"] = True
                return [{"input": {"index": at(text, "id=first"), "text": "None"}},
                        {"click": {"index": at(text, "id=continue_button", "Save and Continue")}}]
            if not state.get("print_clicked"):            # what the agent did on 1.3.2
                state["print_clicked"] = True
                return {"click": {"index": at(text, "id=print", "Print Application")}}
            if not state.get("print_opened"):
                state["print_opened"] = True
                return {"navigate": {"url": site.url("/eform/1/print"), "new_tab": False}}
            if note.startswith("PROGRESS TRACKER: this application's list of sections marks 'Declaration'"):
                state["followed_note"] = True
                return {"click": {"index": at(text, "Section Header: Declaration")}}
        if "Test Analyst posting" in page:
            return {"click": {"index": at(text, "id=apply", "Apply Now")}}
        return {"wait": {"seconds": 1}}

    script = lab.Script(decide)
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert site.posted("/submit") == [{"agree": "yes"}]
    assert state["followed_note"] and not state.get("saw_print_copy")          # the agent never had the print copy in front of it
    assert sum(REFUSAL in seen for seen in script.seen) >= 2                   # the click and the address, both refused
    # The site's own redirect to the print copy was the only time it was asked for; the app went back from it.
    assert [where for how, where, _ in site.requests if how == "GET" and where == "/eform/1/print"] == ["/eform/1/print"]


# ── the site confirmed it, then the agent left that page ───────────────────────
PLAIN_POSTING = f"""<!doctype html><title>Test Analyst at Example Works</title><h1>Test Analyst posting</h1><p>{lab.FILLER}</p>
<a id="apply" href="/apply">Apply Now</a>"""
PLAIN_FORM = f"""<!doctype html><title>Application form</title><h1>Application form</h1><p>{lab.FILLER}</p>
<form method="post" action="/submit"><label for="first">First name</label><input id="first" name="first">
<button id="send" type="submit">Submit application</button></form>"""
SUBMITTED = f"""<!doctype html><title>Application Submitted</title><h1>Application Submitted</h1><p>{lab.FILLER}</p>
<a id="home" href="/home">Return to Home</a>"""
HOME = f"""<!doctype html><title>Candidate Home</title><h1>Candidate Home</h1><p>{lab.FILLER}</p><p>Under review. No tasks.</p>"""


async def test_a_confirmation_the_site_showed_counts_even_after_the_agent_left_that_page(data_dir, monkeypatch):
    site = lab.Site({"/job": PLAIN_POSTING, "/apply": PLAIN_FORM, "/submit": lambda method, data: SUBMITTED, "/home": HOME})
    state = {}

    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Candidate Home" in page:
            return {"done": {"text": "Submitted; the home page lists it as under review.", "success": True}}
        if "Return to Home" in page:
            return {"click": {"index": at(text, "id=home", "Return to Home")}}
        if "Application form" in page:
            return [{"input": {"index": at(text, "id=first"), "text": "Ada"}},
                    {"click": {"index": at(text, "id=send", "Submit application")}}]
        if "Apply Now" in page:
            return {"click": {"index": at(text, "id=apply", "Apply Now")}}
        return {"wait": {"seconds": 1}}

    script = lab.Script(decide)
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert saved["submission_evidence"]["kind"] == "confirmation_page" and not saved.get("submit_attempted_at")
    assert site.posted("/submit") == [{"first": "Ada"}]


# ── the site says this job was already applied to ──────────────────────────────
def _already_applied_run(data_dir, monkeypatch, page_says):
    site = lab.Site({"/job": PLAIN_POSTING,
                     "/apply": f"<!doctype html><title>Your account</title><h1>Your account</h1><p>{lab.FILLER}</p><p>{page_says}</p>"})
    state = {}

    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Your account" in page:
            return {"done": {"success": False, "text": "ALREADY APPLIED: The site says this job was already applied to."}}
        if "Apply Now" in page:
            return {"click": {"index": at(text, "id=apply", "Apply Now")}}
        return {"wait": {"seconds": 1}}

    return site, state, lab.Script(decide)


async def test_already_applied_on_the_sites_word_is_applied_not_failed_and_not_retried(data_dir, monkeypatch):
    site, state, script = _already_applied_run(data_dir, monkeypatch, "You have already applied to this job.")
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "applied", (status, saved.get("error"))
    assert saved["submission_evidence"]["kind"] == "site_says_already_applied" and not saved.get("error")
    assert paths(site) == [] and "_retry_with" not in job              # nothing was sent, and no second try is queued


async def test_control_the_agents_word_without_the_sites_is_left_for_the_user(data_dir, monkeypatch):
    site, state, script = _already_applied_run(data_dir, monkeypatch, "Sign in to start.")
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "needs_you" and saved["error"].startswith("Already applied? Not confirmed")


# ── Create Account, then the sign-in page again ────────────────────────────────
def _sign_in_page(message=""):
    return f"""<!doctype html><title>Sign in</title><h1>Sign in to your account</h1><p>{lab.FILLER}</p><p id="message">{message}</p>
<form method="post" action="/account"><label for="email">Email address</label><input id="email" name="email" type="email">
<label for="pw">Password</label><input id="pw" name="password" type="password">
<button id="signin" type="submit">Sign In</button></form>
<a id="create" href="/register">Create Account</a>"""


REGISTER = f"""<!doctype html><title>Create your account</title><h1>Create your account</h1><p>{lab.FILLER}</p>
<form method="post" action="/register"><label for="email">Email address</label><input id="email" name="email" type="email">
<label for="pw">Password</label><input id="pw" name="password" type="password">
<label for="pw2">Verify new password</label><input id="pw2" name="confirm" type="password">
<button id="register" type="submit">Create Account</button></form>"""


def verified_account_site(account):
    def sign_in(method, data):
        if method == "GET":
            return _sign_in_page()
        if account.get("verified"):
            return 303, {"Location": "/apply"}, ""
        return _sign_in_page("Wrong email or password, or your account is locked.")

    def register(method, data):
        if method == "GET":
            return REGISTER
        account["registered"] = account.get("registered", 0) + 1
        return 303, {"Location": "/account"}, ""          # back at Sign In, without a word about the email it sent

    def verify(method, data):
        account["verified"] = data.get("token") == "abc"
        return 303, {"Location": "/account"}, ""

    posting = PLAIN_POSTING.replace('href="/apply"', 'href="/account"')
    return lab.Site({"/job": posting, "/account": sign_in, "/register": register, "/verify": verify,
                     "/apply": PLAIN_FORM, "/submit": lambda method, data: THANKS})


async def test_back_at_sign_in_after_create_account_the_emailed_link_is_fetched_and_the_job_goes_on(data_dir, monkeypatch):
    account, state = {}, {}
    site = verified_account_site(account)
    asked = []

    async def find(kind, senders, since, kwargs, **binding):
        asked.append(kind)
        return {"link": site.url("/verify?token=abc")}

    def decide(text, step):
        """An agent that does what the app's ACCOUNT MEMORY note says on each account page."""
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Thank you for applying" in page:
            return {"done": {"text": "The application was submitted.", "success": True}}
        if "Application form" in page:
            return [{"input": {"index": at(text, "id=first"), "text": "Ada"}},
                    {"click": {"index": at(text, "id=send", "Submit application")}}]
        if "Create your account" in page:
            state["registrations"] = state.get("registrations", 0) + 1
            if state["registrations"] > 2:              # the old behaviour: registering over and over
                return {"done": {"success": False, "text": "NEEDS YOU: complete account access"}}
            return [{"input": {"index": at(text, "id=email"), "text": "<secret>account_email</secret>"}},
                    {"input": {"index": at(text, "id=pw ", "id=pw"), "text": "<secret>password</secret>"}},
                    {"input": {"index": at(text, "id=pw2"), "text": "<secret>password</secret>"}},
                    {"click": {"index": at(text, "id=register", "Create Account")}}]
        if "Sign in to your account" in page:
            # The note of THIS step (the last one in what the agent is sent).
            note = text[text.rfind("ACCOUNT MEMORY:"):] if "ACCOUNT MEMORY:" in text else ""
            state.setdefault("notes", []).append(note[:60])
            if note.startswith("ACCOUNT MEMORY: you pressed Create Account"):
                return {"get_email_verification": {"kind": "link"}}
            if note.startswith("ACCOUNT MEMORY: the app already has an account"):
                state["sign_ins"] = state.get("sign_ins", 0) + 1
                if state["sign_ins"] > 2:
                    return {"done": {"success": False, "text": "NEEDS YOU: sign-in failed on this site"}}
                return [{"input": {"index": at(text, "id=email"), "text": "<secret>account_email</secret>"}},
                        {"input": {"index": at(text, "id=pw"), "text": "<secret>password</secret>"}},
                        {"click": {"index": at(text, "id=signin", "Sign In")}}]
            return {"click": {"index": at(text, "id=create", "Create Account")}}
        if "Apply Now" in page:
            return {"click": {"index": at(text, "id=apply", "Apply Now")}}
        return {"wait": {"seconds": 1}}

    script = lab.Script(decide)
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
        monkeypatch.setattr(inbox, "find_verification", find)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    saved = shared_config.read_jobs()[job["url"]]
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert account == {"registered": 1, "verified": True}              # registered once, never again
    assert asked == ["link"] and ("GET", "/verify", {"token": "abc"}) in site.requests
    assert site.posted("/submit") == [{"first": "Ada"}]


# ── an invisible hCaptcha (a stand-in served from this computer) ───────────────
def captcha_form(error=""):
    """A form wired the way Lever's is: the page keeps the function hCaptcha answers through, and
    sends the form directly once the hidden answer field holds something."""
    return f"""<!doctype html><title>Apply: Index Analyst</title><h1>Submit your application</h1><p>{lab.FILLER}</p>
<p id="error">{error}</p>
<form id="application-form" method="post" action="/submit">
  <label for="name">Full name</label><input id="name" name="name">
  <input type="hidden" name="h-captcha-response" id="hcaptchaResponseInput" value="">
  <div id="h-captcha" class="h-captcha" data-sitekey="made-up-site-key"></div>
  <button id="hcaptchaSubmitBtn" type="submit" style="display:none">send</button>
  <button id="btn-submit" type="button">Submit application</button>
</form>
<script>
let captchaId; let executedFromSubmitButton;
const clickSubmitButton = function () {{ document.getElementById('hcaptchaSubmitBtn').click(); }};
var onSuccess = function (token) {{
  document.getElementById('hcaptchaResponseInput').value = token;
  if (executedFromSubmitButton) clickSubmitButton();
}};
var onLoad = function () {{
  captchaId = hcaptcha.render('h-captcha', {{ sitekey: 'made-up-site-key', callback: onSuccess }});
  document.getElementById('btn-submit').addEventListener('click', function () {{
    if (document.getElementById('hcaptchaResponseInput').value) {{ clickSubmitButton(); return; }}
    executedFromSubmitButton = true;
    hcaptcha.execute(captchaId);
  }});
}};
// A stand-in for hCaptcha's own script: nothing is loaded from hcaptcha.com.
setTimeout(function () {{
  window.hcaptcha = {{
    render: function (id, params) {{
      this.callback = params.callback;
      const frame = document.createElement('iframe');
      frame.src = '/widget/hcaptcha.com?frame=checkbox-invisible&sitekey=made-up-site-key';
      frame.style.display = 'none';
      document.getElementById(id).appendChild(frame);
      return 'widget-1';
    }},
    execute: function () {{ WIDGET_BEHAVIOUR }},
    getResponse: function () {{ return ''; }},
  }};
  onLoad();
}}, 50);
</script>"""


ANSWERS_ITSELF = "this.callback('widget-token');"
SHOWS_A_PUZZLE = ("const puzzle = document.createElement('iframe'); "
                  "puzzle.src = '/widget/hcaptcha.com?frame=challenge&sitekey=made-up-site-key'; "
                  "puzzle.width = 320; puzzle.height = 240; document.body.appendChild(puzzle);")


def captcha_site(widget, accepts):
    """`accepts(token, attempt)`: whether the site's server takes that answer on that POST."""
    posts = []

    def submit(method, data):
        posts.append(data.get("h-captcha-response", ""))
        if accepts(posts[-1], len(posts)):
            return THANKS
        return captcha_form("There was an error verifying your application. Please try again.").replace(
            "WIDGET_BEHAVIOUR", widget)

    posting = PLAIN_POSTING.replace("Apply Now", "Apply for this job")
    site = lab.Site({"/job": posting, "/apply": captcha_form().replace("WIDGET_BEHAVIOUR", widget), "/submit": submit,
                     "/widget/hcaptcha.com": "<!doctype html><title>widget</title>"})
    return site, posts


def captcha_agent(state):
    def decide(text, step):
        page = lab.browser_state(text)
        state.setdefault("pages", []).append(page[:120])
        if "Thank you for applying" in page:
            return {"done": {"text": "The application was submitted.", "success": True}}
        if "Submit your application" in page:
            if "error verifying your application" in page:
                if state.get("again"):
                    return {"wait": {"seconds": 1}}
                state["again"] = True
                return [{"input": {"index": at(text, "id=name"), "text": "Ada Test"}},
                        {"click": {"index": at(text, "id=btn-submit", "Submit application")}}]
            if state.get("sent"):
                return {"wait": {"seconds": 1}}           # sent: waiting for the site
            state["sent"] = True
            return [{"input": {"index": at(text, "id=name"), "text": "Ada Test"}},
                    {"click": {"index": at(text, "id=btn-submit", "Submit application")}}]
        if "Apply for this job" in page:
            return {"click": {"index": at(text, "id=apply", "Apply for this job")}}
        return {"wait": {"seconds": 1}}
    return decide


def _solver(monkeypatch, apply_jobs):
    bought = []

    async def solve(challenge, api_key):
        bought.append(challenge)
        return "solver-token"

    monkeypatch.setattr(apply_jobs._captcha, "solve", solve)
    return bought


async def _captcha_run(data_dir, monkeypatch, widget, accepts):
    site, posts = captcha_site(widget, accepts)
    state = {}
    script = lab.Script(captcha_agent(state))
    try:
        apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script,
                                                settings={"captcha_api_key": "made-up-key"})
        bought = _solver(monkeypatch, apply_jobs)
        job = lab.add_job(shared_config, site.url("/job"))
        status = await finish(lab.run_job(apply_jobs, job), site, state)
    finally:
        site.close()
    return status, posts, bought, shared_config.read_jobs()[job["url"]], state


async def test_an_invisible_check_that_passes_by_itself_is_left_alone(data_dir, monkeypatch):
    """The answer the widget makes in this browser is the one the site trusts: none is bought.
    (An answer bought before the first try is what a real site refused.)"""
    status, posts, bought, saved, state = await _captcha_run(
        data_dir, monkeypatch, ANSWERS_ITSELF, lambda token, attempt: token == "widget-token")
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert posts == ["widget-token"] and bought == []


async def test_when_the_site_refuses_the_first_try_the_solvers_answer_goes_with_the_second(data_dir, monkeypatch):
    status, posts, bought, saved, state = await _captcha_run(
        data_dir, monkeypatch, ANSWERS_ITSELF, lambda token, attempt: token == "solver-token")
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert posts == ["widget-token", "solver-token"]
    assert len(bought) == 1 and bought[0].type == "hcaptcha_invisible" and bought[0].sitekey == "made-up-site-key"
    assert bought[0].user_agent                              # the solver is told this browser's user agent


async def test_a_puzzle_that_appears_after_submit_is_solved_and_handed_to_the_page(data_dir, monkeypatch):
    """The page asked for the check and waits: the solver's answer goes to the function the page
    gave hCaptcha, which sends the form."""
    status, posts, bought, saved, state = await _captcha_run(
        data_dir, monkeypatch, SHOWS_A_PUZZLE, lambda token, attempt: token == "solver-token")
    assert status == "applied", (status, saved.get("error"), state.get("pages"))
    assert posts == ["solver-token"]
    assert len(bought) == 1 and bought[0].type == "hcaptcha" and bought[0].open
