"""What a job posting's page says before any browser opens: title, company, closed.

Pasted links used to show as "Pasted Link / Unknown" forever, and a posting
that had been taken down still cost a full browser run (a dozen AI requests)
before the agent noticed. Nearly every careers site (Workday, SuccessFactors,
Cornerstone, Oracle, Greenhouse, Lever, ...) puts the job title in the
server-rendered og:title tag and the employer in JSON-LD hiringOrganization,
so one plain HTTP request answers both.

"Closed" is decided conservatively: a 404/410, a Workday page whose og:title is
empty (Workday's not-found page), or a page title that plainly says so. A site
that blocks the request (403, timeouts) is never called closed.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 12
MAX_BYTES = 1_500_000

# Placeholder titles given to links that were added without one.
PLACEHOLDER_TITLES = {"", "pasted link", "manually added", "unknown", "untitled"}

_CLOSED_PHRASES = (
    "page not found", "job not found", "posting not found", "no longer available",
    "no longer accepting applications", "position has been filled", "job has expired",
    "job is closed", "this job has been closed", "requisition is no longer",
)


@dataclass
class Posting:
    title: str = ""
    company: str = ""
    closed: bool = False
    reason: str = ""


def _meta(page: str, prop: str) -> str | None:
    """Content of <meta property|name=prop>; "" when the tag is there but empty, None when absent."""
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        names = re.findall(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if prop in (n.lower() for n in names):
            m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
            return html.unescape(m.group(1)).strip() if m else ""
    return None


def _json_ld_company(page: str) -> str:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.I | re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict):
                continue
            org = item.get("hiringOrganization") or item.get("HiringOrganization")
            if isinstance(org, dict):
                name = org.get("name") or org.get("Name")
                if isinstance(name, str) and name.strip():
                    return html.unescape(name).strip()
    return ""


def _page_title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""


_BOT_CHECK = re.compile(
    r"quick check|just a moment|attention required|access denied|are you a robot|verify you are human|"
    r"security check|captcha|request blocked|forbidden",
    re.I,
)


def _clean_company(name: str) -> str:
    """Workday's JSON-LD names legal entities: "1000 Wayne Tech & Sons LLC" -> "Wayne Tech & Sons LLC"."""
    name = re.sub(r"^\d+\s*(?:-\s*)?", "", name or "").strip()
    return re.sub(r"\s+Legal Entity$", "", name, flags=re.I).strip()


def _workday_company(url: str) -> str:
    """The employer from a descriptive Workday site name: /Umbrella_Labs_External_Site/ -> Umbrella Labs."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]  # locale prefix, e.g. en-US
    site = parts[0] if parts else ""
    words = [w for w in site.split("_") if w and w.lower() not in
             {"external", "site", "careers", "career", "jobs", "job", "search", "portal", "us", "global"}]
    return " ".join(words) if "_" in site and len(words) >= 1 and len(" ".join(words)) > 3 else ""


def parse(url: str, status_code: int, page: str) -> Posting:
    """Read a fetched page (pure function, for tests)."""
    host = (urlparse(url).hostname or "").lower()
    if status_code in (404, 410):
        return Posting(closed=True, reason=f"The posting is gone (HTTP {status_code}).")
    if status_code >= 400:
        return Posting()

    og_title = _meta(page, "og:title")
    page_title = _page_title(page)
    is_workday = "myworkdayjobs.com" in host or "myworkdaysite.com" in host
    if is_workday and og_title == "":
        return Posting(closed=True, reason="The posting was taken down (Workday shows its not-found page).")
    for text in (og_title or "", page_title):
        low = text.lower()
        if low and any(p in low for p in _CLOSED_PHRASES) and len(low) < 120:
            return Posting(closed=True, reason=f'The posting is closed (the page says "{text}").')

    title = og_title or ""
    if not title and page_title:
        # "Data Analyst Intern Job Details | ACX" -> "Data Analyst Intern"
        title = re.split(r"\s+[|–—-]\s+", page_title)[0]
        title = re.sub(r"\s+Job Details$", "", title, flags=re.I).strip()
    if _BOT_CHECK.search(title):
        # A bot-check or error page, not the posting: learn nothing from it.
        return Posting()
    company = _workday_company(url) if is_workday else ""
    company = company or _clean_company(_json_ld_company(page))
    if not company:
        site = _meta(page, "og:site_name") or ""
        # Generic portal names ("Cyberdyne Candidate Experience page", "Careers") are not employers.
        if site and not re.search(r"candidate experience|careers?|jobs?\b|workday|recruiting", site, re.I):
            company = site
    return Posting(title=title[:200], company=company[:120])


def fetch(url: str) -> Posting | None:
    """Fetch and read the posting; None when the page can't be fetched (never raises)."""
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS,
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"}) as client:
            with client.stream("GET", url) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return parse(str(response.url), response.status_code, body)
    except Exception:
        return None


def is_placeholder(title: str | None) -> bool:
    return (title or "").strip().lower() in PLACEHOLDER_TITLES
