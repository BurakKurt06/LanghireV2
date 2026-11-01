"""Collector review regressions: bounded repros, temporary data, no live services."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor

import pytest

from cli import collect_jobs
from core import job_page, job_search, pay, shared_config
from sources import locations
from sources.registry import _parse_plugin_yaml, _safe_pattern
from tests.backend.test_main_api import _isolate_state, auth_client, client  # noqa: F401


def plugin(**listing):
    return _parse_plugin_yaml({
        "name": "localtest", "display_name": "Local Test", "version": "1", "author": "test",
        "countries": ["US"], "website": "https://board.example", "collection_prompt": "", "apply_prompt": "",
        "listing": {"job_link": r"/job/(\d+)", **listing},
    }, "test.yaml", False)


@pytest.mark.parametrize("url", [
    "https://board.example:invalid/job/1", "https://board.example:99999/job/1",
])
def test_bad_link_ports_do_not_abort_a_page(url):
    assert collect_jobs.canonical_job_url(plugin(), url) == ""


@pytest.mark.parametrize("template", [
    "https://user:pass@board.example/job/{id}", "https://board.example:99999/job/{id}",
    "https://board.example:443@outside.example/job/{id}",
])
def test_imported_job_templates_cannot_change_authority(template):
    p = plugin(job_url=template)
    assert collect_jobs.canonical_job_url(p, "https://board.example/job/123") == ""


@pytest.mark.parametrize("pattern", [r"(a|aa)+$", r"(a{1,3})+$", r"a+a+a+a+$",
                                    r"a{1,2}" * 30 + "$", r"(a|aa)" * 30 + "$"])
def test_imported_patterns_reject_exponential_and_overlapping_repeats(pattern):
    assert _safe_pattern(pattern) == ""


def test_long_duplicate_saved_search_ids_terminate_and_remain_unique(data_dir):
    # A child makes a regression fail in bounded time instead of hanging pytest/API.
    script = """
import sys
sys.path.insert(0, 'backend')
from core import job_search
s=job_search.save_searches([{'id':'a'*40,'name':'one'},{'id':'a'*40,'name':'two'}])
assert len({x['id'] for x in s})==2 and all(len(x['id'])<=40 for x in s)
"""
    subprocess.run([sys.executable, "-B", "-c", script], cwd=Path(__file__).resolve().parents[3],
                   env=os.environ.copy(), timeout=8, check=True, capture_output=True)


def test_keep_does_not_remove_a_listing_when_jobs_save_fails(auth_client, monkeypatch):
    url = "https://board.example/job/123"
    job_search.record_filtered(url, {"title": "Analyst", "company": "Acme", "kind": "match"})
    def fail(*args, **kwargs):
        raise OSError("demonstrated full-disk save failure")
    monkeypatch.setattr(collect_jobs, "save_new_jobs", fail)
    try:
        auth_client.post("/jobs/filtered/keep", json={"url": url})
    except OSError:
        pass
    assert url in job_search.filtered_jobs()


def test_keep_racing_an_existing_job_preserves_user_fields(auth_client):
    url = "https://board.example/job/123"
    shared_config.mutate_jobs(lambda jobs: jobs.update({url: {
        "title": "My title", "company": "Acme", "status": "applied", "user_note": "Personal note",
    }}))
    job_search.record_filtered(url, {"title": "Page title", "status": "pending", "user_note": "Page instruction"})
    reply = auth_client.post("/jobs/filtered/keep", json={"url": url})
    assert reply.status_code == 200
    assert shared_config.read_jobs()[url]["status"] == "applied"
    assert shared_config.read_jobs()[url]["user_note"] == "Personal note"


def test_filtered_concurrent_writers_do_not_lose_distinct_entries(data_dir, _isolate_state):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda n: job_search.record_filtered(f"https://board.example/job/{n}", {"title": str(n)}), range(30)))
    assert len(job_search.filtered_jobs()) == 30


@pytest.mark.parametrize("text", [
    "$5,000 sign-on bonus", "Signing bonus: $5,000", "Raised $20 million in funding",
    "Tuition reimbursement $5,250/year", "$50 gift card each month",
])
def test_card_benefits_and_funding_are_not_salary(text):
    assert pay.parse(text) is None


def test_card_benefits_do_not_hide_real_salary():
    found = pay.parse("$5,000 sign-on bonus; salary $25/hr")
    assert found and (found.high, found.period) == (25, "hour")


def test_posting_pay_overrides_the_more_generous_card(data_dir, _isolate_state, monkeypatch):
    monkeypatch.setattr(job_page, "fetch", lambda *a: job_page.Posting(pay=pay.parse("$15/hr")))
    prefs = job_search.for_search({}, {"min_pay": {"amount": 20, "period": "hour"}, "min_match": 0})
    intake = collect_jobs.Intake("Analyst", "test", {}, prefs, 10, {})
    result = asyncio.run(intake.consider({"url": "https://board.example/job/123", "title": "Analyst",
                                        "company": "Acme", "text": "Salary $30/hr", "match": 9}))
    assert not result and not shared_config.read_jobs()
    assert job_search.filtered_jobs()["https://board.example/job/123"]["kind"] == "pay"


def test_uncomparable_pay_uses_the_unknown_pay_match_bar():
    prefs = job_search.for_search({}, {"min_pay": {"amount": 20, "period": "hour", "currency": "USD"}})
    listing = {"title": "Analyst", "company": "Acme", "match": 6, "pay": pay.parse("£30/hr")}
    verdict = job_search.screen(listing, prefs, job_search.Context())
    assert verdict and verdict[0] == "match"


def test_unknown_score_cannot_clear_a_required_unknown_pay_bar():
    prefs = job_search.for_search({}, {"min_pay": {"amount": 20, "period": "hour"}})
    verdict = job_search.screen({"title": "Analyst", "match": None, "pay": None}, prefs, job_search.Context())
    assert verdict and verdict[0] == "match"


def test_same_named_city_in_another_state_is_not_a_duplicate():
    ctx = job_search.Context.from_jobs({"old": {"title": "Analyst", "company": "Acme", "location": "Springfield, IL"}}, {})
    assert not ctx.duplicate({"title": "Analyst", "company": "Acme", "location": "Springfield, MA"})
    assert ctx.duplicate({"title": "Analyst", "company": "Acme", "location": "Springfield, IL"})


@pytest.mark.parametrize("company", ["Hudson River Trading", "Atrium Health", "Cross Country Mortgage"])
def test_ambiguous_agency_brands_do_not_exclude_other_employers(company):
    assert not job_search.is_agency(company)
    assert job_search.is_agency("Hudson") and job_search.is_agency("Atrium Staffing")


def test_location_cannot_cancel_a_conflicting_state_with_a_matching_country():
    result = {"locationType": "C", "locationId": "1", "locationName": "Springfield, IL",
              "cityName": "Springfield", "stateAbbreviation": "IL", "country2LetterIso": "US"}
    assert locations.best_match("Springfield, MA, US", [result]) is None


def test_posted_within_is_checked_when_the_board_only_offers_a_wider_window():
    prefs = job_search.for_search({}, {"posted_within": 3})
    old = (datetime.now(timezone.utc)-timedelta(days=6)).date().isoformat()
    result = job_search.screen({"title": "Analyst", "posted_at": old}, prefs, job_search.Context())
    assert result and result[0] == "posted"


def test_nested_search_override_preserves_the_profiles_pay_period_and_currency():
    profile = {"job_search": {"min_pay": {"amount": 25, "period": "hour", "currency": "CAD"}}}
    assert job_search.for_search(profile, {"min_pay": {"amount": 30}})["min_pay"] == {
        "amount": 30, "period": "hour", "currency": "CAD",
    }


@pytest.mark.parametrize("score", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_scores_do_not_become_perfect_matches(data_dir, _isolate_state, score):
    intake = collect_jobs.Intake("Analyst", "test", {}, job_search.for_search({}), 10, {}, fetch_postings=False)
    assert intake._listing({"title": "Analyst", "match": score})["match"] is None


def test_ai_label_cannot_replace_the_visible_title_to_bypass_exclusions():
    p = plugin()
    jobs, _ = collect_jobs.card_jobs(p, [{"id": "123", "href": "https://board.example/job/123",
        "title": "Senior Analyst", "text": "Senior Analyst\nAcme"}],
        {1: collect_jobs.CardLabel(n=1, title="Junior Analyst", company="Acme", match=10)})
    assert jobs[0]["title"] == "Senior Analyst"


def test_imported_detailed_markers_cannot_fetch_or_save_an_outside_host(data_dir, _isolate_state, monkeypatch):
    read = []
    monkeypatch.setattr(job_page, "fetch", lambda *a, **k: read.append(a[0]))
    intake = collect_jobs.Intake("Analyst", "test", {}, job_search.for_search({}), 10, {})
    intake.allowed_hosts = ["board.example"]
    assert not asyncio.run(intake.consider({"url": "https://outside.example/job/123", "title": "Analyst"}))
    assert read == [] and not shared_config.read_jobs()


def test_description_retry_preserves_the_imported_sources_host_limits(monkeypatch):
    seen = []
    monkeypatch.setattr(collect_jobs, "get_plugin", lambda name: plugin())
    monkeypatch.setattr(job_page, "fetch", lambda *a, **kw: seen.append((a, kw)))
    asyncio.run(collect_jobs.fetch_description_for_job("https://board.example/job/123", {"source": "localtest"}))
    assert seen[0][1] == {"allowed_hosts": ["board.example"]}


def test_json_ld_with_unknown_currency_is_not_assumed_to_meet_a_usd_minimum():
    found = pay.from_json_ld({"baseSalary": {"value": {"value": 50000, "unitText": "YEAR"}}})
    assert found and pay.compare(found, {"amount": 40000, "period": "year", "currency": "USD"}) == "unknown"


def test_visible_card_age_is_checked_even_without_a_posting_date(data_dir, _isolate_state):
    prefs = job_search.for_search({}, {"posted_within": 3})
    intake = collect_jobs.Intake("Analyst", "test", {}, prefs, 10, {}, fetch_postings=False)
    assert not asyncio.run(intake.consider({"url": "https://board.example/job/123", "title": "Analyst",
                                           "text": "Analyst\nAcme\nPosted 6 days ago", "match": 9}))
    assert job_search.filtered_jobs()["https://board.example/job/123"]["kind"] == "posted"


def test_explicit_posting_seniority_is_checked_when_the_board_cannot_filter_it():
    prefs = job_search.for_search({}, {"experience": ["entry"]})
    verdict = job_search.screen({"title": "Analyst", "seniority": "Director", "match": 9},
                                prefs, job_search.Context())
    assert verdict and verdict[0] == "experience"
    assert job_search.screen({"title": "Analyst", "seniority": "", "match": 9}, prefs, job_search.Context()) is None


def test_nonfinite_saved_search_limit_is_normalized_instead_of_crashing(data_dir, _isolate_state):
    result = job_search.save_searches([{"name": "Test", "max_jobs": float("inf")}])
    assert result[0]["max_jobs"] == 0


def test_invalid_filtered_url_is_not_reported_as_kept(auth_client):
    url = "http://127.0.0.1/private"
    job_search.record_filtered(url, {"title": "Invalid local entry"})
    response = auth_client.post("/jobs/filtered/keep", json={"url": url})
    assert response.status_code == 400
    assert url in job_search.filtered_jobs() and url not in shared_config.read_jobs()


def test_model_labels_cannot_invent_quick_pay_or_override_explicit_type_and_arrangement(data_dir, _isolate_state):
    cards = [{"id": "123", "href": "https://board.example/job/123", "title": "Analyst",
              "text": "Analyst\nAcme\nPart-time\nOn-site"}]
    labels = {1: collect_jobs.CardLabel(n=1, company="Acme", location="Remote", pay="$99/hr",
                                       job_type="Full-time", work_mode="Remote", match=6)}
    raws, _ = collect_jobs.card_jobs(plugin(), cards, labels)
    intake = collect_jobs.Intake("Analyst", "test", {}, job_search.for_search({}), 10, {}, fetch_postings=False)
    facts = intake._listing(raws[0])
    assert facts["pay"] is None
    assert facts["job_types"] == ["part_time"] and facts["work_mode"] == "onsite"


def test_posting_employer_overrides_a_wrong_model_label(data_dir, _isolate_state, monkeypatch):
    monkeypatch.setattr(job_page, "fetch", lambda *a, **kw: job_page.Posting(company="Blocked Company", company_stated=True))
    prefs = job_search.for_search({}, {"exclude_companies": ["Blocked Company"]})
    intake = collect_jobs.Intake("Analyst", "test", {}, prefs, 10, {})
    assert not asyncio.run(intake.consider({"url": "https://board.example/job/123", "title": "Analyst",
                                           "company": "Invented Model Company", "match": 9}))


def test_publisher_site_name_is_not_mistaken_for_an_explicit_employer(data_dir, _isolate_state, monkeypatch):
    posting = job_page.parse("https://board.example/job/123", 200,
                             '<title>Analyst</title><meta property="og:site_name" content="Indeed">')
    monkeypatch.setattr(job_page, "fetch", lambda *a, **kw: posting)
    intake = collect_jobs.Intake("Analyst", "test", {}, job_search.for_search({}), 10, {})
    assert asyncio.run(intake.consider({"url": "https://board.example/job/123", "title": "Analyst",
                                       "company": "Acme Employer", "match": 9}))
    assert shared_config.read_jobs()["https://board.example/job/123"]["company"] == "Acme Employer"


def test_minimum_pay_does_not_allow_a_tenth_percent_shortfall():
    prefs = job_search.for_search({}, {"min_pay": {"amount": 20, "period": "hour", "currency": "USD"}})
    listing = {"title": "Analyst", "pay": pay.parse("$19.99/hr"), "match": 10}
    assert pay.compare(listing["pay"], prefs["min_pay"]) == "below"
    assert job_search.screen(listing, prefs, job_search.Context())[0] == "pay"


def test_minimum_pay_does_not_reject_an_exact_decimal_conversion():
    # Binary floating point makes 20945.6 / 2080 slightly less than 10.07.
    assert pay.compare(pay.parse("$20,945.60/year"),
                       {"amount": 10.07, "period": "hour", "currency": "USD"}) == "meets"


def test_benefit_period_does_not_turn_a_gift_into_low_salary():
    assert pay.parse("$50/month gift card") is None
    assert pay.parse("$1,000/year") is not None


@pytest.mark.parametrize("text", ["$0/hour", "$1/hour"])
def test_explicit_zero_and_small_hourly_rates_are_known_below_the_minimum(text):
    value = pay.parse(text)
    assert value is not None
    assert pay.compare(value, {"amount": 20, "period": "hour", "currency": "USD"}) == "below"


def test_explicit_zero_base_salary_is_known_instead_of_missing():
    value = pay.from_json_ld({"baseSalary": {"currency": "USD", "value": 0, "unitText": "YEAR"}})
    assert value is not None
    assert pay.compare(value, {"amount": 40000, "period": "year", "currency": "USD"}) == "below"
