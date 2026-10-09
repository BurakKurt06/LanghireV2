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
    # The link's company id (/recruiting/northwind/) picks the brand out of the legal name.
    assert (p.title, p.company, p.closed) == ("Finance Summer Intern", "Northwind", False)


def test_workday_legal_entity_codes_are_dropped():
    p = job_page.parse("https://msd.wd5.myworkdayjobs.com/SearchJobs/job/X_R1", 200,
                       page(OG.format("Data Science Intern"), LD.format("1000 Wayne Tech &amp; Sons LLC")))
    assert p.company == "Wayne Tech & Sons"


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


def test_candidate_experience_portal_name_gives_the_company():
    p = job_page.parse("https://aclc.fa.oraclecloud.com/x", 200,
                       page(OG.format("MBA Associate"), '<meta property="og:site_name" content="Cyberdyne Candidate Experience page"/>'))
    assert p.company == "Cyberdyne"


def test_generic_portal_name_is_not_a_company():
    p = job_page.parse("https://jobs.example/x", 200,
                       page(OG.format("MBA Associate"), '<meta property="og:site_name" content="Acme Careers"/>'))
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


def test_greenhouse_link_that_lands_on_the_job_board_is_closed():
    asked = "https://job-boards.greenhouse.io/stateaffairs/jobs/4437430009"
    p = job_page.parse("https://job-boards.greenhouse.io/stateaffairs", 200, page(OG.format("State Affairs")), original_url=asked)
    assert p.closed and "job board" in p.reason


def test_greenhouse_company_from_the_page_title():
    p = job_page.parse("https://job-boards.greenhouse.io/initrodeamer/jobs/1", 200,
                       page(OG.format("IT Technician Co-op"), title="Job Application for IT Technician Co-op at Initrode - AMER"))
    assert (p.title, p.company) == ("IT Technician Co-op", "Initrode")


def test_icims_generic_title_falls_back_to_the_link():
    url = "https://careers-hooli.icims.com/jobs/14653/global-real-estate-summer-associate/job"
    p = job_page.parse(url, 200, page(OG.format("About the Firm"), title="Careers"))
    assert (p.title, p.company) == ("Global Real Estate Summer Associate", "")


def test_workday_site_name_beats_a_division_name():
    url = "https://umb.wd1.myworkdayjobs.com/Umbrella_Labs_External_Site/job/United-States-Virginia-Reston/Intern_R1"
    p = job_page.parse(url, 200, page(OG.format("Software Intern"), LD.format("0909 SP - Orbital Space Systems")))
    assert p.company == "Umbrella Labs"
    assert "United-States-Virginia-Reston" in p.locations      # the place in the link is kept


@pytest.mark.parametrize("site,expected", [("W_tx", ""), ("Wernham_Confidential", "Wernham"), ("Vandelay_Careers", "Vandelay")])
def test_workday_site_names(site, expected):
    assert job_page._workday_site_name(f"https://x.wd1.myworkdayjobs.com/{site}/job/A/B_1") == expected


def test_hybrid_workday_job_with_an_office_is_not_remote():
    ld = ('<script type="application/ld+json">{"@type":"JobPosting","title":"Intern","jobLocationType":"TELECOMMUTE",'
          '"jobLocation":{"address":{"addressLocality":"New York, NY"}},"description":"<p>Build things</p>"}</script>')
    p = job_page.parse("https://acme.wd1.myworkdayjobs.com/Acme/job/New-York-NY/Intern_R1", 200, page(OG.format("Intern"), ld))
    assert p.remote is False and p.locations[0] == "New York, NY" and p.description == "Build things"


def test_non_workday_links_carry_no_location():
    p = job_page.parse("https://join.acx.com/job/IT-Systems-Intern/5976-en_US/", 200, page(OG.format("IT Systems Intern")))
    assert p.locations == []


@pytest.mark.parametrize("name,clean", [
    ("NWND Cooperative Northwind U.A., New York Branch", "NWND Cooperative Northwind"),
    ("Greenhill & Co., LLC", "Greenhill"),
    ("WonkaTX, Inc.", "WonkaTX"),
    ("6085-Tyrell Global Services, LLC Legal Entity", "Tyrell Global Services"),
])
def test_clean_company(name, clean):
    assert job_page.clean_company(name) == clean


@pytest.mark.parametrize("title", ["About the Firm", "Careers", "Job Details", "Early Careers Career Site", "Pasted Link"])
def test_generic_titles_count_as_unnamed(title):
    assert job_page.is_placeholder(title)


def test_a_greenhouse_link_sent_to_the_companys_own_site_is_not_closed():
    page = "<html><head><title>Data Intern | Acme</title></head></html>"
    open_elsewhere = job_page.parse("https://careers.acme.com/positions/123?gh_src=abc", 200, page,
                                    original_url="https://boards.greenhouse.io/acme/jobs/123")
    assert not open_elsewhere.closed
    back_to_board = job_page.parse("https://job-boards.greenhouse.io/acme", 200, page,
                                   original_url="https://job-boards.greenhouse.io/acme/jobs/123")
    assert back_to_board.closed
