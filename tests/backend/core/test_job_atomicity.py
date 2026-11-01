"""jobs.json changes are one locked read-change-write: no lost updates.

The collector, the API (add / bulk add / delete / crash recovery) and the
apply workers all change jobs.json. A read_jobs() ... write_jobs() pair saved
its stale copy over whatever another thread or process wrote in between (a
worker's "applied" turned back into "pending").
"""
import threading
import time

import pytest

from cli import collect_jobs
from core import shared_config as sc


@pytest.fixture
def jobs_file(tmp_path, monkeypatch):
    jf = tmp_path / "jobs.json"
    monkeypatch.setattr(sc, "JOBS_FILE", jf)
    monkeypatch.setattr(sc, "JOBS_LOCK", tmp_path / "jobs.json.lock")
    return jf


def _slow_change(started, key):
    def change(jobs):
        started.set()
        time.sleep(0.3)  # another thread tries to write meanwhile
        jobs[key] = {"status": "pending"}
        return "done"
    return change


def test_the_race_is_real_with_a_separate_read_and_write(jobs_file):
    """The old pattern loses a concurrent change (so the next test means something)."""
    sc.write_jobs({"u1": {"status": "in_progress"}})
    started = threading.Event()

    def stale_writer():
        jobs = sc.read_jobs()
        started.set()
        time.sleep(0.3)
        jobs["new"] = {"status": "pending"}
        sc.write_jobs(jobs)

    t = threading.Thread(target=stale_writer)
    t.start()
    started.wait(5)
    sc.update_job("u1", status="applied")
    t.join()
    assert sc.read_jobs()["u1"]["status"] == "in_progress"  # the worker's "applied" was lost


def test_mutate_jobs_keeps_a_concurrent_status_change(jobs_file):
    sc.write_jobs({"u1": {"status": "in_progress"}})
    started = threading.Event()
    result = []
    t = threading.Thread(target=lambda: result.append(sc.mutate_jobs(_slow_change(started, "new"))))
    t.start()
    started.wait(5)
    sc.update_job("u1", status="applied")  # waits for the lock instead of being overwritten
    t.join()
    jobs = sc.read_jobs()
    assert jobs["u1"]["status"] == "applied" and "new" in jobs and result == ["done"]


def test_many_writers_lose_nothing(jobs_file):
    sc.write_jobs({f"w{i}": {"status": "pending", "hits": 0} for i in range(4)})

    def add(i):
        sc.mutate_jobs(lambda jobs: jobs.__setitem__(f"added{i}", {"status": "pending"}))

    def bump(i):
        def change(jobs):
            jobs[f"w{i % 4}"]["hits"] += 1
        sc.mutate_jobs(change)

    threads = [threading.Thread(target=f, args=(i,)) for i in range(20) for f in (add, bump)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    jobs = sc.read_jobs()
    assert sum(1 for k in jobs if k.startswith("added")) == 20
    assert sum(jobs[f"w{i}"]["hits"] for i in range(4)) == 20


def test_a_failed_change_writes_nothing(jobs_file):
    sc.write_jobs({"u1": {"status": "pending"}})

    def broken(jobs):
        jobs.clear()
        raise ValueError("boom")

    with pytest.raises(ValueError):
        sc.mutate_jobs(broken)
    assert sc.read_jobs() == {"u1": {"status": "pending"}}
    sc.update_job("u1", status="applied")  # the lock was released
    assert sc.read_jobs()["u1"]["status"] == "applied"


def test_collector_never_overwrites_an_existing_job(jobs_file):
    url = "https://www.linkedin.com/jobs/view/1234567890/"
    sc.write_jobs({url: {"status": "applied", "title": "Kept", "applied_at": "2026-01-01"}})
    added = collect_jobs.save_new_jobs([
        {"url": url, "title": "Overwrite?", "company": "Acme"},
        {"url": "https://www.linkedin.com/jobs/view/2222222222/", "title": "New", "company": "Acme"},
        {"url": "http://127.0.0.1/jobs/1", "title": "Local"},          # refused
    ], "Analyst", "linkedin")
    jobs = sc.read_jobs()
    assert added == 1
    assert jobs[url] == {"status": "applied", "title": "Kept", "applied_at": "2026-01-01"}
    new = jobs["https://www.linkedin.com/jobs/view/2222222222/"]
    assert new["status"] == "pending" and new["search_title"] == "Analyst" and new["source"] == "linkedin"
    assert "http://127.0.0.1/jobs/1" not in jobs


def test_collector_save_racing_a_worker(jobs_file, monkeypatch):
    """A worker marks a job applied while the collector saves: both changes survive."""
    sc.write_jobs({"u1": {"status": "in_progress"}})
    real = sc.mutate_jobs
    started = threading.Event()

    def slow_mutate(change):
        def wrapped(jobs):
            started.set()
            time.sleep(0.3)
            return change(jobs)
        return real(wrapped)

    monkeypatch.setattr(sc, "mutate_jobs", slow_mutate)
    new = "https://www.linkedin.com/jobs/view/3333333333/"
    t = threading.Thread(target=collect_jobs.save_new_jobs, args=([{"url": new, "title": "T"}], "Analyst", "linkedin"))
    t.start()
    started.wait(5)
    sc.update_job("u1", status="applied")
    t.join()
    jobs = sc.read_jobs()
    assert jobs["u1"]["status"] == "applied" and new in jobs


def test_the_same_url_twice_in_one_delete_is_fine(jobs_file):
    """Codex's case: a repeated URL used to be deleted twice (KeyError, a server error)."""
    url = "https://jobs.example/1"
    sc.write_jobs({url: {"status": "pending"}, "https://jobs.example/2": {"status": "pending"}})

    def delete(jobs):
        gone = [u for u in dict.fromkeys([url, url]) if u in jobs]
        for u in gone:
            del jobs[u]
        return len(gone)

    assert sc.mutate_jobs(delete) == 1 and list(sc.read_jobs()) == ["https://jobs.example/2"]
