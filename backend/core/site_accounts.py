"""Which job sites already have an account for the user's application email: the app's memory of them.

The agent used to guess on every visit whether an account existed (sign in, fail, register...).
Now the app remembers each account it made or used: the agent reports a created account or a
working sign-in (the account_result tool), and a verification email the app read for a site
proves an account there too. Each step tells the agent what the app knows about the site it is on.

Kept per account system, not per applicant-tracking vendor: an Acme Workday account does not
sign in on Globex's Workday. The key is the host, plus the company id where one host serves many
employers (SuccessFactors' company=...). Stored in the app data folder (site_accounts.json):
sites, the email and dates only, never a password (those stay in secret_store).
"""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

try:
    from core import job_page, site_access
    from core.config import get_data_dir
except ImportError:  # imported as backend.core.site_accounts
    from backend.core import job_page, site_access
    from backend.core.config import get_data_dir

EVENTS = ("created", "signed_in", "verified")
_lock = threading.Lock()
# The app's own lines in its run logs that prove an account (a verification email was used for the site).
_LOG_PROOF = re.compile(r"(?:Opened the verification link from the email|Verification code read from the email) for (\S+)")


def _file() -> Path:
    return get_data_dir() / "site_accounts.json"


def _load() -> dict:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(data: dict) -> None:
    path = _file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def account_key(url: str) -> str:
    """The account system a page belongs to: its host, with the company id when the host is shared."""
    host = site_access.host_of(url) if "://" in (url or "") else (url or "").lower().strip()
    if not host:
        return ""
    company = job_page.tenant(url) if "://" in (url or "") else ""
    return f"{host}/{company}" if company and company not in host else host


def _norm(email: str) -> str:
    return (email or "").strip().lower()


def record(url: str, email: str, event: str, when: str | None = None) -> bool:
    """Remember an account on this site for `email` ("created", "signed_in" or "verified")."""
    key, email = account_key(url), _norm(email)
    if not key or not email or event not in EVENTS:
        return False
    stamp = when or datetime.now(timezone.utc).isoformat()
    with _lock:
        data = _load()
        accounts = data.setdefault("accounts", {})
        entry = accounts.get(key) if isinstance(accounts.get(key), dict) else {}
        if entry.get("email") != email:
            entry = {"email": email}            # another application email: the old account isn't this one
        entry.setdefault("since", stamp)
        entry[f"{event}_at"] = stamp
        accounts[key] = entry
        _save(data)
    return True


def known(url: str, email: str) -> dict | None:
    """What the app knows about an account on this site for `email`, or None."""
    _backfill_once(email)
    key = account_key(url)
    entry = _load().get("accounts", {}).get(key) if key else None
    if isinstance(entry, dict) and entry.get("email") == _norm(email):
        return {"site": key, **entry}
    return None


def entries(email: str | None = None) -> list[dict]:
    """Every remembered account (for Settings), newest first."""
    if email:
        _backfill_once(email)
    rows = [{"site": key, **entry} for key, entry in _load().get("accounts", {}).items() if isinstance(entry, dict)]
    return sorted(rows, key=lambda r: r.get("since", ""), reverse=True)


def forget(site: str) -> bool:
    """Drop one remembered account (the user says there is none, or it was deleted)."""
    with _lock:
        data = _load()
        if site not in data.get("accounts", {}):
            return False
        del data["accounts"][site]
        _save(data)
    return True


def describe(entry: dict) -> str:
    """'created Nov 14, 2026, email verified' for the agent and the app."""
    parts = []
    for event, words in (("created", "created"), ("signed_in", "last signed in"), ("verified", "email verified")):
        stamp = entry.get(f"{event}_at")
        if stamp:
            try:
                day = datetime.fromisoformat(stamp).strftime("%b %d, %Y").replace(" 0", " ")
            except ValueError:
                day = ""
            parts.append(f"{words} {day}".strip())
    return ", ".join(parts) or "used before"


def _backfill_once(email: str) -> None:
    """Accounts proven by the app's run logs before this memory existed (verification emails it used)."""
    if not _norm(email):
        return
    with _lock:
        data = _load()
        if data.get("backfilled"):
            return
        data["backfilled"] = datetime.now(timezone.utc).isoformat()
        _save(data)
    try:
        try:
            from memory.metrics import MetricsStore
        except ImportError:
            from backend.memory.metrics import MetricsStore
        store = MetricsStore()
        rows = store._get_conn().execute(
            "SELECT timestamp, message FROM run_logs WHERE message LIKE '%from the email for %' ORDER BY id").fetchall()
        store.close()
    except Exception:
        return
    for stamp, message in rows:
        found = _LOG_PROOF.search(message or "")
        if found:
            record(found.group(1).strip(".,"), email, "verified", when=stamp)
