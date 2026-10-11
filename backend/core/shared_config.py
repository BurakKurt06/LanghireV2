"""Shared config, utilities, and credential management."""
import asyncio
import json
import re
import subprocess
import sys
import threading
from pathlib import Path

import boto3
from filelock import FileLock
from browser_use.llm import ChatAWSBedrock

try:
    from core.config import MAX_PARALLEL_JOBS, clamp_parallel_jobs, get_data_dir, split_name
    from core.markers import parse_marker
    from memory import MemoryStore
except ImportError:
    from backend.core.config import MAX_PARALLEL_JOBS, clamp_parallel_jobs, get_data_dir, split_name
    from backend.core.markers import parse_marker
    from backend.memory import MemoryStore

_SOURCE_DIR = Path(__file__).resolve().parent.parent.parent  # project root (or temp dir if frozen)

DATA_DIR = get_data_dir()

# When frozen (PyInstaller), BASE_DIR would be a temp dir — use DATA_DIR instead
BASE_DIR = DATA_DIR if getattr(sys, 'frozen', False) else _SOURCE_DIR

# Use OS data dir for settings/profile (written by UI), project root for jobs/logs
JOBS_FILE = DATA_DIR / "jobs.json"
JOBS_LOCK = DATA_DIR / "jobs.json.lock"
QA_FILE = DATA_DIR / "qa_repository.json"
CANDIDATE_PROFILE = DATA_DIR / "candidate_profile.json"
LOGS_DIR = BASE_DIR / "logs"
RESUMES_DIR = BASE_DIR / "resumes"

# Browser profile ALWAYS in OS data dir (must match backend/main.py login endpoint)
BROWSER_PROFILE_DIR = DATA_DIR / "browser_profile"

AWS_PROFILE = "default"
AWS_REGION = "us-west-2"
MODEL_ID = "us.anthropic.claude-sonnet-4-6"
ADA_CMD: tuple[str, ...] = ()  # Only needed for Amazon internal credential refresh

# Load settings from OS data dir (written by desktop app UI) if available
_settings_file = DATA_DIR / "settings.json"
_ui_settings = json.loads(_settings_file.read_text()) if _settings_file.exists() else {}
SENSITIVE_DATA = _ui_settings.get("sensitive_data", {"email": "", "password": ""})

# Fall back to profile email if sensitive_data email is blank
if not SENSITIVE_DATA.get("email", "").strip():
    _profile_file = DATA_DIR / "candidate_profile.json"
    if _profile_file.exists():
        _profile_data = json.loads(_profile_file.read_text())
        _profile_email = _profile_data.get("email", "").strip()
        if _profile_email:
            SENSITIVE_DATA["email"] = _profile_email

RESUME_PATH = _ui_settings.get("resume_path", "")
BLOCKED_DOMAINS = _ui_settings.get("blocked_domains", ["meeboss.com"])

_PRIVATE_IP_PREFIXES = ("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
                        "172.19.", "172.20.", "172.21.", "172.22.", "172.23.",
                        "172.24.", "172.25.", "172.26.", "172.27.", "172.28.",
                        "172.29.", "172.30.", "172.31.", "0.", "169.254.")

def load_runtime_settings() -> dict:
    """Read settings.json fresh from disk.

    The module-level constants above snapshot settings at import time, which is
    fine for values that cannot change mid-process. Feature switches must not
    use that snapshot: the desktop app writes settings.json while the backend is
    already running, so a stale read would make a toggle appear to do nothing.
    """
    try:
        from core.config import load_settings
    except ImportError:
        from backend.core.config import load_settings
    try:
        return load_settings()
    except Exception:
        return {}


def selected_browser(settings: dict | None = None) -> tuple[str | None, Path]:
    """(executable or None for automatic, automation profile dir) for the browser picked in Settings."""
    try:
        from core.browsers import resolve_browser
    except ImportError:
        from backend.core.browsers import resolve_browser
    if settings is None:
        settings = load_runtime_settings()
    return resolve_browser(settings, BROWSER_PROFILE_DIR)


def build_browser_kwargs(worker: int = 1) -> dict:
    """Extra BrowserSession kwargs implied by the current settings.

    Always drops browser-use's --extensions-on-chrome-urls default: it only
    lets its extensions run on chrome:// pages, and Chrome shows an
    "unsupported command-line flag" warning bar for it. Adds the stealth
    launch flags when stealth is enabled.
    """
    settings = load_runtime_settings()
    executable, profile_dir = selected_browser(settings)
    profile_dir = worker_profile_dir(profile_dir, worker)
    forget_open_tabs(profile_dir)
    kwargs: dict = {
        "ignore_default_args": list(IGNORED_CHROME_DEFAULT_ARGS),
        "user_data_dir": str(profile_dir),
        "permissions": list(GRANTED_PERMISSIONS),
        "block_ip_addresses": True,  # job links are never bare IPs; this keeps the agent off the LAN
    }
    if executable:
        kwargs["executable_path"] = executable
    if not settings.get("stealth_enabled"):
        return kwargs
    try:
        from core.stealth import STEALTH_ARGS
    except ImportError:
        from backend.core.stealth import STEALTH_ARGS
    kwargs["args"] = list(STEALTH_ARGS)
    return kwargs


_SESSION_FILES = ("Current Session", "Current Tabs", "Last Session", "Last Tabs")

def parallel_jobs(settings: dict | None = None) -> int:
    """Applications to run at the same time (Settings → Agent Settings)."""
    if settings is None:
        settings = load_runtime_settings()
    return clamp_parallel_jobs(settings.get("parallel_jobs"))


def worker_profile_dir(profile_dir, worker: int) -> Path:
    """Worker 1 uses the main profile; workers 2+ their own copy of it.

    A browser profile can only be open in one browser at a time: a second
    browser launched on it exits at once.
    """
    profile_dir = Path(profile_dir)
    return profile_dir if worker <= 1 else profile_dir.parent / f"{profile_dir.name}_w{worker}"


# Not copied into a worker's profile: caches (most of a profile's size) and
# files that only mean something to the browser that wrote them.
_PROFILE_COPY_SKIP = {
    "Cache", "Code Cache", "GPUCache", "GPUPersistentCache", "GrShaderCache", "GraphiteDawnCache",
    "DawnGraphiteCache", "DawnWebGPUCache", "ShaderCache", "CacheStorage", "ScriptCache", "Sessions",
    "Crashpad", "component_crx_cache", "extensions_crx_cache", "Safe Browsing",
    "OnDeviceHeadSuggestModel", "optimization_guide_model_store", "lockfile",
    "SingletonLock", "SingletonCookie", "SingletonSocket",
}


def _skip_in_profile_copy(_folder, names):
    return [n for n in names if n in _PROFILE_COPY_SKIP or n.startswith("BrowserMetrics")]


def prepare_worker_profiles(count: int, settings: dict | None = None) -> list[Path]:
    """Fresh copies of the main profile for workers 2..count, so every worker
    has your sign-ins (cookies, saved logins). Copies for workers above
    `count` are removed. Call when no browser is using the main profile."""
    import shutil
    _, base = selected_browser(settings)
    made = []
    for n in range(2, MAX_PARALLEL_JOBS + 1):
        dest = worker_profile_dir(base, n)
        shutil.rmtree(dest, ignore_errors=True)
        if n > count:
            continue
        if base.exists():
            try:
                shutil.copytree(base, dest, ignore=_skip_in_profile_copy, dirs_exist_ok=True)
            except shutil.Error:
                pass  # a file the OS kept locked; the rest is copied
        made.append(dest)
    return made


def forget_open_tabs(profile_dir) -> int:
    """Delete the profile's saved tab list so the next launch opens no old tabs.

    Each job's browser is killed when the job ends, and Brave (or Chrome set to
    "continue where you left off") reopens every tab from the last launch, so
    old postings and Gmail tabs piled up run after run. Cookies and sign-ins are
    stored elsewhere and stay. Files a running browser holds open are skipped.
    """
    removed = 0
    default = Path(profile_dir) / "Default"
    paths = list((default / "Sessions").glob("*")) + [default / name for name in _SESSION_FILES]
    for path in paths:
        try:
            if path.is_file():
                path.unlink()
                removed += 1
        except OSError:
            pass
    return removed


# Granted to every site up front (browser-use's default). Location is not: the
# agent grants it to the job's own site and known application sites only
# (apply_jobs.allow_location).
GRANTED_PERMISSIONS = ("clipboardReadWrite", "notifications")

# browser-use default Chrome flags we don't launch with (see build_browser_kwargs).
IGNORED_CHROME_DEFAULT_ARGS = ["--extensions-on-chrome-urls"]


async def maybe_apply_stealth(browser_session) -> bool:
    """Inject stealth patches when the setting is on. No-op otherwise.

    Starts the session first because the CDP client only exists once the
    browser is live. Any failure is swallowed: anti-detection is best-effort
    and must never abort a run.
    """
    if not load_runtime_settings().get("stealth_enabled"):
        return False
    try:
        from core.stealth import apply_stealth
    except ImportError:
        from backend.core.stealth import apply_stealth
    try:
        await browser_session.start()
        ok = await apply_stealth(browser_session)
        if ok:
            print("    🕶️  Stealth patches injected")
        return ok
    except Exception as e:
        print(f"    ⚠️  Stealth setup skipped ({type(e).__name__}: {e})")
        return False


async def maybe_human_delay(label: str = "") -> float:
    """Pause for a randomised interval when human delays are enabled."""
    settings = load_runtime_settings()
    if not settings.get("human_delay_enabled"):
        return 0.0
    try:
        from core.stealth import human_delay
    except ImportError:
        from backend.core.stealth import human_delay
    lo = settings.get("human_delay_min", 5)
    hi = settings.get("human_delay_max", 15)
    delay = await human_delay(lo, hi)
    if delay > 0:
        print(f"    ⏳ Waiting {delay:.1f}s before {label or 'the next job'}")
    return delay


async def worker_stagger(default_seconds: float = 5.0) -> float:
    """Space out browser launches when several workers start at once.

    Uses the randomised human-delay window when that switch is on, so there is
    a single delay system rather than two competing settings. Falls back to the
    previous fixed pause otherwise, keeping default behaviour unchanged.
    """
    if load_runtime_settings().get("human_delay_enabled"):
        return await maybe_human_delay("the next worker")
    await asyncio.sleep(default_seconds)
    return default_seconds


def is_private_host(host: str, resolve: bool = True) -> bool:
    """True for this computer, the local network and other non-public addresses.

    Checks IP literals in every form (IPv6, mapped, decimal), and with resolve,
    what a hostname resolves to (a public-looking name for 192.168.1.1).
    """
    import ipaddress
    import socket
    host = (host or "").strip("[]").lower()
    if not host or host == "localhost" or host.endswith(".localhost"):
        return True

    def private(value) -> bool:
        try:
            ip = ipaddress.ip_address(value)
        except ValueError:
            return False
        if getattr(ip, "ipv4_mapped", None):
            ip = ip.ipv4_mapped
        return not ip.is_global or ip.is_multicast

    if private(host.split("%")[0]):
        return True
    if host.isdigit():  # http://2130706433/ is 127.0.0.1
        try:
            return private(int(host))
        except ValueError:
            return True
    if not resolve:
        return False
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False  # unresolvable: opening it fails by itself
    return any(private(info[4][0].split("%")[0]) for info in infos)


def validate_job_url(url: str, resolve: bool = True) -> bool:
    """Reject URLs pointing to this computer or private/internal networks (SSRF prevention)."""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if not host or parsed.scheme not in ("http", "https"):
            return False
        if host in ("0.0.0.0",) or any(host.startswith(p) for p in _PRIVATE_IP_PREFIXES):
            return False
        return not is_private_host(host, resolve=resolve)
    except Exception:
        return False


_secret_cache: dict = {"key": None, "values": ()}


def _secret_values() -> tuple[str, ...]:
    """The account password, the 2Captcha key and LLM API keys (re-read when the settings files change)."""
    try:
        from core.config import get_data_dir as _dd
    except ImportError:
        from backend.core.config import get_data_dir as _dd
    files = [_dd() / "settings.json", _dd() / "llm_settings.json"]
    key = tuple((f.stat().st_mtime_ns if f.exists() else 0) for f in files)
    if _secret_cache["key"] != key:
        values = set()
        for f in files:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            stack = [data]
            while stack:
                item = stack.pop()
                if isinstance(item, dict):
                    for k, v in item.items():
                        if isinstance(v, (dict, list)):
                            stack.append(v)
                        elif isinstance(v, str) and len(v) >= 6 and re.search(r"password|api_key|key$|token|secret", k, re.I):
                            values.add(v)
                elif isinstance(item, list):
                    stack.extend(item)
        _secret_cache.update(key=key, values=tuple(sorted(values, key=len, reverse=True)))
    return _secret_cache["values"]


def redact_secrets(text):
    """`text` with the configured password, 2Captcha key and API keys replaced by ***."""
    if not text or not isinstance(text, str):
        return text
    try:
        for value in _secret_values():
            if value in text:
                text = text.replace(value, "***")
    except Exception:
        pass
    return text


def safe_folder_name(name: str) -> str:
    """A folder name Windows accepts and that stays inside its parent: no separators, no '..'."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).replace(" ", "_").strip(". ")
    return cleaned[:80] or "apply"

# ── Singleton memory store ────────────────────────────────────────────────────
_memory_store: MemoryStore | None = None


def get_memory_store() -> MemoryStore:
    """Get or create the singleton memory store instance."""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore()
    return _memory_store


def load_json(path: Path, default=None):
    if path.exists():
        return json.loads(path.read_text())
    return default if default is not None else []


def save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def read_jobs() -> dict:
    """Read jobs.json with cross-process lock."""
    with FileLock(JOBS_LOCK):
        return load_json(JOBS_FILE, {})


def write_jobs(jobs: dict):
    """Write jobs.json with cross-process lock."""
    with FileLock(JOBS_LOCK):
        save_json(JOBS_FILE, jobs)


def update_job(url: str, **fields):
    """Atomically update a single job entry."""
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        if url in jobs:
            jobs[url].update(fields)
            save_json(JOBS_FILE, jobs)


def _first_place(location_texts: list[str]) -> str:
    """'New York, NY' for the first location the geo table can place ("" if none)."""
    try:
        from core import geo
    except ImportError:
        from backend.core import geo
    for text in location_texts:
        try:
            place = geo.resolve(text)
        except Exception:
            return ""
        if place.get("remote"):
            return "Remote"
        if place.get("place"):
            return place["place"]
    return ""


def set_job_names(url: str, title: str = "", company: str = "", source: str = "agent") -> bool:
    """Name a job (role / company) unless the user named it. True when something changed."""
    try:
        from core import job_page
    except ImportError:
        from backend.core import job_page
    title, company = (title or "").strip()[:200], job_page.clean_company(company or "")[:120]
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        job = jobs.get(url)
        if job is None or job.get("name_source") == "user":
            return False
        changed = False
        typed = job.get("user_named") or []
        if title and "title" not in typed and not job_page.is_placeholder(title) and job.get("title") != title:
            job["title"], changed = title, True
        if company and "company" not in typed and not job_page.is_placeholder(company) and job.get("company") != company:
            job["company"], changed = company, True
        if changed:
            job["name_source"] = source
            save_json(JOBS_FILE, jobs)
        return changed


def refresh_posting_details(url: str, posting=None, mark_closed: bool = True):
    """Fill a job's placeholder title / empty company from its page; mark it closed if taken down.

    posting: an already-fetched job_page.Posting (fetched here when None).
    Returns the Posting, or None when the page couldn't be read.
    """
    try:
        from core import job_page
    except ImportError:
        from backend.core import job_page
    if posting is None:
        posting = job_page.fetch(url)
    if posting is None:
        return None
    place = _first_place(posting.locations) if posting.locations else ""
    with FileLock(JOBS_LOCK):
        jobs = load_json(JOBS_FILE, {})
        job = jobs.get(url)
        if job is None:
            return posting
        changed = False
        source = job.get("name_source")
        auto = source in (None, "page")  # never overwrite the user's or the agent's names
        typed = job.get("user_named") or []  # one field typed by the user: only the other is filled in
        if posting.title and auto and "title" not in typed and (job_page.is_placeholder(job.get("title"))
                                       or (source == "page" and job.get("title") != posting.title)):
            job["title"], job["name_source"], changed = posting.title, "page", True
        if posting.company and auto and "company" not in typed and job.get("company") != posting.company and (
                not (job.get("company") or "").strip() or job.get("company") == "Unknown"
                or source == "page" or job.get("source") == "pasted"):
            job["company"], job["name_source"], changed = posting.company, "page", True
        if place and not (job.get("location") or "").strip():
            job["location"], changed = place, True
        if posting.closed and mark_closed and job.get("status") in ("pending", "failed", "needs_you", "skipped"):
            job["status"], job["error"], changed = "closed", posting.reason, True
        if changed:
            save_json(JOBS_FILE, jobs)
    return posting


_claim_lock = threading.Lock()


def claim_job(url: str) -> bool:
    """Atomically claim a pending job. Returns True if claimed, False if already taken.
    Uses both FileLock (cross-process) and threading lock (in-process workers)."""
    with _claim_lock:
        with FileLock(JOBS_LOCK):
            jobs = load_json(JOBS_FILE, {})
            if url in jobs and jobs[url].get("status") == "pending":
                jobs[url]["status"] = "in_progress"
                save_json(JOBS_FILE, jobs)
                return True
            return False


def refresh_credentials():
    """Run ada credentials update and return True on success."""
    if not ADA_CMD:
        # No credential refresh command configured — skip silently
        # Users should configure AWS credentials via the Settings UI or aws cli
        return True
    print("🔑 Refreshing AWS credentials...")
    try:
        subprocess.run(ADA_CMD, check=True, timeout=30)
        print("✅ Credentials refreshed")
        return True
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as e:
        print(f"❌ Credential refresh failed: {e}", file=sys.stderr)
        return False


async def credential_refresh_loop(interval_minutes: int = 14):
    """Background task that refreshes credentials on a timer. Cancels with parent."""
    while True:
        await asyncio.sleep(interval_minutes * 60)
        refresh_credentials()


def get_llm() -> ChatAWSBedrock:
    """Create a fresh LLM client with current credentials."""
    session = boto3.Session(profile_name=AWS_PROFILE, region_name=AWS_REGION)
    return ChatAWSBedrock(model=MODEL_ID, session=session)


def get_fallback_llm():
    """Backup LLM for agents to switch to on provider errors. The backend overrides this."""
    return None


# Pre-filled Q&A answers sent to the agent (site-specific first, then most seen).
QA_PROMPT_LIMIT = 80


def _with_profile_defaults(profile: dict | None) -> dict:
    """The profile with every missing or empty-typed field filled from DEFAULT_PROFILE.

    A saved profile can lack keys (older app versions, hand edits); indexing a
    missing one used to raise KeyError and fail every application.
    """
    try:
        from core.config import DEFAULT_PROFILE
    except ImportError:
        from backend.core.config import DEFAULT_PROFILE
    merged = {**DEFAULT_PROFILE, **(profile or {})}
    for key, default in DEFAULT_PROFILE.items():
        value = merged.get(key)
        if isinstance(default, dict):
            merged[key] = {**default, **(value if isinstance(value, dict) else {})}
        elif isinstance(default, list) and not isinstance(value, list):
            merged[key] = list(default)
        elif value is None:
            merged[key] = default
    return merged


# Profile -> "Screening answers": the questions that sink applications when guessed.
SCREENING_LABELS = {
    "start_date": "Earliest start date",
    "gpa": "GPA",
    "linkedin_url": "LinkedIn profile URL",
    "portfolio_url": "Portfolio / GitHub URL",
    "gender": "Gender (voluntary self-identification)",
    "race_ethnicity": "Race / ethnicity (voluntary self-identification)",
    "veteran_status": "Veteran status (voluntary self-identification)",
    "disability_status": "Disability status (voluntary self-identification)",
}


_IMMEDIATE = re.compile(r"^\s*(immediately|asap|now|right away|any ?time)\s*$", re.I)


def describe_start_date(value: str, date_format: str = "MM/DD/YYYY", today=None) -> str:
    """The "Start date" rule the agent follows.

    The user's rule for "available immediately": a season internship or co-op
    gets about two weeks before that season usually starts, in the posting's
    year (Summer -> May 15, Fall -> Aug 15, Spring/Winter -> Jan 2); any other
    job gets today's date.
    """
    import datetime as _dt
    today = today or _dt.date.today()
    fmt = (date_format.upper().replace("YYYY", "%Y").replace("MM", "%m").replace("DD", "%d"))
    if value and not _IMMEDIATE.match(value):
        return f"Start date: {value}"
    seasons = "; ".join(f"{season} -> {_dt.date(2000, m, d).strftime(fmt).replace('2000', 'YEAR')}"
                        for season, m, d in (("Summer", 5, 15), ("Fall", 8, 15), ("Spring/Winter", 1, 2)))
    return ("Start date: available immediately. For a season internship or co-op, use the posting's year "
            f"(YEAR) with: {seasons}. For any other job, today: {today.strftime(fmt)}.")


def _describe_screening(screening: dict, date_format: str = "MM/DD/YYYY") -> str:
    lines = []
    for key, label in SCREENING_LABELS.items():
        value = str(screening.get(key) or "").strip()
        if key == "start_date" and value:
            lines.append(describe_start_date(value, date_format))
        elif value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines)


def _describe_name(name: str) -> str:
    """'Name: Ada Lovelace (First name: Ada | Last name: Lovelace)'.

    The profile holds one full name; without the split the agent sometimes
    typed all of it into a form's First name field.
    """
    first, last = split_name(name)
    if not last:
        return f"Name: {name}"
    return f"Name: {name} (First name: {first} | Last name: {last})"


def _describe_salary(currency: str, low, high, period: str) -> str:
    """'USD 20-30 (hourly)'; an empty or lower maximum means no upper limit.

    A maximum of 0 used to read 'USD 50,000-0', which the agent could type
    into a form as the maximum.
    """
    try:
        low, high = float(low or 0), float(high or 0)
    except (TypeError, ValueError):
        return "Not specified"

    def fmt(n: float) -> str:
        return f"{n:,.0f}" if n == int(n) else f"{n:,.2f}"

    if low and high > low:
        amount = f"{fmt(low)}-{fmt(high)}"
    elif low:
        amount = fmt(low) if high == low else f"{fmt(low)}+ (no maximum)"
    elif high:
        amount = f"up to {fmt(high)}"
    else:
        return "Not specified"
    return f"{currency} {amount} ({period})"


def _describe_address(address: dict) -> str:
    """The address split into the fields forms ask for.

    Only city/state/zip used to be sent, so the agent took the street line from
    the resume and put the town (e.g. "Springfield") in Address Line 1.
    """
    fields = [
        ("Street address (Address Line 1)", address.get("street")),
        ("City", address.get("city")),
        ("State", address.get("state")),
        ("ZIP / postal code", address.get("zip")),
        ("Country", address.get("country")),
    ]
    known = [f"{label}: {value}" for label, value in fields if (value or "").strip()]
    return "Address: " + (" | ".join(known) if known else "Not specified")


def _describe_work_mode(mode: str) -> str:
    """Spell out 'any' so the agent picks whatever option a form offers."""
    if (mode or "").lower() == "any":
        return "Any (open to remote, hybrid, or on-site)"
    return mode


def _describe_education(edu: dict) -> str:
    """'B.S. in Computer Science from MIT (started September 2023, graduation 2027)', skipping empty parts."""
    degree = edu.get("degree", "")
    major = edu.get("major", "")
    if major:
        degree = f"{degree} in {major}" if degree else major
    start = (edu.get("start_date") or "").strip()
    graduation = (edu.get("graduation") or "").strip()
    when = ", ".join(p for p in (f"started {start}" if start else "",
                                  f"graduation {graduation}" if graduation and start else graduation) if p)
    return f"{degree} from {edu.get('school', '')} ({when})"


def normalize_question(q: str) -> str:
    return re.sub(r"[^\w\s]", "", q.lower()).strip()


def build_memory_context(
    profile: dict,
    qa: dict,
    applied_labels: list[str] | None = None,
    job_url: str | None = None,
    learnings: bool = True,
) -> str:
    """Build the system message context with candidate profile, Q&A bank, and per-website learnings.

    learnings=False (performance mode): no per-step @@LEARNING notes. Saved
    tips are still sent, and the end-of-run summary still adds new ones.
    """
    parts = []
    profile = _with_profile_defaults(profile)

    # Salary formatting (country-aware)
    sal = profile.get('salary_expectation', {})
    sal_currency = sal.get('currency', 'USD') or 'USD'
    sal_min = sal.get('min', 0) or 0
    sal_max = sal.get('max', 0) or 0
    sal_period = sal.get('period', 'annual')
    salary_str = _describe_salary(sal_currency, sal_min, sal_max, sal_period)

    # Country-aware date format
    date_format = profile.get('date_format', '') or 'MM/DD/YYYY'

    profile_lines = [
        "CANDIDATE PROFILE:",
        _describe_name(profile['name']),
        f"Email: {profile['email']}, Phone: {profile.get('phone_country_code', '')}{profile['phone']}",
        _describe_address(profile['address']),
        f"Work Authorization: {profile['work_authorization']}, Visa Sponsorship Needed: {profile['visa_sponsorship_needed']}",
        f"Willing to Relocate: {profile['willing_to_relocate']}, Preferred Work Mode: {_describe_work_mode(profile['preferred_work_mode'])}",
        f"Years of Experience: {profile['years_of_experience']}",
        f"Education: {_describe_education(profile['education'])}",
        *([f"Field of study if yours isn't listed: {profile['education']['major_alternative']}"]
          if (profile['education'].get('major_alternative') or '').strip() else []),
        f"Current Role: {profile['current_role']}",
        f"Target Roles: {', '.join(profile['target_job_titles'])}",
        f"Target Locations: {', '.join(profile['target_locations'])}",
        f"Languages: {', '.join(profile['languages'])}",
        f"Skills: {', '.join(profile['skills'])}",
        f"Salary: {salary_str}",
    ]
    if profile.get('notice_period'):
        profile_lines.append(f"Notice Period: {profile['notice_period']}")
    if profile.get('nationality'):
        profile_lines.append(f"Nationality: {profile['nationality']}")
    if profile.get('date_of_birth'):
        profile_lines.append(f"Date of Birth: {profile['date_of_birth']} (only when a form asks for it)")

    parts.append("\n".join(profile_lines))

    screening = _describe_screening(profile.get('screening') or {}, date_format)
    if screening:
        parts.append("SCREENING ANSWERS (the candidate's own answers):\n" + screening)

    # Profile -> "Instructions for the AI". Framed as rules, not as one more
    # profile line, so the agent follows them.
    if (profile.get('notes') or '').strip():
        parts.append(
            "USER'S INSTRUCTIONS (the candidate wrote these; follow them unless they would "
            "mean submitting false information):\n" + profile['notes'].strip()
        )

    # Country-specific instructions for the agent
    country_instructions = []
    country_instructions.append(f"DATE FORMAT: When filling date fields, use {date_format} format.")
    if profile.get('notice_period'):
        country_instructions.append(f"NOTICE PERIOD: If asked about notice period or availability, answer: {profile['notice_period']}")
    if profile.get('nationality'):
        country_instructions.append(f"NATIONALITY: If asked about nationality, answer: {profile['nationality']}")
    if profile.get('cover_letter'):
        country_instructions.append(f"COVER LETTER: If a cover letter is requested, use:\n{profile['cover_letter']}")
    if country_instructions:
        parts.append("COUNTRY-SPECIFIC INSTRUCTIONS:\n" + "\n".join(country_instructions))

    if applied_labels:
        parts.append("Already applied — SKIP:\n" + "\n".join(f"- {j}" for j in applied_labels))

    # Try SQLite Q&A first, fall back to passed-in dict. The answers ride along
    # on every LLM step, so only the most relevant QA_PROMPT_LIMIT are sent.
    qa_for_prompt = dict(list((qa or {}).items())[:QA_PROMPT_LIMIT])
    confirmed: set[str] = set()
    try:
        store = get_memory_store()
        if store:
            domain = store.extract_domain(job_url) if job_url else ""
            db_qa = store.qa_get_all_for_prompt(domain=domain, limit=QA_PROMPT_LIMIT)
            if db_qa:
                qa_for_prompt = db_qa
            confirmed = store.qa_user_confirmed()
    except Exception:
        pass
    # The user's own answers are facts. Answers the agent recorded on earlier
    # runs may have been guesses, so they only count when they fit the profile.
    user_answers = [(q, a) for q, a in qa_for_prompt.items() if a and q in confirmed]
    earlier_answers = [(q, a) for q, a in qa_for_prompt.items() if a and q not in confirmed]
    if user_answers:
        parts.append("ANSWERS THE USER GAVE (always use these):\n"
                     + "\n".join(f"Q: {q}\nA: {a}" for q, a in user_answers))
    if earlier_answers:
        parts.append("Answers recorded in earlier applications (use one only when it agrees with the "
                     "profile and resume):\n" + "\n".join(f"Q: {q}\nA: {a}" for q, a in earlier_answers))

    # ── Per-website memory injection ──────────────────────────────────────
    if job_url:
        store = get_memory_store()
        memories = store.get_domain_memories(job_url, limit=20)
        if memories:
            domain = store.extract_domain(job_url)
            mem_count = len(memories)
            print(f"    🧠 Injecting {mem_count} memories for {domain}")
            parts.append(store.format_for_prompt(memories))

    tracking = (
        "TRACKING INSTRUCTIONS:\n"
        "After each successful application: @@JOB_APPLIED: {\"title\": \"...\", \"company\": \"...\", \"location\": \"...\"}\n"
        "For each form question encountered: @@QUESTION: {\"question\": \"...\", \"answer\": \"...\", \"type\": \"text|dropdown|radio|checkbox\"}"
    )
    if not learnings:
        parts.append(tracking)
        return "\n\n".join(parts)
    parts.append(
        tracking + "\n\n"
        "SELF-LEARNING — report observations about THIS WEBSITE's UI/flow as you navigate:\n"
        "@@LEARNING: {\"domain\": \"<website domain>\", \"category\": \"navigation|form_strategy|element_interaction|failure_recovery|site_structure|qa_pattern\", \"insight\": \"<specific actionable observation>\"}\n"
        "Examples of good learnings:\n"
        "- Navigation: 'Easy Apply opens a modal overlay, don't navigate away from the page'\n"
        "- Element interaction: 'The checkbox is inside a scrollable div, must scroll to find it'\n"
        "- Form strategy: 'This ATS splits the form into 4 steps: Personal → Resume → Questions → Review'\n"
        "Report only learnings that are new (not already listed above) and would help the next "
        "application on this site: at most 3 per run, each one once (in the step you learn it, not "
        "repeated in later steps' memory)."
    )
    return "\n\n".join(parts)


def extract_from_history(result):
    """Extract applied jobs and questions from agent history."""
    jobs, questions, seen = [], {}, set()
    for item in result.history:
        if not item.model_output:
            continue
        memory = item.model_output.memory or ""
        for m in re.finditer(r"@@JOB_APPLIED:\s*(\{[^}]{1,2000}\})", memory):
            j = parse_marker(m.group(1))
            if j:
                jobs.append(f"{j.get('title','')} at {j.get('company','')} - {j.get('location','')}")
        for m in re.finditer(r"@@QUESTION:\s*(\{[^}]{1,2000}\})", memory):
            q = parse_marker(m.group(1))
            if not q:
                continue
            qtext, ans = str(q.get("question") or "").strip(), str(q.get("answer") or "").strip()
            norm = normalize_question(qtext)
            if qtext and norm not in seen:
                seen.add(norm)
                questions[qtext] = ans
        # Fallback
        if not jobs and any(kw in memory.lower() for kw in ["application submitted", "successfully applied"]):
            for pat in [r"applied to (.+?) via", r"Application submitted for (.+?) via"]:
                match = re.search(pat, memory, re.IGNORECASE)
                if match:
                    jobs.append(match.group(1).strip())
                    break
    return list(dict.fromkeys(jobs)), questions
