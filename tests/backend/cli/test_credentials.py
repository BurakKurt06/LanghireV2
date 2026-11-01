"""Account credentials: typed only on the employer's https application site, into forms that
post back to it; one generated password per site; verification email read by app code only.

These drive the real typing pipeline (build_tools -> registry.execute_action) and check
what reaches the browser, not prompt wording.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from cli import apply_jobs
from core import inbox, secret_store

JOB = {"url": "https://careers.acme.example/jobs/1", "title": "Intern", "company": "Acme"}
WORKDAY = "https://acme.wd5.myworkdayjobs.com/en-US/careers/job/1/apply"


def approved_workday():
    """Explicit trusted user approval for this test origin/tenant, never name inference."""
    from types import SimpleNamespace
    return SimpleNamespace(credentials_approved=lambda url: apply_jobs.request_gate.origin(url) ==
                           "https://acme.wd5.myworkdayjobs.com")


class Node:
    backend_node_id = 1

    def __init__(self, text=""):
        self.text, self.tag_name, self.attributes = text, "input", {"type": "password"}

    def get_all_children_text(self):
        return self.text


class Session:
    cdp_client = None

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
    async def origin(session, index):
        from urllib.parse import urlparse
        parsed = urlparse(session.url)
        return f"{parsed.scheme}://{parsed.netloc}"
    monkeypatch.setattr(apply_jobs, "field_origin", origin)
    agent_ref = {"secrets": {"email": "ada@example.com", "account_email": "ada.accounts@example.com",
                             "previous_password": "Old-Shared-Pass1"},
                 "secret_masks": {"email": "ada@example.com"}}
    class ExplicitApproval:
        async def approve_credentials(self, url):
            return apply_jobs.request_gate.origin(url) in {
                "https://acme.wd5.myworkdayjobs.com", "https://careers.acme.example"}
        def credentials_approved(self, url):
            return apply_jobs.request_gate.origin(url) == "https://acme.wd5.myworkdayjobs.com"
    agent_ref["request_gate"] = ExplicitApproval()
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)

    async def spy(params, **context):
        typed.append(params.text)
        return apply_jobs.ActionResult(extracted_content="typed")

    tools.registry.registry.actions["input"].function = spy

    def type_into(url, text, form_action=apply_jobs.NO_FORM):
        return asyncio.run(tools.registry.execute_action("input", {"index": 3, "text": text},
                                                         browser_session=Session(url, form_action),
                                                         sensitive_data=agent_ref["secret_masks"]))
    type_into.typed = typed
    type_into.masks = agent_ref["secret_masks"]
    return type_into


async def _dest(session):
    return session.form_action


def test_password_is_typed_on_the_employers_application_site(typing):
    result = typing(WORKDAY, "<secret>password</secret>", form_action=WORKDAY)
    assert result.error is None
    site_pw = secret_store.site_password("https://acme.wd5.myworkdayjobs.com/acme", create=False)
    assert site_pw and typing.typed == [site_pw]
    assert typing.masks["password"] == site_pw  # masked in everything sent to the model


def test_each_employer_gets_its_own_password(data_dir):
    acme = secret_store.site_password("acme.wd5.myworkdayjobs.com")
    globex = secret_store.site_password("globex.wd1.myworkdayjobs.com")
    assert acme != globex and "Old-Shared-Pass1" not in (acme, globex)


# ── one employer per application (Codex's reproductions) ─────────────────────

@pytest.mark.parametrize("placeholder", ["<secret>password</secret>", "<secret>previous_password</secret>",
                                         "<secret>account_email</secret>"])
def test_another_employers_tenant_gets_nothing(typing, placeholder):
    """Acme's job: Globex's Workday tenant is an application system too, but not this employer's."""
    result = typing("https://globex.wd1.myworkdayjobs.com/apply", placeholder)
    assert "only typed on the employer's own application site" in result.error and typing.typed == []


def test_a_form_posting_to_another_tenant_gets_nothing(typing):
    result = typing(WORKDAY, "<secret>password</secret>", form_action="https://globex.wd1.myworkdayjobs.com/collect")
    assert "this form sends to globex.wd1.myworkdayjobs.com" in result.error and typing.typed == []


def test_an_unreadable_form_destination_gets_nothing(typing):
    result = typing(WORKDAY, "<secret>password</secret>", form_action="")
    assert "couldn't read where this field's form sends" in result.error and typing.typed == []


@pytest.mark.parametrize("url", ["https://careers.acme.example:9443/steal", "https://acme.wd5.myworkdayjobs.com:8443/x"])
def test_a_non_standard_port_is_another_site(url):
    assert not apply_jobs.credential_origin_ok(url, JOB["url"], "Acme")


def test_the_first_employer_site_used_is_kept_for_the_run(typing):
    assert typing(WORKDAY, "<secret>account_email</secret>").error is None
    # careers.acme.example is this employer too, but the account is on Workday: a second site is refused.
    result = typing("https://careers.acme.example/login", "<secret>previous_password</secret>")
    assert "already went to acme.wd5.myworkdayjobs.com" in result.error
    assert typing.typed == ["ada.accounts@example.com"]


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
    assert bool(apply_jobs.employer_site(page, job, company)) is ok


@pytest.mark.parametrize("url", [
    "https://evil.example.net/login",                  # another site entirely
    "http://acme.wd5.myworkdayjobs.com/login",         # not https
    "https://www.linkedin.com/login",                  # a job board, never the employer's account system
])
def test_credentials_are_refused_elsewhere(typing, url):
    for placeholder in ("<secret>password</secret>", "<secret>previous_password</secret>",
                        "<secret>account_email</secret>", "password"):
        result = typing(url, placeholder)
        assert "only typed on the employer's own application site" in result.error
    assert typing.typed == []


def test_credentials_are_refused_when_the_form_posts_elsewhere(typing):
    result = typing(WORKDAY, "<secret>password</secret>", form_action="https://collector.evil.example/steal")
    assert "this form sends to collector.evil.example" in result.error and typing.typed == []


def test_the_form_email_can_be_typed_anywhere(typing):
    assert typing("https://forms.example.org/apply", "<secret>email</secret>").error is None
    assert typing.typed == ["ada@example.com"]


def test_the_legacy_password_only_where_credentials_are_allowed(typing):
    assert typing("https://careers.acme.example/login", "<secret>previous_password</secret>").error is None
    assert typing.typed == ["Old-Shared-Pass1"]


def test_site_passwords_are_stored_encrypted(data_dir):
    pw = secret_store.site_password("acme.example")
    raw = (data_dir / "site_credentials.json").read_text()
    assert pw not in raw and "dpapi:v1:" in raw
    assert secret_store.site_password("acme.example") == pw  # stable once made


# ── verification emails ──────────────────────────────────────────────────────

def test_verification_senders_are_the_site_and_its_application_system():
    senders = apply_jobs.verification_senders(WORKDAY, JOB["url"])
    assert {"acme.example", "myworkday.com", "workday.com"} <= senders
    assert "google.com" not in senders and "linkedin.com" not in senders
    assert apply_jobs.verification_senders("https://www.linkedin.com/jobs/1", "https://www.linkedin.com/jobs/1") == set()


@pytest.mark.parametrize("page", ["https://accounts.unrelated.example.org/verify", "https://unrelated.example.net/phish",
                                  "https://globex.wd1.myworkdayjobs.com/verify"])
def test_an_unrelated_page_cant_have_an_email_read(page, monkeypatch):
    """Codex's case: the page asking must be this job's employer site, or no email is read at all."""
    assert apply_jobs.verification_senders(page, JOB["url"], "Acme") == set()
    calls = []

    async def find(*a, **k):
        calls.append(a)
        return {"code": "482913"}

    monkeypatch.setattr(inbox, "find_verification", find)
    tools = apply_jobs.build_tools(JOB, "", False, {}, 1)
    tool = tools.registry.registry.actions["get_email_verification"].function
    result = asyncio.run(tool(params=apply_jobs.VerificationRequest(kind="code"), browser_session=Session(page)))
    assert result.error and calls == []


def test_the_verification_tool_reads_only_through_the_app(monkeypatch):
    calls = []

    async def find(kind, senders, since, kwargs, **binding):
        calls.append((kind, senders, kwargs))
        calls.append(binding)
        return {"code": "482913"}

    monkeypatch.setattr(apply_jobs.inbox, "find_verification", find)
    agent_ref = {"started_at": datetime.now(timezone.utc), "secrets": {"account_email": "ada.accounts@example.com"},
                 "request_gate": approved_workday()}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    tool = tools.registry.registry.actions["get_email_verification"].function
    result = asyncio.run(tool(params=apply_jobs.VerificationRequest(kind="code"), browser_session=Session(WORKDAY)))
    assert "482913" in result.extracted_content
    kind, senders, kwargs = calls[0]
    assert kind == "code" and "myworkday.com" in senders
    assert kwargs["user_data_dir"].endswith("_mail")  # its own browser, never the agent's
    from core import net_guard
    assert kwargs["args"] == net_guard.browser_args(block_mail=False, owner="mail:1")
    assert any(a.startswith("--proxy-server=socks5://127.0.0.1:") for a in kwargs["args"])
    binding = calls[1]   # tied to this application: the employer and the account's address
    assert "Acme" in binding["employer_names"] and binding["recipient"] == "ada.accounts@example.com"
    assert "acme.example" in binding["employer_domains"] and "myworkday.com" not in binding["employer_domains"]


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
        employer_domains={"acme.example"}, employer_names=["Acme"], recipient="ada.accounts@example.com"))
    assert found == {"code": "482913"}
    found = asyncio.run(inbox.find_verification(
        "code", {"myworkday.com"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_domains={"initech.example"}, employer_names=["Initech"], recipient="ada.accounts@example.com"))
    assert "code" not in found


def test_an_email_to_someone_else_is_not_used(monkeypatch):
    import browser_use
    monkeypatch.setattr(browser_use, "BrowserSession", _FakeGmail)
    found = asyncio.run(inbox.find_verification(
        "code", {"myworkday.com"}, datetime.now(timezone.utc), {}, wait_seconds=0,
        employer_domains=set(), employer_names=["Acme"], recipient="someone.else@example.com"))
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
