"""The collector's API: this search's preferences, what was left out, saved searches, board info."""
import asyncio

import main
from core import job_search, shared_config
from tests.backend.test_main_api import _isolate_state, _ready_to_run, auth_client, client  # noqa: F401


def test_a_collection_gets_this_searchs_preferences(auth_client, monkeypatch):
    from cli import collect_jobs
    made, calls = [], []
    monkeypatch.setattr(main, "_kill_browser_processes", lambda: None)
    monkeypatch.setattr(main, "_run_async_in_thread", lambda make_coro, status: made.append(make_coro))

    async def fake_collect(title, jobs, profile, **kwargs):
        calls.append((title, kwargs))
        return []
    monkeypatch.setattr(collect_jobs, "collect_for_title", fake_collect)
    body = {"title": "Barista", "sources": ["indeed"], "search": {"mode": "detailed", "job_types": ["part_time"]}}
    try:
        assert auth_client.post("/jobs/collect", json=body).json()["success"]
        asyncio.run(made[0]())
    finally:
        main._collection_status["running"] = False
    title, kwargs = calls[0]
    assert title == "Barista" and kwargs["plugin"].name == "indeed"
    assert kwargs["search"] == {"mode": "detailed", "job_types": ["part_time"]}


def test_boards_say_whether_they_can_be_read_quickly_and_which_filters_are_shared(auth_client):
    plugins = {p["name"]: p for p in auth_client.get("/plugins").json()["plugins"]}
    assert plugins["linkedin"]["quick"] is True and plugins["ziprecruiter"]["quick"] is False
    assert "job_type" in plugins["linkedin"]["shared_filters"]
    shared = {f["key"]: f["shared"] for f in plugins["linkedin"]["filters"]}
    assert shared == {"date_posted": True, "experience_level": True, "work_type": True, "job_type": True}
    assert {f["key"]: f["shared"] for f in plugins["glassdoor"]["filters"]}["min_rating"] is False


def test_left_out_listings_can_be_seen_kept_or_cleared(auth_client):
    url = "https://www.linkedin.com/jobs/view/123456/"
    job_search.record_filtered(url, {"title": "Data Analyst", "company": "Acme Test Co", "kind": "match",
                                     "reason": "Match 4/10, below 5", "source": "linkedin", "search_title": "Analyst",
                                     "pay": {"low": 20, "high": 25, "currency": "USD", "period": "hour"},
                                     "match": 4, "user_note": "Not a real note"})
    rows = auth_client.get("/jobs/filtered").json()
    assert rows["total"] == 1 and rows["jobs"][0]["url"] == url and rows["jobs"][0]["reason"] == "Match 4/10, below 5"
    assert auth_client.post("/jobs/filtered/keep", json={"url": url}).json() == {"success": True, "added": 1}
    kept = shared_config.read_jobs()[url]
    assert kept["status"] == "pending" and kept["title"] == "Data Analyst" and kept["pay"]["high"] == 25
    assert "user_note" not in kept and "reason" not in kept       # only the listing's own fields
    assert auth_client.get("/jobs/filtered").json()["total"] == 0
    assert auth_client.post("/jobs/filtered/keep", json={"url": url}).status_code == 404
    assert auth_client.post("/jobs/filtered/keep", json={}).status_code == 400
    job_search.record_filtered("https://www.linkedin.com/jobs/view/2/", {"title": "x"})
    assert auth_client.delete("/jobs/filtered").json() == {"success": True, "cleared": 1}


def test_saved_searches_round_trip_and_are_checked(auth_client):
    searches = [{"name": "Weekend barista", "title": "Barista", "sources": ["indeed"],
                 "search": {"job_types": ["part_time"], "min_pay": {"amount": 18, "period": "hour"}}}]
    saved = auth_client.put("/jobs/searches", json={"searches": searches}).json()["searches"]
    assert saved[0]["name"] == "Weekend barista" and saved[0]["search"]["min_pay"]["amount"] == 18
    assert auth_client.get("/jobs/searches").json()["searches"] == saved
    assert auth_client.put("/jobs/searches", json={"searches": "x"}).status_code == 400
    assert auth_client.put("/jobs/searches", json={"searches": [{"name": "x"}] * 51}).status_code == 400


def test_profile_job_search_is_saved_checked(auth_client):
    profile = auth_client.get("/profile").json()
    profile["job_search"] = {"job_types": ["part_time", "nonsense"], "min_match": 99, "notes": "Mornings only"}
    assert auth_client.put("/profile", json=profile).json()["success"]
    saved = auth_client.get("/profile").json()["job_search"]
    assert saved["job_types"] == ["part_time"] and saved["min_match"] == 10 and saved["notes"] == "Mornings only"
    assert auth_client.put("/profile", json={**profile, "job_search": "remote please"}).status_code == 400
