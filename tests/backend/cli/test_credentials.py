"""Account details: the job-site password from Settings, wherever the job link leads.

The user's decision (2026-11) replaced "only on the employer's own origin and tenant, with a
password the app makes per site": a posting on the employer's pages whose account lives on
SuccessFactors or Workday is signed in to (or the account created) with the email and the one
password saved in Settings. What still never gets them: email sites, job boards, pages that
aren't https, local addresses, a form that posts to one of those, and a site the agent opened
by an address no page of the application linked to.

These drive the real typing pipeline (build_tools -> registry.execute_action) and check
what reaches the browser, not prompt wording.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from cli import apply_jobs
from core import inbox, secret_store, site_access

JOB = {"url": "https://careers.acme.example/jobs/1", "title": "Intern", "company": "Acme"}
WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1/apply"
SUCCESSFACTORS = "https://career4.successfactors.com/career?company=acme"
PASSWORD = "Made-Up-Pass-77"


class Node:
    backend_node_id = 1

    def __init__(self, text=""):
        self.text, self.tag_name, self.attributes = text, "input", {"type": "password"}

    def get_all_children_text(self):
        return self.text


class Session:
    cdp_client = None
    frame_url = ""

    agent_focus_target_id = "tab-1"

    def __init__(self, url, form_action=""):
        self.url, self.form_action = url, form_action
        # browser-use matches domain-scoped secrets against the focused tab's own URL.
        from types import SimpleNamespace
        self.session_manager = SimpleNamespace(get_target=lambda target_id: SimpleNamespace(url=url))

    async def get_current_page_url(self):
        return self.url

    async def get_element_by_index(self, index):
        return Node()

    async def get_current_page(self):
        return None


@pytest.fixture
def typing(monkeypatch, data_dir):
    """The agent's tools with a spy in place of the real `input` action: what would be typed."""
    typed = []
    monkeypatch.setattr(apply_jobs, "form_destination", lambda session, index: _dest(session))
    monkeypatch.setattr(apply_jobs, "_is_private",
                        lambda host: host.startswith(("10.", "192.168.")) or host == "nas.home.arpa")

    async def origin(session, index):
        from urllib.parse import urlparse
        parsed = urlparse(session.frame_url or session.url)
        return f"{parsed.scheme}://{parsed.netloc}"

    monkeypatch.setattr(apply_jobs, "field_origin", origin)
    agent_ref = {"secrets": {"email": "ada@example.com", "account_email": "ada.accounts@example.com",
                             "password": PASSWORD},
                 "secret_masks": {"email": "ada@example.com", "password": PASSWORD}}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)

    async def spy(params, **context):
        typed.append(params.text)
        return apply_jobs.ActionResult(extracted_content="typed")

    tools.registry.registry.actions["input"].function = spy

    def type_into(url, text, form_action=apply_jobs.NO_FORM, frame_url=""):
        session = Session(url, form_action)
        session.frame_url = frame_url
        return asyncio.run(tools.registry.execute_action("input", {"index": 3, "text": text},
                                                         browser_session=session,
                                                         sensitive_data=agent_ref["secret_masks"]))
    type_into.typed = typed
    type_into.masks = agent_ref["secret_masks"]
    type_into.trail = agent_ref["trail"]
    return type_into


async def _dest(session):
    return session.form_action


# ── wherever the job leads ───────────────────────────────────────────────────

@pytest.mark.parametrize("page", [JOB["url"], WORKDAY, SUCCESSFACTORS, "https://acme.icims.com/jobs/1/login",
                                  "https://login.acme-sso.example/authorize"])
def test_the_settings_password_is_typed_on_every_hiring_site_the_job_leads_to(typing, page):
    """A real failure: the posting's Apply led to career4.successfactors.com, and "credential entry
    is blocked for this form destination". The site the job leads to gets the account."""
    assert typing(page, "<secret>account_email</secret>", form_action=page).error is None
    assert typing(page, "<secret>password</secret>", form_action=page).error is None
    assert typing.typed == ["ada.accounts@example.com", PASSWORD]


def test_no_password_is_made_up_for_a_site(typing, data_dir):
    """One password for every account: the one the user saved. Nothing is generated per site."""
    typing(WORKDAY, "<secret>password</secret>")
    assert typing.typed == [PASSWORD]
    assert secret_store.site_password_entries() == []


@pytest.mark.parametrize("url, why", [
    ("http://acme.wd5.myworkdayjobs.com/login", "isn't a secure (https) page"),
    ("https://www.linkedin.com/login", "job board"),
    ("https://mail.google.com/mail/u/0/", "email site"),
    ("https://accounts.google.com/signin", "email site"),
    ("https://localhost/login", "local address"),
    ("https://intranet/login", "local address"),
    ("https://nas.home.arpa/login", "local or private address"),
    ("https://careers.acme.example:9443/steal", "unusual port"),
])
def test_account_details_are_refused_where_they_never_belong(typing, url, why):
    for placeholder in ("<secret>password</secret>", "<secret>account_email</secret>", "password"):
        result = typing(url, placeholder)
        assert "Not typed" in result.error and why in result.error
    assert typing.typed == []


@pytest.mark.parametrize("destination, why", [
    ("https://mail.google.com/collect", "email site"),
    ("http://collector.example.net/steal", "isn't a secure (https) page"),
    ("https://www.indeed.com/collect", "job board"),
    ("https://10.0.0.5/collect", "local or private address"),
])
def test_a_form_that_posts_somewhere_forbidden_gets_nothing(typing, destination, why):
    result = typing(WORKDAY, "<secret>password</secret>", form_action=destination)
    assert "this form sends to" in result.error and why in result.error and typing.typed == []


def test_an_unreadable_form_destination_gets_nothing(typing):
    result = typing(WORKDAY, "<secret>password</secret>", form_action="")
    assert "couldn't read where this field's form sends" in result.error and typing.typed == []


def test_a_field_in_a_frame_that_isnt_a_secure_public_page_gets_nothing(typing):
    result = typing(WORKDAY, "<secret>password</secret>", frame_url="http://frames.example.net/login")
    assert "frame the app can't check" in result.error and typing.typed == []


def test_a_site_the_agent_opened_by_itself_gets_nothing(typing):
    """The job link didn't lead there: no page of this application linked to it."""
    typing.trail.navigating("https://unrelated.example.net/login", linked_hosts=set())
    result = typing("https://unrelated.example.net/login", "<secret>password</secret>")
    assert "this job's link didn't lead here" in result.error and typing.typed == []
    # ... nor a page reached from there
    result = typing("https://next.unrelated-two.example.org/login", "<secret>account_email</secret>")
    assert "this job's link didn't lead here" in result.error and typing.typed == []


def test_an_address_a_page_of_the_application_linked_to_counts(typing):
    typing.trail.navigating("https://apply.acme-careers.example/login", linked_hosts={"apply.acme-careers.example"})
    assert typing("https://apply.acme-careers.example/login", "<secret>password</secret>").error is None


def test_account_details_go_to_a_handful_of_sites_at_most(typing):
    for n in range(site_access.MAX_CREDENTIAL_SITES):
        assert typing(f"https://login.site{n}.example/", "<secret>account_email</secret>").error is None
    result = typing("https://login.one-too-many.example/", "<secret>account_email</secret>")
    assert "already went to" in result.error
    assert len(typing.typed) == site_access.MAX_CREDENTIAL_SITES


@pytest.mark.parametrize("page, job, company, ok", [
    (WORKDAY, JOB["url"], "Acme", True),                                            # tenant names the employer
    ("https://globex.wd1.myworkdayjobs.com/x", JOB["url"], "Acme", False),          # another employer's tenant
    ("https://boards.greenhouse.io/acme/jobs/1", "https://www.linkedin.com/jobs/view/1", "Acme Inc", True),
    ("https://boards.greenhouse.io/globex/jobs/1", "https://www.linkedin.com/jobs/view/1", "Acme Inc", False),
    ("https://apply.acme.example/login", JOB["url"], "", True),                      # the job's own domain
    ("https://careers.acme.com/login", "https://www.linkedin.com/jobs/view/1", "Acme", False),
    ("https://careers.globex.com/login", "https://www.linkedin.com/jobs/view/1", "Acme", False),
    ("https://acme.wd5.myworkdayjobs.com/x", "https://globex.wd1.myworkdayjobs.com/job/1", "Globex", False),
    ("https://www.linkedin.com/login", "https://www.linkedin.com/jobs/view/1", "Acme", False),
])
def test_what_counts_as_this_employers_site(page, job, company, ok):
    """Still what the request gate's protocol tests mean by "the employer's own site" (it no longer
    decides where account details are typed)."""
    assert bool(apply_jobs.employer_site(page, job, company)) is ok


def test_the_form_email_can_be_typed_anywhere(typing):
    assert typing("https://forms.example.org/apply", "<secret>email</secret>").error is None
    assert typing.typed == ["ada@example.com"]


# ── a site whose rules reject the password ───────────────────────────────────

def test_a_rejected_password_gets_characters_added_to_its_end_and_is_saved_for_that_site(typing, data_dir):
    rules = site_access.PasswordRules(min_length=20, symbol=True)
    fitted = site_access.fit_password(PASSWORD, rules)
    assert fitted.startswith(PASSWORD) and len(fitted) >= 20 and fitted != PASSWORD
    secret_store.save_site_password(site_access.site_key(WORKDAY), fitted)
    typing(WORKDAY, "<secret>password</secret>")
    typing(SUCCESSFACTORS, "<secret>password</secret>")
    assert typing.typed == [fitted, PASSWORD]              # only that site gets the changed version
    assert fitted in typing.masks.values()                 # masked in everything sent to the model
    assert [(e["site"], e["kind"]) for e in secret_store.site_password_entries()] == [
        ("acme.wd5.myworkdayjobs.com", "changed")]         # listed in Settings, never with the password
    assert fitted not in (data_dir / "site_credentials.json").read_text()


@pytest.mark.parametrize("rules, check", [
    (dict(uppercase=True), lambda p: any(c.isupper() for c in p)),
    (dict(digit=True), lambda p: any(c.isdigit() for c in p)),
    (dict(symbol=True), lambda p: any(not c.isalnum() for c in p)),
    (dict(min_length=16), lambda p: len(p) >= 16),
    (dict(), lambda p: len(p) > len("lowercaseonly")),      # rejected without a rule the page named
])
def test_fitting_only_ever_adds_to_the_end(rules, check):
    fitted = site_access.fit_password("lowercaseonly", site_access.PasswordRules(**rules))
    assert fitted.startswith("lowercaseonly") and check(fitted)
    again = site_access.fit_password("lowercaseonly", site_access.PasswordRules(**rules), previous=fitted)
    assert again.startswith(fitted) and again != fitted     # a second rejection adds more, never replaces


@pytest.mark.parametrize("base, rules", [
    ("has space in it", dict(forbidden=" ")),               # the password itself breaks the site's rules
    ("a-very-long-password-indeed", dict(max_length=12)),
    ("", dict()),
])
def test_a_password_that_adding_characters_cant_fix_is_for_the_user(base, rules):
    with pytest.raises(site_access.PasswordCannotFit):
        site_access.fit_password(base, site_access.PasswordRules(**rules))


def test_the_password_rejected_tool_saves_this_sites_version(data_dir):
    agent_ref = {"secrets": {"account_email": "ada.accounts@example.com", "password": PASSWORD}, "secret_masks": {}}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    tool = tools.registry.registry.actions["password_rejected"].function
    params = apply_jobs.PasswordRejection(message="Password must be at least 18 characters and include a special character")
    result = asyncio.run(tool(params=params, browser_session=Session(WORKDAY)))
    assert result.error is None and "<secret>password</secret>" in result.extracted_content
    saved = secret_store.site_password("acme.wd5.myworkdayjobs.com", create=False)
    assert saved.startswith(PASSWORD) and len(saved) >= 18
    assert PASSWORD not in result.extracted_content and saved in agent_ref["secret_masks"].values()


def test_site_passwords_are_stored_encrypted(data_dir):
    secret_store.save_site_password("acme.example", "Made-Up-Pass-77Aa1!")
    raw = (data_dir / "site_credentials.json").read_text()
    assert "Made-Up-Pass-77" not in raw and "dpapi:v1:" in raw
    assert secret_store.site_password("acme.example", create=False) == "Made-Up-Pass-77Aa1!"


# ── verification emails ──────────────────────────────────────────────────────

def test_verification_senders_are_the_site_and_its_application_system():
    senders = apply_jobs.verification_senders(WORKDAY, JOB["url"])
    assert {"acme.example", "myworkday.com", "workday.com"} <= senders
    assert "google.com" not in senders and "linkedin.com" not in senders


@pytest.mark.parametrize("page", ["https://mail.google.com/mail/u/0/", "http://careers.acme.example/verify",
                                  "https://www.linkedin.com/checkpoint", "https://localhost/verify"])
def test_no_email_is_read_for_a_page_that_never_gets_account_details(page, monkeypatch):
    calls = []

    async def find(*a, **k):
        calls.append(a)
        return {"code": "482913"}

    monkeypatch.setattr(inbox, "find_verification", find)
    tools = apply_jobs.build_tools(JOB, "", False, {}, 1)
    tool = tools.registry.registry.actions["get_email_verification"].function
    result = asyncio.run(tool(params=apply_jobs.VerificationRequest(kind="code"), browser_session=Session(page)))
    assert result.error and calls == []


def test_no_email_is_read_for_a_site_the_agent_opened_by_itself(monkeypatch):
    calls = []

    async def find(*a, **k):
        calls.append(a)
        return {"code": "482913"}

    monkeypatch.setattr(inbox, "find_verification", find)
    agent_ref = {}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    agent_ref["trail"].navigating("https://unrelated.example.net/phish", linked_hosts=set())
    tool = tools.registry.registry.actions["get_email_verification"].function
    result = asyncio.run(tool(params=apply_jobs.VerificationRequest(kind="code"),
                              browser_session=Session("https://unrelated.example.net/phish")))
    assert result.error and calls == []


@pytest.mark.parametrize("page", [WORKDAY, SUCCESSFACTORS,
                                  "https://ezzx.fa.us2.oraclecloud.com/hcmUI/CandidateExperience/en/sites/CX/job/1"])
def test_the_verification_tool_reads_the_code_for_any_site_the_job_led_to(monkeypatch, data_dir, page):
    """A real failure: a code was emailed, and the app refused to fetch it because the page wasn't
    "the recognized employer origin and tenant". The app reads it, through its own reader."""
    calls = []

    async def find(kind, senders, since, kwargs, **binding):
        calls.append((kind, senders, kwargs))
        calls.append(binding)
        return {"code": "482913"}

    monkeypatch.setattr(apply_jobs.inbox, "find_verification", find)
    agent_ref = {"started_at": datetime.now(timezone.utc), "secrets": {"account_email": "ada.accounts@example.com"}}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    tool = tools.registry.registry.actions["get_email_verification"].function
    result = asyncio.run(tool(params=apply_jobs.VerificationRequest(kind="code"), browser_session=Session(page)))
    assert "482913" in result.extracted_content
    kind, senders, kwargs = calls[0]
    assert kind == "code" and "acme.example" in senders
    assert kwargs["user_data_dir"].endswith("_mail")  # its own browser, never the agent's
    from core import net_guard
    assert kwargs["args"] == net_guard.direct_args(block_mail=False)
    assert not any(a.startswith("--host-resolver-rules") for a in kwargs["args"])   # nothing Chrome warns about
    binding = calls[1]   # tied to this application: the employer and the account's address
    assert "Acme" in binding["employer_names"] and binding["recipient"] == "ada.accounts@example.com"


class _FakeGmail:
    """A Gmail search result with two Workday emails: one for Globex, one for Acme."""
    rows = [{"id": "g1", "sender_email": "no-reply@myworkday.com", "subject": "Globex: your code"},
            {"id": "a1", "sender_email": "no-reply@myworkday.com", "subject": "Verify your account"}]
    messages = {"g1": {"subject": "Globex: your code", "text": "Your Globex verification code is 111111", "links": [],
                       "to": ["ada.accounts@example.com"], "ready": True},
                "a1": {"subject": "Verify your account", "text": "Acme Careers. Your verification code is 482913",
                       "links": [], "to": ["ada.accounts@example.com"], "ready": True}}

    def __init__(self, **kwargs):
        self.url = ""

    async def start(self):
        pass

    async def kill(self):
        pass

    async def navigate_to(self, url):
        self.url = url

    async def get_current_page(self):
        browser = self

        class Page:
            async def evaluate(self, js, *args):
                import json
                if "#all/" in browser.url:
                    return json.dumps(_FakeGmail.messages[browser.url.rsplit("/", 1)[-1]])
                return json.dumps({"rows": _FakeGmail.rows, "ready": True})
        return Page()


def test_a_shared_senders_email_must_name_this_employer(monkeypatch):
    """myworkday.com sends for every Workday employer: Globex's code is never read for Acme's application."""
    import browser_use
    monkeypatch.setattr(browser_use, "BrowserSession", _FakeGmail)
    found = asyncio.run(inbox.find_verification(
        "code", {"myworkday.com"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_names=["Acme"], recipient="ada.accounts@example.com"))
    assert found == {"code": "482913"}
    found = asyncio.run(inbox.find_verification(
        "code", {"myworkday.com"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_names=["Initech"], recipient="ada.accounts@example.com"))
    assert "code" not in found


def test_an_email_from_an_address_nobody_listed_counts_when_it_names_the_employer(monkeypatch):
    """Sites send from addresses nobody can list in advance (a mail service, another brand)."""
    import browser_use
    monkeypatch.setattr(browser_use, "BrowserSession", _FakeGmail)
    found = asyncio.run(inbox.find_verification(
        "code", {"acme.example"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_names=["Acme"], recipient="ada.accounts@example.com"))
    assert found == {"code": "482913"}
    found = asyncio.run(inbox.find_verification(
        "code", {"initech.example"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_names=["Initech"], recipient="ada.accounts@example.com"))
    assert "code" not in found          # nothing ties either email to Initech


def test_an_email_to_someone_else_is_not_used(monkeypatch):
    import browser_use
    monkeypatch.setattr(browser_use, "BrowserSession", _FakeGmail)
    found = asyncio.run(inbox.find_verification(
        "code", {"myworkday.com"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_names=["Acme"], recipient="someone.else@example.com"))
    assert "code" not in found


def test_only_emails_from_allowed_senders_are_opened():
    allowed = {"myworkday.com"}
    assert inbox._domain_ok("mail.myworkday.com", allowed) and not inbox._domain_ok("myworkday.com.evil.net", allowed)
    link = inbox.extract_verification("link", "", ["https://evil.example/verify", "http://myworkday.com/verify",
                                                    "https://acme.myworkday.com/verify?x=1"], allowed)
    assert link == "https://acme.myworkday.com/verify?x=1"


def test_blocked_domains_match_the_host_not_the_text():
    from cli import apply_jobs
    blocked = ["Spam.example", " www.junk.test "]
    assert apply_jobs.is_blocked("https://jobs.spam.example/1", blocked)
    assert apply_jobs.is_blocked("https://SPAM.EXAMPLE/1", blocked)
    assert apply_jobs.is_blocked("https://junk.test/a", blocked)
    assert not apply_jobs.is_blocked("https://notspam.example/1", blocked)
    assert not apply_jobs.is_blocked("https://good.example/apply?ref=spam.example", blocked)
    assert not apply_jobs.is_blocked("https://good.example/", ["", "  "])


def test_applicant_systems_are_recognised_by_host_only():
    from core import job_page
    assert job_page.tenant("https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1") == "acme"
    assert job_page.tenant("https://boards.greenhouse.io/acme/jobs/1") == "acme"
    assert job_page.is_workday("https://acme.wd1.myworkdayjobs.com/x")
    assert not job_page.is_workday("https://myworkdayjobs.com.evil.example/x")
    assert job_page.tenant("https://greenhouse.io.evil.example/acme/jobs/1") == ""
    assert job_page.tenant("https://apply.workable.com/acme/j/REQ1/") == "acme"
    assert job_page.tenant("https://apply.workable.com.evil.example/acme/j/REQ1/") == ""
