"""Reading a posting's title, company and closed state with one plain request."""
import httpx
import pytest

from core import job_page

WORKDAY = "https://umb.wd1.myworkdayjobs.com/Umbrella_Labs_External_Site/job/Reston-VA/Intern_R1"
NORTHWIND = "https://wd3.myworkdaysite.com/recruiting/northwind/jobs/job/New-York-NY/Intern_JR_1"


def page(*tags, title=""):
    return f"<html><head><title>{title}</title>{''.join(tags)}</head><body></body></html>"


OG = '<meta name="title" property="og:title" content="{}">'
LD = '<script type="application/ld+json">{{"@type":"JobPosting","hiringOrganization":{{"@type":"Organization","name":"{}"}}}}</script>'


def test_title_and_company_from_og_and_json_ld():
    p = job_page.parse(NORTHWIND, 200, page(OG.format("Finance Summer Intern"),
                                           LD.format("NWND Cooperative Northwind U.A., New York Branch")))
    assert (p.title, p.company, p.closed) == ("Finance Summer Intern",
                                              "NWND Cooperative Northwind U.A., New York Branch", False)


def test_workday_legal_entity_codes_are_dropped():
    p = job_page.parse("https://msd.wd5.myworkdayjobs.com/SearchJobs/job/X_R1", 200,
                       page(OG.format("Data Science Intern"), LD.format("1000 Wayne Tech &amp; Sons LLC")))
    assert p.company == "Wayne Tech & Sons LLC"


def test_descriptive_workday_site_name_wins():
    p = job_page.parse(WORKDAY, 200, page(OG.format("Software Intern"), LD.format("0909 SP - Orbital Space Systems")))
    assert p.company == "Umbrella Labs"


def test_workday_not_found_page_is_closed():
    """Workday serves its not-found page with an empty og:title."""
    p = job_page.parse(WORKDAY, 200, page('<meta name="title" property="og:title">'))
    assert p.closed and "taken down" in p.reason


@pytest.mark.parametrize("status", [404, 410])
def test_gone_status_is_closed(status):
    assert job_page.parse("https://jobs.example/1", status, "").closed


@pytest.mark.parametrize("status", [403, 429, 500])
def test_blocked_or_broken_is_never_called_closed(status):
    p = job_page.parse("https://jobs.example/1", status, page(title="Page not found"))
    assert not p.closed and p.title == ""


def test_closed_wording_in_the_title():
    p = job_page.parse("https://jobs.example/1", 200, page(title="This job is no longer available"))
    assert p.closed


def test_bot_check_page_teaches_nothing():
    """Soylent's 'Quick Check Needed' page must not become the job title."""
    p = job_page.parse("https://soylentcampus.tal.net/vx/x", 200, page(title="Quick Check Needed"))
    assert (p.title, p.company, p.closed) == ("", "", False)


def test_title_from_page_title_when_no_og():
    p = job_page.parse("https://join.acx.com/job/x", 200, page(title="IT Systems Intern Job Details | ACX"))
    assert p.title == "IT Systems Intern"


def test_generic_portal_name_is_not_a_company():
    p = job_page.parse("https://aclc.fa.oraclecloud.com/x", 200,
                       page(OG.format("MBA Associate"), '<meta property="og:site_name" content="Cyberdyne Candidate Experience page"/>'))
    assert p.company == ""


def test_a_job_number_is_not_a_404():
    p = job_page.parse("https://jobs.example/1", 200, page(OG.format("Analyst Intern R104040")))
    assert not p.closed and p.title == "Analyst Intern R104040"


@pytest.mark.parametrize("title,placeholder", [("Pasted Link", True), ("Manually Added", True), ("", True),
                                               ("Unknown", True), ("Data Intern", False)])
def test_placeholders(title, placeholder):
    assert job_page.is_placeholder(title) is placeholder


@pytest.mark.real_posting_fetch
def test_fetch_reads_the_final_page(monkeypatch):
    def handler(request):
        return httpx.Response(200, html=page(OG.format("Finance Intern")))

    real = httpx.Client
    monkeypatch.setattr(job_page.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert job_page.fetch("https://jobs.example/1").title == "Finance Intern"


@pytest.mark.real_posting_fetch
def test_fetch_never_raises(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("offline")

    real = httpx.Client
    monkeypatch.setattr(job_page.httpx, "Client", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert job_page.fetch("https://jobs.example/1") is None
