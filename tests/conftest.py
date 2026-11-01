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

# Run from source, the memory/metrics database sits in the repo root: that is
# the developer's own data. Point it at the session folder before any test
# module imports it; the autouse fixture below then gives each test its own.
import memory.metrics as _mem_metrics  # noqa: E402
import memory.store as _mem_store  # noqa: E402

_mem_store.DB_PATH = SESSION_HOME / "memory_store.db"
_mem_metrics.DB_PATH = SESSION_HOME / "memory_store.db"

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "0:0:0:0:0:0:0:1"}
_BROWSER_EXES = ("chrome", "chromium", "brave", "msedge", "firefox", "vivaldi", "opera")


def live_browser():
    """The executable for opt-in real-browser tests: the bundled Chromium (the app's default) first.

    With USERPROFILE pointing at SESSION_HOME, an installed Google Chrome on Windows never
    answers on its debugging port (CI 2026-10-29: no answer in 90 s; under a real USERPROFILE
    it answers in seconds), so every test using it timed out. The bundled Chromium is unaffected.
    """
    from core.browsers import find_browser, find_playwright_chromium
    return find_playwright_chromium() or find_browser("brave") or find_browser("chrome")


@pytest.fixture(autouse=True)
def _safety_net(request, tmp_path, monkeypatch):
    """Every test: its own memory database, no outside network, no real browsers.

    - memory/metrics databases in the test's tmp folder (never the repo-root DB);
    - sockets may only connect to this machine (tests use fake LLMs and pages);
    - browsers can't be launched (no sign-in windows, no Chromium) unless a test
      opts in with @pytest.mark.real_browser.
    Browser clean-up is already safe: it only matches this app's profiles under
    the (throwaway) data folder.
    """
    import core.shared_config as shared_config

    db = tmp_path / "memory_store.db"
    monkeypatch.setattr(_mem_store, "DB_PATH", db)
    monkeypatch.setattr(_mem_metrics, "DB_PATH", db)
    monkeypatch.setattr(shared_config, "_memory_store", None)

    import socket
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def _check(address):
        host = address[0] if isinstance(address, tuple) else None
        if host is not None and str(host).lower() not in _LOOPBACK:
            raise OSError(f"tests may not reach the network ({host})")

    def connect(self, address):
        _check(address)
        return real_connect(self, address)

    def connect_ex(self, address):
        _check(address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)

    if not request.node.get_closest_marker("real_browser"):
        import subprocess
        real_popen = subprocess.Popen

        class GuardedPopen(real_popen):
            def __init__(self, args, *a, **k):
                first = args[0] if isinstance(args, (list, tuple)) and args else str(args)
                if any(name in Path(str(first)).name.lower() for name in _BROWSER_EXES):
                    raise RuntimeError(f"tests may not launch a browser ({first})")
                super().__init__(args, *a, **k)

        monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
        try:
            from browser_use import BrowserSession

            async def no_start(self, *a, **k):
                raise RuntimeError("tests may not start a real browser session (mark real_browser)")

            monkeypatch.setattr(BrowserSession, "start", no_start)
        except ImportError:
            pass


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


@pytest.fixture(autouse=True)
def _no_posting_fetch(request, monkeypatch):
    """Never fetch real job pages: apply runs and job adds read a posting's page first.

    Tests of job_page.fetch itself opt out with @pytest.mark.real_posting_fetch.
    """
    if request.node.get_closest_marker("real_posting_fetch"):
        return
    import core.job_page as job_page
    monkeypatch.setattr(job_page, "fetch", lambda url, *args, **kwargs: None)
    try:
        import backend.core.job_page as backend_job_page
        monkeypatch.setattr(backend_job_page, "fetch", lambda url, *args, **kwargs: None)
    except ImportError:
        pass


def pytest_configure(config):
    config.addinivalue_line("markers", "real_posting_fetch: let job_page.fetch run (with a fake transport)")
    config.addinivalue_line("markers", "real_browser: allow launching a real browser (never in the default run)")
    config.addinivalue_line("markers", "real_readiness: keep main._start_problem (the start-run readiness check) real")
