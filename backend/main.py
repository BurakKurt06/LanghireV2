"""
Python backend for LangHireV2 desktop app.
Runs as a local FastAPI server (sidecar process launched by Tauri).
"""
import os as _os

# browser-use sends each run's task (with the resume), the values typed into
# forms and the pages visited to its analytics service unless this is off.
_os.environ["ANONYMIZED_TELEMETRY"] = "false"
_os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
import json
import re
import signal
import subprocess
import sys
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Optional

# `python backend/main.py` must also be able to import the sibling cli package.
# Frozen builds already expose their bundled root on sys.path.
_SOURCE_ROOT = str(Path(__file__).resolve().parent.parent)
if not getattr(sys, "frozen", False) and _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

# Windows consoles default to cp1252, which can't encode the emoji browser-use
# logs: every such line printed a "--- Logging error ---" traceback into the
# Output Log. Write UTF-8 instead (replacing anything unencodable).
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError, OSError):
        pass

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

try:
    from models import CollectRequest, ApplyRequest, DecayRequest, CleanupRequest, AddJobsBulkRequest
except ImportError:
    from backend.models import CollectRequest, ApplyRequest, DecayRequest, CleanupRequest, AddJobsBulkRequest

from core.config import get_data_dir, load_settings, save_settings, load_profile, save_profile
from core.config import load_llm_settings, save_llm_settings

# Project root for data files
PROJECT_ROOT = Path(__file__).parent.parent
DEFAULT_PORT = 8743
MAX_LOG_LINES = 500
CREDENTIAL_REFRESH_MINUTES = 14


# ── Lifespan ──────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup and shutdown logic."""
    _log.info("Backend starting (build 20260425-v3)...")
    data_dir = get_data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)
    _log.info(f"Data directory: {data_dir}")
    _log.info("API token ready")
    # Secrets saved in plain by an older version: encrypted now (each file only after it reads back).
    from core.config import migrate_secrets
    if migrate_secrets():
        _log.info("Encrypted saved passwords and keys")
    # Clean up orphaned browser processes from previous crash
    _kill_browser_processes()
    # Reset any jobs stuck in "in_progress" from a previous crash
    _recover_stale_jobs()
    # Clean up old run logs (>30 days)
    _metrics = _get_metrics_store()
    if _metrics:
        cleaned = _metrics.cleanup_old_logs(days=30)
        if cleaned:
            _log.info(f"Cleaned up {cleaned} old log entries")
    # Auto-install Chromium if needed (runs in background so /health is available immediately)
    _ensure_chromium_async()
    # Start parent-process watchdog (self-terminate if Tauri app dies)
    _start_parent_watchdog()
    # Scheduled / requested Gmail checks (never during an application run)
    _start_inbox_scheduler()
    yield
    # Graceful shutdown: stop running operations
    import traceback
    _log.info(f"Backend shutting down... (trigger: lifespan exit)")
    _log.info(f"Shutdown stack:\n{''.join(traceback.format_stack())}")
    _stop_operations_and_browsers()


def _stop_operations_and_browsers():
    """Stop running operations, give workers a moment to clean up, then close this app's browsers."""
    for status_dict in (_collection_status, _apply_status):
        if status_dict.get("running"):
            _force_stop(status_dict)
    import time
    time.sleep(2)
    _kill_browser_processes()

app = FastAPI(title="LangHireV2 Backend", version="1.0.0", lifespan=lifespan)


# ── Auth token (generated per-session, written to data dir for frontend) ──
import secrets as _secrets
import logging
from logging.handlers import RotatingFileHandler

_LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
# basicConfig ignores supplied handlers when a host already configured logging.
# Constructing a file handler first would still open a file, then discard it
# without closing it (notably when main is embedded or imported by pytest).
if not logging.getLogger().handlers:
    logging.basicConfig(
        level=logging.INFO,
        format=_LOG_FORMAT,
        handlers=[
            RotatingFileHandler(
                get_data_dir() / "backend.log",
                maxBytes=5 * 1024 * 1024,
                backupCount=3,
            ),
            logging.StreamHandler(),
        ],
    )


# Secrets out of every log line, tracebacks included.
from core.shared_config import RedactingFormatter as _RedactingFormatter

for _handler in logging.getLogger().handlers:
    _handler.setFormatter(_RedactingFormatter(_LOG_FORMAT))
# Silence noisy low-level loggers but keep browser_use agent logs visible
for _noisy in (
    "browser_use.dom", "browser_use.browser.chrome",
    "browser_use.browser.cdp", "browser_use.browser.session",
    "httpx", "httpcore", "urllib3", "filelock",
    "websockets", "charset_normalizer",
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)
_log = logging.getLogger("backend")

_API_TOKEN = os.environ.get("JOB_APPLICANT_TOKEN") or _secrets.token_hex(32)

# Write token to data dir so the Tauri frontend can read it
import stat as _stat
_token_path = get_data_dir() / ".api_token"
_token_path.parent.mkdir(parents=True, exist_ok=True)
_token_path.write_text(_API_TOKEN)
try:
    os.chmod(_token_path, _stat.S_IRUSR | _stat.S_IWUSR)
except OSError as e:
    _log.warning(f"Could not set restrictive permissions on token file: {e}")
# Verify token was written correctly
_written = _token_path.read_text().strip()
_log.info(f"Token written to {_token_path} (verified: {_written == _API_TOKEN})")

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
import time as _time

class _RateLimiter:
    """Simple per-path rate limiter using a sliding window."""
    def __init__(self, max_calls: int = 30, window_seconds: int = 60):
        self._max = max_calls
        self._window = window_seconds
        self._calls: dict[str, list[float]] = {}

    def is_allowed(self, path: str) -> bool:
        now = _time.monotonic()
        calls = self._calls.setdefault(path, [])
        calls[:] = [t for t in calls if now - t < self._window]
        if len(calls) >= self._max:
            return False
        calls.append(now)
        return True

_rate_limiter = _RateLimiter(max_calls=60, window_seconds=60)
_rate_limited_prefixes = ("/jobs/collect", "/apply/start", "/apply/stop", "/llm/test", "/auth/login", "/chatgpt/login")

_ALLOWED_ORIGINS = {"http://localhost:1420", "tauri://localhost", "http://tauri.localhost", "https://tauri.localhost", "http://127.0.0.1:1420"}

class _AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        _log.info(f"REQ {request.method} {request.url.path} origin='{request.headers.get('Origin', '')}' auth={'yes' if request.headers.get('Authorization') else 'no'}")
        origin = request.headers.get("Origin", "")
        if origin and origin not in _ALLOWED_ORIGINS:
            # Before the token-free paths too: no web page can see the app is installed.
            _log.warning(f"403 blocked origin: '{origin}' not in {_ALLOWED_ORIGINS}")
            return JSONResponse({"error": "forbidden"}, status_code=403)
        if request.url.path in ("/health", "/chromium/status"):
            return await call_next(request)
        auth_header = request.headers.get("Authorization", "")
        token = auth_header.removeprefix("Bearer ").strip()
        if token != _API_TOKEN:
            _log.warning(f"401 {request.method} {request.url.path}")
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if any(request.url.path.startswith(p) for p in _rate_limited_prefixes):
            if not _rate_limiter.is_allowed(request.url.path):
                return JSONResponse({"error": "rate limited"}, status_code=429)
        return await call_next(request)

app.add_middleware(_AuthMiddleware)


class _LoopbackHostMiddleware(TrustedHostMiddleware):
    """Validate the entire authority, not just the part before its first colon."""
    async def __call__(self, scope, receive, send):
        if scope["type"] in ("http", "websocket"):
            hosts = [value.decode("latin-1") for key, value in scope.get("headers", []) if key.lower() == b"host"]
            match = re.fullmatch(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", hosts[0]) if len(hosts) == 1 else None
            if match is None or (match.group(1) and not 1 <= int(match.group(1)) <= 65535):
                await PlainTextResponse("Invalid host header", status_code=400)(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


# Allow frontend to connect
app.add_middleware(
    CORSMiddleware,
    allow_origins=sorted(_ALLOWED_ORIGINS),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
# Registered last to run before CORS, including preflight requests. A page whose name resolves to
# loopback still sends its own Host and must be refused before any middleware returns a response.
app.add_middleware(_LoopbackHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"])


# ── Helpers ───────────────────────────────────────────────────────────────

def _watched_parent_pid() -> int:
    """PID of the app that launched us.

    A PyInstaller one-file build runs as two processes: a bootloader, then the
    real interpreter as its child, both from the same exe. Our direct parent is
    then the bootloader, which lives exactly as long as we do, so watching it
    never fires. Skip over it to the Tauri app.
    """
    parent_pid = os.getppid()
    if getattr(sys, "frozen", False):
        try:
            import psutil
            parent = psutil.Process(parent_pid)
            if parent.name().lower() == psutil.Process().name().lower():
                grandparent = parent.ppid()
                if grandparent > 1:
                    return grandparent
        except Exception:
            pass
    return parent_pid


def _is_backend_process(proc) -> bool:
    """True only for LangHire backend processes, never anything else on the port."""
    try:
        name = proc.name().lower()
        if "langhirev2-backend" in name:
            return True
        cmdline = " ".join(proc.cmdline()).lower()
        return "python" in name and "main.py" in cmdline and "backend" in cmdline
    except Exception:
        return False


def _kill_stale_backend(port: int) -> None:
    """Stop a leftover LangHire backend still holding our port (e.g. after an upgrade)."""
    import psutil
    me = psutil.Process()
    mine = {me.pid, me.ppid()}
    victims = {}
    for conn in psutil.net_connections(kind="tcp"):
        if conn.laddr and conn.laddr.port == port and conn.status == psutil.CONN_LISTEN and conn.pid:
            if conn.pid in mine:
                continue
            try:
                proc = psutil.Process(conn.pid)
            except psutil.NoSuchProcess:
                continue
            if not _is_backend_process(proc):
                _log.warning(f"Port {port} is held by PID {conn.pid} ({proc.name()}), not a LangHire backend; leaving it")
                continue
            victims[proc.pid] = proc
            # Take its one-file bootloader too, so nothing respawns or lingers.
            try:
                parent = proc.parent()
                if parent and parent.pid not in mine and _is_backend_process(parent):
                    victims[parent.pid] = parent
            except psutil.NoSuchProcess:
                pass
    for proc in victims.values():
        _log.info(f"Killing stale backend on port {port}: PID {proc.pid}")
        try:
            proc.kill()
        except psutil.NoSuchProcess:
            pass
    psutil.wait_procs(list(victims.values()), timeout=5)


def _start_parent_watchdog():
    """Exit if the parent process (Tauri app) dies — prevents orphaned sidecar."""
    parent_pid = _watched_parent_pid()
    if parent_pid <= 1:
        _log.info(f"Parent watchdog skipped (ppid={parent_pid}, already orphaned or init)")
        return

    def _watch():
        import time as _time
        try:
            import psutil
            parent = psutil.Process(parent_pid)
        except Exception:
            parent = None

        # Grace period: wait 10s before starting checks.
        _time.sleep(10)
        
        while True:
            _time.sleep(5)
            if parent:
                if not parent.is_running():
                    break
            else:
                # Fallback to os.getppid() if psutil fails
                if os.getppid() != parent_pid:
                    break
        
        _on_parent_exit()

    t = threading.Thread(target=_watch, daemon=True, name="parent-watchdog")
    t.start()
    _log.info(f"Parent watchdog started (monitoring PID {parent_pid})")


def _on_parent_exit():
    """The app (our parent) is gone: shut down as the lifespan would, then end this process.

    The SIGTERM below ends the process without running lifespan shutdown on Windows
    (TerminateProcess) and in the packaged app (no signal handlers): this app's browsers,
    and a running collection or application, would otherwise be left behind.
    """
    _log.info("Parent process died — shutting down backend")
    try:
        # An open sign-in window is the user's (no run can be going meanwhile): a forced close
        # can lose a sign-in not yet saved to disk, so it stays for them to finish and close.
        if not (_login_process is not None and _login_process.poll() is None):
            _stop_operations_and_browsers()
    finally:
        os.kill(os.getpid(), signal.SIGTERM)


INTERRUPTED_NOTE = "Interrupted: the app closed during this application. It will be tried again on the next run."


def _recover_stale_jobs():
    """Reset jobs stuck in 'in_progress' back to 'pending' (crash recovery)."""
    try:
        from core.shared_config import mutate_jobs
    except ImportError:
        from backend.core.shared_config import mutate_jobs
    from cli.apply_jobs import OUTCOME_UNKNOWN_NOTE

    def recover(jobs):
        recovered = 0
        for job in jobs.values():
            if job.get("status") == "in_progress":
                if job.get("submit_attempted_at"):
                    # Submit was clicked before the app closed: it may have been sent. Never retried
                    # automatically; the user checks and decides.
                    job["status"] = "needs_you"
                    job["error"] = OUTCOME_UNKNOWN_NOTE.format(why="the app closed right after")
                else:
                    job["status"] = "pending"
                    job["error"] = INTERRUPTED_NOTE
                recovered += 1
        return recovered

    recovered = mutate_jobs(recover)
    if recovered:
        _log.info(f"Recovered {recovered} stale in_progress jobs")


def _api_error(code: str, message: str, status_code: int = 400) -> JSONResponse:
    """Return a structured error response."""
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message}},
        status_code=status_code,
    )


def _text(body: dict, key: str, limit: int = 2000) -> str:
    """A string field of a JSON body, trimmed and capped; "" when missing or not a string.

    The raw-dict endpoints used to call .strip() on whatever came in, so a number
    or a list (a bad client, a stale page) was a 500 instead of a clear 400.
    """
    value = body.get(key) if isinstance(body, dict) else None
    return value.strip()[:limit] if isinstance(value, str) else ""


def _str_list(value, limit: int = 1000) -> list[str] | None:
    """A list of strings, or None when `value` is anything else."""
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(v, str) for v in value):
        return None
    return value


def _get_jobs_file() -> Path:
    """Find jobs.json — try data dir first, then project root."""
    data_dir = get_data_dir()
    if (data_dir / "jobs.json").exists():
        return data_dir / "jobs.json"
    if (PROJECT_ROOT / "jobs.json").exists():
        return PROJECT_ROOT / "jobs.json"
    return data_dir / "jobs.json"


def _load_jobs() -> dict:
    path = _get_jobs_file()
    if path.exists():
        return json.loads(path.read_text())
    return {}


def _get_memory_store():
    """Get the memory store, falling back gracefully."""
    try:
        from memory.store import MemoryStore
        return MemoryStore()
    except (ImportError, FileNotFoundError, OSError) as e:
        _log.warning(f"Memory store unavailable: {e}")
        return None


def _get_metrics_store():
    """Get the metrics store, falling back gracefully."""
    try:
        from memory.metrics import MetricsStore
        return MetricsStore()
    except (ImportError, FileNotFoundError, OSError) as e:
        _log.warning(f"Metrics store unavailable: {e}")
        return None


# ── Playwright Chromium Discovery ─────────────────────────────────────────
def _find_playwright_chromium() -> str | None:
    """Use the same executable discovery as the collection browser."""
    from core.browsers import find_playwright_chromium
    return find_playwright_chromium()

# ── Chromium Auto-Install ─────────────────────────────────────────────────
_chromium_status: dict = {"state": "checking", "message": "Checking for Chromium..."}


def _get_playwright_install_cmd() -> list[str] | None:
    """Build the command to run `playwright install chromium`.
    Works in both frozen (PyInstaller) and development modes."""
    # Use the bundled Playwright driver (node + cli.js)
    try:
        from playwright._impl._driver import compute_driver_executable
        result = compute_driver_executable()
        if isinstance(result, tuple):
            node_bin, cli_js = result
            if os.path.exists(node_bin) and os.path.exists(cli_js):
                return [str(node_bin), str(cli_js), "install", "chromium"]
        elif os.path.exists(str(result)):
            return [str(result), "install", "chromium"]
    except (ImportError, Exception) as e:
        _log.warning(f"Bundled Playwright driver not usable: {e}")

    # Fallback: try system playwright
    import shutil
    if shutil.which("playwright"):
        return ["playwright", "install", "chromium"]
    for python in ["python3", "python"]:
        if shutil.which(python):
            try:
                r = subprocess.run([python, "-m", "playwright", "--version"],
                                   capture_output=True, text=True, timeout=10)
                if r.returncode == 0:
                    return [python, "-m", "playwright", "install", "chromium"]
            except Exception:
                pass
    return None


def _ensure_chromium_async():
    """Check if Playwright Chromium is installed, install in background if not.
    Updates _chromium_status so the frontend can poll progress."""
    global _chromium_status

    if _find_playwright_chromium() is not None:
        _chromium_status = {"state": "ready", "message": "Chromium found"}
        _log.info("Chromium found")
        return

    _chromium_status = {"state": "installing", "message": "Installing Chromium (one-time download, ~400MB)..."}
    _log.info("Chromium not found — installing in background")

    def _install():
        global _chromium_status
        try:
            cmd = _get_playwright_install_cmd()
            if not cmd:
                _chromium_status = {"state": "failed", "message": "Could not find Playwright installer. Please reinstall the application."}
                _log.error("No working Playwright install command found")
                return
            _log.info(f"Chromium install command: {cmd}")
            _chromium_status = {"state": "installing", "message": "Downloading Chromium browser (~162 MB). This may take a few minutes, please don't close the app..."}

            # Monitor the download directory for progress instead of parsing stdout
            # (Playwright suppresses progress bars when stdout is not a TTY)
            import re as _re
            cache_dir = Path.home() / "Library" / "Caches" / "ms-playwright"
            if sys.platform == "win32":
                local_app = os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
                cache_dir = Path(local_app) / "ms-playwright"
            elif sys.platform != "darwin":
                cache_dir = Path.home() / ".cache" / "ms-playwright"

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            )

            # Poll the download directory size while process runs
            while proc.poll() is None:
                import time as _t
                _t.sleep(3)
                # Check for partial download files
                try:
                    total_bytes = 0
                    for f in cache_dir.rglob("*"):
                        if f.is_file():
                            total_bytes += f.stat().st_size
                    mb = total_bytes / (1024 * 1024)
                    if mb > 1:
                        _chromium_status = {"state": "installing", "message": f"Downloading Chromium... {mb:.0f} MB downloaded. Please don't close the app."}
                except Exception:
                    pass

            # Read any remaining output
            out = proc.stdout.read()
            if out:
                text = out.decode("utf-8", errors="replace").strip()
                clean = _re.sub(r'\x1b\[[0-9;]*m', '', text)
                for line in clean.splitlines():
                    if line.strip():
                        _log.info(f"Chromium: {line.strip()}")
            if proc.returncode == 0:
                _chromium_status = {"state": "ready", "message": "Chromium installed successfully"}
                _log.info("Chromium installed successfully")
            else:
                cur_msg = _chromium_status.get("message", "Unknown error")
                _chromium_status = {"state": "failed", "message": f"Install failed: {cur_msg}"}
                _log.error(f"Chromium install failed (code {proc.returncode}): {cur_msg}")
        except Exception as e:
            msg = str(e)
            _chromium_status = {"state": "failed", "message": f"Install failed: {msg[:200]}"}
            _log.error(f"Chromium install error: {e}")

    threading.Thread(target=_install, daemon=True, name="chromium-install").start()


@app.get("/chromium/status")
async def chromium_status():
    return _chromium_status


# ── Health ────────────────────────────────────────────────────────────────
@app.get("/health")
async def health():
    db_ok = False
    try:
        store = _get_memory_store()
        if store:
            store.get_stats()
            db_ok = True
    except Exception:
        pass

    chromium_ok = _chromium_status.get("state") == "ready"
    chromium_installing = _chromium_status.get("state") == "installing"
    if not chromium_ok and not chromium_installing:
        try:
            chromium_ok = _find_playwright_chromium() is not None
        except Exception:
            pass

    llm_configured = bool(load_llm_settings().get("provider"))
    worker_running = _collection_status.get("running") or _apply_status.get("running")

    all_ok = db_ok and chromium_ok and llm_configured
    return {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "frozen": getattr(sys, 'frozen', False),
        "checks": {
            "database": db_ok,
            "chromium": chromium_ok,
            "chromium_installing": chromium_installing,
            "llm_configured": llm_configured,
            "worker_running": bool(worker_running),
        },
    }


# ── Profile ───────────────────────────────────────────────────────────────
@app.get("/profile")
async def get_profile():
    return load_profile()

_PROFILE_OBJECTS = ("address", "education", "salary_expectation", "screening", "search_area", "job_search")
_PROFILE_LISTS = ("target_job_titles", "target_locations", "languages", "skills")


@app.put("/profile")
async def update_profile(profile: dict):
    # The agent reads these shapes: a wrong one (a string for the address) is refused, not saved.
    for key in _PROFILE_OBJECTS:
        if key in profile and not isinstance(profile[key], dict):
            return _api_error("invalid_profile", f"{key} must be an object", 400)
    for key in _PROFILE_LISTS:
        if key in profile and _str_list(profile[key]) is None:
            return _api_error("invalid_profile", f"{key} must be a list of text", 400)
    if "job_search" in profile:
        from core import job_search
        profile["job_search"] = job_search.normalize(profile["job_search"])
    save_profile(profile)
    return {"success": True}


# ── LLM Settings ──────────────────────────────────────────────────────────
@app.get("/settings/llm")
async def get_llm():
    """The AI settings, with each saved key as SECRET_MASK (the page never gets the keys)."""
    from core.config import LLM_SECRETS, mask_secrets
    return mask_secrets(load_llm_settings(), LLM_SECRETS)

@app.put("/settings/llm")
async def update_llm(settings: dict):
    from core.config import LLM_SECRETS, keep_masked
    save_llm_settings(keep_masked(settings, load_llm_settings(), LLM_SECRETS))
    return {"success": True}


_MODEL_LISTS = {
    "openai": ("https://api.openai.com/v1/models", lambda key: {"Authorization": f"Bearer {key}"}),
    "anthropic": ("https://api.anthropic.com/v1/models?limit=1000",
                  lambda key: {"x-api-key": key, "anthropic-version": "2023-06-01"}),
    "gemini": ("https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000", lambda key: {"x-goog-api-key": key}),
    "openrouter": ("https://openrouter.ai/api/v1/models", lambda key: {}),
}


@app.post("/llm/models")
async def list_provider_models(body: dict):
    """A provider's model list (its JSON as is), fetched here rather than by the page.

    The app's content policy blocks the page from calling AI providers (so live
    lists never loaded in the installed app), and the saved key stays here: a
    masked key means "the saved one".
    """
    import httpx
    from core.config import SECRET_MASK
    from core.shared_config import redact_secrets
    provider = str(body.get("provider") or "")
    stored = load_llm_settings().get(provider) or {}
    key = str(body.get("api_key") or "").strip()
    if not key or key == SECRET_MASK:
        key = str(stored.get("api_key") or "")
    if provider == "openai_compatible":
        base = str(body.get("base_url") or stored.get("base_url") or "").strip().rstrip("/")
        if not base.lower().startswith(("http://", "https://")):
            return _api_error("invalid_url", "The server address must start with http:// or https://", 400)
        url, headers = f"{base}/models", ({"Authorization": f"Bearer {key}"} if key else {})
    elif provider in _MODEL_LISTS:
        url, make_headers = _MODEL_LISTS[provider]
        if provider != "openrouter" and not key:
            return {"success": False, "data": None, "message": "No API key"}
        headers = make_headers(key)
    else:
        return _api_error("invalid_provider", "Unknown provider", 400)
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            return {"success": True, "data": resp.json()}
    except Exception as e:
        return {"success": False, "data": None, "message": redact_secrets(str(e))[:300]}


@app.post("/llm/test")
async def test_llm(settings: dict):
    """Test LLM connection with given settings (masked keys are the saved ones)."""
    try:
        from core.config import LLM_SECRETS, keep_masked
        from core.llm_factory import create_llm, test_connection
        settings = keep_masked(settings, load_llm_settings(), LLM_SECRETS)
        llm = create_llm(settings)
        result = await test_connection(llm)
        return {"success": True, "message": result}
    except Exception as e:
        msg = str(e)
        if "api_key" in msg.lower() or "unauthorized" in msg.lower() or "401" in msg:
            return {"success": False, "message": "Invalid API key or unauthorized. Check your credentials."}
        if "timeout" in msg.lower() or "connect" in msg.lower():
            return {"success": False, "message": "Connection failed. Check your network and provider settings (is your local LLM server running?)"}
        
        from browser_use.llm.exceptions import ModelProviderError
        if isinstance(e, ModelProviderError):
            return {"success": False, "message": f"LLM Provider Error: {msg}"}
            
        _log.error(f"LLM test failed: {e}", exc_info=True)
        return {"success": False, "message": f"LLM test failed: {msg}"}


@app.post("/chatgpt/login")
async def chatgpt_login():
    """Start "Sign in with ChatGPT" in the default browser; returns the sign-in URL too."""
    from core import chatgpt_auth
    try:
        return {"url": await asyncio.to_thread(chatgpt_auth.start_login), "error": ""}
    except RuntimeError as e:  # both callback ports busy
        return {"url": "", "error": str(e)}


@app.get("/chatgpt/status")
async def chatgpt_status():
    from core import chatgpt_auth
    return chatgpt_auth.status()


@app.post("/chatgpt/logout")
async def chatgpt_logout():
    from core import chatgpt_auth
    await asyncio.to_thread(chatgpt_auth.logout)
    return chatgpt_auth.status()


@app.get("/chatgpt/models")
async def chatgpt_models():
    """Models the signed-in ChatGPT plan offers, with their reasoning effort levels."""
    from core import chatgpt_auth
    try:
        return {"success": True, "models": await asyncio.to_thread(chatgpt_auth.list_models)}
    except Exception as e:
        return {"success": False, "models": [], "message": str(e)}


@app.post("/llm/ollama-models")
async def list_ollama_models(body: dict):
    """Fetch available models from an Ollama server."""
    import httpx
    base_url = body.get("base_url", "http://localhost:11434").rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
            return {"success": True, "models": models}
    except Exception as e:
        return {"success": False, "models": [], "message": str(e)}


# ── App Settings ──────────────────────────────────────────────────────────
@app.get("/settings")
async def get_settings():
    """App settings, with the saved password and 2Captcha key as SECRET_MASK."""
    from core.config import SETTINGS_SECRETS, mask_secrets
    return mask_secrets(load_settings(), SETTINGS_SECRETS)


@app.get("/credentials/sites")
async def employer_password_sites():
    """Employer sites the app made an account password for (names only)."""
    from core import secret_store
    return {"sites": secret_store.site_password_sites()}


@app.post("/credentials/reveal")
async def reveal_employer_password(body: dict):
    """One employer site's app-made password, shown when the user asks (to sign in by hand)."""
    from core import secret_store
    site = str(body.get("site") or "").strip().lower()
    if site not in secret_store.site_password_sites():
        return _api_error("not_found", "No saved password for that site", 404)
    password = secret_store.site_password(site, create=False)
    if not password:
        return _api_error("unreadable", "That password can't be read on this Windows account", 409)
    return {"site": site, "password": password}


@app.get("/browsers")
async def list_browsers():
    """Chromium-based browsers installed on this machine, for the browser picker."""
    from core.browsers import detect_browsers
    return {"browsers": detect_browsers()}

@app.put("/settings")
async def update_settings(settings: dict):
    """Update app settings.

    Merged into what is already stored rather than replacing it. save_settings()
    rewrites settings.json wholesale, so a partial PUT used to wipe every key it
    did not mention — clicking a theme button (which sends only {"theme"}) would
    silently destroy the resume path, blocked domains, the account email and
    password and the 2Captcha key. Keys present in the request still win, so
    sending a full list (e.g. blocked_domains) replaces it as before.
    """
    from core.config import SETTINGS_SECRETS, keep_masked
    problem = _settings_problem(settings)
    if problem:
        return _api_error("invalid_settings", problem, 400)
    stored = load_settings()
    merged = {**stored, **keep_masked(settings, stored, SETTINGS_SECRETS)}
    save_settings(merged)
    return {"success": True}


_SETTINGS_TYPES = {
    "resume_path": str, "browser": str, "browser_path": str, "theme": str, "captcha_api_key": str,
    "blocked_domains": list, "other_documents": list, "sensitive_data": dict,
    "max_failures": int, "parallel_jobs": int, "human_delay_min": int, "human_delay_max": int,
    "telemetry_enabled": bool, "job_collection_enabled": bool, "stealth_enabled": bool,
    "human_delay_enabled": bool, "captcha_solver_enabled": bool, "performance_mode": bool,
    "fit_check_enabled": bool, "cover_letter_boxes": bool, "onboarding_completed": bool,
}


def _settings_problem(settings: dict) -> str:
    """Why a settings update can't be saved ("" when it can): each known key must have its type."""
    for key, kind in _SETTINGS_TYPES.items():
        if key not in settings:
            continue
        value = settings[key]
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)) if kind is int else isinstance(value, kind)
        if not ok:
            return f"{key} must be {'a number' if kind is int else 'true or false' if kind is bool else 'a ' + kind.__name__}"
    sensitive = settings.get("sensitive_data")
    if isinstance(sensitive, dict) and not all(isinstance(sensitive.get(k, ""), str) for k in ("email", "password")):
        return "sensitive_data must hold text"
    return ""


# ── In-process task runner ────────────────────────────────────────────────
import asyncio
import io
import contextvars

# Which run (its run_id) the current code belongs to. Set in a run's thread; asyncio
# tasks and asyncio.to_thread calls inherit it. A run's log takes only its own
# prints and log records, never an API request's, the Inbox check's or another run's.
_current_run: contextvars.ContextVar[str] = contextvars.ContextVar("langhire_run", default="")
_run_captures: dict[str, "_LogCapture"] = {}


class _RoutedStream:
    """Stands in for sys.stdout/sys.stderr: a run's prints go to its log, the rest where they went before.

    contextlib.redirect_stdout swapped the stream for the whole process, so with two
    runs (or a run and an API request) printing at once, lines landed in the wrong log.
    """

    def __init__(self, real):
        self._real = real

    def _target(self):
        return _run_captures.get(_current_run.get()) or self._real

    def write(self, s):
        target = self._target()
        return target.write(s) if target is not None else len(s)  # no console in the packaged app

    def flush(self):
        target = self._target()
        if target is not None:
            target.flush()

    def __getattr__(self, name):  # isatty(), encoding, ... of the stream in use
        return getattr(self._target(), name)


def _route_std_streams() -> None:
    for name in ("stdout", "stderr"):
        if not isinstance(getattr(sys, name), _RoutedStream):
            setattr(sys, name, _RoutedStream(getattr(sys, name)))


class _LogCapture(io.StringIO):
    """Captures print output and appends to a status log list + persists to DB."""
    _ansi_re = __import__("re").compile(r"\x1b\[[0-9;]*m")

    def __init__(self, log_list: list, status_dict: dict = None,
                 max_lines: int = MAX_LOG_LINES, metrics_store=None, run_id: str = ""):
        super().__init__()
        self._log = log_list
        self._status = status_dict
        self._max = max_lines
        self._metrics = metrics_store
        self._run_id = run_id

    def write(self, s):
        import re as _re
        from core.shared_config import redact_secrets
        s = redact_secrets(s)
        for line in s.splitlines():
            line = self._ansi_re.sub("", line).strip()
            if line:
                with _status_lock:
                    self._log.append(line)
                    if len(self._log) > self._max:
                        del self._log[:len(self._log) - self._max // 2]
                    if self._status is not None and "💾" in line:
                        m = _re.search(r"total this title:\s*(\d+)", line)
                        if m:
                            self._status["collected"] = int(m.group(1))
                if self._metrics and self._run_id:
                    try:
                        level = "ERROR" if "❌" in line else "WARNING" if "⚠️" in line else "INFO"
                        self._metrics.log(self._run_id, line, level=level)
                    except Exception:
                        pass
        return len(s)

    def flush(self):
        pass


_run_metrics_start_lock = threading.Lock()


def _run_async_in_thread(coro_factory, status_dict):
    """Run an async function in a new thread with its own event loop, capturing stdout and logs."""
    run_id = str(uuid.uuid4())[:8]
    status_dict["run_id"] = run_id

    def _worker():
        # SQLite connections belong to the thread that opens them. Constructing
        # this store on the caller left an unused connection there, while logs
        # opened another one on the worker. Own and close it on this thread.
        # Concurrent first runs must not race SQLite's WAL/schema setup.
        with _run_metrics_start_lock:
            metrics = _get_metrics_store()
        capture = _LogCapture(status_dict["log"], status_dict=status_dict,
                              metrics_store=metrics, run_id=run_id)

        class _ListHandler(logging.Handler):
            _noise = {"httpx", "httpcore", "urllib3", "filelock", "websockets",
                       "charset_normalizer", "botocore", "boto3", "s3transfer"}
            _collected_re = __import__("re").compile(r"(\d+)\s+jobs?\s+collected", __import__("re").IGNORECASE)
            _ansi_re = __import__("re").compile(r"\x1b\[[0-9;]*m")
            def emit(self, record):
                if record.name.split(".")[0] in self._noise or _current_run.get() != run_id:
                    return
                from core.shared_config import redact_secrets
                msg = redact_secrets(self._ansi_re.sub("", record.getMessage()).strip())
                if msg and not msg.startswith("🌎"):
                    with _status_lock:
                        status_dict["log"].append(msg)
                        if len(status_dict["log"]) > MAX_LOG_LINES:
                            del status_dict["log"][:MAX_LOG_LINES // 2]
                        if "collected" in status_dict:
                            m = self._collected_re.search(msg)
                            if m:
                                status_dict["collected"] = max(status_dict.get("collected", 0), int(m.group(1)))
                    if metrics and run_id:
                        try:
                            level = record.levelname
                            metrics.log(run_id, msg, level=level)
                        except Exception:
                            pass

        log_handler = _ListHandler()
        log_handler.setLevel(logging.INFO)
        logging.getLogger().addHandler(log_handler)

        _current_run.set(run_id)
        _run_captures[run_id] = capture
        _route_std_streams()
        try:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            status_dict["_loop"] = loop
            loop.run_until_complete(coro_factory())
            with _status_lock:
                if status_dict.get("cancel_requested"):
                    status_dict["log"].append("🛑 Stopped by user")
                    status_dict["error"] = "cancelled"
                else:
                    status_dict["log"].append("✅ Finished successfully")
                    status_dict["error"] = None
        except asyncio.CancelledError:
            with _status_lock:
                status_dict["log"].append("🛑 Stopped by user")
                status_dict["error"] = "cancelled"
        except Exception as e:
            _log.error(f"Worker thread error: {e}", exc_info=True)
            with _status_lock:
                status_dict["log"].append(f"❌ Error: {e}")
                status_dict["error"] = str(e)
        finally:
            logging.getLogger().removeHandler(log_handler)
            _run_captures.pop(run_id, None)
            if metrics:
                metrics.close()
            loop.close()
            with _status_lock:
                status_dict["running"] = False
                status_dict["cancel_requested"] = False
                status_dict["finished_at"] = datetime.now().isoformat()
            status_dict.pop("_loop", None)

    status_dict["running"] = True
    status_dict["cancel_requested"] = False
    status_dict["error"] = None
    status_dict["finished_at"] = None
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    return t


def _owned_profile_prefix() -> tuple[str, ...]:
    """This app's automation profiles: <data folder>/browser_profile* (normalised for matching).

    As written and as resolved: browser-use launches with the resolved path, which differs when
    the data folder is reached through a link or a Windows 8.3 short name (RUNNER~1).
    """
    base = get_data_dir() / "browser_profile"
    return tuple(dict.fromkeys(str(p).replace("\\", "/").lower() for p in (base, base.resolve())))


def _uses_owned_profile(cmdline: list[str], prefixes: tuple[str, ...]) -> bool:
    """True when a browser command line runs on one of this app's profiles (--user-data-dir=<prefix>...)."""
    for arg in cmdline or []:
        arg = arg.replace("\\", "/").lower().strip('"')
        if arg.startswith("--user-data-dir="):
            path = arg.split("=", 1)[1].strip('"')
            if any(path == p or path.startswith(p + "_") or path.startswith(p + "/") for p in prefixes):
                return True
    return False


def _kill_browser_processes():
    """Close browsers running on this app's automation profiles (and nothing else).

    Matched by the exact profile folders under this app's data folder, so the
    user's own Chrome/Brave/Edge, another LangHire data folder (tests) and other
    programs are never touched.
    """
    try:
        import psutil
    except ImportError:
        _log.warning("psutil missing: leftover automation browsers were not closed")
        return
    try:
        prefixes = _owned_profile_prefix()
        procs_to_kill = []
        for proc in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                if _uses_owned_profile(proc.info.get("cmdline") or [], prefixes):
                    procs_to_kill.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        for proc in procs_to_kill:
            try:
                proc.terminate()
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        _, alive = psutil.wait_procs(procs_to_kill, timeout=3)
        for proc in alive:
            try:
                proc.kill()
                _log.warning(f"Force-killed browser process {proc.pid}")
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
    except Exception as e:
        _log.warning(f"Couldn't close leftover automation browsers: {e}")


# ── Who uses the agents' browser profiles ─────────────────────────────────
# Applying, collecting, a sign-in window and the Gmail check all use the browser
# profile folders under the data folder, and starting or stopping one closes every
# browser on them. So one at a time: each start claims them under this lock
# (the Inbox scheduler's thread too, through _inbox_busy).
_browser_lock = threading.Lock()

_BROWSER_BUSY = {
    "apply": "Applications are running. Stop them first.",
    "collect": "Job collection is running. Stop it first.",
    "login": "A sign-in window is open. Finish there and close it first.",
    "inbox": "The Inbox is checking Gmail right now. Try again in a moment.",
}


def _browser_user(exclude: str = "") -> str:
    """Which operation is using the browser profiles now ("" for none)."""
    users = (
        ("apply", _apply_status.get("running")),
        ("collect", _collection_status.get("running")),
        ("login", _login_process is not None and _login_process.poll() is None),
        ("inbox", _inbox_checking()),
    )
    return next((name for name, busy in users if busy and name != exclude), "")


def _force_stop(status_dict):
    """Force-stop a running operation: cancel async tasks and kill browser processes.

    Only while this operation runs (it then owns the browser profiles): a Stop on
    an operation that isn't running must not close another one's browsers or the
    sign-in window.
    """
    with _status_lock:
        if not status_dict.get("running"):
            return
        status_dict["cancel_requested"] = True

    # Cancel all tasks in the worker's event loop
    loop = status_dict.get("_loop")
    if loop and loop.is_running():
        for task in asyncio.all_tasks(loop):
            loop.call_soon_threadsafe(task.cancel)

    _kill_browser_processes()
    with _status_lock:
        status_dict["log"].append("🛑 Force stopped")


# ── Thread-safe status access ─────────────────────────────────────────────
_status_lock = threading.Lock()


# ── Job Collection ────────────────────────────────────────────────────────
_collection_status: dict = {"running": False, "title": None, "log": []}
_collection_thread: threading.Thread | None = None


@app.post("/jobs/collect")
async def start_collection(body: CollectRequest):
    """Start job collection in-process (no subprocess, no external Python needed)."""
    global _collection_thread, _collection_status

    # Read the switch fresh — the UI writes settings.json while we are running.
    if not load_settings().get("job_collection_enabled", True):
        return _api_error(
            "collection_disabled",
            "Job board collection is turned off in Settings. Turn it back on to collect, "
            "or add jobs with Paste Links.",
            409,
        )

    problem = _start_problem(apply=False)
    if problem:
        return {"success": False, "message": problem}
    with _browser_lock:
        busy = _browser_user()
        if busy == "collect":
            return {"success": False, "message": "Collection already running"}
        if busy:
            # Starting closes every browser on the agents' profiles: theirs too.
            return {"success": False, "message": _BROWSER_BUSY[busy]}
        _collection_status["running"] = True  # claims the browser profiles
    try:
        # Kill any leftover browser processes from previous runs
        _kill_browser_processes()
    except Exception:
        _collection_status["running"] = False
        raise

    title = body.title
    max_jobs = body.max_jobs
    filters = body.filters or {}
    search = body.search if isinstance(body.search, dict) else None
    # Sources to collect from. `sources` (list) wins; fall back to the single
    # `source` field so older callers keep working. Duplicates are dropped and
    # order is preserved.
    selected_sources = body.sources if body.sources else [body.source or "linkedin"]
    selected_sources = [s for s in dict.fromkeys(selected_sources) if s]
    _collection_status = {"running": True, "title": title or "all titles", "log": ["Starting collection... This may take several minutes per job title."], "collected": 0, "max_jobs": max_jobs}

    async def _do_collect():
        """Run collection logic directly, bypassing argparse."""
        from core.shared_config import LOGS_DIR, read_jobs, refresh_credentials, credential_refresh_loop
        from cli import collect_jobs
        import core.shared_config as _config

        # Override config.get_llm with the user's UI-configured LLM
        llm_settings = load_llm_settings()
        if llm_settings.get("provider"):
            from core.llm_factory import create_llm, create_fallback_llm
            _config.get_llm = lambda: create_llm(llm_settings)
            _config.get_fallback_llm = lambda: create_fallback_llm(llm_settings)
            print(f"🤖 Using {llm_settings['provider']} LLM from settings")

        # Load profile from the app data dir (set via UI), not the project root
        profile = load_profile()
        jobs = read_jobs()
        LOGS_DIR.mkdir(exist_ok=True)

        titles = [title] if title else profile.get("target_job_titles", [])
        cred_task = asyncio.create_task(credential_refresh_loop(CREDENTIAL_REFRESH_MINUTES))

        # Resolve the selected source names to plugins. Every source, LinkedIn
        # included, now runs from its plugin YAML.
        registry = _get_plugin_registry()
        resolved: list[tuple[str, object | None]] = []
        for name in selected_sources:
            plugin = registry.get_by_name(name)
            if plugin is None:
                print(f"  ⚠️  Unknown source '{name}' — skipping")
            elif not plugin.enabled:
                print(f"  ⚠️  Source '{name}' is disabled in Settings — skipping")
            else:
                resolved.append((plugin.display_name, plugin))
        if not resolved:
            print("No valid sources selected — nothing to collect.")
            cred_task.cancel()
            return

        for i, t in enumerate(titles):
            if _collection_status.get("cancel_requested"):
                print("🛑 Stop requested — halting collection")
                break
            for si, (label, plugin) in enumerate(resolved):
                if _collection_status.get("cancel_requested"):
                    break
                # Randomised pause between sources/titles when delays are on.
                if i > 0 or si > 0:
                    from core.shared_config import maybe_human_delay
                    await maybe_human_delay("the next source")
                print(f"\n{'='*60}")
                print(f"[{i+1}/{len(titles)}] Collecting: {t} — from {label}")
                print(f"{'='*60}")
                try:
                    found = await collect_jobs.collect_for_title(t, jobs, profile, max_jobs=max_jobs, filters=filters,
                                                                 plugin=plugin, search=search)
                    jobs = read_jobs()
                    # Don't increment collected here — it's already updated by the stdout/log handler
                    # parsing "total this title: N" and "N jobs collected" from agent output
                    print(f"  Found {len(found)} new jobs (total: {len(jobs)})")
                except Exception as e:
                    print(f"  Error: {e}")

        # Phase 2: Fetch descriptions for collected jobs
        if not _collection_status.get("cancel_requested"):
            jobs = read_jobs()
            needs_desc = [
                (url, j) for url, j in jobs.items()
                if j.get("status") == "pending" and not j.get("description")
            ]
            if needs_desc:
                print(f"\n📋 Fetching descriptions for {len(needs_desc)} jobs...")
                for i, (url, job) in enumerate(needs_desc):
                    if _collection_status.get("cancel_requested"):
                        print("🛑 Stop requested — halting description fetch")
                        break
                    job_title = job.get("title", "Unknown")
                    company = job.get("company", "Unknown")
                    print(f"  [{i+1}/{len(needs_desc)}] {job_title} at {company}...")
                    try:
                        desc = await collect_jobs.fetch_description_for_job(url, job)
                        if desc:
                            from core.shared_config import update_job as _upd
                            _upd(url, description=desc)
                            print(f"    ✅ Got description ({len(desc)} chars)")
                        else:
                            print(f"    ⚠️  No description extracted")
                    except Exception as e:
                        print(f"    ❌ Error: {e}")

        cred_task.cancel()

        # Summary
        jobs = read_jobs()
        easy = sum(1 for j in jobs.values() if j.get("easy_apply"))
        pending = sum(1 for j in jobs.values() if j.get("status") == "pending")
        descs = sum(1 for j in jobs.values() if j.get("description"))
        print(f"\nCollection complete! Total: {len(jobs)} (Easy Apply: {easy}), Pending: {pending}, With descriptions: {descs}")

    def make_coro():
        return _do_collect()

    try:
        _collection_thread = _run_async_in_thread(make_coro, _collection_status)
    except Exception:
        _collection_status["running"] = False  # the thread would have reset it
        raise
    return {"success": True, "message": f"Collection started for {title or 'all titles'}"}


@app.post("/jobs/collect/stop")
async def stop_collection():
    _force_stop(_collection_status)
    return {"success": True}


@app.get("/jobs/collect/status")
async def collection_status():
    with _status_lock:
        return {
            "running": _collection_status.get("running", False),
            "title": _collection_status.get("title"),
            "log": list(_collection_status.get("log", [])[-100:]),
            "collected": _collection_status.get("collected", 0),
            "max_jobs": _collection_status.get("max_jobs", 0),
            "error": _collection_status.get("error"),
            "finished_at": _collection_status.get("finished_at"),
            "run_id": _collection_status.get("run_id"),
            "login_window_open": _login_process is not None and _login_process.poll() is None,
        }


# ── Application Control ──────────────────────────────────────────────────
_apply_status: dict = {"running": False, "mode": None, "workers": 1, "log": []}
_apply_thread: threading.Thread | None = None


# Jobs the Apply / Retry buttons may (re)start.
# Every job status (in_progress is only ever set by the app), and those a run may (re)start.
JOB_STATUSES = ("pending", "applied", "failed", "blocked", "in_progress", "closed", "needs_you", "skipped")
RETRYABLE_STATUSES = ("pending", "failed", "needs_you", "skipped")


def _reset_for_apply(url: str, job: dict) -> None:
    """Back to pending so a worker can claim it. Applying to a Skipped job by hand overrides the fit check."""
    from core.shared_config import update_job
    override = job.get("status") == "skipped" or job.get("fit_override", False)
    # Applying again is the user's decision about an unknown outcome: a fresh attempt.
    update_job(url, status="pending", error=None, fit_override=override, submit_attempted_at=None)
    job["status"], job["fit_override"], job["submit_attempted_at"] = "pending", override, None


@app.post("/apply/start")
async def start_applying(body: ApplyRequest):
    """Start job application in-process."""
    global _apply_thread, _apply_status

    problem = _start_problem(apply=True)
    if problem:
        return {"success": False, "message": problem}
    requested = ([body.job_url] if body.job_url else body.job_urls or [])
    jobs_before_start = _load_jobs()
    if not body.acknowledge_unknown and any((jobs_before_start.get(url) or {}).get("submit_attempted_at") for url in requested):
        return {"success": False, "message": "An earlier request may already have submitted this application. "
            "Check the employer's site or email before retrying, then explicitly acknowledge the duplicate risk."}
    with _browser_lock:
        busy = _browser_user()
        if busy == "apply":
            return {"success": False, "message": "Application already running"}
        if busy:
            # Starting closes every browser on the agents' profiles: theirs too (and
            # closing the sign-in window can lose a sign-in not yet saved to disk).
            return {"success": False, "message": _BROWSER_BUSY[busy]}
        _apply_status["running"] = True  # claims the browser profiles
    from core.shared_config import parallel_jobs
    workers = body.workers or parallel_jobs(load_settings())
    mode = body.mode
    limit = body.limit
    target_job_url = body.job_url
    target_job_urls = body.job_urls
    from cli import apply_jobs as _apply_jobs_module

    # Claimed above, before closing browsers; released if starting fails before
    # the run's thread takes over (it resets the flag when the run ends).
    try:
        _apply_jobs_module.set_run_mode(body.run_mode or "trial")
        # Kill any leftover browser processes from previous runs
        _kill_browser_processes()
        _set_apply_paused(False)
    except Exception:
        _apply_status["running"] = False
        raise

    if target_job_url:
        _apply_status = {"running": True, "mode": mode, "workers": 1, "log": [f"Applying to single job..."]}
    elif target_job_urls:
        _apply_status = {"running": True, "mode": mode, "workers": workers, "log": [f"Applying to {len(target_job_urls)} selected jobs..."]}
    else:
        _apply_status = {"running": True, "mode": mode, "workers": workers, "log": [f"Starting {mode} apply with {workers} worker(s)..."]}

    # mode="all" applies to all pending jobs regardless of easy_apply status
    easy_apply_filter = None if mode == "all" else (mode != "external")

    async def _do_apply():
        """Run apply logic directly, bypassing argparse."""
        from core.shared_config import JOBS_FILE, CANDIDATE_PROFILE, QA_FILE, LOGS_DIR, load_json, credential_refresh_loop, get_memory_store
        from cli import apply_jobs
        import core.shared_config as _config

        # Override config.get_llm with the user's UI-configured LLM
        llm_settings = load_llm_settings()
        if llm_settings.get("provider"):
            from core.llm_factory import create_llm, create_fallback_llm
            _config.get_llm = lambda: create_llm(llm_settings)
            _config.get_fallback_llm = lambda: create_fallback_llm(llm_settings)
            print(f"🤖 Using {llm_settings['provider']} LLM from settings")

        jobs = load_json(JOBS_FILE, {})
        profile = load_json(CANDIDATE_PROFILE, {})
        qa = load_json(QA_FILE, {})
        LOGS_DIR.mkdir(exist_ok=True)

        if target_job_url:
            target = jobs.get(target_job_url)
            if not target:
                print("Job not found.")
                return
            if target.get("status") not in RETRYABLE_STATUSES:
                print(f"Job is already {target.get('status')}.")
                return
            _reset_for_apply(target_job_url, target)
            pending = [target]
        elif target_job_urls:
            # Batch apply: only apply to the specific selected jobs
            pending = []
            for url in target_job_urls:
                j = jobs.get(url)
                if j and j.get("status") in RETRYABLE_STATUSES:
                    _reset_for_apply(url, j)
                    pending.append(j)
        elif easy_apply_filter is None:
            pending = [j for j in jobs.values() if j.get("status") == "pending"]
        else:
            pending = [
                j for j in jobs.values()
                if j.get("status") == "pending"
                and (j.get("easy_apply") is True) == easy_apply_filter
            ]
        if limit and not target_job_url and not target_job_urls:
            pending = pending[:limit]

        if not pending:
            print("No pending jobs to apply to.")
            return

        applied_labels = [
            f"{j.get('title','')} at {j.get('company','')}"
            for j in jobs.values() if j.get("status") == "applied"
        ]

        label = "All" if easy_apply_filter is None else ("Easy Apply" if easy_apply_filter else "Non-Easy Apply")
        print(f"Applying to {len(pending)} {label} jobs with {workers} worker(s)\n")

        queue = asyncio.Queue()
        for job in pending:
            queue.put_nowait(job)
        apply_jobs.new_run(_apply_status.get("run_id"))

        stats = {}
        num_workers = min(workers, len(pending))
        if num_workers > 1:
            print(f"Copying your browser sign-ins for {num_workers} applications at the same time...")
        from core.shared_config import prepare_mail_profile, prepare_worker_profiles
        await asyncio.to_thread(prepare_worker_profiles, num_workers)
        # The verification-email reader's own copy of the sign-in cookies (agents never open Gmail).
        await asyncio.to_thread(prepare_mail_profile)
        cred_task = asyncio.create_task(credential_refresh_loop(CREDENTIAL_REFRESH_MINUTES))

        # For mode="all", pass True for easy_apply (agent handles both types via the job URL)
        worker_easy_apply = True if easy_apply_filter is None else easy_apply_filter
        tasks = []
        for i in range(num_workers):
            if i > 0:
                from core.shared_config import worker_stagger
                await worker_stagger()
            tasks.append(asyncio.create_task(
                apply_jobs.worker(f"W{i+1}", i+1, queue, profile, qa, applied_labels, worker_easy_apply, stats, cancel_flag=_apply_status)
            ))
        await asyncio.gather(*tasks)
        cred_task.cancel()

        print(f"\nResults: {stats}")
        total_applied = sum(1 for j in load_json(JOBS_FILE, {}).values() if j.get("status") == "applied")
        print(f"Total applied: {total_applied}")

    def make_coro():
        return _do_apply()

    try:
        _apply_thread = _run_async_in_thread(make_coro, _apply_status)
    except Exception:
        _apply_status["running"] = False  # the thread would have reset it
        raise
    return {"success": True, "message": f"Started {mode} apply with {workers} worker(s)"}


@app.post("/apply/stop")
async def stop_applying():
    _set_apply_paused(False)
    _force_stop(_apply_status)
    return {"success": True}


def _set_apply_paused(paused: bool) -> None:
    from cli import apply_jobs
    apply_jobs.set_paused(paused)


def _run_mode() -> str:
    try:
        from cli import apply_jobs as _aj
        return _aj._run_options["mode"]
    except Exception:
        return "auto"


def _current_jobs() -> list[dict]:
    try:
        from cli import apply_jobs as _aj
        return _aj.current_jobs()
    except Exception:
        return []


@app.post("/apply/skip")
async def skip_current_job(body: dict):
    """Stop the application to one job and move on to the next (the job stays pending)."""
    url = _text(body, "url")
    if not _apply_status.get("running") or not url:
        return {"success": False}
    from cli import apply_jobs as _aj
    ok = _aj.skip_job(url)
    if ok:
        with _status_lock:
            _apply_status["log"].append("⏭️ Skipping this job: moving on to the next one.")
    return {"success": ok}


@app.post("/apply/pause")
async def pause_applying():
    """Pause the running applications so the user can take over the browser."""
    if not _apply_status.get("running"):
        return {"success": False, "paused": False}
    _set_apply_paused(True)
    with _status_lock:
        _apply_status["log"].append("⏸️ Paused: the agent stops after its current action. Make your changes, then Resume.")
    return {"success": True, "paused": True}


@app.post("/apply/resume")
async def resume_applying():
    _set_apply_paused(False)
    if _apply_status.get("running"):
        with _status_lock:
            _apply_status["log"].append("▶️ Resumed: the agent re-reads the page and keeps your changes.")
    return {"success": True, "paused": False}


def _apply_paused() -> bool:
    from cli import apply_jobs
    return apply_jobs.is_paused()


def _pending_reviews() -> list[dict]:
    from cli import apply_jobs
    return apply_jobs.pending_reviews()


def _pending_help() -> list[dict]:
    from cli import apply_jobs
    return apply_jobs.pending_help()


@app.post("/apply/help")
async def help_application(body: dict):
    """Reply to a trial-run agent that is stuck: a message and Continue, or Give up."""
    from cli import apply_jobs
    give_up = bool(body.get("give_up"))
    ok = apply_jobs.answer_help(_text(body, "url"), _text(body, "message"), give_up)
    if ok:
        with _status_lock:
            _apply_status["log"].append("🛑 Gave up on this application." if give_up else "▶️ Continuing with your help.")
    return {"success": ok}


@app.post("/apply/review")
async def review_application(body: dict):
    """Approve (submit) or skip an application that is waiting for the user's review."""
    from cli import apply_jobs
    if not isinstance(body.get("approve"), bool):
        return _api_error("invalid_field", "approve must be a boolean", 400)
    approve = body["approve"]
    ok = apply_jobs.decide_review(_text(body, "url"), approve)
    if ok:
        with _status_lock:
            _apply_status["log"].append("✅ Approved: submitting." if approve else "⏭️ Skipped: not submitted.")
    return {"success": ok}


@app.post("/apply/requests/decide")
async def decide_browser_requests(body: dict):
    from core import request_gate
    ids = _str_list(body.get("ids"), limit=request_gate.MAX_PENDING)
    if not ids or any(len(i) != 32 for i in ids) or not isinstance(body.get("approve"), bool):
        return _api_error("invalid_field", "Supply displayed request IDs and a boolean approve", 400)
    return {"success": request_gate.decide(ids, body["approve"])}


@app.get("/apply/status")
async def apply_status():
    from core import request_gate
    with _status_lock:
        return {
            "running": _apply_status.get("running", False),
            "paused": _apply_status.get("running", False) and _apply_paused(),
            "reviews": _pending_reviews() if _apply_status.get("running") else [],
            "requests": request_gate.pending(),
            "help": _pending_help() if _apply_status.get("running") else [],
            "current": _current_jobs() if _apply_status.get("running") else [],
            "run_mode": _run_mode() if _apply_status.get("running") else None,
            "mode": _apply_status.get("mode"),
            "workers": _apply_status.get("workers", 1),
            "log": list(_apply_status.get("log", [])[-100:]),
            "error": _apply_status.get("error"),
            "finished_at": _apply_status.get("finished_at"),
            "run_id": _apply_status.get("run_id"),
        }


# ── Jobs ──────────────────────────────────────────────────────────────────
@app.get("/jobs")
async def get_jobs(
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    limit: int = Query(500, ge=1),
    fields: Optional[str] = Query(None, description="Comma-separated fields to return (the url always)"),
):
    """Get all jobs, optionally filtered by status and search.

    `fields` keeps the answer small for pages that count every job (Analytics):
    the full jobs carry their descriptions.
    """
    jobs = _load_jobs()
    keep = {f.strip() for f in fields.split(",") if f.strip()} | {"url"} if fields else None
    result = []
    for url, job in jobs.items():
        job["url"] = url
        if status and job.get("status") != status:
            continue
        if search:
            q = search.lower()
            if q not in (job.get("title", "") or "").lower() and \
               q not in (job.get("company", "") or "").lower():
                continue
        result.append(job)
    # Sort by collected_at desc
    result.sort(key=lambda j: j.get("collected_at", ""), reverse=True)
    if keep is not None:
        return [{k: v for k, v in j.items() if k in keep} for j in result[:limit]]
    return result[:limit]


@app.get("/jobs/stats")
async def get_job_stats():
    jobs = _load_jobs()
    stats = {"total": len(jobs), **{s: 0 for s in JOB_STATUSES}}
    for j in jobs.values():
        s = j.get("status", "pending")
        if s in stats:
            stats[s] += 1
    return stats


CSV_COLUMNS = ["Last Attempt", "Date Applied", "Practised (Skipped at Review)", "Date Collected", "Job Title",
               "Company", "Location", "Status", "Reason", "Duration (s)", "Steps", "Source", "Easy Apply", "URL"]


def _csv_cell(value) -> str:
    """A spreadsheet-safe cell: text from websites starting with = + - @ would run as a formula in Excel."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def _jobs_csv() -> str:
    """Every job as CSV text (with a UTF-8 BOM so Excel reads accents correctly)."""
    import csv
    import io

    jobs = _load_jobs()
    # The latest run of each job, for the attempt time, duration and steps the History list shows.
    last_run: dict[str, dict] = {}
    metrics = _get_metrics_store()
    if metrics:
        try:
            for run in metrics.get_all_runs(limit=5000):  # newest first
                last_run.setdefault(run.get("job_url") or "", run)
        except Exception as e:
            _log.warning(f"CSV export: couldn't read run history: {e}")

    rows = list(jobs.values())
    rows.sort(key=lambda j: (j.get("applied_at") or (last_run.get(j.get("url", "")) or {}).get("started_at") or "",
                             j.get("collected_at") or ""), reverse=True)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for j in rows:
        run = last_run.get(j.get("url", "")) or {}
        duration = run.get("duration_seconds")
        writer.writerow([_csv_cell(v) for v in (
            run.get("started_at") or j.get("applied_at") or "",
            j.get("applied_at") or "",
            j.get("practised_at") or "",
            j.get("collected_at") or "",
            j.get("title") or "",
            j.get("company") or "",
            j.get("location") or "",
            j.get("status") or "",
            j.get("error") or "",
            "" if duration is None else round(float(duration)),
            run.get("step_count") if run.get("step_count") is not None else "",
            j.get("source") or "",
            "" if j.get("easy_apply") is None else ("yes" if j.get("easy_apply") else "no"),
            j.get("url") or "",
        )])
    return "﻿" + buf.getvalue()


@app.get("/jobs/export")
async def export_jobs_csv():
    """Export all jobs as a downloadable CSV file."""
    from fastapi.responses import Response

    return Response(
        content=_jobs_csv().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="langhirev2-jobs.csv"'},
    )


@app.post("/jobs/export/save")
async def save_jobs_csv(body: dict):
    """Write the CSV export to the path the user picked in the Save dialog."""
    path = Path(str(body.get("path") or "")).expanduser()
    if not path.name or path.suffix.lower() != ".csv":
        return _api_error("invalid_path", "Choose a file name ending in .csv", 400)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_jobs_csv().encode("utf-8"))
    except OSError as e:
        return _api_error("write_failed", f"Couldn't save the file: {e.strerror or e}", 500)
    return {"success": True, "path": str(path), "rows": len(_load_jobs())}


# ── Job Management ────────────────────────────────────────────────────────

@app.put("/jobs/status")
async def update_job_status(body: dict):
    """Manually change a job's status."""
    url = _text(body, "url")
    new_status = _text(body, "status")
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    valid_statuses = tuple(s for s in JOB_STATUSES if s != "in_progress")
    if new_status not in valid_statuses:
        return _api_error("invalid_status", f"Status must be one of: {', '.join(valid_statuses)}", 400)

    jobs = _load_jobs()
    if url not in jobs:
        return _api_error("not_found", "Job not found", 404)

    from core.shared_config import update_job
    update_job(url, status=new_status, error=None, submission_evidence=(
        {"kind": "user_confirmed", "confirmed_at": datetime.now(timezone.utc).isoformat()}
        if new_status == "applied" else None))
    return {"success": True, "url": url, "status": new_status}


@app.put("/jobs/note")
async def update_job_note(body: dict):
    """A note for the agent on a job, used on its next attempt (e.g. how to get past a failure)."""
    url = _text(body, "url")
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    if url not in _load_jobs():
        return _api_error("not_found", "Job not found", 404)
    if not isinstance(body.get("note"), str) or len(body["note"]) > 10000:
        return _api_error("invalid_note", "The note must be text of at most 10,000 characters", 400)
    from core.shared_config import update_job
    update_job(url, user_note=_text(body, "note", 10000))
    return {"success": True}


@app.put("/jobs/cover-letter")
async def save_job_cover_letter(body: dict):
    """A cover letter written for one job, kept on that job ("" removes it).

    It used to be saved as the profile's cover letter, which every later
    application then used: one company's letter sent to all the others.
    """
    url = _text(body, "url")
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    if url not in _load_jobs():
        return _api_error("not_found", "Job not found", 404)
    from core.shared_config import update_job
    update_job(url, cover_letter=_text(body, "cover_letter", 10000))
    return {"success": True}


@app.post("/jobs/add")
async def add_job_manually(body: dict):
    """Manually add a job URL to the collection queue."""
    from datetime import datetime, timezone
    from core.shared_config import mutate_jobs, validate_job_url

    url = _text(body, "url")
    if not url:
        return _api_error("missing_field", "Job URL is required", 400)
    if not await asyncio.to_thread(validate_job_url, url):  # resolves the host: off the event loop
        return _api_error("invalid_url", "Invalid or internal URL", 400)

    entry = {
        "url": url,
        "title": _text(body, "title", 200) or "Manually Added",
        "company": _text(body, "company", 200),
        "location": _text(body, "location", 200),
        # A title or company typed by the user is never renamed automatically
        # (the other one is still filled in from the page or by the agent).
        **({"user_named": typed} if (typed := [f for f in ("title", "company") if (body.get(f) or "").strip()]) else {}),
        **({"name_source": "user"} if len(typed) == 2 else {}),
        "easy_apply": None,
        "status": "pending",
        "source": body.get("source", "manual"),
        "collected_at": datetime.now(timezone.utc).isoformat(),
        "applied_at": None,
        "error": None,
    }

    def add(jobs):
        if url in jobs:
            return False
        jobs[url] = entry
        return True

    if not mutate_jobs(add):
        return _api_error("duplicate", "Job already exists in the queue", 409)
    _schedule_posting_details([url])
    return {"success": True, "url": url}


def _schedule_posting_details(urls: list[str]) -> None:
    """Read each new link's real title and company (and whether it's closed) in the background."""
    from core.shared_config import refresh_posting_details

    async def _run():
        for u in urls:
            try:
                await asyncio.to_thread(refresh_posting_details, u)
            except Exception as e:
                _log.warning(f"Couldn't read posting details for {u[:80]}: {e}")

    try:
        asyncio.get_running_loop().create_task(_run())
    except RuntimeError:
        pass


@app.post("/jobs/add-bulk")
async def add_jobs_bulk(body: AddJobsBulkRequest):
    """Add many job URLs at once, from a pasted block or an uploaded file.

    Accepts either a list of URLs or one raw text blob with one URL per line.
    Blank lines and anything that is not a valid external URL are skipped and
    reported back rather than failing the whole batch.
    """
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor
    from core.shared_config import mutate_jobs, validate_job_url

    # Accept both shapes: a list, and/or a pasted/uploaded text blob.
    raw_lines: list[str] = []
    if body.urls:
        raw_lines.extend(body.urls)
    if body.text:
        raw_lines.extend(body.text.splitlines())

    # Strip whitespace and common list decorations (bullets, quotes, commas).
    candidates: list[str] = []
    for line in raw_lines:
        cleaned = (line or "").strip().strip('"\'').lstrip("-*•\t ").rstrip(",;").strip()
        if cleaned:
            candidates.append(cleaned)

    now = datetime.now(timezone.utc).isoformat()
    unique = list(dict.fromkeys(candidates))
    skipped_duplicate = len(candidates) - len(unique)
    # Each check resolves the host name: in a few threads, off the event loop
    # (a slow DNS answer used to stall status and Stop requests).
    with ThreadPoolExecutor(max_workers=8) as pool:
        valid = await asyncio.to_thread(lambda: list(pool.map(validate_job_url, unique)))
    skipped_invalid = [url[:120] for url, ok in zip(unique, valid) if not ok]
    invalid_count = len(skipped_invalid)
    skipped_invalid = skipped_invalid[:10]  # a short sample for the UI
    to_add = [url for url, ok in zip(unique, valid) if ok]

    def add_all(jobs):
        added = []
        for url in to_add:
            if url in jobs:
                continue
            jobs[url] = {
            "url": url,
            "title": "Pasted Link",
            "company": "",
            "location": "",
            # Pasted links are external applications by definition, so they flow
            # into the External and All apply modes.
            "easy_apply": False,
            "status": "pending",
            "source": body.source or "pasted",
            "collected_at": now,
            "applied_at": None,
            "error": None,
            }
            added.append(url)
        return added

    added = mutate_jobs(add_all) if to_add else []
    skipped_duplicate += len(to_add) - len(added)
    if added:
        _schedule_posting_details(added)

    return {
        "success": True,
        "added": len(added),
        "skipped_duplicate": skipped_duplicate,
        "skipped_invalid": invalid_count,
        "invalid_samples": skipped_invalid,
        "total_submitted": len(candidates),
    }


@app.delete("/jobs")
async def delete_jobs(body: dict):
    """Delete one or more jobs by URL."""
    from core.shared_config import mutate_jobs

    urls = body.get("urls", [])
    if not urls or not isinstance(urls, list):
        return _api_error("missing_field", "urls array is required", 400)
    if len(urls) > 10000:
        return _api_error("too_many", "At most 10000 jobs at a time", 400)

    def delete(jobs):
        gone = [url for url in dict.fromkeys(u for u in urls if isinstance(u, str)) if url in jobs]  # each once
        for url in gone:
            del jobs[url]
        return len(gone)

    return {"success": True, "deleted": mutate_jobs(delete)}


# ── Collection: what was left out, and saved searches ─────────────────────
@app.get("/jobs/filtered")
async def get_filtered_jobs(limit: int = Query(200, ge=1, le=1000)):
    """Listings the collector left out (they didn't fit the search), newest first, with why."""
    from core import job_search
    rows = [{**entry, "url": url} for url, entry in job_search.filtered_jobs().items() if isinstance(entry, dict)]
    rows.sort(key=lambda r: str(r.get("filtered_at") or ""), reverse=True)
    return {"success": True, "total": len(rows), "jobs": rows[:limit]}


@app.post("/jobs/filtered/keep")
async def keep_filtered_job(body: dict):
    """Add a left-out listing to the job list after all (as pending)."""
    from core import job_search
    from cli import collect_jobs
    from core.shared_config import validate_job_url
    url = body.get("url") if isinstance(body.get("url"), str) else ""
    if not url:
        return _api_error("missing_field", "url is required", 400)
    if not validate_job_url(url, resolve=False):
        return _api_error("invalid_url", "Invalid or internal URL", 400)
    def save(entry):
        return collect_jobs.save_new_jobs([{**entry, "url": url}], str(entry.get("search_title") or ""),
                                          str(entry.get("source") or "collected"))
    added = job_search.keep_filtered(url, save)
    if added is None:
        return _api_error("not_found", "That listing isn't in the left-out list", 404)
    return {"success": True, "added": added}


@app.delete("/jobs/filtered")
async def clear_filtered_jobs():
    """Forget the left-out listings (a later collection may find them again)."""
    from core import job_search
    return {"success": True, "cleared": job_search.clear_filtered()}


@app.get("/jobs/searches")
async def get_saved_searches():
    from core import job_search
    return {"success": True, "searches": job_search.saved_searches()}


@app.put("/jobs/searches")
async def put_saved_searches(body: dict):
    """Replace the saved searches (at most 50; each checked, malformed ones dropped)."""
    from core import job_search
    items = body.get("searches")
    if not isinstance(items, list):
        return _api_error("invalid_searches", "searches must be a list", 400)
    if len(items) > job_search.MAX_SEARCHES:
        return _api_error("too_many", f"At most {job_search.MAX_SEARCHES} saved searches", 400)
    return {"success": True, "searches": job_search.save_searches(items)}


# ── Auth / Login Sessions ─────────────────────────────────────────────────
_login_browser_process = None  # track the login browser subprocess

def _get_browser_profile_dir() -> str:
    """Automation profile dir for the browser picked in Settings (the same one the agents use).

    For the original shared profile, migrates from old per-worker profiles if needed.
    """
    from core.browsers import resolve_browser
    data_dir = get_data_dir()
    _, profile = resolve_browser(load_settings(), data_dir / "browser_profile")
    profile.mkdir(parents=True, exist_ok=True)
    if profile != data_dir / "browser_profile":
        return str(profile)

    # Auto-migrate: if shared profile has no cookies but an old one does, copy it
    if not (profile / "Default" / "Cookies").exists():
        import shutil
        for old_name in ["browser_profile_w1", "browser_profile_collect"]:
            old = data_dir / old_name
            if (old / "Default" / "Cookies").exists():
                for item in old.iterdir():
                    dest = profile / item.name
                    if not dest.exists():
                        if item.is_dir():
                            shutil.copytree(item, dest)
                        else:
                            shutil.copy2(item, dest)
                _log.info(f"Migrated browser profile from {old_name}")
                break

    return str(profile)


# ── Inbox (Gmail check, prep sheets) ──────────────────────────────────────
def _inbox_busy() -> bool:
    """The Gmail check shares the agents' browser profile: never during a run or a sign-in.

    The Inbox marks itself checking before asking, and a start claims under the same
    lock, so the two can't both go ahead.
    """
    with _browser_lock:
        return bool(_browser_user(exclude="inbox"))


def _inbox_checking() -> bool:
    try:
        from core import inbox
        return bool(inbox._state.get("checking"))
    except Exception:
        return False


def _performance_mode() -> bool:
    return bool(load_settings().get("performance_mode"))


def _inbox_check_now() -> dict:
    """One full Gmail check (runs in the scheduler thread, with its own event loop)."""
    from core import inbox
    from core.llm_factory import create_llm
    import core.shared_config as shared
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return {"error": "No AI model set up. Choose one in LLM Settings first.", "new": 0}
    llm = create_llm(llm_settings)
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(inbox.run_check(llm, shared.build_browser_kwargs(purpose="inbox"), shared.read_jobs, shared.update_job))
    finally:
        loop.close()


def _start_inbox_scheduler() -> None:
    try:
        from core import inbox
        inbox.start_scheduler(_inbox_busy, _inbox_check_now)
    except Exception as e:
        _log.warning(f"Inbox scheduler didn't start: {e}")


@app.get("/inbox")
async def get_inbox():
    from core import inbox
    return {**inbox.status(), "busy": _inbox_busy(), "performance_mode": _performance_mode()}


@app.post("/inbox/check")
async def check_inbox():
    """Check Gmail now (or as soon as the current application run ends)."""
    from core import inbox
    if not load_llm_settings().get("provider"):
        return _api_error("no_llm", "No AI model set up. Choose one in LLM Settings first.")
    inbox.request_check()
    return {"success": True}


@app.put("/inbox/schedule")
async def set_inbox_schedule(body: dict):
    from core import inbox
    if not inbox.set_schedule(str(body.get("schedule") or "")):
        return _api_error("invalid_schedule", "Schedule must be off, daily or weekly", 400)
    return {"success": True, "schedule": body["schedule"]}


@app.post("/inbox/prep")
async def make_prep_sheet(body: dict):
    """Write an interview prep sheet for one email (only when the user asks)."""
    from core import inbox, job_page
    from core.llm_factory import create_llm, _friendly_llm_error
    from core.shared_config import read_jobs
    if _performance_mode():
        return _api_error("performance_mode", "Prep sheets are off in performance mode (Settings).", 400)
    email = inbox.find(str(body.get("id") or ""))
    if email is None:
        return _api_error("not_found", "Email not found", 404)
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return _api_error("no_llm", "No AI model set up. Choose one in LLM Settings first.")
    job = read_jobs().get(email.get("job_url") or "") if email.get("job_url") else None
    posting = await asyncio.to_thread(job_page.fetch, email["job_url"]) if job else None
    from cli.apply_jobs import resume_text
    resume = resume_text(load_settings().get("resume_path") or "")
    try:
        text = await inbox.write_prep_sheet(create_llm(llm_settings), email, job,
                                            getattr(posting, "description", "") or "", resume, load_profile(),
                                            settings=llm_settings)
    except Exception as e:
        return _api_error("llm_error", f"Couldn't write the prep sheet: {_friendly_llm_error(str(e), getattr(e, 'status_code', None))}", 500)
    inbox.save_prep(email["id"], text)
    return {"success": True, "prep": text}


@app.get("/auth/status")
async def auth_status():
    """Check if LinkedIn and Gmail sessions are valid by inspecting cookies."""
    import sqlite3
    profile_dir = _get_browser_profile_dir()
    # Chromium 96+ keeps cookies in Default/Network/Cookies; older builds in Default/Cookies.
    cookies_db = next(
        (p for p in (Path(profile_dir) / "Default" / "Network" / "Cookies", Path(profile_dir) / "Default" / "Cookies") if p.exists()),
        None,
    )

    result = {"linkedin": {"logged_in": False}, "gmail": {"logged_in": False},
              "login_window_open": _login_process is not None and _login_process.poll() is None}

    if cookies_db is None:
        return result

    try:
        import shutil, tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir) / "Cookies"
            shutil.copy2(cookies_db, tmp)

            conn = sqlite3.connect(str(tmp))
            try:
                cursor = conn.cursor()
                cursor.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin.com' AND name = 'li_at'"
                )
                li_count = cursor.fetchone()[0]
                result["linkedin"]["logged_in"] = li_count > 0

                cursor.execute(
                    "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%google.com' AND name IN ('SID', 'SSID', 'HSID')"
                )
                gmail_count = cursor.fetchone()[0]
                result["gmail"]["logged_in"] = gmail_count >= 2
            finally:
                conn.close()
    except Exception as e:
        _log.warning(f"Cookie check failed: {e}")

    return result


_login_running = False
_login_process: subprocess.Popen | None = None

@app.post("/auth/login/{service}")
async def auth_login(service: str):
    """Open a user-controlled sign-in window in the collector's browser profile.

    The window is a plain browser process (the browser picked in Settings, or
    Playwright's Chromium in automatic mode) started with the agents' profile
    folder: no automation flags and no debugging connection. Google refuses
    sign-ins in browsers it sees as automated ("This browser or app may not be
    secure"), which a Playwright-driven window can trigger. The cookies land in
    the same profile the agents use, so one sign-in lasts across runs.
    """
    global _login_running, _login_process

    if service == "gmail":
        url = "https://accounts.google.com/ServiceLogin?service=mail&continue=https://mail.google.com/mail/"
        name = "Gmail"
    else:
        # Only bundled boards can launch an unrestricted, user-operated login.
        # An imported plugin or caller-supplied URL must not gain this authority.
        plugin = _get_plugin_registry().get_by_name(service)
        if not plugin or not plugin.is_builtin or not plugin.login_url:
            return {"success": False, "message": f"Unknown service: {service}"}
        from urllib.parse import urlsplit
        try:
            target = urlsplit(plugin.login_url)
            valid = (target.scheme == "https" and target.hostname and not target.username
                     and not target.password and target.port in (None, 443))
        except ValueError:
            valid = False
        if not valid:
            return {"success": False, "message": "This board has no valid HTTPS sign-in address."}
        url, name = plugin.login_url, plugin.display_name
    from core.browsers import resolve_browser
    from core.shared_config import forget_open_tabs
    with _browser_lock:  # the window is the profile's user from here until it's closed
        busy = _browser_user()
        if busy == "login":
            return {"success": False, "message": "A sign-in window is already open. Finish there, then close it."}
        if busy:
            return {"success": False, "message": _BROWSER_BUSY[busy]}
        # Profile preparation/migration must also wait until its current user
        # has finished, not just the actual browser launch.
        profile_dir = _get_browser_profile_dir()
        chosen, _ = resolve_browser(load_settings(), get_data_dir() / "browser_profile")
        executable = chosen or _find_playwright_chromium()
        if not executable:
            return {"success": False, "message": "No browser found. Pick one in Settings → Browser."}
        args = [executable, f"--user-data-dir={profile_dir}", "--no-first-run", "--no-default-browser-check",
                "--new-window", url]
        forget_open_tabs(profile_dir)
        try:
            _login_process = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as e:
            _log.error(f"Failed to open sign-in window: {e}")
            return {"success": False, "message": f"Couldn't open the browser: {e}"}
    _login_running = True
    _log.info(f"Opened {service} sign-in window ({Path(executable).name}) with profile {profile_dir}")
    return {"success": True, "message": f"Sign in to {name} in the window that opened, then close it."}


# ── Memory ────────────────────────────────────────────────────────────────
@app.get("/memory/stats")
async def get_memory_stats():
    store = _get_memory_store()
    if store:
        return store.get_stats()
    return {"total_memories": 0, "unique_domains": 0, "by_category": {}}


@app.get("/memory/domains")
async def get_memory_domains():
    store = _get_memory_store()
    if store:
        return store.get_all_domains()
    return []


@app.get("/memory/domain/{domain}")
async def get_memories_for_domain(domain: str, limit: int = Query(50)):
    store = _get_memory_store()
    if store:
        return store.search(website_domain=domain, success_only=False, limit=limit)
    return []


@app.get("/memory/search")
async def search_memories(q: str = Query(""), limit: int = Query(50)):
    store = _get_memory_store()
    if not store or not q:
        return []
    # Simple text search across all memories
    all_mems = store.export_all()
    q_lower = q.lower()
    results = [m for m in all_mems if q_lower in m.get("content", "").lower() or q_lower in m.get("website_domain", "").lower()]
    return results[:limit]


@app.post("/memory/decay")
async def decay_memories(body: DecayRequest):
    store = _get_memory_store()
    if store:
        affected = store.decay_confidence(days_old=body.days, decay_factor=body.factor)
        return {"success": True, "affected": affected}
    return {"success": False, "message": "Memory store not available"}


@app.post("/memory/cleanup")
async def cleanup_memories(body: CleanupRequest):
    store = _get_memory_store()
    if store:
        deleted = store.delete_low_confidence(threshold=body.threshold)
        return {"success": True, "deleted": deleted}
    return {"success": False, "message": "Memory store not available"}


@app.get("/memory/export")
async def export_memories():
    store = _get_memory_store()
    if store:
        return store.export_all()
    return []


# ── Profile: Parse Resume ─────────────────────────────────────────────────
def _inside_home(path: Path) -> bool:
    """`path` (resolved) is in the user's home folder (or /tmp off Windows), by path parts.

    A text prefix check let C:/Users/ada2/... pass as inside C:/Users/ada (and /tmpx as /tmp).
    """
    roots = [Path.home().resolve()] + ([Path("/tmp").resolve()] if sys.platform != "win32" else [])
    return any(path.is_relative_to(root) for root in roots)


@app.post("/profile/parse-resume")
async def parse_resume_to_profile():
    """Read the user's resume PDF and use the configured LLM to extract profile fields."""
    import re as _re

    settings = load_settings()
    resume_path = settings.get("resume_path", "").strip()
    if not resume_path:
        return {"success": False, "message": "No resume path configured. Set it in Settings first."}

    resume_file = Path(resume_path).resolve()
    if not resume_file.exists():
        return {"success": False, "message": f"Resume file not found: {resume_path}"}
    if resume_file.suffix.lower() != ".pdf":
        return {"success": False, "message": "Resume must be a PDF file."}
    if not _inside_home(resume_file):
        return {"success": False, "message": "Resume path must be within your home directory."}

    try:
        import fitz  # pymupdf
        doc = fitz.open(str(resume_file))
        resume_text = ""
        for page in doc:
            resume_text += page.get_text()
        doc.close()
        resume_text = resume_text.strip()
        if not resume_text:
            return {"success": False, "message": "Could not extract any text from the resume PDF."}
    except Exception as e:
        return {"success": False, "message": f"Failed to read resume PDF: {e}"}

    # 2. Send to LLM for structured extraction
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return {"success": False, "message": "No LLM provider configured. Set it in LLM Settings first."}

    try:
        from core.llm_factory import create_llm
        llm = create_llm(llm_settings)
    except Exception as e:
        return {"success": False, "message": f"Failed to create LLM: {e}"}

    prompt = f"""Extract candidate profile information from this resume text. Return ONLY a valid JSON object with these fields (use empty string "" for missing text fields, 0 for missing numbers, empty array [] for missing lists, false for missing booleans):

{{
  "name": "Full Name",
  "email": "email@example.com",
  "phone": "phone number",
  "address": {{"street": "", "city": "City", "state": "State", "zip": "ZIP", "country": "USA"}},
  "work_authorization": "Authorized to work in the US",
  "visa_sponsorship_needed": false,
  "willing_to_relocate": false,
  "preferred_work_mode": "hybrid",
  "years_of_experience": 0,
  "education": {{"degree": "Degree Name", "major": "Field of Study", "school": "University Name", "start_date": "Month Year started", "graduation": "Year or Date"}},
  "current_role": "Current or Most Recent Job Title",
  "target_job_titles": ["Job Title 1", "Job Title 2"],
  "target_locations": ["City, State"],
  "languages": ["English"],
  "skills": ["Skill 1", "Skill 2"],
  "salary_expectation": {{"min": 50000, "max": 100000, "currency": "USD"}},
  "notes": ""
}}

Rules:
- Extract the MOST RECENT job title as current_role
- For target_job_titles, infer 2-4 relevant titles based on their experience and skills
- For target_locations, use the candidate's current city/state if mentioned
- For skills, extract ALL mentioned technical and soft skills
- For years_of_experience, calculate from work history dates if possible
- For education, use the highest degree
- Return ONLY the JSON object, no other text

RESUME TEXT:
{resume_text[:6000]}"""

    try:
        from browser_use.llm.messages import UserMessage
        from core.llm_factory import ainvoke_with_fallback, completion_text
        response = await ainvoke_with_fallback(llm, llm_settings, [UserMessage(content=prompt)])
        response_text = completion_text(response)

        # Parse JSON from response
        match = _re.search(r"\{.*\}", response_text, _re.DOTALL)
        if not match:
            return {"success": False, "message": "LLM did not return valid JSON. Try again."}

        parsed_profile = json.loads(match.group())

        # Normalize skills: LLM sometimes returns {technical_skills: [...], soft_skills: [...]}
        if isinstance(parsed_profile.get("skills"), dict):
            skills_dict = parsed_profile["skills"]
            flat_skills = []
            for v in skills_dict.values():
                if isinstance(v, list):
                    flat_skills.extend(v)
                elif isinstance(v, str) and v.strip():
                    flat_skills.append(v)
            parsed_profile["skills"] = flat_skills

        # Merge with existing profile — overwrite with parsed values when they're non-empty
        existing = load_profile()
        for key, value in parsed_profile.items():
            if key not in existing:
                existing[key] = value
                continue
            if isinstance(value, str) and value.strip():
                existing[key] = value
            elif isinstance(value, list) and value:
                existing[key] = value
            elif isinstance(value, dict):
                if key not in existing or not isinstance(existing[key], dict):
                    existing[key] = value
                else:
                    for sub_key, sub_val in value.items():
                        if isinstance(sub_val, str) and sub_val.strip():
                            existing[key][sub_key] = sub_val
                        elif isinstance(sub_val, (int, float)) and sub_val > 0:
                            existing[key][sub_key] = sub_val
            elif isinstance(value, (int, float)) and value > 0:
                existing[key] = value

        # Save the merged profile
        save_profile(existing)

        # Count how many fields were filled
        filled_count = 0
        for key, value in parsed_profile.items():
            if isinstance(value, str) and value.strip():
                filled_count += 1
            elif isinstance(value, list) and value:
                filled_count += 1
            elif isinstance(value, dict):
                for sv in value.values():
                    if isinstance(sv, str) and sv.strip():
                        filled_count += 1
                        break
            elif isinstance(value, (int, float)) and value > 0:
                filled_count += 1

        return {
            "success": True,
            "message": f"Extracted {filled_count} fields from your resume!",
            "profile": existing,
            "fields_filled": filled_count,
        }

    except json.JSONDecodeError:
        return {"success": False, "message": "LLM returned invalid JSON. Try again."}
    except Exception as e:
        from core.llm_factory import _friendly_llm_error
        return {"success": False, "message": f"LLM parsing failed: {_friendly_llm_error(str(e), getattr(e, 'status_code', None))}"}


# ── Setup Status ──────────────────────────────────────────────────────────
def _llm_ready(llm: dict) -> bool:
    """An AI model is set up: a provider and what it needs (a key, a server address or a sign-in)."""
    llm_done = False
    provider = llm.get("provider", "")
    if provider == "openai":
        llm_done = bool((llm.get("openai") or {}).get("api_key", "").strip())
    elif provider == "anthropic":
        llm_done = bool((llm.get("anthropic") or {}).get("api_key", "").strip())
    elif provider == "bedrock":
        bedrock = llm.get("bedrock") or {}
        if bedrock.get("auth_mode") == "keys":
            llm_done = bool(bedrock.get("access_key", "").strip() and bedrock.get("secret_key", "").strip())
        else:
            # Profile mode — considered configured even with default profile
            llm_done = True
    elif provider == "openrouter":
        llm_done = bool((llm.get("openrouter") or {}).get("api_key", "").strip())
    elif provider == "ollama":
        ollama = llm.get("ollama") or {}
        llm_done = bool(ollama.get("base_url", "").strip())
    elif provider == "gemini":
        llm_done = bool((llm.get("gemini") or {}).get("api_key", "").strip())
    elif provider == "openai_compatible":
        compat = llm.get("openai_compatible") or {}
        llm_done = bool(compat.get("base_url", "").strip())
    elif provider == "chatgpt":
        from core import chatgpt_auth
        llm_done = chatgpt_auth.load() is not None
    return llm_done


def _start_problem(apply: bool) -> str:
    """Why a run can't start yet ("" when it can): without these, every job would just fail."""
    if not _llm_ready(load_llm_settings()):
        return "Set up an AI model in LLM Settings first."
    if apply:
        resume = (load_settings().get("resume_path") or "").strip()
        if not resume or not Path(resume).is_file():
            return "Choose your resume PDF in Settings first."
        if not (load_profile().get("name") or "").strip():
            return "Fill in your name in Profile first."
    return ""


@app.get("/setup/status")
async def get_setup_status():
    """Check which onboarding steps are complete."""
    profile = load_profile()
    llm = load_llm_settings()
    settings = load_settings()

    profile_done = bool(profile.get("name", "").strip())
    llm_done = _llm_ready(llm)
    resume_done = bool(settings.get("resume_path", "").strip())
    onboarding_completed = settings.get("onboarding_completed", False)

    # Check Chromium
    chromium_done = _find_playwright_chromium() is not None

    # Check login status
    auth = await auth_status()
    linkedin_done = auth.get("linkedin", {}).get("logged_in", False)
    gmail_done = auth.get("gmail", {}).get("logged_in", False)

    return {
        "profile": profile_done,
        "llm": llm_done,
        "resume": resume_done,
        "chromium": chromium_done,
        "linkedin": linkedin_done,
        "gmail": gmail_done,
        "onboarding_completed": onboarding_completed,
        "all_required_done": profile_done and llm_done and resume_done and linkedin_done,
    }


@app.post("/setup/complete-onboarding")
async def complete_onboarding():
    """Mark the onboarding wizard as completed."""
    settings = load_settings()
    settings["onboarding_completed"] = True
    save_settings(settings)
    return {"success": True}


# ── Metrics / Dashboard ───────────────────────────────────────────────────
@app.get("/dashboard")
async def get_dashboard():
    stats = await get_job_stats()
    mem_stats = await get_memory_stats()

    # Try to get metrics data
    metrics_data = {}
    metrics = _get_metrics_store()
    if metrics:
        try:
            metrics_data = {
                "overall": metrics.get_overall_stats(),
                "memory_impact": metrics.get_memory_impact(),
                "domain_stats": metrics.get_domain_stats(),
                "trend": metrics.get_trend(window_size=5),
                "recent_runs": metrics.get_all_runs(limit=10),
            }
        except (OSError, ValueError) as e:
            _log.warning(f"Failed to load metrics: {e}")

    return {
        "jobs": stats,
        "memory": mem_stats,
        "metrics": metrics_data,
    }


@app.get("/metrics/runs")
async def get_metric_runs(limit: int = Query(50)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_all_runs(limit=limit)
    return []


@app.get("/metrics/domains")
async def get_metric_domains():
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_domain_stats()
    return []


# ── Logs ──────────────────────────────────────────────────────────────────
@app.get("/logs/runs/{run_id}")
async def get_run_logs(run_id: str, limit: int = Query(500)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_run_logs(run_id, limit=limit)
    return []


@app.get("/logs/recent")
async def get_recent_logs(limit: int = Query(100)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_recent_logs(limit=limit)
    return []


@app.get("/logs/runs")
async def get_runs_with_logs(limit: int = Query(50)):
    metrics = _get_metrics_store()
    if metrics:
        return metrics.get_runs_with_logs(limit=limit)
    return []


# ── Q&A Repository ────────────────────────────────────────────────────────

@app.get("/qa")
async def get_qa(search: str = Query(""), unanswered: bool = Query(False)):
    store = _get_memory_store()
    if not store:
        return []
    return store.qa_list(search=search, unanswered_only=unanswered)

@app.get("/qa/stats")
async def get_qa_stats():
    store = _get_memory_store()
    if not store:
        return {"total": 0, "answered": 0, "unanswered": 0}
    return store.qa_stats()

@app.put("/qa/{qa_id}")
async def update_qa(qa_id: int, body: dict):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    answer = body.get("answer", "")
    if not isinstance(answer, str):
        return _api_error("invalid_field", "answer must be text", 400)
    store.qa_update(qa_id, answer.strip()[:5000])
    return {"success": True}

@app.delete("/qa/{qa_id}")
async def delete_qa(qa_id: int):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    store.qa_delete(qa_id)
    return {"success": True}

@app.post("/qa/{source_id}/merge/{target_id}")
async def merge_qa(source_id: int, target_id: int, force: bool = False):
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    problem = store.qa_merge_problem(source_id, target_id, allow_conflict=force)
    if problem:
        return _api_error("invalid_merge", problem, 409)
    store.qa_merge(source_id, target_id, allow_conflict=force)
    return {"success": True}

@app.post("/qa/auto-squash")
async def auto_squash_qa():
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    merged = store.qa_auto_squash()
    return {"success": True, "merged": merged}

@app.post("/qa/smart-squash")
async def smart_squash_qa():
    """LLM-based semantic deduplication of Q&A questions."""
    store = _get_memory_store()
    if not store:
        return _api_error("no_store", "Memory store not available")
    questions = store.qa_list()
    if len(questions) < 2:
        return {"success": True, "merged": 0}

    from core.llm_factory import create_llm
    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return _api_error("no_llm", "No LLM configured. Set up an LLM provider in Settings first.")

    llm = create_llm(llm_settings)
    q_list = "\n".join(f"[{q['id']}] {q['question']}" for q in questions)
    prompt = (
        "Below is a numbered list of screening questions from job applications. "
        "Find groups of questions that ask the same thing in different words. "
        "Return ONLY a JSON array of merge instructions: [{\"keep\": <id_to_keep>, \"merge\": [<ids_to_merge>]}, ...]. "
        "Only group questions that are truly semantically identical. If no duplicates exist, return [].\n\n"
        f"{q_list}"
    )
    try:
        from browser_use.llm.messages import UserMessage
        from core.llm_factory import ainvoke_with_fallback, completion_text
        response = await ainvoke_with_fallback(llm, llm_settings, [UserMessage(content=prompt)])
        import json as _json
        text = completion_text(response)
        # Extract JSON from response
        import re as _re_mod
        match = _re_mod.search(r"\[.*\]", text, _re_mod.DOTALL)
        if not match:
            return {"success": True, "merged": 0}
        groups = _json.loads(match.group(0))
        known = {q["id"] for q in questions}
        merged_count, skipped = 0, 0
        # The model only suggests: every merge is checked like a manual one (ids that
        # exist, no self-merge or loop, no differing answers); bad suggestions are skipped.
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            keep_id = group.get("keep")
            merge_ids = group.get("merge", [])
            if not isinstance(keep_id, int) or keep_id not in known or not isinstance(merge_ids, list):
                skipped += len(merge_ids) if isinstance(merge_ids, list) else 1
                continue
            for mid in merge_ids:
                if isinstance(mid, int) and mid in known and store.qa_merge(mid, keep_id):
                    merged_count += 1
                else:
                    skipped += 1
        return {"success": True, "merged": merged_count, "skipped": skipped}
    except Exception as e:
        return _api_error("llm_error", f"Smart squash failed: {str(e)}", 500)


# ── Resume Tailoring ──────────────────────────────────────────────────────

@app.post("/resume/tailor")
async def tailor_resumes(body: dict):
    """Generate tailored resumes for one or more jobs."""
    from resume.tailor import tailor_resume
    from core.shared_config import read_jobs, update_job

    job_urls = _str_list(body.get("job_urls", []), limit=200)
    options = body.get("options", {"skills": True, "overview": True, "experience": True, "title": False})
    if job_urls is None or not isinstance(options, dict):
        return _api_error("invalid_field", "job_urls must be a list of links and options an object", 400)

    if not job_urls:
        return _api_error("missing_field", "job_urls is required", 400)

    jobs = read_jobs()
    results = []

    for url in job_urls:
        job = jobs.get(url)
        if not job:
            results.append({"url": url, "status": "error", "message": "Job not found"})
            continue
        description = job.get("description", "")
        if not description:
            # Fallback: use title + company + location as context for tailoring
            title = job.get("title", "")
            company = job.get("company", "")
            location = job.get("location", "")
            if not title:
                results.append({"url": url, "status": "error", "message": "No job description or title available"})
                continue
            description = f"Job Title: {title}\nCompany: {company}\nLocation: {location}\n\nTailor the resume for this role based on the job title and company."
        try:
            result = await tailor_resume(url, description, options)
            update_job(url, tailored_resume_path=result["path"])
            results.append({"url": url, "status": "done", "path": result["path"], "content": result["content"]})
        except Exception as e:
            results.append({"url": url, "status": "error", "message": str(e)})

    return {"success": True, "results": results}


@app.post("/resume/tailor/refine")
async def refine_tailored_resume(body: dict):
    """Refine an existing tailored resume with additional instructions."""
    from resume.tailor import tailor_resume
    from core.shared_config import read_jobs, update_job

    job_url = _text(body, "job_url")
    instruction = _text(body, "instruction")

    if not job_url:
        return _api_error("missing_field", "job_url is required", 400)

    jobs = read_jobs()
    job = jobs.get(job_url)
    if not job:
        return _api_error("not_found", "Job not found", 404)

    description = job.get("description", "")
    options = body.get("options", {"skills": True, "overview": True, "experience": True, "title": False})

    try:
        result = await tailor_resume(job_url, description + f"\n\nADDITIONAL INSTRUCTION: {instruction}", options)
        update_job(job_url, tailored_resume_path=result["path"])
        return {"success": True, "content": result["content"], "path": result["path"]}
    except Exception as e:
        return _api_error("tailor_error", str(e), 500)


@app.post("/resume/tailor/get-by-url")
async def get_tailored_by_url(body: dict):
    """Get tailored resume content by job URL."""
    from resume.tailor import get_tailored_content
    job_url = _text(body, "job_url")
    if not job_url:
        return _api_error("missing_field", "job_url is required", 400)
    result = get_tailored_content(job_url)
    if not result:
        return _api_error("not_found", "No tailored resume for this job", 404)
    return {"success": True, "content": result["content"], "path": result["path"]}


_URL_HASH = re.compile(r"[0-9a-f]{12}")


@app.get("/resume/tailor/{url_hash}")
async def get_tailored_resume(url_hash: str):
    """Get tailored resume content by URL hash."""
    from resume.tailor import _get_tailored_dir
    if not _URL_HASH.fullmatch(url_hash):  # never a path: ..\..\Desktop\notes
        return _api_error("not_found", "No tailored resume found", 404)
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    pdf_path = _get_tailored_dir() / f"{url_hash}.pdf"
    if not md_path.exists():
        return _api_error("not_found", "No tailored resume found", 404)
    return {
        "success": True,
        "content": md_path.read_text(encoding="utf-8"),
        "path": str(pdf_path) if pdf_path.exists() else None,
    }


@app.delete("/resume/tailor/{url_hash}")
async def delete_tailored(url_hash: str):
    """Delete a tailored resume."""
    from resume.tailor import _get_tailored_dir
    if not _URL_HASH.fullmatch(url_hash):
        return _api_error("not_found", "No tailored resume found", 404)
    md_path = _get_tailored_dir() / f"{url_hash}.md"
    pdf_path = _get_tailored_dir() / f"{url_hash}.pdf"
    if md_path.exists():
        md_path.unlink()
    if pdf_path.exists():
        pdf_path.unlink()
    return {"success": True}


# ── Cover Letter Generation ───────────────────────────────────────────────

def _resume_block() -> str:
    """The saved resume's text for one-shot prompts, or "" when there is none to read."""
    from cli.apply_jobs import resume_text
    text = resume_text(load_settings().get("resume_path", ""))
    return f"CANDIDATE RESUME (the source of truth for experience):\n{text}\n\n" if text else ""


def _describe_education(edu: dict) -> str:
    from core.shared_config import _describe_education as describe
    return describe(edu or {})


@app.post("/cover-letter/generate")
async def generate_cover_letter(body: dict):
    """Generate a tailored cover letter using LLM based on job description + profile."""
    job_description = _text(body, "job_description", 20000)
    job_title = _text(body, "job_title", 200)
    company = _text(body, "company", 200)

    if not job_description:
        return _api_error("missing_field", "Job description is required", 400)

    llm_settings = load_llm_settings()
    if not llm_settings.get("provider"):
        return _api_error("no_llm", "No LLM configured. Set up an LLM provider in Settings first.")

    profile = load_profile()

    prompt = (
        "Write a professional cover letter for the following job application.\n\n"
        f"JOB TITLE: {job_title}\n"
        f"COMPANY: {company}\n"
        f"JOB DESCRIPTION:\n{job_description}\n\n"
        f"CANDIDATE PROFILE:\n"
        f"Name: {profile.get('name', '')}\n"
        f"Current Role: {profile.get('current_role', '')}\n"
        f"Years of Experience: {profile.get('years_of_experience', 0)}\n"
        f"Skills: {', '.join(profile.get('skills', []))}\n"
        f"Education: {_describe_education(profile.get('education', {}))}\n"
        f"Languages: {', '.join(profile.get('languages', []))}\n\n"
        f"{_resume_block()}"
        "INSTRUCTIONS:\n"
        "- Write 3-4 paragraphs\n"
        "- Highlight relevant skills and experience that match the job requirements\n"
        "- Be professional but not overly formal\n"
        "- Do NOT make up experience or skills the candidate doesn't have\n"
        "- Output ONLY the cover letter text — no subject line, headers, or metadata\n"
    )

    try:
        from core.llm_factory import create_llm
        from browser_use.llm.messages import UserMessage
        from core.llm_factory import ainvoke_with_fallback, completion_text, _friendly_llm_error
        llm = create_llm(llm_settings)
        response = await ainvoke_with_fallback(llm, llm_settings, [UserMessage(content=prompt)])
        text = completion_text(response)
        return {"success": True, "cover_letter": text.strip()}
    except Exception as e:
        return _api_error("llm_error", f"Cover letter generation failed: {_friendly_llm_error(str(e), getattr(e, 'status_code', None))}", 500)


# ── Countries & Plugins ───────────────────────────────────────────────────

from core.country_config import COUNTRY_CONFIGS, get_country_config, NOTICE_PERIOD_OPTIONS

try:
    from models import PluginImportRequest, PluginToggleRequest
except ImportError:
    from backend.models import PluginImportRequest, PluginToggleRequest

from sources.registry import PluginRegistry

_plugin_registry: PluginRegistry | None = None


def _get_plugin_registry() -> PluginRegistry:
    global _plugin_registry
    if _plugin_registry is None:
        _plugin_registry = PluginRegistry()
    return _plugin_registry


@app.get("/countries")
async def get_countries():
    """Return all supported country configurations."""
    return {"success": True, "countries": COUNTRY_CONFIGS, "notice_period_options": NOTICE_PERIOD_OPTIONS}


@app.get("/countries/{code}")
async def get_country(code: str):
    """Get config for a specific country."""
    if code not in COUNTRY_CONFIGS:
        return _api_error("not_found", f"Country code '{code}' not supported", 404)
    return {"success": True, "config": COUNTRY_CONFIGS[code]}


@app.get("/plugins")
async def get_plugins(country: Optional[str] = Query(None)):
    """List all plugins, optionally filtered by country."""
    registry = _get_plugin_registry()
    if country:
        plugins = registry.get_for_country(country)
    else:
        plugins = registry.get_all()
    return {
        "success": True,
        "plugins": [
            {
                "name": p.name,
                "display_name": p.display_name,
                "version": p.version,
                "author": p.author,
                "description": p.description,
                "countries": p.countries,
                "website": p.website,
                "requires_login": p.requires_login,
                "login_url": p.login_url,
                "is_builtin": p.is_builtin,
                "enabled": p.enabled,
                # Quick collection reads this board's results pages; without it, one job at a time.
                "quick": p.listing is not None,
                # The shared Job search preferences this board takes in its search address.
                "shared_filters": sorted(p.search_params),
                "filters": [
                    {
                        "key": f.key,
                        "label": f.label,
                        "type": f.type,
                        "options": f.options,
                        "url_param": f.url_param,
                        "default": f.default,
                        # Set by the shared Job search preferences: the page doesn't offer it twice.
                        "shared": f.url_param in shared,
                    }
                    for f in p.filters
                ],
            }
            for p in plugins
            for shared in [p.shared_filter_params()]
        ],
    }


@app.post("/plugins/import")
async def import_plugin(body: PluginImportRequest):
    """Import a community plugin from a YAML file path."""
    registry = _get_plugin_registry()
    try:
        plugin = registry.import_plugin(body.file_path)
        return {"success": True, "plugin": {"name": plugin.name, "display_name": plugin.display_name}}
    except (FileNotFoundError, ValueError) as e:
        return _api_error("import_failed", str(e), 400)


@app.put("/plugins/{name}/toggle")
async def toggle_plugin(name: str, body: PluginToggleRequest):
    """Enable or disable a plugin."""
    registry = _get_plugin_registry()
    if not registry.set_enabled(name, body.enabled):
        return _api_error("not_found", f"Plugin '{name}' not found", 404)
    return {"success": True, "name": name, "enabled": body.enabled}


@app.delete("/plugins/{name}")
async def remove_plugin(name: str):
    """Remove a community plugin."""
    registry = _get_plugin_registry()
    try:
        registry.remove_plugin(name)
        return {"success": True}
    except ValueError as e:
        return _api_error("remove_failed", str(e), 400)


@app.post("/plugins/reload")
async def reload_plugins():
    """Reload all plugins from disk."""
    registry = _get_plugin_registry()
    registry.reload()
    return {"success": True, "count": len(registry.get_all())}


# ── Main ──────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    def _handle_sigterm(signum, frame):
        _log.info(f"Received signal {signum} — cleaning up and exiting")
        _kill_browser_processes()
        sys.exit(0)

    # Register SIGTERM handler AFTER uvicorn starts (uvicorn installs its own)
    # signal.signal(signal.SIGTERM, _handle_sigterm)  — disabled, let uvicorn handle signals

    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    _log.info(f"Starting uvicorn on port {port} (pid={os.getpid()}, ppid={os.getppid()})")

    # Use Server directly instead of uvicorn.run() — the latter fails in
    # PyInstaller frozen builds because it can't manage the event loop properly
    # Check if port is already in use (stale process from previous run)
    import socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        sock.close()
        _log.info(f"Port {port} is available")
    except OSError as e:
        _log.error(f"Port {port} already in use: {e}. Killing stale process...")
        sock.close()
        try:
            if sys.platform == "win32":
                # lsof and SIGKILL don't exist on Windows.
                _kill_stale_backend(port)
            else:
                import subprocess as _sp
                result = _sp.run(["lsof", "-ti", f":{port}"], capture_output=True, text=True, timeout=5)
                if result.stdout.strip():
                    for pid in result.stdout.strip().split("\n"):
                        _log.info(f"Killing stale process on port {port}: PID {pid}")
                        try:
                            os.kill(int(pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    import time
                    time.sleep(1)
        except Exception as kill_err:
            _log.warning(f"Could not kill stale process: {kill_err}")

    # Ignore SIGHUP — Tauri sidecar may receive it when parent process group changes
    if sys.platform != "win32":
        signal.signal(signal.SIGHUP, signal.SIG_IGN)

    import asyncio

    if getattr(sys, 'frozen', False):
        # In PyInstaller frozen builds, uvicorn's signal handling and lifespan
        # protocol break. Use a minimal approach: disable lifespan protocol
        # and manage signals ourselves.
        _log.info("Frozen build detected — using uvicorn with lifespan=off")
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="info",
            lifespan="off",
        )
        # Fix: uvicorn 0.30+ requires config.load() to be called to initialize 
        # internal attributes like ssl, http_protocol_class, etc. when not using run().
        config.load()
        server = uvicorn.Server(config)
        from uvicorn.lifespan.off import LifespanOff
        server.lifespan = LifespanOff(config)
        
        server.install_signal_handlers = lambda: None
        import contextlib as _ctxlib
        server.capture_signals = _ctxlib.contextmanager(lambda: (yield))

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        async def _run():
            async with lifespan(app):
                _log.info("Lifespan started, launching server...")
                # Run startup and log the result
                await server.startup()
                _log.info(f"After startup: should_exit={server.should_exit} started={server.started}")
                if server.should_exit:
                    _log.warning("Uvicorn wants to exit — forcing main_loop anyway")
                    server.should_exit = False
                if not server.started:
                    _log.error("Server failed to start!")
                    return
                _log.info("Entering main_loop...")
                await server.main_loop()
                _log.info("main_loop exited, shutting down...")
                await server.shutdown()

        try:
            loop.run_until_complete(_run())
        except (KeyboardInterrupt, SystemExit):
            _log.info("Server stopped")
        finally:
            loop.close()
            _log.info("Uvicorn exited")
    else:
        # Development mode — standard uvicorn.run() works fine
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="info")
