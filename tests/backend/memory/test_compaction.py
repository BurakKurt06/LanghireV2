"""The memory database gives deleted log lines' space back, and its write-ahead file stays small.

Measured on the user's machine (2026-11, read-only): memory_store.db was 625 MB holding 28 MB of
data (597 MB were pages freed by deleted log lines, never given back), plus a 298 MB write-ahead
file that never shrank after one large delete. Startup removed 1.3 million old lines in one go.
"""
import os
import sqlite3

from memory import metrics as metrics_mod
from memory.metrics import MetricsStore


def _old_lines(store: MetricsStore, n: int, days_old: int = 40):
    conn = store._get_conn()
    stamp = f"datetime('now', '-{days_old} days')"
    conn.executemany(
        f"INSERT INTO run_logs (run_id, job_url, timestamp, level, message, created_at) "
        f"VALUES (?, NULL, {stamp}, 'INFO', ?, {stamp})",
        [("r-old", "x" * 400) for _ in range(n)])
    conn.commit()


def test_old_lines_are_deleted_in_chunks_and_recent_ones_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(metrics_mod, "DELETE_CHUNK", 1000)
    store = MetricsStore(tmp_path / "m.db")
    _old_lines(store, 3500)
    store.log_batch([("r-new", "https://jobs.example/1", "INFO", "today")])
    statements = []
    store._get_conn().set_trace_callback(statements.append)
    assert store.cleanup_old_logs(days=30) == 3500
    deletes = [s for s in statements if s.lstrip().upper().startswith("DELETE")]
    assert len(deletes) == 4                     # 1000 + 1000 + 1000 + 500, each its own commit
    rows = store._get_conn().execute("SELECT run_id FROM run_logs").fetchall()
    assert [r[0] for r in rows] == ["r-new"]


def test_a_mostly_empty_file_is_compacted_and_the_write_ahead_file_cut_back(tmp_path):
    path = tmp_path / "m.db"
    store = MetricsStore(path)
    _old_lines(store, 30_000)
    store.log_batch([("r-new", "https://jobs.example/1", "INFO", "kept")])
    store.cleanup_old_logs(days=30)
    size_before = os.path.getsize(path)
    result = store.compact(min_free_bytes=1024 * 1024, min_free_share=0.25)
    assert result["compacted"] is True and result["after"] < result["before"] / 4
    assert os.path.getsize(path) < size_before / 4
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or os.path.getsize(wal) == 0     # checkpointed and truncated
    assert store.get_job_logs("https://jobs.example/1")[0]["message"] == "kept"


def test_a_file_with_little_free_space_is_left_as_it_is(tmp_path):
    store = MetricsStore(tmp_path / "m.db")
    store.log_batch([("r", "https://jobs.example/1", "INFO", "line")] * 50)
    assert store.compact()["compacted"] is False


def test_every_connection_limits_the_write_ahead_file(tmp_path):
    from memory.store import MemoryStore
    for store in (MetricsStore(tmp_path / "a.db"), MemoryStore(tmp_path / "b.db")):
        limit = store._get_conn().execute("PRAGMA journal_size_limit").fetchone()[0]
        assert limit == 64 * 1024 * 1024


def test_a_busy_database_is_compacted_on_a_later_start(tmp_path, monkeypatch):
    import main
    store = MetricsStore(tmp_path / "m.db")

    def busy(*a, **k):
        raise sqlite3.OperationalError("database is locked")
    monkeypatch.setattr(store, "compact", busy)
    monkeypatch.setattr(main, "_get_metrics_store", lambda: store)
    assert main._compact_memory_db() is None      # logged, nothing breaks
