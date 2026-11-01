"""What the user is looking for (Profile -> Job search) and which listings fit it."""
from datetime import datetime, timedelta, timezone

import pytest

from core import job_search, pay
from tests.backend.test_main_api import _isolate_state  # noqa: F401


def prefs(**changes):
    return job_search.normalize({**changes})


def listing(**fields):
    base = {"title": "Data Analyst", "company": "Acme Test Co", "location": "Boston, MA", "text": "",
            "job_types": [], "work_mode": "", "pay": None, "description": "", "match": None}
    return {**base, **fields}


EMPTY = job_search.Context()


def test_malformed_preferences_fall_back_to_their_defaults():
    clean = job_search.normalize({
        "mode": "turbo", "job_types": ["full_time", "gig", 3], "work_modes": "remote", "posted_within": 5,
        "min_pay": {"amount": "-20", "period": "fortnight", "currency": "dollars"}, "part_time_hours": 500,
        "min_match": 11, "notes": "x" * 5000, "exclude_companies": ["Acme", "acme", "", None, "B" * 300],
        "skip_staffing_agencies": "yes", "company_cooldown_days": True,
    })
    assert clean["mode"] == "quick" and clean["job_types"] == ["full_time"] and clean["work_modes"] == []
    assert clean["posted_within"] == 7
    assert clean["min_pay"] == {"amount": 0, "period": "year"}
    assert clean["part_time_hours"] == 39 and clean["min_match"] == 10 and len(clean["notes"]) == 2000
    assert clean["exclude_companies"] == ["Acme", "B" * 80]
    assert clean["skip_staffing_agencies"] is False and clean["company_cooldown_days"] == 1
    assert job_search.normalize(None) == job_search.normalize({}) == job_search.normalize("junk")


def test_a_search_changes_the_profile_defaults_and_pay_uses_the_salary_currency():
    profile = {"salary_expectation": {"currency": "cad"},
               "job_search": {"job_types": ["part_time"], "min_match": 6, "min_pay": {"amount": 18, "period": "hour"}}}
    search = job_search.for_search(profile, {"min_match": 3})
    assert search["job_types"] == ["part_time"] and search["min_match"] == 3
    assert search["min_pay"] == {"amount": 18, "period": "hour", "currency": "CAD"}
    assert job_search.for_search({}, None)["min_pay"]["currency"] == "USD"


@pytest.mark.parametrize("text, types", [
    ("Permanent, full-time", ["full_time"]), ("Part-time", ["part_time"]), ("FULL_TIME", ["full_time"]),
    ("Summer Internship", ["internship"]), ("Contract", ["contract"]), ("Teilzeit", ["part_time"]),
    ("Internal Auditor", []), ("International Sales", []), ("", []),
])
def test_job_types_from_words(text, types):
    assert job_search.job_types_in(text) == types


@pytest.mark.parametrize("text, mode", [
    ("United States (Remote)", "remote"), ("Hybrid work in New York, NY", "hybrid"), ("On-site", "onsite"),
    ("Teilweise Home-Office", "hybrid"), ("New York, NY", ""),
])
def test_work_arrangement_from_words(text, mode):
    assert job_search.work_mode_in(text) == mode


def test_excluded_employers_and_title_words_match_whole_words():
    p = prefs(exclude_companies=["Meta", "Amazon"], exclude_title_words=["Senior", "Lead"])
    assert job_search.screen(listing(company="Metalworks Test Inc"), p, EMPTY) is None
    assert job_search.screen(listing(company="Amazon Web Services"), p, EMPTY)[0] == "company"
    assert job_search.screen(listing(title="Senior Data Analyst"), p, EMPTY)[0] == "title"
    assert job_search.screen(listing(title="Leadership Program Analyst"), p, EMPTY) is None


def test_staffing_agencies_are_left_out_only_when_asked():
    agency = listing(company="Robert Half")
    assert job_search.screen(agency, prefs(), EMPTY) is None
    assert job_search.screen(agency, prefs(skip_staffing_agencies=True), EMPTY)[0] == "agency"
    assert job_search.screen(listing(company="Northwind Staffing Group"), prefs(skip_staffing_agencies=True), EMPTY)[0] == "agency"
    assert job_search.screen(listing(company="Acme Test Co"), prefs(skip_staffing_agencies=True), EMPTY) is None


def test_the_same_job_from_another_board_is_a_duplicate():
    ctx = job_search.Context.from_jobs({"https://a.example/1": {"title": "Data Analyst (m/w/d)", "company": "Acme Test Co, Inc.",
                                                                "location": "Boston, MA"}}, prefs())
    assert job_search.screen(listing(company="Acme Test Co"), prefs(), ctx)[0] == "duplicate"
    assert job_search.screen(listing(company="Acme Test Co", location=""), prefs(), ctx)[0] == "duplicate"
    # The same title at the same employer in another city is another job.
    assert job_search.screen(listing(company="Acme Test Co", location="Denver, CO"), prefs(), ctx) is None


def test_companies_applied_to_recently_wait_their_cooldown():
    now = datetime.now(timezone.utc)
    jobs = {"https://a.example/1": {"title": "Other", "company": "Acme Test Co", "status": "applied",
                                    "applied_at": (now - timedelta(days=10)).isoformat()},
            "https://a.example/2": {"title": "Other", "company": "Globex Test", "status": "applied",
                                    "applied_at": (now - timedelta(days=90)).isoformat()}}
    p = prefs(company_cooldown_days=30)
    ctx = job_search.Context.from_jobs(jobs, p)
    assert job_search.screen(listing(company="Acme Test Co"), p, ctx)[0] == "cooldown"
    assert job_search.screen(listing(company="Globex Test"), p, ctx) is None
    assert job_search.screen(listing(company="Acme Test Co"), prefs(), job_search.Context.from_jobs(jobs, prefs())) is None


def test_job_type_and_work_arrangement_only_when_the_listing_says():
    p = prefs(job_types=["part_time"], work_modes=["remote", "hybrid"])
    assert job_search.screen(listing(job_types=["full_time"]), p, EMPTY)[0] == "job_type"
    assert job_search.screen(listing(job_types=["full_time", "part_time"]), p, EMPTY) is None
    assert job_search.screen(listing(work_mode="onsite"), p, EMPTY)[0] == "work_mode"
    assert job_search.screen(listing(), p, EMPTY) is None   # unknown: kept


def test_minimum_pay():
    p = prefs(min_pay={"amount": 20, "period": "hour", "currency": "USD"})
    assert job_search.screen(listing(pay=pay.parse("$15/hr")), p, EMPTY)[0] == "pay"
    assert job_search.screen(listing(pay=pay.parse("$45,000 a year")), p, EMPTY) is None   # ≈ $21.6/hr
    assert job_search.screen(listing(pay=pay.parse("£15/hr"), match=8), p, EMPTY) is None  # unknown pay, qualifies
    assert job_search.screen(listing(match=8), p, EMPTY) is None                         # not listed, qualifies


def test_dealbreakers_and_the_match_wait_for_the_whole_posting():
    p = prefs(dealbreakers=["security clearance"], min_match=5)
    early = listing(description="Requires an active security clearance.", match=2)
    assert job_search.screen(early, p, EMPTY, final=False) is None
    assert job_search.screen(early, p, EMPTY)[0] == "dealbreaker"
    assert job_search.screen(listing(match=4.5), p, EMPTY)[0] == "match"
    assert job_search.screen(listing(match=5), p, EMPTY) is None
    assert job_search.screen(listing(match=None), p, EMPTY) is None    # not scored: kept


def test_without_pay_a_job_must_be_a_better_match_when_a_minimum_is_set():
    """The user's rule: a 5/10 bar becomes 7.5/10 for a posting that doesn't list pay."""
    p = prefs(min_match=5, min_pay={"amount": 20, "period": "hour", "currency": "USD"})
    assert job_search.match_bar(p, has_pay=True) == 5
    assert job_search.match_bar(p, has_pay=False) == 7.5
    assert job_search.screen(listing(match=7), p, EMPTY)[0] == "match"
    assert "no pay listed" in job_search.screen(listing(match=7), p, EMPTY)[1]
    assert job_search.screen(listing(match=7.5), p, EMPTY) is None
    assert job_search.screen(listing(match=6, pay=pay.parse("$25/hr")), p, EMPTY) is None
    # No minimum pay: no raised bar. No bar at all: every score.
    assert job_search.match_bar(prefs(min_match=5), has_pay=False) == 5
    assert job_search.match_bar(prefs(min_match=0, min_pay={"amount": 20}), has_pay=False) == 0


def test_left_out_listings_are_kept_with_their_reason(data_dir, _isolate_state):
    job_search.record_filtered("https://a.example/1", {"title": "Senior Analyst", "reason": "Title has \"Senior\""})
    assert job_search.filtered_jobs()["https://a.example/1"]["reason"] == 'Title has "Senior"'
    assert job_search.take_filtered("https://a.example/1")["title"] == "Senior Analyst"
    assert job_search.take_filtered("https://a.example/1") is None
    for i in range(job_search.FILTERED_LIMIT + 5):
        job_search.record_filtered(f"https://a.example/n{i}", {"title": "x"})
    assert len(job_search.filtered_jobs()) == job_search.FILTERED_LIMIT
    assert job_search.clear_filtered() == job_search.FILTERED_LIMIT and job_search.filtered_jobs() == {}


def test_saved_searches_are_checked(data_dir, _isolate_state):
    saved = job_search.save_searches([
        {"name": "  Weekend  barista ", "title": "Barista", "sources": ["indeed", "bad name!", "linkedin"],
         "filters": {"date_posted": "1", "x": {"nested": 1}}, "max_jobs": 9999, "search": {"job_types": ["part_time"]}},
        {"name": "Weekend barista", "title": "Other"},
        {"name": ""}, "junk",
    ])
    assert [s["name"] for s in saved] == ["Weekend barista", "Weekend barista"]
    assert saved[0]["id"] != saved[1]["id"]
    assert saved[0]["sources"] == ["indeed", "linkedin"] and saved[0]["filters"] == {"date_posted": "1"}
    assert saved[0]["max_jobs"] == 500 and saved[0]["search"]["job_types"] == ["part_time"]
    assert job_search.saved_searches() == saved
