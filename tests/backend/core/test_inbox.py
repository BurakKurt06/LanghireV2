"""Inbox: schedule, sorting, matching to jobs, storage, prep sheets (no real Gmail or model)."""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import inbox

JOBS = {
    "https://contoso/shelbyville": {"title": "Intern, Branch Office - Shelbyville, NJ", "company": "Contoso", "status": "applied"},
    "https://contoso/springfield": {"title": "Intern, Branch Office - Springfield, NY", "company": "Contoso", "status": "failed"},
    "https://northwind/sa": {"title": "Operations Summer Intern", "company": "Northwind", "status": "applied"},
}


@pytest.fixture(autouse=True)
def _data(data_dir, monkeypatch):
    monkeypatch.setattr(inbox, "get_data_dir", lambda: data_dir)
    inbox._state.update(checking=False, requested=False, status="", error="")


def test_schedule_and_due():
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    assert not inbox.due({"schedule": "off"}, now)
    assert inbox.due({"schedule": "daily", "last_checked": None}, now)
    assert not inbox.due({"schedule": "daily", "last_checked": (now - timedelta(hours=5)).isoformat()}, now)
    assert inbox.due({"schedule": "daily", "last_checked": (now - timedelta(hours=25)).isoformat()}, now)
    assert not inbox.due({"schedule": "weekly", "last_checked": (now - timedelta(days=3)).isoformat()}, now)
    assert inbox.set_schedule("weekly") and inbox.load()["schedule"] == "weekly"
    assert not inbox.set_schedule("hourly")


def test_a_failed_check_waits_an_hour_before_trying_again():
    now = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)
    data = {"schedule": "daily", "last_checked": None, "last_failed": (now - timedelta(minutes=10)).isoformat()}
    assert not inbox.due(data, now)
    data["last_failed"] = (now - timedelta(hours=2)).isoformat()
    assert inbox.due(data, now)


def test_match_prefers_the_role_and_applied_jobs():
    assert inbox.match_job({"company": "Contoso", "role": "Intern, Branch Office - Springfield, NY"}, JOBS) == "https://contoso/springfield"
    assert inbox.match_job({"company": "Contoso Investments", "role": ""}, JOBS) == "https://contoso/shelbyville"
    assert inbox.match_job({"company": "Contoso", "role": ""}, JOBS) == "https://contoso/shelbyville"
    assert inbox.match_job({"company": "Northwind", "role": "Operations Summer Intern"}, JOBS) == "https://northwind/sa"
    assert inbox.match_job({"company": "Globex", "role": "Intern"}, JOBS) is None
    assert inbox.match_job({"company": "", "role": "Intern"}, JOBS) is None
    # Whole words only: 'Ally' is not 'Rally Health'.
    assert inbox.match_job({"company": "Ally", "role": ""}, {"https://rally": {"company": "Rally Health"}}) is None
    assert inbox.match_job({"company": "A.C. Labs", "role": ""}, {"https://acl": {"company": "AC Labs"}}) == "https://acl"


@pytest.mark.parametrize("text,iso", [
    ("Tue, Oct 6, 2026, 4:12 PM", "2026-10-06T16:12"),
    ("Oct 6, 2026, 4:12 PM", "2026-10-06T16:12"),
    ("Oct 6, 2026", "2026-10-06T00:00"),
    ("yesterday", "yesterday"),
])
def test_gmail_dates(text, iso):
    assert inbox._iso_date(text) == iso


class FakeLLM:
    def __init__(self, labels):
        self.labels, self.prompts = labels, []

    async def ainvoke(self, messages, output_format=None):
        self.prompts.append(messages[0].content)
        return SimpleNamespace(completion=inbox.EmailLabels(labels=[inbox.EmailLabel(**l) for l in self.labels]))


EMAILS = [
    {"id": "a", "sender": "Contoso", "sender_email": "c@contoso.example", "subject": "Thank you for applying", "snippet": "", "date": "Oct 6, 2026", "body": "We received your application."},
    {"id": "b", "sender": "Northwind", "sender_email": "r@northwind.example", "subject": "Update", "snippet": "", "date": "Oct 7, 2026", "body": "Unfortunately..."},
    {"id": "c", "sender": "LinkedIn", "sender_email": "j@linkedin.com", "subject": "30 new jobs", "snippet": "", "date": "Oct 8, 2026", "body": "Apply now"},
]
LABELS = [{"index": 0, "category": "applied", "company": "Contoso", "role": "Intern, Branch Office - Shelbyville, NJ"},
          {"index": 1, "category": "rejected", "company": "Northwind", "role": "Operations Summer Intern"},
          {"index": 2, "category": "other", "company": "", "role": ""}]


def test_classify_keeps_application_emails_only():
    out, labeled = asyncio.run(inbox.classify(FakeLLM(LABELS), EMAILS))
    assert [(e["id"], e["category"]) for e in out] == [("a", "applied"), ("b", "rejected")]
    assert labeled == {"a", "b", "c"}


def test_a_full_check_stores_badges_and_skips_known_mail(monkeypatch):
    async def fake_read(since, known, kwargs):
        return [e for e in EMAILS if e["id"] not in known], "", True

    monkeypatch.setattr(inbox, "read_gmail", fake_read)
    updates = {}
    result = asyncio.run(inbox.run_check(FakeLLM(LABELS), {}, lambda: JOBS, lambda url, **f: updates.update({url: f})))
    assert result == {"error": "", "new": 2, "read": 3}
    assert updates["https://northwind/sa"]["inbox_status"] == "rejected"
    stored = inbox.load()
    assert {e["id"] for e in stored["emails"]} == {"a", "b"} and stored["ignored_ids"] == ["c"]
    assert stored["emails"][0]["gmail_url"].startswith("https://mail.google.com/") and "text" in stored["emails"][0]
    again = asyncio.run(inbox.run_check(FakeLLM([]), {}, lambda: JOBS, lambda url, **f: None))
    assert again == {"error": "", "new": 0, "read": 0}


def test_a_signed_out_gmail_is_reported(monkeypatch):
    async def signed_out(since, known, kwargs):
        return [], "Gmail isn't signed in. Sign in to Gmail in Settings → Accounts, then check again.", False

    monkeypatch.setattr(inbox, "read_gmail", signed_out)
    result = asyncio.run(inbox.run_check(FakeLLM([]), {}, lambda: {}, lambda url, **f: None))
    assert "Settings → Accounts" in result["error"] and inbox.load()["last_checked"] is None


def test_first_check_looks_back_sixty_days(monkeypatch):
    seen = {}

    async def fake_read(since, known, kwargs):
        seen["since"] = since
        return [], "", True

    monkeypatch.setattr(inbox, "read_gmail", fake_read)
    asyncio.run(inbox.run_check(FakeLLM([]), {}, lambda: {}, lambda url, **f: None))
    assert 59 <= (datetime.now(timezone.utc) - seen["since"]).days <= 60


def test_search_url_is_job_mail_since_a_date():
    url = inbox.search_url(datetime(2026, 8, 9, tzinfo=timezone.utc))
    assert url.startswith("https://mail.google.com/mail/u/0/#search/after%3A2026%2F08%2F09")
    assert "in%3Asent" in url and "interview" in url


def test_prep_sheet_prompt_and_storage():
    inbox.save({"emails": [{"id": "x", "category": "interview", "company": "Stark Global", "role": "Quant Intern", "text": "30-minute call"}],
                "schedule": "off", "last_checked": None})

    class TextLLM:
        async def ainvoke(self, messages):
            assert "30-minute call" in messages[0].content and "don't invent facts" in messages[0].content
            return SimpleNamespace(completion="## THE INTERVIEW\n- **30 minutes**")

    text = asyncio.run(inbox.write_prep_sheet(TextLLM(), inbox.find("x"), None, "", "", {"name": "Ada"}))
    assert text == "THE INTERVIEW\n- 30 minutes"
    assert inbox.save_prep("x", text) and inbox.find("x")["prep"] == text
    assert not inbox.save_prep("nope", text)


def test_scheduler_runs_a_request_only_when_not_busy(monkeypatch):
    import threading
    calls, busy, stop = [], {"v": True}, threading.Event()
    inbox.request_check()

    def check():
        calls.append(1)
        stop.set()
        return {"error": "", "new": 2}

    def is_busy():
        result = busy["v"]
        busy["v"] = False   # busy on the first look, free on the next
        return result

    thread = inbox.start_scheduler(is_busy, check, interval_seconds=0, stop=stop)
    thread.join(timeout=5)
    assert calls == [1] and inbox._state["status"] == "Found 2 new job email(s)."


class BrokenLLM:
    async def ainvoke(self, messages, output_format=None):
        raise TimeoutError("the model didn't answer")


def test_emails_the_ai_couldnt_sort_are_read_again_next_time(monkeypatch):
    windows = []

    async def fake_read(since, known, kwargs):
        windows.append(since)
        return [e for e in EMAILS if e["id"] not in known], "", True

    monkeypatch.setattr(inbox, "read_gmail", fake_read)
    asyncio.run(inbox.run_check(BrokenLLM(), {}, lambda: JOBS, lambda url, **f: None))
    stored = inbox.load()
    assert stored["emails"] == [] and stored.get("ignored_ids", []) == []
    assert stored["search_from"] == windows[0].isoformat()  # the same window again
    result = asyncio.run(inbox.run_check(FakeLLM(LABELS), {}, lambda: JOBS, lambda url, **f: None))
    assert result["new"] == 2 and windows[1] == windows[0]
    assert inbox.load()["search_from"] is None


def test_an_unfinished_window_is_read_again(monkeypatch):
    windows = []

    async def capped(since, known, kwargs):
        windows.append(since)
        return [], "", False

    monkeypatch.setattr(inbox, "read_gmail", capped)
    asyncio.run(inbox.run_check(FakeLLM([]), {}, lambda: {}, lambda url, **f: None))
    asyncio.run(inbox.run_check(FakeLLM([]), {}, lambda: {}, lambda url, **f: None))
    assert windows[0] == windows[1] and inbox.load()["last_checked"]  # still due on schedule, same window


def test_a_job_shows_its_latest_email(monkeypatch):
    newer_first = [
        {"id": "i", "sender": "Contoso", "subject": "Interview", "date": "Oct 6, 2026", "body": "Let's talk."},
        {"id": "r", "sender": "Contoso", "subject": "Received", "date": "Sep 20, 2026", "body": "Thanks."},
    ]
    labels = [{"index": 0, "category": "interview", "company": "Contoso", "role": "Intern, Branch Office - Shelbyville, NJ"},
              {"index": 1, "category": "applied", "company": "Contoso", "role": "Intern, Branch Office - Shelbyville, NJ"}]

    async def fake_read(since, known, kwargs):
        return newer_first, "", True

    monkeypatch.setattr(inbox, "read_gmail", fake_read)
    jobs = {url: dict(job) for url, job in JOBS.items()}
    updates = []
    asyncio.run(inbox.run_check(FakeLLM(labels), {}, lambda: jobs, lambda url, **f: updates.append((url, f))))
    assert updates[-1][1]["inbox_status"] == "interview"

    # A later check that turns up an older email doesn't overwrite it either.
    jobs["https://contoso/shelbyville"]["inbox_date"] = "2026-10-06T00:00"
    older = [{"id": "o", "sender": "Contoso", "subject": "Old", "date": "Sep 1, 2026", "body": "Thanks."}]

    async def read_older(since, known, kwargs):
        return older, "", True

    monkeypatch.setattr(inbox, "read_gmail", read_older)
    updates.clear()
    asyncio.run(inbox.run_check(FakeLLM([{**labels[1], "index": 0}]), {}, lambda: jobs,
                                lambda url, **f: updates.append((url, f))))
    assert updates == []


def test_older_result_pages_use_before():
    from datetime import date
    url = inbox.search_url(datetime(2026, 8, 9, tzinfo=timezone.utc), date(2026, 9, 30))
    assert "after%3A2026%2F08%2F09%20before%3A2026%2F09%2F30" in url
    assert inbox._oldest_day([{"date": "Oct 6, 2026, 4:12 PM"}, {"date": "Sep 3, 2026"}, {"date": "yesterday"}]) == date(2026, 9, 3)


def test_the_scheduler_gives_way_to_a_run_that_just_started():
    calls = []
    inbox._state.update(requested=True)
    inbox._check_now(lambda: calls.append(1) or {"new": 0}, is_busy=lambda: True)
    assert calls == [] and inbox._state["requested"] and not inbox._state["checking"]
