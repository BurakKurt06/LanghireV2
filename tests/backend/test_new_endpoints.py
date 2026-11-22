"""The endpoints added for the 2026-11 decisions, through the real app (fake data, temp data folder).

/default-answers (one place to see and change the ordinary answers), /jobs/answers (a job's
waiting questions answered on its card), /logs/job (the card's "View log"), /secrets/reveal (the
eye button next to a saved password or key) and the Dashboard's counts in /jobs/stats.
"""
import pytest

import main
from core import default_answers
from core import shared_config as sc
from tests.backend.test_main_api import AUTH, _isolate_state, auth_client, client  # noqa: F401 (fixtures)

QUESTION = "Are you legally allowed to work in this country?"
ESSAY = "Describe a project you are proud of."


def _job(status="needs_you", **fields):
    url = "https://jobs.example/1"
    sc.write_jobs({url: {"url": url, "title": "Data Intern", "company": "Contoso", "status": status, "source": "pasted",
                         "pending_questions": [{"question": QUESTION, "options": ["Yes", "No"]}, {"question": ESSAY}],
                         **fields}})
    return url


# ── default answers ─────────────────────────────────────────────────────────

def test_default_answers_are_listed_changed_and_put_back(auth_client):
    rows = auth_client.get("/default-answers").json()["answers"]
    assert len(rows) == len(default_answers.DEFAULTS) and all(not r["changed"] for r in rows)
    first = rows[0]
    changed = auth_client.put(f"/default-answers/{first['id']}", json={"answer": "Made-up answer"}).json()
    row = next(r for r in changed["answers"] if r["id"] == first["id"])
    assert (row["answer"], row["changed"]) == ("Made-up answer", True)
    assert main.load_settings()["default_answers"][first["id"]] == "Made-up answer"
    back = auth_client.put(f"/default-answers/{first['id']}", json={"answer": ""}).json()
    row = next(r for r in back["answers"] if r["id"] == first["id"])
    assert (row["answer"], row["changed"]) == (first["default"], False)


def test_default_answers_refuse_unknown_ids_and_non_text(auth_client):
    assert auth_client.put("/default-answers/no_such_answer", json={"answer": "x"}).status_code == 404
    first = default_answers.DEFAULTS[0][0]
    assert auth_client.put(f"/default-answers/{first}", json={"answer": 5}).status_code == 400


# ── a job's waiting questions, answered on its card ─────────────────────────

def test_answering_every_question_makes_the_job_ready_and_saves_the_answers(auth_client):
    url = _job()
    res = auth_client.post("/jobs/answers", json={"url": url, "answers": {QUESTION: "Yes", ESSAY: "A made-up robot."}}).json()
    assert res == {"success": True, "continued": False, "ready": True, "remaining": 0, "may_have_been_sent": False}
    job = sc.read_jobs()[url]
    assert (job["status"], job.get("pending_questions"), job.get("error")) == ("pending", None, None)
    assert main._get_memory_store().qa_user_answers()[QUESTION] == "Yes"     # the user's own answer, for later jobs


def test_a_partial_answer_keeps_the_rest_waiting(auth_client):
    url = _job()
    res = auth_client.post("/jobs/answers", json={"url": url, "answers": {QUESTION: "Yes"}}).json()
    assert (res["ready"], res["remaining"]) == (False, 1)
    job = sc.read_jobs()[url]
    assert job["status"] == "needs_you" and [q["question"] for q in job["pending_questions"]] == [ESSAY]


def test_a_possibly_sent_application_is_never_made_ready_again(auth_client):
    url = _job(submit_attempted_at="2026-11-10T10:00:00+00:00")
    res = auth_client.post("/jobs/answers", json={"url": url, "answers": {QUESTION: "Yes", ESSAY: "x"}}).json()
    assert (res["ready"], res["may_have_been_sent"]) == (False, True)
    assert sc.read_jobs()[url]["status"] == "needs_you"


def test_answers_need_a_known_job_and_some_text(auth_client):
    assert auth_client.post("/jobs/answers", json={"url": "https://jobs.example/none", "answers": {QUESTION: "Yes"}}).status_code == 404
    url = _job()
    assert auth_client.post("/jobs/answers", json={"url": url, "answers": {QUESTION: "  "}}).status_code == 400
    assert sc.read_jobs()[url]["status"] == "needs_you"


def test_a_run_waiting_on_the_job_continues_with_the_answer(auth_client, monkeypatch):
    from cli import apply_jobs
    url = _job()
    given = []
    monkeypatch.setattr(apply_jobs, "answer_help", lambda u, message, give_up, answers: given.append((u, answers)) or True)
    res = auth_client.post("/jobs/answers", json={"url": url, "answers": {QUESTION: "No"}}).json()
    assert res["continued"] is True and given == [(url, {QUESTION: "No"})]
    assert sc.read_jobs()[url]["status"] == "needs_you"      # the run decides what happens next


# ── one job's own log ───────────────────────────────────────────────────────

def test_a_jobs_log_holds_only_its_own_lines_oldest_first(auth_client):
    metrics = main._get_metrics_store()
    metrics.log_batch([("r1", "https://jobs.example/1", "INFO", "first", "2026-11-01T10:00:00+00:00"),
                       ("r1", "https://jobs.example/2", "INFO", "another job", "2026-11-01T10:00:01+00:00"),
                       ("r1", "https://jobs.example/1", "ERROR", "second", "2026-11-01T10:00:02+00:00")])
    lines = auth_client.get("/logs/job", params={"url": "https://jobs.example/1"}).json()
    assert [line["message"] for line in lines] == ["first", "second"]
    assert auth_client.get("/logs/job", params={"url": "https://jobs.example/1", "limit": 1}).json()[0]["message"] == "second"
    assert auth_client.get("/logs/job", params={"url": "https://jobs.example/1", "limit": 0}).status_code == 422


# ── the eye button next to a saved secret ───────────────────────────────────

def test_a_saved_secret_is_shown_in_full_and_nothing_else_can_be_asked_for(auth_client):
    assert auth_client.put("/settings", json={"captcha_api_key": "made-up-key-123"}).status_code == 200
    assert auth_client.get("/settings").json()["captcha_api_key"] != "made-up-key-123"        # the page gets dots
    shown = auth_client.post("/secrets/reveal", json={"kind": "settings", "path": ["captcha_api_key"]}).json()
    assert shown == {"value": "made-up-key-123"}
    # Only the fields the app stores as secrets: not other settings, not made-up kinds.
    assert auth_client.post("/secrets/reveal", json={"kind": "settings", "path": ["resume_path"]}).status_code == 404
    assert auth_client.post("/secrets/reveal", json={"kind": "files", "path": ["captcha_api_key"]}).status_code == 404
    assert auth_client.post("/secrets/reveal", json={"kind": "settings", "path": "captcha_api_key"}).status_code == 404


def test_secrets_are_not_shown_without_the_app_token(client):
    assert client.post("/secrets/reveal", json={"kind": "settings", "path": ["captcha_api_key"]}).status_code == 401


# ── the Dashboard's counts ──────────────────────────────────────────────────

def test_stats_count_every_finished_attempt_that_didnt_apply(auth_client):
    sc.write_jobs({
        "a": {"status": "applied"}, "b": {"status": "applied"},
        "c": {"status": "failed"}, "d": {"status": "blocked"},
        "e": {"status": "needs_you"}, "f": {"status": "needs_you", "submit_attempted_at": "2026-11-10T10:00:00+00:00"},
        "g": {"status": "pending"}, "h": {"status": "closed"},
    })
    stats = auth_client.get("/jobs/stats").json()
    assert (stats["not_successful"], stats["finished"], stats["outcome_unknown"]) == (4, 6, 1)
    assert stats["success_rate"] == 33        # 2 of 6, not "100%" over a list full of errors


def test_no_success_rate_before_the_first_finished_attempt(auth_client):
    sc.write_jobs({"g": {"status": "pending"}})
    assert auth_client.get("/jobs/stats").json()["success_rate"] is None


@pytest.fixture(autouse=True)
def _no_real_requests(monkeypatch):
    # Nothing here reads a page; a slip would only reach the tests' blocked network anyway.
    from core import job_page
    monkeypatch.setattr(job_page, "fetch", lambda *a, **k: None)


# ── pasting links starts nothing ────────────────────────────────────────────

def test_pasting_links_never_starts_an_application(auth_client, monkeypatch):
    from cli import apply_jobs
    started = []
    # Every application run goes through these two (main.start_applying).
    monkeypatch.setattr(apply_jobs, "new_run", lambda *a, **k: started.append(("new_run", a)))
    monkeypatch.setattr(apply_jobs, "worker", lambda *a, **k: started.append(("worker", a)))
    monkeypatch.setattr(main, "_read_posting_names", lambda urls, rendered=True: _noop(), raising=True)
    body = auth_client.post("/jobs/add-bulk", json={"text": "https://jobs.example/a\nhttps://jobs.example/b"}).json()
    assert body["added"] == 2
    assert not main._apply_status.get("running") and started == []
    assert {job["status"] for job in sc.read_jobs().values()} == {"pending"}      # "Ready to apply", waiting for the user


async def _noop():
    return {"read": 0, "rendered": 0}
