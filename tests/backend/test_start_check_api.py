"""The app's side of Start: the check, the questions asked once, the grouped answers, the last-page picture.

In-process (FastAPI's TestClient), with made-up jobs in a throwaway data folder. Nothing starts a
run, opens a browser or calls an AI provider; the CAPTCHA solver's balance call is a stand-in.
"""
import pytest

import main
from core import captcha, job_screenshots, shared_config, usage_meter
from tests.backend.core.test_usage_meter import headers
from tests.backend.test_main_api import AUTH, _isolate_state, auth_client, client  # noqa: F401 (fixtures)

JPEG = b"\xff\xd8\xff\xe0" + b"made-up picture" * 30
NOTICE = {"question": "What is your notice period in weeks?", "section": "About you", "options": [], "field_type": "text"}


def jobs(**by_url):
    shared_config.write_jobs({url: {"title": "Analyst", "company": "Example Works", **fields}
                              for url, fields in by_url.items()})


@pytest.fixture
def all_set(monkeypatch, data_dir):
    """Everything a run needs is there (each test takes one thing away)."""
    resume = data_dir / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 made up")
    monkeypatch.setattr(main, "_llm_ready", lambda llm: True)
    monkeypatch.setattr(main, "load_llm_settings", lambda: {"provider": "chatgpt"})
    monkeypatch.setattr(main, "load_settings", lambda: {"resume_path": str(resume)})
    monkeypatch.setattr(main, "load_profile", lambda: {"name": "Ada Test", "education": {"graduation": "May 2028"}})
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {
        "sensitive_data": {"email": "ada@example.com", "password": "Made-Up-Pass-77"}, "captcha_api_key": "made-up-key"})
    monkeypatch.setattr(captcha, "balance", lambda key, seconds=8: 2.5)

    async def signed_in():
        return {"gmail": {"logged_in": True}, "linkedin": {"logged_in": False}}

    monkeypatch.setattr(main, "auth_status", signed_in)
    return monkeypatch


def test_with_everything_in_place_start_has_nothing_to_say(auth_client, all_set):
    jobs(**{"https://boards.example/jobs/1": {"status": "pending"}})
    answer = auth_client.post("/apply/check", json={}).json()
    assert answer == {"ok": True, "problems": [], "questions": [], "jobs": 1}


def test_what_is_missing_is_listed_in_one_answer(auth_client, all_set):
    async def signed_out():
        return {"gmail": {"logged_in": False}}

    all_set.setattr(main, "auth_status", signed_out)
    all_set.setattr(captcha, "balance", lambda key, seconds=8: 0.0)
    all_set.setattr(main, "load_settings", lambda: {"resume_path": ""})
    jobs(**{"https://boards.example/jobs/1": {"status": "pending"}})
    answer = auth_client.post("/apply/check", json={}).json()
    assert answer["ok"] is False
    assert [(p["id"], p["required"]) for p in answer["problems"]] == [("resume", True), ("gmail", False), ("captcha", False)]
    assert "Gmail isn't connected" in answer["problems"][1]["message"]
    assert "2Captcha balance is empty" in answer["problems"][2]["message"]


def test_no_room_on_the_ai_plan_is_said_before_starting_but_does_not_stop_the_start(auth_client, all_set):
    """The run waits for the reset like a pause (the user's wish, 2026-11-20), so Start may go ahead."""
    usage_meter.note_reply(headers(primary=99, secondary=40, primary_reset=9000))
    jobs(**{"https://boards.example/jobs/1": {"status": "pending"}})
    answer = auth_client.post("/apply/check", json={}).json()
    assert [(p["id"], p["required"]) for p in answer["problems"]] == [("ai_limit", False)]
    assert "99% of the 5-hour limit is used" in answer["problems"][0]["message"]
    assert "The run will wait and start by itself when it resets" in answer["problems"][0]["message"]
    assert main._start_problem(apply=True) == ""


def test_continue_now_is_refused_without_a_run(auth_client, all_set):
    assert auth_client.post("/apply/limit/continue", json={}).json() == {"success": False}


def test_the_check_asks_what_the_queued_jobs_sites_are_known_to_need(auth_client, all_set):
    jobs(**{"https://acme.tal.net/vx/opp/1": {"status": "pending"}, "https://acme.tal.net/vx/opp/2": {"status": "pending"},
            "https://boards.example/jobs/3": {"status": "pending"},
            "https://boards.example/jobs/4": {"status": "needs_you", "pending_questions": [NOTICE]}})
    answer = auth_client.post("/apply/check", json={}).json()
    assert answer["jobs"] == 3
    asked = {q["question"][:40]: q["jobs"] for q in answer["questions"]}
    assert asked == {"Your school before university (high scho": 2, "What is your notice period in weeks?": 1}


def test_only_the_jobs_about_to_run_are_looked_at(auth_client, all_set):
    jobs(**{"https://acme.tal.net/vx/opp/1": {"status": "pending"}, "https://boards.example/jobs/3": {"status": "pending"}})
    answer = auth_client.post("/apply/check", json={"job_urls": ["https://boards.example/jobs/3"]}).json()
    assert answer["jobs"] == 1 and answer["questions"] == []


def test_one_answer_goes_to_every_job_that_waits_on_the_question_and_is_never_asked_again(auth_client, all_set):
    other = {"question": "Your T-shirt size?", "options": ["S", "M", "L"], "field_type": "dropdown"}
    jobs(**{
        "https://a.example/1": {"status": "needs_you", "error": "Your answer is needed", "pending_questions": [NOTICE]},
        "https://a.example/2": {"status": "needs_you", "error": "Your answer is needed", "pending_questions": [NOTICE, other]},
        "https://a.example/3": {"status": "needs_you", "pending_questions": [NOTICE],
                                "submit_attempted_at": "2026-11-15T10:00:00+00:00"},
        "https://a.example/4": {"status": "pending"},
    })
    groups = auth_client.get("/jobs/questions").json()["questions"]
    assert [(g["question"], len(g["jobs"])) for g in groups] == [(NOTICE["question"], 2), ("Your T-shirt size?", 1)]

    answer = auth_client.post("/jobs/questions/answer", json={"question": NOTICE["question"], "answer": "2 weeks"}).json()
    assert answer == {"success": True, "ready": 1, "still_waiting": 1, "continued": 0}
    saved = shared_config.read_jobs()
    assert saved["https://a.example/1"]["status"] == "pending" and not saved["https://a.example/1"].get("pending_questions")
    assert saved["https://a.example/1"]["error"] is None
    assert saved["https://a.example/2"]["status"] == "needs_you"
    assert [q["question"] for q in saved["https://a.example/2"]["pending_questions"]] == ["Your T-shirt size?"]
    # The one that may already have been sent is never made ready by an answer.
    assert saved["https://a.example/3"]["status"] == "needs_you" and saved["https://a.example/3"]["pending_questions"]
    # Saved as the user's own: no later job is asked this, on any site.
    assert main._get_memory_store().qa_user_answer("What is your notice period, in weeks?") == "2 weeks"
    assert [g["question"] for g in auth_client.get("/jobs/questions").json()["questions"]] == ["Your T-shirt size?"]


def test_an_empty_answer_changes_nothing(auth_client, all_set):
    jobs(**{"https://a.example/1": {"status": "needs_you", "pending_questions": [NOTICE]}})
    assert auth_client.post("/jobs/questions/answer", json={"question": NOTICE["question"], "answer": " "}).status_code == 400
    assert shared_config.read_jobs()["https://a.example/1"]["status"] == "needs_you"


# ── the picture of the last page ───────────────────────────────────────────────
def test_a_jobs_last_page_picture_is_served_to_the_app_only(auth_client, client, all_set):
    url = "https://a.example/1"
    jobs(**{url: {"status": "failed"}})
    job_screenshots.save(url, JPEG)
    reply = auth_client.get("/jobs/screenshot", params={"url": url})
    assert reply.status_code == 200 and reply.content == JPEG and reply.headers["content-type"] == "image/jpeg"
    from fastapi.testclient import TestClient
    assert TestClient(main.app, base_url="http://127.0.0.1").get("/jobs/screenshot", params={"url": url}).status_code == 401


def test_no_picture_and_no_such_job_are_plain_not_found(auth_client, all_set):
    jobs(**{"https://a.example/1": {"status": "failed"}})
    assert auth_client.get("/jobs/screenshot", params={"url": "https://a.example/1"}).status_code == 404
    job_screenshots.save("https://gone.example/9", JPEG)                    # a picture without its job is not served
    assert auth_client.get("/jobs/screenshot", params={"url": "https://gone.example/9"}).status_code == 404


def test_removing_a_job_or_marking_it_applied_deletes_its_picture(auth_client, all_set):
    first, second = "https://a.example/1", "https://a.example/2"
    jobs(**{first: {"status": "failed"}, second: {"status": "needs_you"}})
    job_screenshots.save(first, JPEG)
    job_screenshots.save(second, JPEG)
    assert auth_client.request("DELETE", "/jobs", json={"urls": [first]}).json() == {"success": True, "deleted": 1}
    assert not job_screenshots.exists(first) and job_screenshots.exists(second)
    assert auth_client.put("/jobs/status", json={"url": second, "status": "applied"}).json()["success"]
    assert not job_screenshots.exists(second)


def test_the_run_panel_is_told_why_a_run_stopped(auth_client, all_set):
    from cli import apply_jobs
    apply_jobs.new_run()
    assert auth_client.get("/apply/status").json()["notice"] == {"text": "", "kind": "", "resets_at": None}
    usage_meter.limit_reached(3 * 3600)
    apply_jobs._run_notice.update(text="Stopped: ChatGPT limit reached, resets in about 3 hours", kind="limit_stop")
    notice = auth_client.get("/apply/status").json()["notice"]
    assert notice["kind"] == "limit_stop" and notice["text"] == "Stopped: ChatGPT limit reached, resets in about 3 hours"
    usage_meter.reset()                                                      # ... and the limit has reset since
    usage_meter._state["loaded"] = True
    assert "It has reset since: press Start to go on" in auth_client.get("/apply/status").json()["notice"]["text"]
    apply_jobs.new_run()
