"""A posting built in the browser gets its real title and employer (real browser, local pages).

Oracle's "Candidate Experience" sites and some employers' own careers sites send a shell: the plain page
fetch sees "Candidate Experience site" and nothing else. The reader opens the page headless in a
temporary profile, waits until it stops changing, and reads what the browser shows, including
a posting shown in a same-site frame (as iCIMS does). The profile is deleted afterwards.
"""
import asyncio
import os
import tempfile

import pytest

from core import job_page, posting_reader
from tests.backend import e2e_harness as lab

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

SHELL = """<!doctype html><html><head><title>Candidate Experience site</title>
<meta property="og:site_name" content="Candidate Experience site"></head>
<body><div id="app">Loading...</div>
<script>
setTimeout(() => {
  document.title = "Data Analyst Intern - Contoso Careers";
  const ld = document.createElement("script");
  ld.type = "application/ld+json";
  ld.textContent = JSON.stringify({"@context": "https://schema.org", "@type": "JobPosting", "title": "Data Analyst Intern",
    "hiringOrganization": {"@type": "Organization", "name": "Contoso"},
    "jobLocation": {"@type": "Place", "address": {"addressLocality": "Austin", "addressRegion": "TX", "addressCountry": "US"}}});
  document.head.appendChild(ld);
  document.getElementById("app").innerHTML = "<h1>Data Analyst Intern</h1><p>" + "%s" + "</p>";
}, 1200);
</script></body></html>""" % lab.FILLER

FRAMED = """<!doctype html><html><head><title>Careers</title></head><body>
<iframe src="/inner" style="width:100%%;height:600px"></iframe><p>%s</p></body></html>""" % lab.FILLER
INNER = """<!doctype html><html><head><title>Treasury Intern | Northwind</title>
<script type="application/ld+json">{"@type": "JobPosting", "title": "Treasury Intern",
"hiringOrganization": {"@type": "Organization", "name": "Northwind"}}</script></head>
<body><h1>Treasury Intern</h1><p>%s</p></body></html>""" % lab.FILLER


@pytest.fixture
def site():
    s = lab.Site({"/job/1": SHELL, "/job/2": FRAMED, "/inner": INNER})
    yield s
    s.close()


def test_a_page_built_in_the_browser_gets_its_real_names(site, monkeypatch):
    # Before: the plain read of the same page has only the careers system's name.
    plain = job_page.parse(site.url("/job/1"), 200, SHELL)
    assert job_page.is_placeholder_company(plain.company) and plain.title in ("", "Candidate Experience site") \
        or job_page.is_placeholder(plain.title)

    made = []
    real_mkdtemp = tempfile.mkdtemp
    monkeypatch.setattr(tempfile, "mkdtemp", lambda *a, **k: made.append(real_mkdtemp(*a, **k)) or made[-1])
    postings = asyncio.run(posting_reader.read_rendered_many([site.url("/job/1"), site.url("/job/2")]))

    one, two = postings[site.url("/job/1")], postings[site.url("/job/2")]
    assert (one.title, one.company) == ("Data Analyst Intern", "Contoso")
    assert one.locations and "Austin" in one.locations[0]
    assert (two.title, two.company) == ("Treasury Intern", "Northwind")       # read from the same-site frame
    # One temporary profile for the batch, used in place (browser-use copies other folders and
    # leaves the copy behind) and gone afterwards; nothing was sent to the site.
    assert len(made) == 1 and not any(os.path.exists(path) for path in made)
    assert site.posted("/job/1") == [] and site.posted("/job/2") == []
