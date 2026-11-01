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
    from core.job_page import _letters
except ImportError:  # pragma: no cover
    from backend.core.config import get_data_dir
    from backend.core.job_page import _letters

CATEGORIES = ("applied", "assessment", "interview", "offer", "rejected")
SCHEDULES = {"off": None, "daily": timedelta(days=1), "weekly": timedelta(days=7)}
FIRST_CHECK_DAYS = 60
# Kept emails: after a year only what the Inbox lists (sender, subject, category, date)
# stays, not the text; and at most this many (newest). The originals stay in Gmail.
KEEP_TEXT_DAYS = 365
MAX_KEPT_EMAILS = 5000
MAX_EMAILS_PER_CHECK = 40
# A results page with at least this many rows may have older matches after it.
RESULT_PAGE_ROWS = 20
MAX_RESULT_PAGES = 5
BATCH = MAX_EMAILS_PER_CHECK  # one sorting request per check (40 x 1200 chars fits easily)
PAGE_WAIT_SECONDS = 25
GMAIL = "https://mail.google.com/mail/u/0/"

# Gmail search for job mail. Keeps out sent mail and promotions.
SEARCH_TERMS = ('(application OR applied OR applying OR interview OR offer OR unfortunately OR assessment '
                'OR "next steps" OR candidacy OR candidate OR "thank you for your interest" OR position) '
                '-in:sent -category:promotions -category:social')

_lock = threading.Lock()
_state = {"checking": False, "requested": False, "status": "", "error": ""}
WAITING = "Waiting for the current run to finish…"


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
    """For the Inbox page (polled): everything but each email's stored text, which only prep sheets use."""
    data = load()
    emails = [{k: v for k, v in e.items() if k != "text"} for e in data["emails"]]
    return {**_state, "schedule": data["schedule"], "last_checked": data["last_checked"],
            "emails": sorted(emails, key=lambda e: e.get("date") or "", reverse=True)}


_schedule_cache: dict = {"mtime": None, "fields": {}}


def _schedule_fields() -> dict:
    """schedule / last_checked / last_failed, re-read only when inbox.json changes (the scheduler asks every minute)."""
    try:
        mtime = _path().stat().st_mtime_ns
    except OSError:
        return {}
    if _schedule_cache["mtime"] != mtime:
        data = load()
        _schedule_cache.update(mtime=mtime, fields={k: data.get(k) for k in ("schedule", "last_checked", "last_failed")})
    return _schedule_cache["fields"]


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
    _state["status"] = WAITING


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
def search_url(since: datetime, before=None) -> str:
    """Job mail after `since` (and before the date `before`, for older result pages)."""
    query = f"after:{since.strftime('%Y/%m/%d')} " + (f"before:{before.strftime('%Y/%m/%d')} " if before else "") + SEARCH_TERMS
    return GMAIL + "#search/" + quote(query, safe="")


def _oldest_day(rows: list[dict]):
    """The earliest date among result rows (None if none can be read)."""
    days = []
    for row in rows:
        try:
            days.append(datetime.fromisoformat(_iso_date(row.get("date"))).date())
        except ValueError:
            continue
    return min(days) if days else None


# Result rows. Gmail's classes (tr.zA, .bog, .y2, .yW [email], .xW [title]) have
# been stable for years; role=row innerText is the fallback.
_LIST_JS = r"""(expected) => {
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
  const searched = !box || (box.value || '').includes(expected || 'after:');
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


async def read_gmail(since: datetime, known_ids: set[str], browser_kwargs: dict) -> tuple[list[dict], str, bool]:
    """New job emails since `since`: ([{id, sender, sender_email, subject, snippet, date, body}], error, complete).

    complete is False when matching mail was left unread (the per-check cap, or
    more result pages than MAX_RESULT_PAGES); the next check reads the same
    window again, skipping what it already has.
    """
    from browser_use import BrowserSession
    browser = BrowserSession(**browser_kwargs)
    try:
        await browser.start()
        emails, seen, before = [], set(known_ids), None
        for _ in range(MAX_RESULT_PAGES):
            await browser.navigate_to(search_url(since, before))
            expected = f"before:{before.strftime('%Y/%m/%d')}" if before else f"after:{since.strftime('%Y/%m/%d')}"
            listing = await _poll(await browser.get_current_page(), _LIST_JS, expected)
            if listing.get("signedOut"):
                return [], "Gmail isn't signed in. Sign in to Gmail in Settings → Accounts, then check again.", False
            if not listing.get("ready"):
                return (emails, "", False) if emails else ([], "Gmail didn't load in time. Try again.", False)
            rows = listing.get("rows", [])
            for row in rows:
                if not row.get("id") or row["id"] in seen:
                    continue
                if len(emails) >= MAX_EMAILS_PER_CHECK:
                    return emails, "", False
                seen.add(row["id"])
                await browser.navigate_to(f"{GMAIL}#all/{row['id']}")
                message = await _poll(await browser.get_current_page(), _MESSAGE_JS, row.get("subject", ""), wait=12)
                # Never another email's text: without a matching message, keep the row's snippet.
                body = message.get("body") if message.get("ready") else ""
                emails.append({**row, "body": body or row.get("snippet") or ""})
            if len(rows) < RESULT_PAGE_ROWS:
                return emails, "", True
            # A full page: older matches may follow. before: excludes its own day,
            # so the next page starts the day after the oldest row (rows already
            # read that day are skipped).
            oldest = _oldest_day(rows)
            following = oldest + timedelta(days=1) if oldest else None
            if following is not None and following <= since.date():
                return emails, "", True
            if following is None or following == before:
                return emails, "", False
            before = following
        return emails, "", False
    except Exception as e:
        return [], f"Couldn't read Gmail: {e}", False
    finally:
        try:
            await browser.kill()
        except Exception:
            pass


# ── verification emails for a running application (narrow reader) ──────────
# The application agent never opens Gmail (its browser can't reach it). When a
# site emails a code or a link, the app reads it here, in a separate browser on
# a copy of the sign-in cookies, and hands back only that code or link, after
# checking the sender belongs to the site that asked and the email is new.

VERIFY_TERMS = '(code OR verify OR verification OR confirm OR activate OR passcode OR OTP OR "one-time")'

_VERIFY_JS = r"""(expected) => {
  const norm = (t) => (t || '').toLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  const subject = (document.querySelector('h2.hP') || {}).innerText || '';
  const bodies = Array.from(document.querySelectorAll('.a3s')).filter((b) => b.innerText.trim());
  const body = bodies.length ? bodies[bodies.length - 1] : null;
  const same = !expected || norm(subject) === norm(expected) || norm(subject).includes(norm(expected).slice(0, 40));
  const links = body ? Array.from(body.querySelectorAll('a[href]')).map((a) => a.href).slice(0, 50) : [];
  const to = Array.from(document.querySelectorAll('.hb span[email], span.g2[email]')).map((s) => s.getAttribute('email'));
  return JSON.stringify({ text: body ? body.innerText.slice(0, 5000) : '', links, subject, to, ready: !!body && same });
}"""

_CODE_WORD = re.compile(r"\b(code|passcode|otp|pin|verification|one[- ]time)\b", re.I)
# A code: 4-10 capitals/digits with at least one digit (case-sensitive, so words never count).
_CODE_TOKEN = re.compile(r"\b(?=[A-Z0-9]*[0-9])[A-Z0-9]{4,10}\b")
# Whitespace that stays on the code's line: "\s*" ran across blank lines, quadratic on a body of them.
_CODE_ALONE = re.compile(r"^[^\S\n]*([0-9]{4,8})[^\S\n]*$", re.M)
_LINK_WORDS = re.compile(r"verif|confirm|activat|validat|registration|account", re.I)


def registrable(host: str) -> str:
    """'careers.acme.co.uk' -> 'acme.co.uk', 'mail.acme.com' -> 'acme.com' (for sender checks)."""
    try:
        from core.domains import registrable as site_of
    except ImportError:
        from backend.core.domains import registrable as site_of
    return site_of(host)


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def about_employer(subject: str, text: str, names) -> bool:
    """The email names one of the employer's names (3+ letters), in its subject or text."""
    haystack = " ".join(re.findall(r"[a-z0-9]+", f"{subject or ''} {text or ''}".lower()))
    for name in names or []:
        words = re.findall(r"[a-z0-9]+", (name or "").lower())
        if len("".join(words)) >= 3 and re.search(r"(?<!\w)" + re.escape(" ".join(words)) + r"(?!\w)", haystack):
            return True
    return False


def addressed_to(recipients, address: str) -> bool:
    """Verification codes require a readable recipient matching the application account."""
    recipients = [r.strip().lower() for r in recipients or [] if isinstance(r, str) and r.strip()]
    return bool(address and address.strip().lower() in recipients)


def _domain_ok(domain: str, allowed: set[str]) -> bool:
    domain = (domain or "").lower().strip(".")
    return any(domain == a or domain.endswith("." + a) for a in allowed)


def _unwrap(link: str) -> str:
    """Gmail wraps outside links as https://www.google.com/url?q=<real link>."""
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(link)
    if parsed.hostname in ("www.google.com", "google.com") and parsed.path == "/url":
        return (parse_qs(parsed.query).get("q") or [link])[0]
    return link


def extract_verification(kind: str, text: str, links: list[str], allowed: set[str]) -> str:
    """The code (kind="code") or the https link to an allowed domain (kind="link") in an email; "" if none."""
    from urllib.parse import urlparse
    if kind == "code":
        text = text or ""
        for word in _CODE_WORD.finditer(text):
            token = _CODE_TOKEN.search(text[word.end():word.end() + 80])
            if token:
                return token.group(0)
        alone = _CODE_ALONE.search(text)
        return alone.group(1) if alone else ""
    for link in links or []:
        link = _unwrap(link)
        parsed = urlparse(link)
        if parsed.scheme == "https" and _domain_ok(parsed.hostname or "", allowed) and _LINK_WORDS.search(link):
            return link
    return ""


async def find_verification(kind: str, allowed_domains: set[str], since: datetime, browser_kwargs: dict,
                            wait_seconds: float = 90, *, employer_domains: set[str] | None = None,
                            employer_names=(), recipient: str = "", link_validator=None) -> dict:
    """{"code"|"link": value} from the newest matching email since `since`, or {"error": ...}.

    Only emails whose sender address is at one of `allowed_domains` (the site
    that asked and its application system) count; everything else in the inbox
    is never read beyond its list row. An email from a shared application system
    (myworkday.com sends for every Workday employer) also has to name this
    employer (`employer_names`); one from the employer's own domain
    (`employer_domains`) doesn't. Its recipients must be readable and match
    `recipient`; ambiguous matches and unapproved link destinations are refused.
    """
    if not recipient:
        return {"error": "No application email is configured for verification."}
    own = {d.lower() for d in employer_domains or set() if d}
    from browser_use import BrowserSession
    allowed = {d.lower() for d in allowed_domains if d}
    query = f"after:{int(since.timestamp())} {VERIFY_TERMS}"
    url = GMAIL + "#search/" + quote(query, safe="")
    browser = BrowserSession(**browser_kwargs)
    deadline = time.monotonic() + wait_seconds
    try:
        await browser.start()
        while True:
            candidates = set()
            await browser.navigate_to(url)
            listing = await _poll(await browser.get_current_page(), _LIST_JS, f"after:{int(since.timestamp())}")
            if listing.get("signedOut"):
                return {"error": "Gmail isn't signed in (Settings -> Accounts)."}
            for row in listing.get("rows", []):
                sender = (row.get("sender_email") or "").split("@")[-1]
                if not row.get("id") or not _domain_ok(sender, allowed):
                    continue
                await browser.navigate_to(f"{GMAIL}#all/{row['id']}")
                message = await _poll(await browser.get_current_page(), _VERIFY_JS, row.get("subject", ""), wait=12)
                if not message.get("ready"):
                    continue
                if not addressed_to(message.get("to"), recipient):
                    continue
                if not _domain_ok(sender, own) and not about_employer(message.get("subject", ""), message.get("text", ""),
                                                                     employer_names):
                    continue  # a shared application system's email for another employer
                value = extract_verification(kind, message.get("text", ""), message.get("links", []), allowed)
                if value and kind == "link" and (link_validator is None or not link_validator(value)):
                    continue
                if value:
                    candidates.add(value)
                    if len(candidates) > 1:
                        return {"error": "Multiple verification messages match this application. Choose the correct email yourself."}
            if candidates:
                return {kind: next(iter(candidates))}
            if time.monotonic() >= deadline:
                return {"error": f"No {kind} email from {', '.join(sorted(allowed))} arrived."}
            await asyncio.sleep(10)  # emails can take a moment to arrive
    except Exception as e:
        return {"error": f"Couldn't read the verification email: {type(e).__name__}"}
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


async def classify(llm, emails: list[dict]) -> tuple[list[dict], set[str]]:
    """(the emails about applications, each with category / company / role; the ids the AI labeled at all)."""
    from browser_use.llm.messages import UserMessage
    sorted_emails, labeled = [], set()
    for start in range(0, len(emails), BATCH):
        batch = emails[start:start + BATCH]
        try:
            response = await asyncio.wait_for(
                llm.ainvoke([UserMessage(content=classify_prompt(batch))], output_format=EmailLabels), timeout=120)
            labels = response.completion.labels
        except Exception:
            continue
        for label in labels:
            if not 0 <= label.index < len(batch):
                continue
            labeled.add(batch[label.index]["id"])
            if label.category in CATEGORIES:
                sorted_emails.append({**batch[label.index], "category": label.category,
                                      "company": label.company.strip(), "role": label.role.strip()})
    return sorted_emails, labeled


# ── matching to jobs ─────────────────────────────────────────────────────────
def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(w) > 2}


def _same_company(a: str, b: str) -> bool:
    """'Contoso' ~ 'Contoso Investments' (whole words, in order), 'A.C. Labs' ~ 'AC Labs' (same letters);
    not 'Ally' ~ 'Rally Health'."""
    if _letters(a) == _letters(b):
        return True
    words_a, words_b = re.findall(r"[a-z0-9]+", a.lower()), re.findall(r"[a-z0-9]+", b.lower())
    short, long = sorted((words_a, words_b), key=len)
    return bool(short) and any(long[i:i + len(short)] == short for i in range(len(long) - len(short) + 1))


def match_job(email: dict, jobs: dict) -> str | None:
    """The job URL this email is about (company must match; the role breaks ties), or None.

    None too when two of the employer's jobs fit it equally well: an interview
    email shown on the wrong application is worse than one shown on none (it
    stays in the Inbox either way).
    """
    company = email.get("company") or ""
    if len(_letters(company)) < 3:
        return None
    scores = {}
    for url, job in jobs.items():
        name = job.get("company") or ""
        if not _letters(name) or not _same_company(company, name):
            continue
        role_words, title_words = _words(email.get("role")), _words(job.get("title"))
        score = 1.0 + (len(role_words & title_words) / max(1, len(role_words)) if role_words else 0)
        if job.get("status") == "applied":
            score += 0.05  # only breaks ties; the role decides
        scores[url] = score
    if not scores:
        return None
    best = max(scores, key=scores.get)
    if sum(1 for s in scores.values() if abs(s - scores[best]) < 1e-9) > 1:
        return None
    return best


# ── a whole check ─────────────────────────────────────────────────────────────
async def run_check(llm, browser_kwargs: dict, read_jobs, update_job) -> dict:
    """Read new job mail, sort it, store it and badge the matching jobs. Returns a summary."""
    data = load()
    # search_from: a window an earlier check didn't finish (cap, page limit, sorting failed).
    since = (datetime.fromisoformat(data["search_from"]) if data.get("search_from")
             else datetime.fromisoformat(data["last_checked"]) - timedelta(days=1) if data.get("last_checked")
             else datetime.now(timezone.utc) - timedelta(days=FIRST_CHECK_DAYS))
    known = {e["id"] for e in data["emails"]}
    known |= set(data.get("ignored_ids", []))
    _state["status"] = "Reading Gmail…"
    emails, error, complete = await read_gmail(since, known, browser_kwargs)
    if error:
        return {"error": error, "new": 0}
    _state["status"] = f"Sorting {len(emails)} email(s)…"
    sorted_emails, labeled = await classify(llm, emails) if emails else ([], set())
    # Emails the AI didn't get to (an error, a timeout) aren't ignored: the next check reads them again.
    complete = complete and labeled >= {e["id"] for e in emails}
    kept_ids = {e["id"] for e in sorted_emails}
    jobs = read_jobs()
    for email in sorted_emails:
        email["gmail_url"] = f"https://mail.google.com/mail/u/0/#all/{email['id']}"  # always the real Gmail
        email["date"] = _iso_date(email.get("date"))
        email["job_url"] = match_job(email, jobs)
    # Oldest first, and never over a newer email's status: each job shows its latest email.
    for email in sorted(sorted_emails, key=lambda e: e["date"]):
        job = jobs.get(email["job_url"]) if email["job_url"] else None
        if job is not None and email["date"] >= (job.get("inbox_date") or ""):
            update_job(email["job_url"], inbox_status=email["category"], inbox_date=email["date"],
                       inbox_subject=email.get("subject", "")[:200])
            job["inbox_date"] = email["date"]
    with _lock:
        data = load()
        data["emails"] = _retained(data["emails"] + [
            {k: v for k, v in e.items() if k != "body"} | {"text": e.get("body", "")[:2500]} for e in sorted_emails])
        data["ignored_ids"] = sorted(set(data.get("ignored_ids", [])) | (labeled - kept_ids))[-2000:]
        data["last_checked"] = datetime.now(timezone.utc).isoformat()
        data["search_from"] = None if complete else since.isoformat()
        save(data)
    return {"error": "", "new": len(sorted_emails), "read": len(emails)}


def _retained(emails: list[dict], now: datetime | None = None) -> list[dict]:
    """The newest MAX_KEPT_EMAILS, with the text (and prep sheet) of those over KEEP_TEXT_DAYS old dropped."""
    cutoff = ((now or datetime.now(timezone.utc)) - timedelta(days=KEEP_TEXT_DAYS)).strftime("%Y-%m-%d")
    kept = []
    for email in sorted(emails, key=lambda e: e.get("date") or "", reverse=True)[:MAX_KEPT_EMAILS]:
        date = email.get("date") or ""
        if date[:4].isdigit() and date[:10] < cutoff and (email.get("text") or email.get("prep")):
            email = {**email, "text": "", "prep": ""}
        kept.append(email)
    return kept


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


async def write_prep_sheet(llm, email: dict, job: dict | None, posting: str, resume: str, profile: dict,
                           settings: dict | None = None) -> str:
    try:
        from core.ai_extras import ask_text
    except ImportError:  # pragma: no cover
        from backend.core.ai_extras import ask_text
    text = await ask_text(llm, prep_prompt(email, job, posting, resume, profile), timeout=120, settings=settings)
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
def _check_now(check, is_busy=lambda: False) -> None:
    """One check, its outcome left in _state for the Inbox page."""
    requested = _state["requested"]
    _state.update(checking=True, requested=False, status="Opening Gmail…", error="")
    if is_busy():
        # A run started between the scheduler's look and here: it owns the browser profile.
        _state.update(checking=False, requested=requested, status=WAITING if requested else "")
        return
    try:
        try:
            result = check()
        except Exception as e:
            result = {"error": f"Check failed: {e}"}
        error, new = result.get("error", ""), result.get("new", 0)
        _state.update(error=error, status="" if error else (
            f"Found {new} new job email(s)." if new else "No new job emails."))
        if error:
            _note_failure()
    finally:
        _state["checking"] = False


def start_scheduler(is_busy, check, interval_seconds: int = 60, stop: threading.Event | None = None) -> threading.Thread:
    """Run requested and scheduled checks in the background, never during an application run.

    is_busy() -> bool: True while applications run or the sign-in window is open.
    check() -> dict: one full check (runs in this thread).
    """
    stop = stop or threading.Event()

    def loop():
        while not stop.is_set():
            try:
                if not _state["checking"] and (_state["requested"] or due(_schedule_fields())):
                    if not is_busy():
                        _check_now(check, is_busy)
                    elif _state["requested"]:
                        _state["status"] = WAITING
            except Exception:
                pass
            stop.wait(interval_seconds if not _state["requested"] else 2)

    thread = threading.Thread(target=loop, name="inbox-scheduler", daemon=True)
    thread.start()
    return thread
