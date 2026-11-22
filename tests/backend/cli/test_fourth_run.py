"""What the user's two runs on 1.3.3 of 2026-11-20 showed, fixed. Made-up jobs; no browser, no network.

* A job never sent was saved "Applied": the form's small print ("if you have applied to a specific
  opening ...") was read as a confirmation. A job the site confirmed was saved "Failed": the agent
  reported a problem after the site's "Application submitted!".
* A site's "Submit Profile" / section "Submit" buttons left a job cut off mid-application "may have
  been sent".
* Verification: the per-action time limit cut the 5-minute wait short and the agent waited again and
  again (16 minutes on one job); a code read for an account being made was taken for an existing
  account, and the agent started over; a checkbox in a sign-up form counted as "Create Account".
* Questions asked of the user that follow from the profile: initials, U.S. citizenship, a remote
  employer's city; jobs in other countries got the U.S. answers.
* An invisible reCAPTCHA was bought on sight at the first step; a picture puzzle failed locally twice.
* The posting's own "Apply" was held back as if it were the form's Submit.
* The ChatGPT limit: the meter is in the log, a cut-off job's card says why, and jobs an older version
  failed because of the limit are ready again.
"""
import asyncio
from types import SimpleNamespace

import pytest

import main
from cli import apply_jobs
from core import captcha, default_answers, shared_config, site_access, site_accounts, submit_guard, usage_meter
from core.submit_guard import Control, SubmitGuard
from tests.backend.cli.test_apply_jobs import FakeHistory, run  # noqa: F401 (fixture)
from tests.backend.core.test_usage_meter import headers
from tests.backend.test_main_api import _isolate_state  # noqa: F401 (isolated job list)


# ── a confirmation is the site's own word, both ways ───────────────────────────
@pytest.mark.parametrize("text", [
    "Please be aware that if you have applied to a specific opening the information you provide here is available "
    "to the reviewer",
    "Once you have applied, you can follow your application on the candidate home page.",
    "Whether you've applied before or not, create a profile to continue.",
])
def test_small_print_about_applying_is_not_a_confirmation(text):
    assert not apply_jobs.confirms_submission(text)


@pytest.mark.parametrize("text", ["You have successfully applied for this job.", "You've applied to this position.",
                                  "Thank you for applying!", "Application submitted! Your application was received."])
def test_control_real_confirmations_still_count(text):
    assert apply_jobs.confirms_submission(text)


async def _final_page_confirmed(agent):
    agent._langhire_ref["receipt_text_seen"] = True          # what the done callback saw on the last page


def test_a_job_the_site_confirmed_is_applied_even_when_the_agent_then_reports_a_problem(run):
    """Seen 2026-11-20: 'Application submitted!' on the page, then done(success=False) only to flag one
    answer for the user; the job was saved Failed with no submit mark (a select-all run would apply again)."""
    apply_jobs.Agent.during_run = _final_page_confirmed
    history = FakeHistory(success=False, done_text="NEEDS YOU: check one answer\nThe UK sponsorship answer may be wrong.",
                          step_errors=[None] * 3)
    status, _, saved = run(history=history)
    assert status == "applied" and saved[-1] == ("applied", None)


def test_control_a_problem_report_without_a_confirmation_is_left_for_the_user(run):
    history = FakeHistory(success=False, done_text="NEEDS YOU: work authorization\nThe application was not submitted.",
                          step_errors=[None] * 3)
    status, _, saved = run(history=history)
    assert status == "needs_you" and saved[-1][0] == "needs_you"


def test_control_in_trial_a_confirmation_without_the_final_submit_is_for_the_user_to_check(run):
    apply_jobs.Agent.during_run = _final_page_confirmed
    apply_jobs.set_run_mode("trial")
    try:
        status, _, saved = run(history=FakeHistory(success=False, done_text="NEEDS YOU: check one answer",
                                                   step_errors=[None] * 3))
    finally:
        apply_jobs.set_run_mode("auto")
    assert status == "needs_you" and saved[-1] == ("needs_you", apply_jobs.TRIAL_UNCONFIRMED_NOTE)


def test_a_job_the_limit_cut_off_says_so_on_the_logs_page(run, monkeypatch):
    recorded = []
    monkeypatch.setattr(apply_jobs, "MetricsStore", lambda: SimpleNamespace(record_run=lambda **kw: recorded.append(kw)))
    history = FakeHistory(success=False, steps=3, step_errors=[None, "Not clicked 'Apply': these required questions",
                                                               "Your ChatGPT plan's usage limit is reached."])
    status, _, _ = run(history=history)
    assert status == "limit" and recorded[-1]["error_message"] == apply_jobs.LIMIT_NOTE


# ── a site's section buttons ("Submit Profile", "Submit" next to "Finish Later") ──
@pytest.mark.parametrize("control", [
    Control(text="Submit Profile", tag="input", type="submit", in_form=True, form_fields=6),
    Control(text="Submit Answers", tag="button", in_form=True, form_fields=4),
    Control(text="Submit", tag="input", type="submit", in_form=True, form_fields=4, save_later=True),
])
def test_a_sections_submit_button_is_only_possibly_the_final_one(control):
    """Seen 2026-11-19: a hiring site labels each section's button 'Submit Profile' or 'Submit' (next to
    'Finish Later'); the limit cut the job off after one and it stayed 'may have been sent' for good."""
    decision = SubmitGuard(mode="auto").decide("click", control)
    assert decision.allowed and decision.submit and decision.tentative


@pytest.mark.parametrize("control", [
    Control(text="Submit", tag="input", type="submit", in_form=True, form_fields=4),
    Control(text="Submit Application", tag="button", in_form=True, form_fields=4, save_later=True),
    Control(text="Submit", tag="button", in_form=True, form_fields=4, save_later=True, final_context=True),
    Control(text="Submit Profile", tag="button", in_form=True, form_fields=4, application_review=True),
])
def test_control_a_plain_or_final_submit_stays_a_real_submit(control):
    decision = SubmitGuard(mode="auto").decide("click", control)
    assert decision.submit and not decision.tentative


def test_control_trial_still_never_presses_a_bare_submit_even_next_to_finish_later():
    control = Control(text="Submit", tag="input", type="submit", in_form=True, form_fields=4, save_later=True)
    decision = SubmitGuard(mode="trial", live_steps=True).decide("click", control)
    assert decision.submit and not decision.allowed


def test_the_page_reader_looks_for_a_save_for_later_button():
    assert "save_later" in submit_guard._FORM_CONTEXT_JS
    assert submit_guard.control_from_json('{"text": "Submit", "save_later": true}').save_later is True


# ── verification ───────────────────────────────────────────────────────────────
def test_a_checkbox_in_a_sign_up_form_is_not_create_account():
    """Seen 2026-11-20: two checkboxes and a privacy link clicked inside a sign-up form each counted as
    'Create Account' (the form's own button says so), and the agent was sent to wait for an email."""
    for control in (Control(text="I agree to the terms", tag="input", type="checkbox", in_form=True,
                            submitter_text="Create Account"),
                    Control(text="Privacy policy", tag="a", in_form=True, submitter_text="Create Account")):
        assert not submit_guard.is_account_creation(control, clicked=True)
    assert submit_guard.is_account_creation(Control(text="Create Account", tag="button", in_form=True), clicked=True)
    # Enter in a field of that form presses its button: that one does create the account.
    assert submit_guard.is_account_creation(Control(text="Email", tag="input", type="email", in_form=True,
                                                    submitter_text="Create Account"))


async def test_the_verification_action_has_room_for_reading_the_email_and_the_five_minute_wait(monkeypatch):
    seen = []

    async def act(self, action, browser_session=None, **kwargs):
        seen.append(kwargs.get("action_timeout"))

    monkeypatch.setattr(apply_jobs.Tools, "act", act)
    tools = apply_jobs.ApplicationTools()
    for name in ("get_email_verification", "click"):
        await tools.act(SimpleNamespace(model_dump=lambda exclude_unset=True, n=name: {n: {}}), None)
    verification, other = seen
    assert verification >= apply_jobs.USER_WAIT_SECONDS + apply_jobs.MAIL_READ_LIMIT_SECONDS + 30
    assert other == apply_jobs.USER_WAIT_SECONDS + 90


async def test_after_one_wait_nobody_answered_the_job_is_not_waited_on_again(monkeypatch):
    """Seen 2026-11-20: one job waited three times for a code nobody gave (about 16 minutes)."""
    ref, job = {}, {"url": "https://jobs.example/1", "title": "Analyst", "company": "Example Works"}
    assert await apply_jobs.wait_for_user_reply(ref, job, "verification", "Code?", seconds=0.1) is None
    loop = asyncio.get_running_loop()
    started = loop.time()
    assert await apply_jobs.wait_for_user_reply(ref, job, "help", "Still there?", seconds=30) is None
    assert loop.time() - started < 1


def _verification_tools(monkeypatch, page_url, found, page_asks=True):
    recorded, waits = [], []
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: None)
    monkeypatch.setattr(apply_jobs, "credential_refusal", lambda url: "")
    monkeypatch.setattr(apply_jobs, "read_jobs", lambda: {})
    monkeypatch.setattr(apply_jobs.config, "build_browser_kwargs", lambda **kw: {})
    monkeypatch.setattr(site_accounts, "record", lambda url, email, event, when=None: recorded.append(event) or True)

    async def find(*args, **kwargs):
        return dict(found)

    async def evaluate(session, js, *args):
        return page_asks

    async def wait(agent_ref, job, kind, problem, questions=None, until=None, seconds=None):
        waits.append(until)
        return {"resolved": True} if until is not None and await until() else None

    monkeypatch.setattr(apply_jobs.inbox, "find_verification", find)
    monkeypatch.setattr(apply_jobs.browser_policy, "evaluate", evaluate)
    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", wait)
    ref = {"secrets": {"email": "ada.accounts@example.com"}, "trail": site_access.SiteTrail(page_url)}
    tools = apply_jobs.build_tools({"url": page_url, "title": "Intern", "company": "Example"},
                                   "careers.example", False, ref, 1)
    session = SimpleNamespace(get_current_page_url=lambda: _now(page_url), navigate_to=lambda url: _now(None))
    action = tools.registry.registry.actions["get_email_verification"].function
    return action, session, ref, recorded, waits


async def _now(value):
    return value


async def test_a_code_read_for_an_account_being_made_is_not_taken_for_an_existing_account(monkeypatch):
    """Seen 2026-11-20: the code was read, the app noted 'an account exists here', the next step told the
    agent never to create another one, and it started the sign-up over and gave the job up."""
    page = "https://careers.example/account/verify"
    action, session, ref, recorded, _ = _verification_tools(monkeypatch, page, {"code": "123456"})
    monkeypatch.setattr(site_accounts, "known", lambda url, email: None)
    result = await action(params=apply_jobs.VerificationRequest(kind="code"), browser_session=session)
    assert "123456" in result.extracted_content and recorded == []
    agent = SimpleNamespace(_langhire_ref=ref, browser_session=session)
    note = await apply_jobs.account_memory_note(agent)
    assert "already has an account" not in note and "never create another" not in note
    assert "don't start over" in note.lower()


async def test_control_a_code_for_an_account_the_app_knows_is_noted(monkeypatch):
    page = "https://careers.example/account/verify"
    action, session, _, recorded, _ = _verification_tools(monkeypatch, page, {"code": "123456"})
    monkeypatch.setattr(site_accounts, "known", lambda url, email: {"site": "careers.example", "created": "2026-11-01"})
    await action(params=apply_jobs.VerificationRequest(kind="code"), browser_session=session)
    assert recorded == ["verified"]


async def test_control_the_email_after_create_account_was_pressed_confirms_that_account(monkeypatch):
    """Create Account, then the site's emailed link: the account exists, and the next sign-in page is
    signed in to (test_second_run_live's site), never registered on again."""
    page = "https://careers.example/account/verify"
    action, session, ref, recorded, _ = _verification_tools(monkeypatch, page, {"code": "123456"})
    monkeypatch.setattr(site_accounts, "known", lambda url, email: None)
    ref["registered_at"] = "2026-11-20T22:00:00+00:00"
    await action(params=apply_jobs.VerificationRequest(kind="code"), browser_session=session)
    assert recorded == ["verified"] and "verification_read" not in ref


async def test_the_wait_for_a_verification_email_ends_when_the_page_has_moved_on(monkeypatch):
    """Seen 2026-11-20: after Create Account the site signed the user in by itself, and the agent still
    waited 6.5 minutes for a verification email."""
    page = "https://careers.example/account/created"
    action, session, _, _, waits = _verification_tools(monkeypatch, page, {"error": "No verification email."},
                                                       page_asks=False)
    result = await action(params=apply_jobs.VerificationRequest(kind="link"), browser_session=session)
    assert waits and waits[0] is not None
    assert "moved on" in result.extracted_content


async def test_control_a_page_that_still_asks_for_the_code_keeps_waiting(monkeypatch):
    page = "https://careers.example/account/verify"
    action, session, _, _, _ = _verification_tools(monkeypatch, page, {"error": "No verification email."}, page_asks=True)
    result = await action(params=apply_jobs.VerificationRequest(kind="code"), browser_session=session)
    assert result.error and "NEEDS YOU" in result.error


# ── answers that follow from the profile ───────────────────────────────────────
def test_initials_are_a_declaration_never_a_question():
    """Seen 2026-11-20: 'To confirm that this application is being completed by a human, please enter your
    initials' was put to the user as a human verification (5-minute wait)."""
    certify = next(d[2] for d in default_answers.DEFAULTS if d[0] == "certify")
    assert "initials" in certify and "not a CAPTCHA" in certify
    block = default_answers.prompt_block({}, {"name": "Ada Lovelace Test"})
    assert "INITIALS" in block and "ALT" in block


def test_control_without_a_name_no_initials_line():
    assert "INITIALS" not in default_answers.prompt_block({}, {"name": ""})


@pytest.mark.parametrize("status,question,expected", [
    ("TN Visa", "This position requires a security clearance. Are you a U.S. Citizen? (Yes/No)", "No"),
    ("Green Card (Permanent Resident)", "Are you a US citizen?", "No"),
    ("H1B Visa", "Are you a citizen of the United States?", "No"),
    ("OPT/CPT", "U.S. citizenship status: are you a US citizen?", "No"),
    ("US Citizen", "Are you a U.S. citizen?", "Yes"),
])
def test_us_citizenship_follows_from_the_profile(status, question, expected):
    told = apply_jobs.citizenship_answer(question, {"country": "US", "work_authorization": status})
    assert told is not None and f"Answer {expected}" in told


@pytest.mark.parametrize("profile,question", [
    ({"country": "US", "work_authorization": ""}, "Are you a U.S. citizen?"),                 # nothing to go on
    ({"country": "US", "work_authorization": "H1B Visa"}, "What is your notice period?"),
    ({"country": "US", "work_authorization": "EAD"}, "Are you a Canadian citizen?"),
])
def test_control_citizenship_is_not_guessed_without_a_basis(profile, question):
    assert apply_jobs.citizenship_answer(question, profile) is None


async def test_the_citizenship_question_is_answered_without_a_wait(tmp_path, monkeypatch):
    from memory.store import MemoryStore
    store, waits = MemoryStore(tmp_path / "m.db"), []

    async def no_reply(*args, **kwargs):
        waits.append(args)

    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: store)
    monkeypatch.setattr(apply_jobs, "wait_for_user_reply", no_reply)
    ref = {"profile": {"country": "US", "work_authorization": "OPT/CPT"}}
    tools = apply_jobs.build_tools({"url": "https://jobs.example/1", "title": "Intern", "company": "Example"},
                                   "jobs.example", False, ref, 1)
    ask = tools.registry.registry.actions["ask_for_answers"].function
    result = await ask(params=apply_jobs.UnansweredQuestions(questions=[apply_jobs.UnansweredQuestion(
        question="Are you a U.S. Citizen? (Yes/No)", section="Eligibility")]), browser_session=object())
    assert waits == [] and not result.is_done and "Answer No" in result.extracted_content


def test_jobs_in_another_country_get_answers_for_that_country():
    """Seen 2026-11-20: a UK job's sponsorship question got the answer the profile gives for the U.S."""
    rows = {d[0]: d[2] for d in default_answers.DEFAULTS}
    assert "another country" in rows["work_authorization"] and "another country" in rows["sponsorship"]
    assert "Yes" in rows["sponsorship"].split("another country", 1)[1]


@pytest.mark.parametrize("question", ["City", "State", "Country", "Location (city)"])
def test_a_remote_employers_city_is_where_the_candidate_worked(question):
    """Seen 2026-11-20: City and ZIP of a remote employer were asked of the user (5-minute wait)."""
    told = apply_jobs.address_answer(question, "Professional Experience — Example Works (remote)",
                                     {"address": {"city": "Springfield", "state": "IL", "zip": "62701", "country": "USA"}})
    assert told is not None and "Springfield" in told and "remote" in told


def test_a_remote_employers_zip_is_the_candidates_own():
    told = apply_jobs.address_answer("Zip/Postal Code", "Work history — Example Works (remote)",
                                     {"address": {"city": "Springfield", "zip": "62701"}})
    assert "62701" in told


@pytest.mark.parametrize("question,section", [("City", "Personal information"), ("Favourite city?", "About you")])
def test_control_the_candidates_own_city_and_other_questions_are_left_alone(question, section):
    assert apply_jobs.address_answer(question, section, {"address": {"city": "Springfield"}}) is None


# ── CAPTCHAs ───────────────────────────────────────────────────────────────────
def _captcha_handler(run, monkeypatch, challenge):
    """The application's own CAPTCHA step (deal_with_captcha), taken from a fake run."""
    found, bought = {}, []

    async def grab(agent):
        found["deal"] = agent._langhire_ref["captcha"]

    async def detect(session):
        return challenge

    async def handle(session, settings, include_invisible=False):
        bought.append(include_invisible)
        return True

    apply_jobs.Agent.during_run = grab
    monkeypatch.setattr(apply_jobs.config, "load_runtime_settings",
                        lambda: {"max_failures": 6, "captcha_api_key": "made-up-key"})
    run()
    monkeypatch.setattr(apply_jobs._captcha, "detect", detect)
    monkeypatch.setattr(apply_jobs._captcha, "handle", handle)
    return found["deal"], bought


def test_an_invisible_recaptcha_is_left_to_pass_by_itself_first(run, monkeypatch):
    """Seen 2026-11-20: 2Captcha was paid for an invisible reCAPTCHA at the first step of every
    Greenhouse and Ashby job (one step stalled 62 s), and both of a job's solver tries went on it."""
    challenge = captcha.Challenge(type=captcha.RECAPTCHA_V3, sitekey="k", page_url="https://jobs.example/apply")
    deal, bought = _captcha_handler(run, monkeypatch, challenge)

    async def go():
        session = SimpleNamespace()
        await deal(session, "step")
        await deal(session, "advance")           # the form is sent: the check runs by itself
        assert bought == []
        await deal(session, "advance")           # sent again: the first try didn't get it through
        assert bought == [True]

    asyncio.run(go())


def test_a_picture_puzzle_that_cant_be_sent_is_given_to_the_user_at_once(monkeypatch):
    """Seen 2026-11-20: both solver tries failed in 3 ms with '2Captcha error (ValueError)'."""
    calls = []
    challenge = captcha.Challenge(type=captcha.IMAGE_CAPTCHA, image_b64="https://captcha.example/puzzle.png",
                                  page_url="https://jobs.example/sign-up")

    async def detect(session):
        return challenge

    real = captcha._solve_sync
    monkeypatch.setattr(captcha, "detect", detect)
    monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: calls.append(1) or real(key, ch))
    with pytest.raises(captcha.CaptchaUnsolved) as unsolved:
        asyncio.run(captcha.handle(SimpleNamespace(), {"captcha_api_key": "made-up-key"}))
    assert calls == [1]
    assert "ValueError" not in unsolved.value.reason and "solve it in the browser" in unsolved.value.reason


# ── the posting's own Apply ────────────────────────────────────────────────────
def _node(text, *, in_form):
    form = SimpleNamespace(tag_name="form", parent_node=None)
    body = SimpleNamespace(tag_name="body", parent_node=None)
    return SimpleNamespace(get_all_children_text=lambda: text, attributes={}, tag_name="button",
                           parent_node=SimpleNamespace(tag_name="div", parent_node=form if in_form else body))


@pytest.mark.parametrize("label,in_form,held", [("Apply", False, False), ("Apply", True, True),
                                                ("Submit application", True, True), ("Next", False, True)])
async def test_the_postings_apply_button_is_never_held_back(monkeypatch, label, in_form, held):
    """Seen 2026-11-20: a posting's 'Apply' (it scrolls to the form below) was held back for the form's
    15 unanswered questions: a wasted AI step on every job of that hiring system."""
    clicked = []

    class FakeTools:
        async def _click_by_index(self, params, browser_session):
            clicked.append(params.index)
            return "clicked"

    async def scan(session):
        return {"fields": [{"label": f"Question {n}", "required": True, "value": ""} for n in range(5)]}

    async def node(index):
        return _node(label, in_form=in_form)

    async def no_captcha(ref, session):
        return None

    monkeypatch.setattr(apply_jobs.form_check, "scan", scan)
    monkeypatch.setattr(apply_jobs, "_advance_captcha", no_captcha)
    tools = FakeTools()
    apply_jobs.guard_next_clicks(tools, {}, 1)
    result = await tools._click_by_index(SimpleNamespace(index=7), SimpleNamespace(get_element_by_index=node))
    assert (clicked == []) is held and (result != "clicked") is held


# ── a browser that never starts ────────────────────────────────────────────────
def test_a_browser_that_never_starts_ends_the_attempt(run, monkeypatch):
    """Seen 2026-11-19: one worker hung for 38 minutes right after its browser was set up."""
    async def never(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(run.browser, "start", never)
    monkeypatch.setattr(apply_jobs, "BROWSER_START_SECONDS", 0.05)
    status, _, saved = run()
    assert status == "failed" and "browser didn't start" in (saved[-1][1] or "")


# ── the ChatGPT limit ──────────────────────────────────────────────────────────
def test_the_meter_reading_is_said_in_plain_words():
    assert usage_meter.describe() == ""                              # nothing read yet
    usage_meter.note_reply(headers(primary=72, secondary=16, primary_reset=3 * 3600 + 600))
    line = usage_meter.describe()
    assert "72% of the 5-hour limit" in line and "16% of the weekly limit" in line and "a job takes about" in line


def test_jobs_an_older_version_failed_because_of_the_limit_are_ready_again(data_dir):
    shared_config.write_jobs({
        "https://jobs.example/1": {"status": "failed", "error": "Your ChatGPT plan's usage limit is reached. Try later."},
        "https://jobs.example/2": {"status": "failed", "error": "The salary field only takes digits."},
        "https://jobs.example/3": {"status": "failed", "error": "usage limit is reached",
                                   "submit_attempted_at": "2026-11-18T20:42:00+00:00"},
        "https://jobs.example/4": {"status": "applied", "error": None},
    })
    never = lambda url: False
    assert main._ready_again_after_limit(sent_before=never) == 1
    jobs = shared_config.read_jobs()
    assert jobs["https://jobs.example/1"]["status"] == "pending"
    assert jobs["https://jobs.example/1"]["error"] == apply_jobs.LIMIT_NOTE
    assert jobs["https://jobs.example/2"]["status"] == "failed"                  # a real failure stays
    assert jobs["https://jobs.example/3"]["status"] == "failed"                  # may have been sent: never by itself
    assert main._ready_again_after_limit(sent_before=never) == 0                 # nothing left to do


def test_control_a_limit_failure_whose_log_once_said_it_may_have_been_sent_stays(data_dir):
    """Some had an earlier "outcome unknown", whose mark a retry by hand cleared: ready again by itself,
    such a job could be applied to twice."""
    error = "Your ChatGPT plan's usage limit is reached."
    shared_config.write_jobs({"https://jobs.example/1": {"status": "failed", "error": error},
                              "https://jobs.example/2": {"status": "failed", "error": error}})
    assert main._ready_again_after_limit(sent_before=lambda url: url.endswith("/1")) == 1
    jobs = shared_config.read_jobs()
    assert jobs["https://jobs.example/1"]["status"] == "failed" and jobs["https://jobs.example/2"]["status"] == "pending"


def test_the_run_log_tells_which_jobs_may_have_been_sent(tmp_path):
    from memory.metrics import MetricsStore
    store = MetricsStore(tmp_path / "metrics.db")
    try:
        store.log("run-1", "❓ [W1] Submission outcome unknown (the page stayed blank)", job_url="https://jobs.example/1")
        store.log("run-1", "❌ [W1] Failed: usage limit", job_url="https://jobs.example/2")
        assert store.job_log_says("https://jobs.example/1", "Submission outcome unknown")
        assert not store.job_log_says("https://jobs.example/2", "Submission outcome unknown")
    finally:
        store.close()


def test_an_answer_is_used_for_a_longer_wording_of_the_same_question(tmp_path):
    """Seen 2026-11-20: asked again with a note added after it, the user's answer wasn't found and the run
    waited five minutes (the user's rule: an answer is never asked again)."""
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    saved = "Which of our office locations would you be able to work from in person?"
    store.qa_set_answer(saved, "Springfield office", source="user")
    longer = saved + " Please note we do not provide relocation assistance for this internship."
    assert store.qa_user_answer(longer) == "Springfield office"
    assert store.qa_user_answer("Before you answer: " + saved) == "Springfield office"


@pytest.mark.parametrize("asked", [
    "Which of our office locations would you be able to work from in person during the summer?",   # narrower
    "Do you have a degree in nursing?",
])
def test_control_a_question_that_only_starts_the_same_way_is_not_answered(tmp_path, asked):
    from memory.store import MemoryStore
    store = MemoryStore(tmp_path / "m.db")
    store.qa_set_answer("Which of our office locations would you be able to work from in person?",
                        "Springfield office", source="user")
    store.qa_set_answer("Do you have a degree?", "Yes", source="user")       # too short to be found inside others
    assert store.qa_user_answer(asked) == ""


def test_small_print_is_not_an_already_applied_notice_either():
    assert not apply_jobs.says_already_applied("Please be aware that if you have applied to a specific opening the "
                                               "information you provide here is available to the reviewer")
    assert apply_jobs.says_already_applied("You have already applied to this job.")
    assert apply_jobs.says_already_applied("You applied to this position on Nov 2.")


def test_jobs_waiting_for_the_user_hold_no_room_on_the_plan(monkeypatch):
    monkeypatch.setattr(apply_jobs, "_current", {1: {"url": "https://jobs.example/1"}, 2: {"url": "https://jobs.example/2"},
                                                 3: {"url": "https://jobs.example/3"}})
    monkeypatch.setattr(apply_jobs, "_help_requests", {"https://jobs.example/2": {}})
    assert apply_jobs._jobs_using_the_plan() == 2


def test_no_learning_request_while_the_plan_is_used_up(run, monkeypatch):
    calls = []
    monkeypatch.setattr(apply_jobs, "extract_learnings_via_llm", lambda *a, **k: calls.append(1) or [])
    usage_meter.limit_reached(3600)
    run(history=FakeHistory(success=False, steps=3, step_errors=[None, None, "usage limit is reached"]))
    assert calls == []
    usage_meter.reset()
    usage_meter._state["loaded"] = True
    run(history=FakeHistory(success=False, steps=3, step_errors=[None, None, "form broken"]))
    assert calls == [1]                                    # control: a failure with room on the plan is learned from
