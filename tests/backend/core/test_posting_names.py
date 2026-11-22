"""Pasted links get their real title and employer, also from pages built in the browser.

Seen 2026-11: some pasted jobs were still called "Pasted Link" or had the careers system's name
("Candidate Experience site") as the employer. The plain page fetch
of today's parser already finds most of them (they were saved by older versions and never read
again); pages built in the browser (a large employer's own careers site) need the page as a browser shows it. A
second reading only fills in what is still a placeholder: on one site it named a building as
the employer, where the plain read had the right one.
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from core import job_page, posting_reader
from core import shared_config as sc
from core.job_page import Posting

import main  # noqa: E402


@pytest.fixture
def jobs(tmp_path, monkeypatch):
    monkeypatch.setattr(sc, "JOBS_FILE", tmp_path / "jobs.json")
    monkeypatch.setattr(sc, "JOBS_LOCK", tmp_path / "jobs.json.lock")

    def write(entries):
        sc.write_jobs({url: {"status": "pending", "source": "pasted", **fields} for url, fields in entries.items()})
    return write


# ── filling in only what is missing ─────────────────────────────────────────

def test_only_placeholders_are_filled(jobs):
    jobs({"https://jobs.example/1": {"title": "Pasted Link", "company": "Fabrikam"},
          "https://jobs.example/2": {"title": "Risk Intern", "company": "Candidate Experience site"}})
    rendered = Posting(title="Risk Intern in New York", company="Elm Plaza", locations=["New York, NY"])
    assert sc.fill_missing_names("https://jobs.example/1", rendered)
    assert sc.fill_missing_names("https://jobs.example/2", rendered)
    one, two = sc.read_jobs()["https://jobs.example/1"], sc.read_jobs()["https://jobs.example/2"]
    # The real employer from the plain read stays; only the placeholder title is replaced.
    assert (one["title"], one["company"], one["location"]) == ("Risk Intern in New York", "Fabrikam", "New York, NY")
    assert (two["title"], two["company"]) == ("Risk Intern", "Elm Plaza")
    assert one["name_source"] == two["name_source"] == "page"


def test_names_from_the_agent_or_the_user_are_never_replaced(jobs):
    jobs({"https://jobs.example/a": {"title": "Pasted Link", "company": "", "name_source": "agent"},
          "https://jobs.example/u": {"title": "Pasted Link", "company": "", "user_named": ["company"]}})
    assert not sc.fill_missing_names("https://jobs.example/a", Posting(title="X", company="Y"))
    sc.fill_missing_names("https://jobs.example/u", Posting(title="Analyst", company="Page Co"))
    agent, user = sc.read_jobs()["https://jobs.example/a"], sc.read_jobs()["https://jobs.example/u"]
    assert (agent["title"], agent["company"]) == ("Pasted Link", "")
    assert (user["title"], user["company"]) == ("Analyst", "")      # the field the user typed stays theirs


def test_a_placeholder_from_the_second_reading_changes_nothing(jobs):
    jobs({"https://jobs.example/1": {"title": "Pasted Link", "company": ""}})
    assert not sc.fill_missing_names("https://jobs.example/1", Posting(title="Careers", company="Candidate Experience site"))
    assert sc.read_jobs()["https://jobs.example/1"]["title"] == "Pasted Link"


def test_a_closed_posting_closes_only_an_unsent_job(jobs):
    jobs({"https://jobs.example/1": {"title": "Pasted Link", "company": ""},
          "https://jobs.example/2": {"title": "Pasted Link", "company": "", "status": "needs_you",
                                     "submit_attempted_at": "2026-11-10T10:00:00+00:00"},
          "https://jobs.example/3": {"title": "Analyst", "company": "Acme", "status": "applied"}})
    done = Posting(closed=True, reason="The posting is closed (its title says \"FILLED: Analyst\").")
    for url in ("https://jobs.example/1", "https://jobs.example/2", "https://jobs.example/3"):
        sc.fill_missing_names(url, done)
    saved = sc.read_jobs()
    assert saved["https://jobs.example/1"]["status"] == "closed" and "FILLED" in saved["https://jobs.example/1"]["error"]
    assert saved["https://jobs.example/2"]["status"] == "needs_you"     # may have been sent: kept for the user
    assert saved["https://jobs.example/3"]["status"] == "applied"


def test_the_check_time_is_recorded(jobs):
    jobs({"https://jobs.example/1": {"title": "Pasted Link", "company": ""}})
    assert sc.fill_missing_names("https://jobs.example/1", None, "2026-11-12T10:00:00+00:00")
    assert sc.read_jobs()["https://jobs.example/1"]["names_checked_at"] == "2026-11-12T10:00:00+00:00"


# ── at paste time: the plain read first, the browser only for what is left ──

def test_the_browser_reads_only_links_the_plain_read_left_unnamed(jobs, monkeypatch):
    jobs({"https://jobs.example/plain": {"title": "Pasted Link", "company": ""},
          "https://jobs.example/spa": {"title": "Pasted Link", "company": ""}})
    pages = {"https://jobs.example/plain": Posting(title="Finance Intern", company="Northwind"),
             "https://jobs.example/spa": Posting(title="Data Intern", company="Candidate Experience site")}
    monkeypatch.setattr(job_page, "fetch", lambda url: pages.get(url))
    asked = []

    async def rendered(urls, **_):
        asked.extend(urls)
        return {u: Posting(title="Data Intern", company="Contoso") for u in urls}
    monkeypatch.setattr(posting_reader, "read_rendered_many", rendered)

    counts = asyncio.run(main._read_posting_names(list(pages)))
    assert asked == ["https://jobs.example/spa"]
    assert counts == {"read": 2, "rendered": 1}
    saved = sc.read_jobs()
    assert (saved["https://jobs.example/plain"]["title"], saved["https://jobs.example/plain"]["company"]) == ("Finance Intern", "Northwind")
    assert (saved["https://jobs.example/spa"]["title"], saved["https://jobs.example/spa"]["company"]) == ("Data Intern", "Contoso")
    assert all(job.get("names_checked_at") for job in saved.values())


def test_without_the_browser_reading_only_the_plain_read_runs(jobs, monkeypatch):
    jobs({"https://jobs.example/spa": {"title": "Pasted Link", "company": ""}})
    monkeypatch.setattr(job_page, "fetch", lambda url: Posting(title="Data Intern"))

    async def rendered(urls, **_):
        raise AssertionError("no browser while applications run")
    monkeypatch.setattr(posting_reader, "read_rendered_many", rendered)
    assert asyncio.run(main._read_posting_names(["https://jobs.example/spa"], rendered=False)) == {"read": 1, "rendered": 0}


def test_a_browser_that_cannot_start_leaves_the_names_as_they_are(jobs, monkeypatch):
    # (Tests may not start a real browser: the reader's start fails, as on a computer without one.)
    jobs({"https://jobs.example/spa": {"title": "Pasted Link", "company": ""}})
    monkeypatch.setattr(job_page, "fetch", lambda url: None)
    assert asyncio.run(posting_reader.read_rendered_many(["https://jobs.example/spa"])) == {}
    asyncio.run(main._read_posting_names(["https://jobs.example/spa"]))
    assert sc.read_jobs()["https://jobs.example/spa"]["title"] == "Pasted Link"


# ── saved jobs from older versions: looked at again, at most once a week ────

def test_saved_jobs_without_names_are_looked_at_again_once_a_week(jobs):
    now = datetime(2026, 11, 12, 12, 0, tzinfo=timezone.utc)
    jobs({"https://jobs.example/new": {"title": "Pasted Link", "company": ""},
          "https://jobs.example/old": {"title": "Analyst", "company": "Candidate Experience site",
                                       "names_checked_at": (now - timedelta(days=8)).isoformat()},
          "https://jobs.example/recent": {"title": "Pasted Link", "company": "",
                                          "names_checked_at": (now - timedelta(days=2)).isoformat()},
          "https://jobs.example/named": {"title": "Analyst", "company": "Acme"},
          "https://jobs.example/applied": {"title": "Pasted Link", "company": "", "status": "applied"},
          "https://jobs.example/running": {"title": "Pasted Link", "company": "", "status": "in_progress"}})
    assert main._unnamed_saved_jobs(now) == ["https://jobs.example/new", "https://jobs.example/old"]


def test_the_look_at_saved_jobs_is_capped(jobs):
    jobs({f"https://jobs.example/{i}": {"title": "Pasted Link", "company": ""} for i in range(60)})
    assert len(main._unnamed_saved_jobs()) == main.NAMES_STARTUP_MAX_JOBS
