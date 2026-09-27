"""The test run must never write to the real app-data folder.

main.py writes .api_token when imported; if that hit the real folder, a
running LangHire app would read the test token and get 401s from its own
backend. tests/conftest.py points the home folder at a throwaway one first.
"""
from pathlib import Path

import conftest


def _is_under(path: Path, folder: Path) -> bool:
    return Path(path).resolve().is_relative_to(Path(folder).resolve())


def test_importing_main_writes_the_token_to_the_session_folder():
    import main
    assert _is_under(main._token_path, conftest.SESSION_HOME)
    assert not _is_under(main._token_path, conftest.REAL_DATA_DIR)
    assert main._token_path.read_text() == main._API_TOKEN


def test_the_real_token_file_is_untouched():
    import main  # noqa: F401  (the import is what used to write it)
    real = conftest.REAL_DATA_DIR / ".api_token"
    now = (real.stat().st_mtime_ns, real.stat().st_size) if real.exists() else None
    assert now == conftest.REAL_TOKEN_STAT


def test_import_time_paths_point_at_the_session_folder():
    import core.shared_config as shared_config
    import memory.store as mem_store
    import memory.metrics as mem_metrics
    assert _is_under(shared_config.DATA_DIR, conftest.SESSION_HOME)
    # From source the memory DB sits in the repo root; only the frozen app uses the data folder.
    for path in (shared_config.DATA_DIR, mem_store.DB_PATH, mem_metrics.DB_PATH):
        assert not _is_under(path, conftest.REAL_DATA_DIR), path

