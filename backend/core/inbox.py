"""Inbox: read job emails from Gmail, sort them, match them to jobs, write prep sheets.

Gmail is read through the user's existing sign-in in the agents' browser
profile (Settings -> Accounts): a browser window opens Gmail's search for job
mail since the last check, and the result rows and message texts are read
with code. The AI only sorts the emails (one request per ~10) and, when the
user asks, writes an interview prep sheet. Nothing is sent, deleted or marked.

Stored in <data dir>/inbox.json.
"""
from __future__ import annotations

import asyncio
import json
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Literal
from urllib.parse import quote

from pydantic import BaseModel, Field

try:
    from core.config import get_data_dir
except ImportError:  # pragma: no cover
    from backend.core.config import get_data_dir

CATEGORIES = ("applied", "assessment", "interview", "offer", "rejected")
SCHEDULES = {"off": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}
FIRST_CHECK_DAYS = 60
MAX_EMAILS_PER_CHECK = 40
BATCH = 10
PAGE_WAIT_SECONDS = 25
GMAIL = "https://mail.google.com/mail/u/0/"

# Gmail search for job mail. Keeps out sent mail and promotions.
SEARCH_TERMS = ('(application OR applied OR applying OR interview OR offer OR unfortunately OR assessment '
                'OR "next steps" OR candidacy OR candidate OR "thank you for your interest" OR position) '
                '-in:sent -category:promotions -category:social')

_lock = threading.Lock()
_state = {"checking": False, "requested": False, "status": "", "error": ""}


# ── storage ──────────────────────────────────────────────────────────────────
def _path():
    return get_data_dir() / "inbox.json"


def load() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("emails", [])
    data.setdefault("schedule", "off")
    data.setdefault("last_checked", None)
    return data


def save(data: dict) -> None:
    path = _path()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def status() -> dict:
    data = load()
    return {**_state, "schedule": data["schedule"], "last_checked": data["last_checked"],
            "emails": sorted(data["emails"], key=lambda e: e.get("date") or "", reverse=True)}


def set_schedule(schedule: str) -> bool:
    if schedule not in SCHEDULES:
        return False
    with _lock:
        data = load()
        data["schedule"] = schedule
        save(data)
    return True


def request_check() -> None:
    """Ask for a check; the scheduler runs it as soon as no application is running."""
    _state["requested"] = True
    _state["error"] = ""
    _state["status"] = "Waiting for the current run to finish…"


RETRY_AFTER_FAILURE = timedelta(hours=1)


def due(data: dict, now: datetime | None = None) -> bool:
    every = SCHEDULES.get(data.get("schedule") or "off")
    if every is None:
        return False
    now = now or datetime.now(timezone.utc)
    if data.get("last_failed") and now - datetime.fromisoformat(data["last_failed"]) < RETRY_AFTER_FAILURE:
        return False  # don't reopen Gmail every minute while it's signed out
    if not data.get("last_checked"):
        return True
    return now - datetime.fromisoformat(data["last_checked"]) >= every


def _note_failure() -> None:
    with _lock:
        data = load()
        data["last_failed"] = datetime.now(timezone.utc).isoformat()
        save(data)


# ── reading Gmail with code ─────────────────────────────────────────────────
def search_url(since: datetime) -> str:
    query = f"after:{since.strftime('%Y/%m/%d')} {SEARCH_TERMS}"
    return GMAIL + "#search/" + quote(query, safe="")


# Result rows. Gmail's classes (tr.zA, .bog, .y2, .yW [email], .xW [title]) have
# been stable for years; role=row innerText is the fallback.
_LIST_JS = r"""() => {
  const out = [];
  const rows = Array.from(document.querySelectorAll('tr.zA'));
  for (const row of rows) {
    const idEl = row.querySelector('[data-legacy-thread-id]');
    const sender = row.querySelector('.yW [email], .yW span[name]');
    const date = row.querySelector('.xW [title], td.xW span');
    out.push({
      id: idEl ? idEl.getAttribute('data-legacy-thread-id') : '',
      sender: sender ? (sender.getAttribute('name') || sender.innerText || '') : '',
      sender_email: sender ? (sender.getAttribute('email') || '') : '',
      subject: (row.querySelector('.bog') || {}).innerText || '',
      snippet: ((row.querySelector('.y2') || {}).innerText || '').replace(/^\s*-\s*/, ''),
      date: date ? (date.getAttribute('title') || date.innerText || '') : '',
    });
  }
  const signedOut = location.hostname.startsWith('accounts.google.') || !!document.querySelector('input[type=email][name=identifier]');
  // Gmail can show the inbox for a moment before the search results: the search
  // box holds the query once the results are on screen.
  const box = document.querySelector('input[name=q]');
  const searched = !box || /after:/.test(box.value || '');
  const empty = !rows.length && /no messages matched|didn't match any/i.test(document.body ? document.body.innerText : '');
  return JSON.stringify({ rows: searched ? out : [], signedOut, empty, ready: signedOut || (searched && (rows.length > 0 || empty)) });
}"""

# The open message. Gmail keeps the previous message on screen for a moment
# after navigating, so it only counts once the subject is the one clicked.
_MESSAGE_JS = r"""(expected) => {
  const norm = (t) => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const subject = (document.querySelector('h2.hP') || {}).innerText || '';
  const bodies = Array.from(document.querySelectorAll('.a3s')).filter((b) => b.innerText.trim());
  const body = bodies.length ? bodies[bodies.length - 1].innerText : '';
  const same = !expected || norm(subject) === norm(expected) || norm(subject).includes(norm(expected).slice(0, 40));
  return JSON.stringify({ body: body.replace(/\n{3,}/g, '\n\n').slice(0, 2500), ready: bodies.length > 0 && same });
}"""


async def _poll(page, js: str, *args, key: str = "ready", wait: float = PAGE_WAIT_SECONDS) -> dict:
    deadline = time.monotonic() + wait
    data = {}
    while time.monotonic() < deadline:
        try:
            data = json.loads(await page.evaluate(js, *args) or "{}")
        except Exception:
            data = {}
        if data.get(key):
            return data
        await asyncio.sleep(0.7)
    return data


async def read_gmail(since: datetime, known_ids: set[str], browser_kwargs: dict) -> tuple[list[dict], str]:
    """New job emails since `since` ([{id, sender, sender_email, subject, snippet, date, body}], error)."""
    from browser_use import BrowserSession
    browser = BrowserSession(**browser_kwargs)
    try:
        await browser.start()
        await browser.navigate_to(search_url(since))
        page = await browser.get_current_page()
        listing = await _poll(page, _LIST_JS)
        if listing.get("signedOut"):
            return [], "Gmail isn't signed in. Sign in to Gmail in Settings → Accounts, then check again."
        if not listing.get("ready"):
            return [], "Gmail didn't load in time. Try again."
        emails = []
        for row in listing.get("rows", []):
            if not row.get("id") or row["id"] in known_ids:
                continue
            if len(emails) >= MAX_EMAILS_PER_CHECK:
                break
            await browser.navigate_to(f"{GMAIL}#all/{row['id']}")
            message = await _poll(await browser.get_current_page(), _MESSAGE_JS, row.get("subject", ""), wait=12)
            # Never another email's text: without a matching message, keep the row's snippet.
            body = message.get("body") if message.get("ready") else ""
            emails.append({**row, "body": body or row.get("snippet") or ""})
        return emails, ""
    except Exception as e:
        return [], f"Couldn't read Gmail: {e}"
    finally:
        try:
            await browser.kill()
        except Exception:
            pass


# ── sorting with the AI ─────────────────────────────────────────────────────
class EmailLabel(BaseModel):
    index: int
    category: Literal["applied", "assessment", "interview", "offer", "rejected", "other"] = Field(
        description="applied = application received/confirmation; assessment = test, HireVue, coding challenge; "
                    "interview = an invitation to interview or schedule a call; offer = a job offer; "
                    "rejected = not moving forward; other = not about the candidate's own job application")
    company: str = Field(description="The employer, everyday name (e.g. 'Contoso'); '' if unclear")
    role: str = Field(description="The job title if the email names it, else ''")


class EmailLabels(BaseModel):
    labels: list[EmailLabel]


def classify_prompt(batch: list[dict]) -> str:
    lines = ["Sort these emails a job applicant received. Use 'other' for newsletters, job alerts, "
             "recommendations, marketing and anything that isn't about one of their own applications.\n"]
    for i, e in enumerate(batch):
        lines.append(f"[{i}] From: {e.get('sender')} <{e.get('sender_email')}>\nSubject: {e.get('subject')}\n"
                     f"{(e.get('body') or e.get('snippet') or '')[:1200]}\n")
    return "\n".join(lines)


async def classify(llm, emails: list[dict]) -> list[dict]:
    """The emails that are about applications, each with category / company / role."""
    from browser_use.llm.messages import UserMessage
    sorted_emails = []
    for start in range(0, len(emails), BATCH):
        batch = emails[start:start + BATCH]
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([UserMessage(content=classify_prompt(batch))], output_format=EmailLabels), timeout=120)
            labels = response.completion.labels
        except Exception:
            continue
        for label in labels:
            if 0 <= label.index < len(batch) and label.category in CATEGORIES:
                sorted_emails.append({**batch[label.index], "category": label.category,
                                      "company": label.company.strip(), "role": label.role.strip()})
    return sorted_emails


# ── matching to jobs ─────────────────────────────────────────────────────────
def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def match_job(email: dict, jobs: dict) -> str | None:
    """The job URL this email is about (company must match; the role breaks ties), or None."""
    company = _letters(email.get("company"))
    if len(company) < 3:
        return None
    best, best_score = None, 0.0
    for url, job in jobs.items():
        name = _letters(job.get("company"))
        if not name or not (company in name or name in company):
            continue
        role_words, title_words = _words(email.get("role")), _words(job.get("title"))
        score = 1.0 + (len(role_words & title_words) / max(1, len(role_words)) if role_words else 0)
        if job.get("status") == "applied":
            score += 0.05  # only breaks ties; the role decides
        if score > best_score:
            best, best_score = url, score
    return best


# ── a whole check ─────────────────────────────────────────────────────────────
async def run_check(llm, browser_kwargs: dict, read_jobs, update_job) -> dict:
    """Read new job mail, sort it, store it and badge the matching jobs. Returns a summary."""
    data = load()
    since = (datetime.fromisoformat(data["last_checked"]) - timedelta(days=1) if data.get("last_checked")
             else datetime.now(timezone.utc) - timedelta(days=FIRST_CHECK_DAYS))
    known = {e["id"] for e in data["emails"]}
    known |= set(data.get("ignored_ids", []))
    _state["status"] = "Reading Gmail…"
    emails, error = await read_gmail(since, known, browser_kwargs)
    if error:
        return {"error": error, "new": 0}
    _state["status"] = f"Sorting {len(emails)} email(s)…"
    sorted_emails = await classify(llm, emails) if emails else []
    kept_ids = {e["id"] for e in sorted_emails}
    jobs = read_jobs()
    for email in sorted_emails:
        email["gmail_url"] = f"https://mail.google.com/mail/u/0/#all/{email['id']}"  # always the real Gmail
        email["date"] = _iso_date(email.get("date"))
        email["job_url"] = match_job(email, jobs)
        if email["job_url"]:
            update_job(email["job_url"], inbox_status=email["category"], inbox_date=email["date"],
                       inbox_subject=email.get("subject", "")[:200])
    with _lock:
        data = load()
        data["emails"] = data["emails"] + [{k: v for k, v in e.items() if k != "body"} | {"text": e.get("body", "")[:2500]}
                                           for e in sorted_emails]
        data["ignored_ids"] = sorted(set(data.get("ignored_ids", [])) | {e["id"] for e in emails if e["id"] not in kept_ids})[-2000:]
        data["last_checked"] = datetime.now(timezone.utc).isoformat()
        save(data)
    return {"error": "", "new": len(sorted_emails), "read": len(emails)}


def _iso_date(text: str) -> str:
    """Gmail's row title ('Mon, Oct 6, 2026, 4:12 PM') -> '2026-10-06T16:12'; '' if unreadable."""
    text = re.sub(r"\s+", " ", (text or "").replace(" ", " ")).strip()
    for fmt in ("%a, %b %d, %Y, %I:%M %p", "%b %d, %Y, %I:%M %p", "%a, %b %d, %Y %I:%M %p", "%b %d, %Y"):
        try:
            return datetime.strptime(text, fmt).isoformat(timespec="minutes")
        except ValueError:
            continue
    return text


# ── interview prep sheet ─────────────────────────────────────────────────────
def prep_prompt(email: dict, job: dict | None, posting: str, resume: str, profile: dict) -> str:
    return (
        f"Write a one-page interview prep sheet for {profile.get('name', 'the candidate')}.\n\n"
        f"ROLE: {email.get('role') or (job or {}).get('title', '')} at {email.get('company') or (job or {}).get('company', '')}\n"
        f"INVITATION EMAIL:\n{email.get('text', '')[:2000]}\n\n"
        f"JOB POSTING:\n{posting[:4000] or '(not available)'}\n\n"
        f"RESUME:\n{resume[:5000]}\n\n"
        "Sections, in plain text with short headings and simple '-' lists (no markdown symbols like # or **):\n"
        "1. The interview: what the email says (format, date, who, how to prepare or confirm).\n"
        "2. The company and role in a few lines (only what the posting and email say; don't invent facts).\n"
        "3. Likely questions: 6-8 for this role, each with a one-line pointer to a matching resume experience.\n"
        "4. Stories to have ready: 3 resume experiences to tell (situation, what you did, result).\n"
        "5. Questions to ask them: 4.\n"
    )


async def write_prep_sheet(llm, email: dict, job: dict | None, posting: str, resume: str, profile: dict) -> str:
    from browser_use.llm.messages import UserMessage
    response = await asyncio.wait_for(llm.ainvoke([UserMessage(content=prep_prompt(email, job, posting, resume, profile))]),
                                      timeout=120)
    text = response.completion if isinstance(response.completion, str) else str(response.completion)
    return re.sub(r"[#*`]+", "", text).strip()


def save_prep(email_id: str, text: str) -> bool:
    with _lock:
        data = load()
        for email in data["emails"]:
            if email["id"] == email_id:
                email["prep"] = text
                save(data)
                return True
    return False


def find(email_id: str) -> dict | None:
    return next((e for e in load()["emails"] if e["id"] == email_id), None)


# ── scheduler ────────────────────────────────────────────────────────────────
def start_scheduler(is_busy, check, interval_seconds: int = 60, stop: threading.Event | None = None) -> threading.Thread:
    """Run requested and scheduled checks in the background, never during an application run.

    is_busy() -> bool: True while applications run or the sign-in window is open.
    check() -> dict: one full check (runs in this thread).
    """
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                wanted = _state["requested"] or due(load())
                if wanted and not _state["checking"]:
                    if is_busy():
                        if _state["requested"]:
                            _state["status"] = "Waiting for the current run to finish…"
                    else:
                        _state.update(checking=True, requested=False, status="Opening Gmail…", error="")
                        try:
                            result = check()
                            _state["error"] = result.get("error", "")
                            if result.get("error"):
                                _note_failure()
                            _state["status"] = "" if result.get("error") else (
                                f"Found {result.get('new', 0)} new job email(s)." if result.get("new") else "No new job emails.")
                        except Exception as e:
                            _state.update(error=f"Check failed: {e}", status="")
                            _note_failure()
                        finally:
                            _state["checking"] = False
            except Exception:
                pass
            stop.wait(interval_seconds if not _state["requested"] else 2)

    thread = threading.Thread(target=loop, name="inbox-scheduler", daemon=True)
    thread.start()
    return thread
