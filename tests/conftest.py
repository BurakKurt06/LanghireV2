"""
Shared pytest fixtures for the LangHireV2 backend test suite.

The backend modules import each other with a `backend/`-rooted style
(e.g. `from core.config import ...`), so we put `backend/` on sys.path here.
We also isolate every test from the real OS app-data directory by pointing
HOME at a per-test temporary directory.
"""
import atexit
import os
import platform
import shutil
import sys
import tempfile
from pathlib import Path

import pytest


# ── Keep the whole run out of the real app-data folder ──────────────────────
# Some backend modules write to get_data_dir() as soon as they are imported,
# before any fixture runs: main.py writes .api_token and opens backend.log,
# shared_config resolves DATA_DIR. Test modules import them at collection
# time, so a running LangHire app would get its token overwritten and 401 on
# its own backend. Point the home folder at a throwaway one before that.
def _data_dir_under(home: Path) -> Path:
    """Where core.config.get_data_dir() puts the data folder for `home` (without creating it)."""
    system = platform.system()
    if system == "Darwin":
        return home / "Library" / "Application Support" / "langhirev2"
    if system == "Windows":
        return home / "AppData" / "Roaming" / "langhirev2"
    return home / ".config" / "langhirev2"


REAL_DATA_DIR = _data_dir_under(Path.home())
# Snapshot of the real token file, for the test that checks it is never written.
REAL_TOKEN_STAT = (lambda p: (p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None)(REAL_DATA_DIR / ".api_token")
SESSION_HOME = Path(tempfile.mkdtemp(prefix="langhire-tests-"))
os.environ["HOME"] = str(SESSION_HOME)
os.environ["USERPROFILE"] = str(SESSION_HOME)  # Path.home() on Windows
atexit.register(shutil.rmtree, SESSION_HOME, ignore_errors=True)

# ── Make `backend/` importable as a top-level package root ──────────────────
# Modules do `from core.config import ...`, so the *backend* dir must be on path.
ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
for p in (str(ROOT), str(BACKEND)):
    if p not in sys.path:
        sys.path.insert(0, p)


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """
    Redirect the app data directory to an isolated tmp dir for the test.

    `core.config.get_data_dir()` derives its location from `Path.home()`, so
    pointing HOME (and the macOS/Windows equivalents) at tmp_path gives each
    test a clean, writable, throwaway config root.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))  # Windows
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    # Return the resolved data dir so tests can assert on files directly.
    import core.config as config
    return config.get_data_dir()
