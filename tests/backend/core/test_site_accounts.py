"""The app remembers which job sites already have an account for the application email.

The user (2026-11-15): "it already has a memory tab so it should know if I created an account there
before". On the first 1.3.0 run the agent guessed on each site: it tried to sign in on a site where
no account existed and gave up, and on another it was unsure whether to register.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from core import site_accounts
from core.site_accounts import account_key, known, record

EMAIL = "ada.accounts@example.com"


@pytest.fixture(autouse=True)
def _own_file(tmp_path, monkeypatch):
    monkeypatch.setattr(site_accounts, "_file", lambda: tmp_path / "site_accounts.json")
    # No run logs to learn from unless a test gives some.
    monkeypatch.setattr(site_accounts, "_backfill_once", lambda email: None)


def test_one_account_per_company_not_per_vendor():
    sf = "https://career4.successfactors.com/careers?company=NORTHWND&career_ns=job_listing"
    assert account_key(sf) == "career4.successfactors.com/northwnd"
    assert account_key("https://career4.successfactors.com/careers?company=CONTOSO") != account_key(sf)
    assert account_key("https://acme.wd108.myworkdayjobs.com/Acme_Careers/job/x") == "acme.wd108.myworkdayjobs.com"
    assert account_key("https://globex.wd5.myworkdayjobs.com/2/job/x") == "globex.wd5.myworkdayjobs.com"


def test_an_account_made_on_one_page_is_known_on_the_sites_other_pages():
    assert record("https://acme.wd1.myworkdayjobs.com/Ext/job/1/apply", EMAIL, "created")
    entry = known("https://acme.wd1.myworkdayjobs.com/Ext/login", EMAIL)
    assert entry and entry["site"] == "acme.wd1.myworkdayjobs.com" and entry["created_at"]
    assert known("https://globex.wd1.myworkdayjobs.com/Ext/login", EMAIL) is None     # another company's Workday


def test_an_account_for_another_email_isnt_this_one():
    record("https://acme.wd1.myworkdayjobs.com/Ext/job/1", "old.address@example.com", "created")
    assert known("https://acme.wd1.myworkdayjobs.com/Ext/job/1", EMAIL) is None
    record("https://acme.wd1.myworkdayjobs.com/Ext/job/1", EMAIL.upper(), "signed_in")       # emails compare in lowercase
    entry = known("https://acme.wd1.myworkdayjobs.com/Ext/job/1", EMAIL)
    assert entry["email"] == EMAIL and "created_at" not in entry                              # the old address's dates are gone


def test_events_add_up_and_read_in_plain_words():
    url = "https://campus.example.net/apply"
    record(url, EMAIL, "created", when="2026-11-14T23:40:00+00:00")
    record(url, EMAIL, "verified", when="2026-11-14T23:50:00+00:00")
    entry = known(url, EMAIL)
    assert site_accounts.describe(entry) == "created Nov 14, 2026, email verified Nov 14, 2026"
    assert [e["site"] for e in site_accounts.entries()] == ["campus.example.net"]


def test_forget_removes_one_site_only():
    record("https://a.example.net/x", EMAIL, "created")
    record("https://b.example.net/x", EMAIL, "created")
    assert site_accounts.forget("a.example.net") and not site_accounts.forget("a.example.net")
    assert [e["site"] for e in site_accounts.entries()] == ["b.example.net"]


def test_nothing_is_recorded_without_an_email_or_a_known_event():
    assert not record("https://a.example.net/x", "", "created")
    assert not record("https://a.example.net/x", EMAIL, "deleted")
    assert site_accounts.entries() == []


def test_accounts_proven_by_earlier_runs_are_learned_once(tmp_path, monkeypatch):
    # Before this memory existed, the run log already showed the verification emails the app used.
    monkeypatch.undo()
    monkeypatch.setattr(site_accounts, "_file", lambda: tmp_path / "accounts.json")
    from memory.metrics import MetricsStore
    store = MetricsStore(tmp_path / "m.db")
    store.log_batch([("r1", None, "INFO", "  📧 [W1] Opened the verification link from the email for campus.example.net",
                      "2026-11-14T23:54:05+00:00"),
                     ("r1", None, "INFO", "  📧 [W1] Verification code read from the email for acme.wd1.myworkdayjobs.com.",
                      "2026-11-13T10:00:00+00:00"),
                     ("r1", None, "INFO", "an unrelated line", "2026-11-13T10:00:00+00:00")])
    monkeypatch.setattr("memory.metrics.MetricsStore", lambda: MetricsStore(tmp_path / "m.db"))
    sites = {e["site"] for e in site_accounts.entries(EMAIL)}
    assert sites == {"campus.example.net", "acme.wd1.myworkdayjobs.com"}
    site_accounts.forget("campus.example.net")
    assert {e["site"] for e in site_accounts.entries(EMAIL)} == {"acme.wd1.myworkdayjobs.com"}    # once, not again


# ── what the agent is told on each step ────────────────────────────────────

class _Trail:
    def __init__(self, hosts):
        self.hosts = hosts

    def led_to(self, url):
        return any(h in url for h in self.hosts)


class _Session:
    def __init__(self, url, password_field):
        self.url, self.password_field = url, password_field

    async def get_current_page_url(self):
        return self.url


class _Agent:
    def __init__(self, url, password_field=True, hosts=("campus.example.net",)):
        self.browser_session = _Session(url, password_field)
        self._langhire_ref = {"trail": _Trail(hosts), "secrets": {"account_email": EMAIL, "password": "Made-Up-1!"}}


@pytest.fixture
def _page(monkeypatch):
    from core import browser_policy

    async def evaluate(session, js, *args):
        return session.password_field
    monkeypatch.setattr(browser_policy, "evaluate", evaluate)


def test_a_known_account_means_sign_in(_page):
    from cli import apply_jobs
    record("https://campus.example.net/register", EMAIL, "created", when="2026-11-14T23:40:00+00:00")
    note = asyncio.run(apply_jobs.account_memory_note(_Agent("https://campus.example.net/login")))
    assert note.startswith("ACCOUNT MEMORY: the app already has an account on this site (campus.example.net")
    assert "SIGN IN" in note and "never create another account" in note


def test_no_known_account_on_a_sign_in_page_means_register(_page):
    from cli import apply_jobs
    note = asyncio.run(apply_jobs.account_memory_note(_Agent("https://campus.example.net/login")))
    assert "has no account on this site" in note and "Create one" in note and "account_result" in note


def test_no_note_off_account_pages_or_off_the_jobs_path(_page):
    from cli import apply_jobs
    assert asyncio.run(apply_jobs.account_memory_note(_Agent("https://campus.example.net/job/1", password_field=False))) == ""
    record("https://elsewhere.example.org/login", EMAIL, "created")
    assert asyncio.run(apply_jobs.account_memory_note(_Agent("https://elsewhere.example.org/login"))) == ""


def test_the_agent_reports_an_account_and_the_app_remembers_it(monkeypatch):
    from cli import apply_jobs
    from tests.backend.cli.test_credentials import JOB

    class Session:
        async def get_current_page_url(self):
            return "https://campus.example.net/account/welcome"
    agent_ref = {}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    agent_ref["secrets"] = {"account_email": EMAIL, "password": "Made-Up-1!"}
    agent_ref["trail"].navigating("https://campus.example.net/account/welcome", linked_hosts={"campus.example.net"})
    tool = tools.registry.registry.actions["account_result"].function
    asyncio.run(tool(params=apply_jobs.AccountResult(result="created"), browser_session=Session()))
    assert known("https://campus.example.net/login", EMAIL)["created_at"]


def test_a_site_the_job_didnt_lead_to_is_never_remembered(monkeypatch):
    from cli import apply_jobs
    from tests.backend.cli.test_credentials import JOB

    class Session:
        async def get_current_page_url(self):
            return "https://unrelated.example.org/welcome"
    agent_ref = {}
    tools = apply_jobs.build_tools(JOB, "", False, agent_ref, 1)
    agent_ref["secrets"] = {"account_email": EMAIL, "password": "Made-Up-1!"}
    # The agent typed this address itself; no page of the job linked to it.
    agent_ref["trail"].navigating("https://unrelated.example.org/welcome", linked_hosts=set())
    tool = tools.registry.registry.actions["account_result"].function
    asyncio.run(tool(params=apply_jobs.AccountResult(result="created"), browser_session=Session()))
    assert site_accounts.entries() == []
