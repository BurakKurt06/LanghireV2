"""Reading a posting's title, company and closed state with one plain request."""
import json

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
    monkeypatch.setattr(job_page.httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))
    assert job_page.fetch("https://jobs.example/1").title == "Finance Intern"


@pytest.mark.real_posting_fetch
def test_fetch_never_raises(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("offline")

    real = httpx.Client
    monkeypatch.setattr(job_page.httpx, "Client", lambda **kw: real(**{**kw, "transport": httpx.MockTransport(handler)}))
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


@pytest.mark.real_posting_fetch
@pytest.mark.parametrize("url", ["http://127.0.0.1:8000/admin", "http://192.168.1.1/", "http://localhost/x"])
def test_fetch_never_reads_this_computer_or_the_home_network(url):
    assert job_page.fetch(url) is None


@pytest.mark.real_posting_fetch
def test_a_redirect_to_the_home_network_is_refused(monkeypatch):
    import httpx
    request = httpx.Request("GET", "http://192.168.1.1/")
    with pytest.raises(httpx.InvalidURL):
        job_page._public_only(request)
    import socket
    addresses = {"jobs.example.com": "93.184.216.34", "router.example.com": "192.168.1.1",
                 "127.0.0.1.nip.io": "127.0.0.1"}
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port: [(2, 1, 6, "", (addresses[host], 0))])
    job_page._public_only(httpx.Request("GET", "https://jobs.example.com/1"))  # a public address passes
    for host in ("router.example.com", "127.0.0.1.nip.io"):  # public-looking names for private addresses
        with pytest.raises(httpx.InvalidURL):
            job_page._public_only(httpx.Request("GET", f"http://{host}/"))


@pytest.mark.parametrize("url, tenant", [
    ("https://wd3.myworkdaysite.com/recruiting/acme/External/job/1", "acme"),
    ("https://wd3.myworkdaysite.com/en-US/recruiting/acme/External/job/1", "acme"),
    ("https://wd3.myworkdaysite.com/en-GB/recruiting/globex/Careers/job/1", "globex"),
    ("https://wd3.myworkdaysite.com/fr-FR/careers/job/1", ""),  # no /recruiting/<tenant>: no guess
])
def test_workday_hosted_sites_have_a_tenant_in_any_locale(url, tenant):
    assert job_page.tenant(url) == tenant


@pytest.mark.real_posting_fetch
def test_the_connection_goes_to_the_address_that_was_checked(monkeypatch):
    """DNS rebinding: a public answer for the check, then a private one for the connection."""
    import socket
    answers = iter(["93.184.216.34", "192.168.1.1", "192.168.1.1", "192.168.1.1"])
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", (next(answers), 0))])
    connected = []
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request",
                        lambda self, request: connected.append(request) or httpx.Response(200, html="<title>x</title>"))
    assert job_page.fetch("https://jobs.example.com/1") is None   # the second answer is refused
    assert connected == []


@pytest.mark.real_posting_fetch
def test_a_public_page_is_fetched_from_its_checked_address_with_its_name(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    connected = []

    def handle(self, request):
        # What the connection used, as it was made (the request gets its name back afterwards).
        connected.append((request.url.host, request.extensions.get("sni_hostname"), request.headers["Host"]))
        return httpx.Response(200, html=page(OG.format("Finance Intern")), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    assert job_page.fetch("https://jobs.example.com/1").title == "Finance Intern"
    host, sni, host_header = connected[0]
    assert host == "93.184.216.34"
    assert sni == "jobs.example.com"   # the certificate is checked for the name
    assert host_header == "jobs.example.com"


@pytest.mark.real_posting_fetch
def test_the_page_is_read_under_its_name_and_a_relative_redirect_is_pinned_again(monkeypatch):
    """The fetched page was parsed as https://93.184.216.34/...: site rules (LinkedIn, Workday,
    Greenhouse) never matched, and a relative redirect went to the bare address and failed."""
    import socket
    lookups = []
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, *a, **k: lookups.append(host) or [(2, 1, 6, "", ("93.184.216.34", 0))])
    connected = []
    linkedin = page('<meta property="og:title" content="Acme Test Co hiring Data Analyst in Boston, MA | LinkedIn">')

    def handle(self, request):
        connected.append((request.url.host, request.url.path, request.extensions.get("sni_hostname")))
        if request.url.path == "/jobs/view/1/":
            return httpx.Response(301, headers={"Location": "/jobs/view/data-analyst-at-acme-1"}, request=request)
        return httpx.Response(200, html=linkedin, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)
    posting = job_page.fetch("https://www.linkedin.com/jobs/view/1/")
    assert (posting.title, posting.company, posting.locations) == ("Data Analyst", "Acme Test Co", ["Boston, MA"])
    assert connected == [("93.184.216.34", "/jobs/view/1/", "www.linkedin.com"),
                         ("93.184.216.34", "/jobs/view/data-analyst-at-acme-1", "www.linkedin.com")]
    assert set(lookups) == {"www.linkedin.com"} and len(lookups) >= 2   # the redirect is checked by name again


LINKEDIN_PAGE = """<html><head>
<meta property="og:title" content="Acme Test Co hiring Data Analyst in Austin, TX | LinkedIn">
<meta property="og:description" content="Apply for Data Analyst at Acme Test Co in Austin, TX.">
</head><body>
<div class="salary compensation__salary">
  $25.00/hr - $30.00/hr
</div>
<ul class="description__job-criteria-list">
<li><h3 class="description__job-criteria-subheader">Seniority level</h3>
<span class="description__job-criteria-text description__job-criteria-text--criteria">Entry level</span></li>
<li><h3 class="description__job-criteria-subheader">Employment type</h3>
<span class="description__job-criteria-text description__job-criteria-text--criteria">Part-time</span></li>
</ul>
<div class="show-more-less-html__markup relative overflow-hidden"><strong>About the job</strong><br>
<div><p>Work with our data team 20 hours per week.</p></div><ul><li>SQL</li></ul></div>
<div class="footer">Other</div>
</body></html>"""


def test_linkedins_public_page_gives_pay_job_type_and_the_whole_description():
    posting = job_page.parse("https://www.linkedin.com/jobs/view/1/", 200, LINKEDIN_PAGE)
    assert (posting.title, posting.company, posting.locations) == ("Data Analyst", "Acme Test Co", ["Austin, TX"])
    assert posting.job_types == ["part_time"] and posting.seniority == "Entry level"
    assert (posting.pay.low, posting.pay.high, posting.pay.period) == (25, 30, "hour")
    assert "20 hours per week" in posting.description and "SQL" in posting.description
    assert "Other" not in posting.description   # only the description's own element


def test_posting_data_gives_pay_job_type_work_arrangement_and_date():
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Barista", '
          '"hiringOrganization": {"name": "Bean Test Co"}, "employmentType": ["PART_TIME"], '
          '"jobLocationType": "TELECOMMUTE", "datePosted": "2026-10-10", '
          '"baseSalary": {"currency": "USD", "value": {"minValue": 18, "maxValue": 21, "unitText": "HOUR"}}, '
          '"description": "Make coffee."}</script>')
    posting = job_page.parse("https://jobs.example.com/1", 200, page(ld))
    assert posting.job_types == ["part_time"] and posting.work_mode == "remote"
    assert posting.posted_at == "2026-10-10"
    assert (posting.pay.low, posting.pay.high, posting.pay.currency, posting.pay.period) == (18, 21, "USD", "hour")


def test_pay_written_in_the_description_is_found_when_the_data_has_none():
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": "Analyst", '
          '"hiringOrganization": {"name": "Acme"}, "description": "The pay range is $60,000 - $70,000 a year. '
          'We also offer a $2,000 sign-on bonus."}</script>')
    posting = job_page.parse("https://jobs.example.com/1", 200, page(ld))
    assert (posting.pay.low, posting.pay.high, posting.pay.period) == (60000, 70000, "year")
    assert job_page.parse("https://jobs.example.com/1", 200, page(OG.format("Analyst"))).pay is None


@pytest.mark.parametrize("og, title, company, place", [
    # 2026 LinkedIn pages; "&" arrives escaped twice.
    ("Senior Data Analyst at Acme Test Co — New York, NY | LinkedIn Jobs", "Senior Data Analyst", "Acme Test Co", "New York, NY"),
    ("Data Analyst - Analytics &amp;amp; Transformation at Globex Test · Austin, TX | LinkedIn Jobs",
     "Data Analyst - Analytics & Transformation", "Globex Test", "Austin, TX"),
    ("Acme Test Co hiring Data Analyst in Boston, MA | LinkedIn", "Data Analyst", "Acme Test Co", "Boston, MA"),
])
def test_linkedin_page_titles_give_title_company_and_place(og, title, company, place):
    posting = job_page.parse("https://www.linkedin.com/jobs/view/1/", 200,
                             page(f'<meta property="og:title" content="{og}">'))
    assert (posting.title, posting.company, posting.locations) == (title, company, [place])


STEPSTONE = "https://www.stepstone.de/stellenangebote--Data-Analyst-Teststadt-Acme-Test--10000001-inline.html"


@pytest.mark.parametrize("og, ld_title, title", [
    # Job boards add their own words after the posting's title (StepStone, 2026): the posting
    # data's title is the job's own; Quick collection stores whatever title this returns.
    ("Data Analyst (m/w/d) Controlling - Feste Anstellung, Vollzeit Job in Teststadt",
     "Data Analyst (m/w/d) Controlling", "Data Analyst (m/w/d) Controlling"),
    ("Data Analyst - Retail &amp; Sales Analytics (all genders) - Befristeter Vertrag, Teilzeit, Homeoffice Job in Teststadt",
     "Data Analyst - Retail & Sales Analytics (all genders)", "Data Analyst - Retail & Sales Analytics (all genders)"),
    ("Data Analyst (gn) – Engagement | Acme Test Careers", "Data Analyst (gn) – Engagement", "Data Analyst (gn) – Engagement"),
    # The page's title stays when the data's title is not just it without additions.
    ("Data Analyst Intern", "Data Analyst", "Data Analyst Intern"),
    ("Senior Payments Engineer", "Engineer II", "Senior Payments Engineer"),
    ("Payments Engineer", "Payments Engineer", "Payments Engineer"),
])
def test_the_posting_datas_title_wins_over_a_page_title_with_the_boards_additions(og, ld_title, title):
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": %s, '
          '"hiringOrganization": {"name": "Acme Test GmbH"}}</script>') % json.dumps(ld_title)
    posting = job_page.parse(STEPSTONE, 200, page(OG.format(og), ld, title=og))
    assert (posting.title, posting.company) == (title, "Acme Test")


@pytest.mark.parametrize("asked, landed, head", [
    # Pages these sites showed an anonymous request for a job (2026); the job ids are made up.
    ("https://app.joinhandshake.com/stu/jobs/10000001", "https://app.joinhandshake.com/access",
     page('<meta property="og:site_name" content="Handshake">', title="Log in or sign up | Handshake")),
    ("https://www.linkedin.com/jobs/view/10000001/",
     "https://www.linkedin.com/authwall?trk=gf&sessionRedirect=https%3A%2F%2Fwww.linkedin.com%2Fjobs%2Fview%2F10000001%2F",
     page(OG.format("Sign Up | LinkedIn"))),
    ("https://www.linkedin.com/jobs/view/10000001/", "https://www.linkedin.com/checkpoint/challenge/AgEx-test",
     page(title="Security Verification | LinkedIn")),
    ("https://www.naukri.com/job-listings-data-analyst-acme-test-teststadt-3-to-5-years-100000001",
     "https://www.naukri.com/nlogin/login?URL=https://www.naukri.com/job-listings-data-analyst-100000001",
     page(title="Jobseeker&#39;s Login: Search the Best Jobs available in India &amp; Abroad - Naukri.com")),
    (STEPSTONE, "https://www.stepstone.de/candidate/login",
     page(title="Loggen Sie sich in Ihren My StepStone Account ein")),
])
def test_a_sign_in_page_in_place_of_the_posting_teaches_nothing(asked, landed, head):
    # Quick collection stores the fetched title over the card's, and a pasted link shows it:
    # neither may become "Log in or sign up | Handshake" at "Handshake". Not closed either.
    assert job_page.parse(landed, 200, head, original_url=asked) == job_page.Posting()


def test_a_posting_that_moved_to_another_posting_address_is_still_read():
    posting = job_page.parse("https://jobs.example.com/jobs/access-control-engineer-100", 200,
                             page(OG.format("Access Control Engineer")), original_url="https://jobs.example.com/jobs/100")
    assert posting.title == "Access Control Engineer"



@pytest.mark.parametrize("og, ld_title, title", [
    # Lever (2026) puts the employer first: "Palantir Technologies - Administrative Business Partner".
    ("Acme Test Technologies - Data Analyst - Payments", "Data Analyst - Payments", "Data Analyst - Payments"),
    ("Acme Test Technologies – Account Executive, Commercial", "Account Executive, Commercial", "Account Executive, Commercial"),
    # Only the posting's own employer is dropped from the front, never part of the role.
    ("Data Analyst - Payments", "Payments", "Data Analyst - Payments"),
])
def test_the_employer_in_front_of_the_title_is_not_part_of_it(og, ld_title, title):
    ld = ('<script type="application/ld+json">{"@type": "JobPosting", "title": %s, '
          '"hiringOrganization": {"name": "Acme Test Technologies"}}</script>') % json.dumps(ld_title)
    posting = job_page.parse("https://jobs.lever.co/acmetest/00000000-0000-4000-8000-000000000001", 200,
                             page(OG.format(og), ld, title=og))
    assert (posting.title, posting.company) == (title, "Acme Test")


def _greenhouse_board_page(post):
    # The shape of job-boards.greenhouse.io posting pages (2026): no JSON-LD; og:description is the
    # place; the posting itself is in the page's route data.
    context = {"state": {"loaderData": {"root": {}, "routes/$url_token_.jobs_.$job_post_id": {"jobPost": post}}}}
    return (f'<html><head><title>Job Application for {post["title"]} at {post["company_name"]}</title>'
            + OG.format(post["title"]) + f'<meta property="og:description" content="{post["job_post_location"]}">'
            + f"</head><body><script>window.__remixContext = {json.dumps(context)};</script></body></html>")


def test_a_greenhouse_board_posting_gives_its_description_places_pay_and_date():
    post = {"title": "Data Analyst", "company_name": "Acme Test", "job_post_location": "San Francisco, CA • New York, NY",
            "introduction": "<p>Acme Test makes test widgets.</p>",
            "content": "<p>You will build dashboards in SQL &amp; Python.</p>", "conclusion": "<p>Benefits.</p>",
            "published_at": "2026-10-20T09:00:00-04:00",
            "pay_ranges": [{"title": "Zone 1", "min": "$150,000", "max": "$180,000", "currency_type": "USD"},
                           {"title": "Zone 2", "min": "$130,000", "max": "$160,000", "currency_type": "USD"}]}
    url = "https://job-boards.greenhouse.io/acmetest/jobs/1000001"
    posting = job_page.parse(url, 200, _greenhouse_board_page(post), original_url=url)
    assert (posting.title, posting.company) == ("Data Analyst", "Acme Test")
    # The description was the place ("San Francisco, CA • New York, NY"): the fit check and tailoring read it.
    assert "dashboards in SQL & Python" in posting.description and "test widgets" in posting.description
    assert posting.locations == ["San Francisco, CA", "New York, NY"]
    assert (posting.pay.low, posting.pay.high, posting.pay.currency) == (130000, 180000, "USD")
    assert posting.posted_at.startswith("2026-10-20")


def test_a_greenhouse_board_page_without_its_data_is_read_as_before():
    page_ = ('<html><head>' + OG.format("Data Analyst") + '<meta property="og:description" content="Remote (US)">'
             '</head><body><script>window.__remixContext = {"state": </script></body></html>')
    posting = job_page.parse("https://job-boards.greenhouse.io/acmetest/jobs/1000001", 200, page_)
    assert (posting.title, posting.closed) == ("Data Analyst", False)


@pytest.mark.parametrize("og, site, title, company", [
    # Employers' own career sites (2026: Airbnb, Datadog) add their site name to every title.
    ("AirCover Specialist, Claims Experience - Careers at Acme Test", "Careers at Acme Test",
     "AirCover Specialist, Claims Experience", "Acme Test"),
    ("Accounts Payable Analyst | Acme Test Careers", "Acme Test Careers | Acme Test", "Accounts Payable Analyst", "Acme Test"),
    # A Recruitee career site (bunq, 2026): the site's name with "Careers", and "at" the site's employer.
    ("Data Scientist Marketing at Acme Test | Acme Test Careers", "Acme Test", "Data Scientist Marketing", "Acme Test"),
    # Only the site's own name is dropped: a role can end in "Careers", or in "at" something else.
    ("Data Engineer - Growth Careers", "Acme Test Jobs", "Data Engineer - Growth Careers", ""),
    ("Data Analyst at Large | Acme Test Careers", "Acme Test", "Data Analyst at Large", "Acme Test"),
])
def test_a_career_sites_name_is_not_part_of_the_title(og, site, title, company):
    head = page(OG.format(og), f'<meta property="og:site_name" content="{site}">', title=og)
    posting = job_page.parse("https://careers.acmetest.example/positions/1000001?gh_jid=1000001", 200, head)
    assert (posting.title, posting.company) == (title, company)


def test_a_posting_marked_up_as_microdata_gives_its_places_and_date():
    # SmartRecruiters (2026) marks its postings up as schema.org microdata, not JSON-LD.
    body = '''<div itemscope itemtype="http://schema.org/JobPosting"><h1 itemprop="title">Data Analyst</h1>
      <div itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">
        <div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
          <meta itemprop="addressLocality" content="Austin"><meta itemprop="addressRegion" content="TX">
          <meta content="United States" itemprop="addressCountry"></div></div>
      <div itemprop="jobLocation" itemscope itemtype="http://schema.org/Place">
        <div itemprop="address" itemscope itemtype="http://schema.org/PostalAddress">
          <meta itemprop="addressLocality" content="Denver"><meta itemprop="addressRegion" content="CO"></div></div>
      <meta itemprop="datePosted" content="2026-10-29T14:00:00.000Z"></div>'''
    html_ = page(OG.format("Data Analyst"), '<meta property="og:site_name" content="Acme Test">').replace(
        "<body></body>", f"<body>{body}</body>")
    posting = job_page.parse("https://jobs.smartrecruiters.com/AcmeTest/1000001-data-analyst", 200, html_)
    assert (posting.title, posting.company) == ("Data Analyst", "Acme Test")
    assert posting.locations == ["Austin, TX, United States", "Denver, CO"]
    assert posting.posted_at.startswith("2026-10-29")


def test_a_linkedin_posting_no_longer_accepting_applications_is_closed():
    # LinkedIn's public page for a closed posting (2026): the title is unchanged; the top card says so.
    closed = page(OG.format("Data Analyst at Acme Test Co — Austin, TX | LinkedIn Jobs")).replace("<body></body>", """<body>
      <figure class="closed-job closed-job__flavor topcard__flavor-row">
        <span class="closed-job__icon closed-job__icon--error-pebble lazy-load"></span>
        <figcaption class="closed-job__flavor--closed">No longer accepting applications</figcaption>
      </figure></body>""")
    posting = job_page.parse("https://www.linkedin.com/jobs/view/1/", 200, closed)
    assert posting.closed and "No longer accepting applications" in posting.reason
    assert not job_page.parse("https://www.linkedin.com/jobs/view/1/", 200, LINKEDIN_PAGE).closed


def test_a_hostile_page_cannot_stall_the_reader():
    # Unclosed tags and a whitespace-padded title made the microdata and title readers quadratic:
    # minutes for a page this size (hours at the 1.5 MB fetch limit), holding a collection worker.
    import time
    hostile = ('<html><head>' + OG.format("x" + " " * 60_000 + "y") + '<meta property="og:site_name" content="Acme Test">'
               + '</head><body><div itemtype="http://schema.org/JobPosting">' + "<a " * 100_000
               + "<meta " * 50_000)    # cut off mid-tag, as at the fetch's size limit: no ">" follows
    start = time.monotonic()
    job_page.parse("https://jobs.example.com/1", 200, hostile)
    assert time.monotonic() - start < 5
