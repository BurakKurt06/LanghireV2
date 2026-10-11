"""What a job posting's page says before any browser opens: title, company, places, closed.

Pasted links used to show as "Pasted Link / Unknown" forever, and a posting
that had been taken down still cost a full browser run (a dozen AI requests)
before the agent noticed. Nearly every careers site (Workday, SuccessFactors,
Cornerstone, Oracle, Greenhouse, Lever, iCIMS, ...) puts the job title in the
server-rendered og:title tag or the page title, and many add JSON-LD with the
employer, the locations and the description, so one plain HTTP request
answers most of it. The description and locations feed the fit check.

"Closed" is decided conservatively: a 404/410, a Workday page whose og:title is
empty (Workday's not-found page), a Greenhouse link that redirects to the
company's job board, or a page title that plainly says so. A site that blocks
the request (403, timeouts) is never called closed.
"""
from __future__ import annotations

import html
import json
import re
import socket
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"
)
TIMEOUT_SECONDS = 12
MAX_BYTES = 1_500_000
DESCRIPTION_LIMIT = 6000

# Placeholder titles given to links that were added without one, and generic
# page titles that aren't a role ("About the Firm" on an iCIMS page).
PLACEHOLDER_TITLES = {"", "pasted link", "manually added", "unknown", "untitled"}
GENERIC_TITLES = re.compile(
    r"^(about( us| the (firm|company))?|careers?( site| page| home)?|jobs?( search| board)?|job details|"
    r"search jobs|current openings|open positions|home|welcome|apply|application|candidate (home|experience)( page)?|"
    r"[\w .&-]{0,40} career site)$", re.I)

_CLOSED_PHRASES = (
    "page not found", "job not found", "posting not found", "no longer available", "no longer open",
    "no longer accepting applications", "position has been filled", "job has expired",
    "job is closed", "this job has been closed", "requisition is no longer",
)
_BOT_CHECK = re.compile(
    r"quick check|just a moment|attention required|access denied|are you a robot|verify you are human|"
    r"security check|captcha|request blocked|forbidden",
    re.I,
)
# Legal-form endings and leftovers in employer names.
_LEGAL = re.compile(
    r"(,?\s+(llc|l\.l\.c\.|inc\.?|incorporated|corp\.?|corporation|co\.|ltd\.?|limited|llp|l\.p\.|lp|plc|n\.a\.|"
    r"u\.a\.|gmbh|s\.a\.|ag|b\.v\.|legal entity|confidential))+\.?$", re.I)


@dataclass
class Posting:
    title: str = ""
    company: str = ""
    closed: bool = False
    reason: str = ""
    locations: list[str] = field(default_factory=list)
    remote: bool = False
    description: str = ""


def _meta(page: str, prop: str) -> str | None:
    """Content of <meta property|name=prop>; "" when the tag is there but empty, None when absent."""
    for tag in re.findall(r"<meta\b[^>]*>", page, re.I):
        names = re.findall(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if prop in (n.lower() for n in names):
            m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
            return html.unescape(m.group(1)).strip() if m else ""
    return None


def _json_ld_postings(page: str) -> list[dict]:
    out = []
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.I | re.S):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out += [i for i in items if isinstance(i, dict) and
                (i.get("@type") == "JobPosting" or "hiringOrganization" in i or "HiringOrganization" in i)]
    return out


def _org_name(item: dict) -> str:
    org = item.get("hiringOrganization") or item.get("HiringOrganization")
    if isinstance(org, dict):
        name = org.get("name") or org.get("Name")
        return html.unescape(name).strip() if isinstance(name, str) else ""
    return html.unescape(org).strip() if isinstance(org, str) else ""


def _ld_locations(item: dict) -> tuple[list[str], bool]:
    places = item.get("jobLocation") or []
    places = places if isinstance(places, list) else [places]
    texts = []
    for place in places:
        address = place.get("address") if isinstance(place, dict) else None
        if isinstance(address, dict):
            parts = [address.get(k) for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry")]
            parts = [p.get("name") if isinstance(p, dict) else p for p in parts]
            text = ", ".join(str(p) for p in parts if p)
            if text:
                texts.append(html.unescape(text))
        elif isinstance(address, str) and address.strip():
            texts.append(html.unescape(address.strip()))
    remote = "TELECOMMUTE" in str(item.get("jobLocationType") or "").upper()
    return texts, remote


def _text(fragment: str) -> str:
    fragment = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", fragment or "", flags=re.I | re.S)
    fragment = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", fragment, flags=re.I)
    fragment = html.unescape(re.sub(r"<[^>]+>", " ", fragment))
    fragment = html.unescape(fragment)  # some sites escape twice
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", fragment)).strip()


def _page_title(page: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", page, re.I | re.S)
    return re.sub(r"\s+", " ", html.unescape(m.group(1))).strip() if m else ""


def clean_company(name: str) -> str:
    """'1000 Wayne Tech & Sons LLC' -> 'Wayne Tech & Sons'; 'Stark Global, LLC' -> 'Stark Global'."""
    name = html.unescape(name or "").strip()
    name = re.sub(r"^\d+\s*(?:-\s*)?", "", name)
    name = re.sub(r",?\s+[A-Z][\w ]+ Branch$", "", name)  # "..., New York Branch"
    for _ in range(3):
        name = _LEGAL.sub("", name).strip(" ,.-&")
    return name


def _workday_site_name(url: str) -> str:
    """A descriptive Workday site name: /Umbrella_Labs_External_Site/ -> Umbrella Labs,
    /Wernham_Confidential/ -> Wernham, /Vandelay_Careers/ -> Vandelay (never cryptic ones like W_tx)."""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if parts and parts[0] in ("recruiting",):
        return ""
    if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
        parts = parts[1:]
    site = parts[0] if parts else ""
    if "_" not in site:
        return ""
    stop = {"external", "site", "careers", "career", "jobs", "job", "search", "portal", "us", "usa", "global",
            "confidential", "university", "campus", "students", "early", "internal", "en"}
    words = [w for w in site.split("_") if w and w.lower() not in stop]
    if not words or any(len(w) < 3 for w in words) or len(" ".join(words)) < 4:
        return ""
    return " ".join(words)


def tenant(url: str) -> str:
    """The company's own id in an applicant-tracking link: northwind, wernham, hooli, initrodeamer, ..."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if "myworkdaysite.com" in host:
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts = parts[1:]  # locale prefix: en-US, en-GB, fr-FR, ...
        # /recruiting/<tenant>/...; any other shape names no tenant (never a generic segment).
        return parts[1] if len(parts) >= 2 and parts[0] == "recruiting" else ""
    if "myworkdayjobs.com" in host or host.endswith(".csod.com"):
        return host.split(".")[0]
    if "greenhouse.io" in host or "lever.co" in host or "ashbyhq.com" in host or "smartrecruiters.com" in host:
        return parts[0] if parts else ""
    m = re.match(r"(?:careers|jobs)-([a-z0-9]+)\.icims\.com$", host)
    if m:
        return m.group(1)
    return ""


def _letters(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _brand_in(name: str, slug: str) -> str:
    """The words of `name` that spell the link's company id: ('NWND Cooperative Northwind U.A.', 'northwind') -> 'Northwind'."""
    slug = _letters(slug)
    if len(slug) < 3:
        return ""
    words = re.findall(r"[A-Za-z0-9&.'-]+", name)
    for i in range(len(words)):
        for j in range(i + 1, min(len(words), i + 4) + 1):
            if _letters(" ".join(words[i:j])) == slug:
                return " ".join(words[i:j])
    return ""


def _company_from_titles(page_title: str, site_name: str) -> str:
    m = re.match(r"^(.{2,30}?)\s+Candidate Experience(?: page)?$", site_name or "", re.I)  # "Cyberdyne Candidate Experience page"
    if m:
        return m.group(1)
    patterns = (
        r"^Job Application for .+? at (.+)$",           # Greenhouse
        r"\|\s*Careers at ([^|]+)$",                    # iCIMS, others
        r"^.+? (?:Job Details|Careers) \|\s*([^|]{2,40})$",  # SuccessFactors: "X Job Details | ACX"
        r"^Jobs at (.+)$",
    )
    for pattern in patterns:
        m = re.search(pattern, page_title, re.I)
        if m:
            return re.split(r"\s+[-–—|]\s+", m.group(1).strip())[0]
    if site_name and not re.search(r"\b(careers?|jobs?|recruiting|workday|talent|hiring)\b", site_name, re.I):
        return site_name
    return ""


def _title_from_url(url: str) -> str:
    """iCIMS links carry the role: /jobs/14653/global-real-estate-summer-intern/job -> 'Global Real Estate Summer Intern'."""
    m = re.search(r"/jobs/\d+/([a-z0-9-]{6,})/job", urlparse(url).path, re.I)
    return m.group(1).replace("-", " ").title() if m else ""


def _url_location(url: str) -> str:
    """Workday links carry a place: /job/New-York-NY/... or /job/United-States-Virginia-Reston/..."""
    m = re.search(r"/job/([^/]+)/[^/]+", urlparse(url).path)
    return m.group(1) if m and re.search(r"[A-Za-z]", m.group(1)) else ""


def is_placeholder(title: str | None) -> bool:
    title = (title or "").strip()
    return title.lower() in PLACEHOLDER_TITLES or bool(GENERIC_TITLES.match(title))


def is_workday(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return "myworkdayjobs.com" in host or "myworkdaysite.com" in host


def parse(url: str, status_code: int, page: str, original_url: str | None = None) -> Posting:
    """Read a fetched page (pure function, for tests). `url` is where it ended up after redirects."""
    host = (urlparse(url).hostname or "").lower()
    if status_code in (404, 410):
        return Posting(closed=True, reason=f"The posting is gone (HTTP {status_code}).")
    if status_code >= 400:
        return Posting()
    asked = original_url or url
    if "greenhouse.io" in (urlparse(asked).hostname or "") and "/jobs/" in urlparse(asked).path \
            and "greenhouse.io" in host and "/jobs/" not in urlparse(url).path and "gh_jid" not in url:
        return Posting(closed=True, reason="The posting is closed (the link now opens the company's job board).")

    og_title = _meta(page, "og:title")
    page_title = _page_title(page)
    workday = is_workday(url)
    if workday and og_title == "":
        return Posting(closed=True, reason="The posting was taken down (Workday shows its not-found page).")
    for text in (og_title or "", page_title):
        low = text.lower()
        if low and any(p in low for p in _CLOSED_PHRASES) and len(low) < 120:
            return Posting(closed=True, reason=f'The posting is closed (the page says "{text}").')

    postings = _json_ld_postings(page)
    ld = postings[0] if postings else {}

    title = og_title or ""
    if (not title or is_placeholder(title)) and isinstance(ld.get("title"), str):
        title = html.unescape(ld["title"]).strip()
    if not title or is_placeholder(title):
        title = ""
        m = re.match(r"^Job Application for (.+?) at .+$", page_title, re.I)
        candidate = m.group(1) if m else re.split(r"\s+[|–—]\s+", page_title)[0]
        candidate = re.sub(r"\s+Job Details$", "", candidate, flags=re.I).strip()
        if candidate and not is_placeholder(candidate):
            title = candidate
    title = title or _title_from_url(url)
    if _BOT_CHECK.search(title) or _BOT_CHECK.search(page_title) and not og_title:
        # A bot-check or error page, not the posting: learn nothing from it.
        return Posting()

    slug = tenant(asked)
    org = _org_name(ld)
    company = _brand_in(org, slug) if org and slug else ""
    if not company and workday:
        # Workday's JSON-LD often names a legal entity or division
        # ("0909 SP - Orbital Space Systems"); the site name is the brand.
        company = _workday_site_name(asked)
    company = company or clean_company(org)
    if not company:
        company = clean_company(_company_from_titles(page_title, _meta(page, "og:site_name") or ""))
    if company and is_placeholder(company):
        company = ""

    locations, remote = _ld_locations(ld)
    if remote and locations:
        # Workday marks hybrid jobs TELECOMMUTE too; with an office listed, go by the office.
        remote = bool(re.search(r"\bremote\b", f"{title} {' '.join(locations)}", re.I))
    if workday and _url_location(asked):
        locations.append(_url_location(asked))
    description = _text(ld.get("description") or "") if isinstance(ld.get("description"), str) else ""
    if not description:
        description = _meta(page, "og:description") or _meta(page, "description") or ""
    return Posting(title=title[:200], company=company[:120], locations=locations[:20], remote=remote,
                   description=description[:DESCRIPTION_LIMIT])


def _private_address(host: str) -> bool:
    """True when the host resolves to this computer, the local network or another non-public address."""
    import ipaddress
    try:
        infos = socket.getaddrinfo(host, None)
    except (OSError, UnicodeError):
        return False  # unresolvable: the request fails by itself
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%")[0])
        except ValueError:
            continue
        if not ip.is_global or ip.is_multicast:
            return True
    return False


def _public_only(request: httpx.Request) -> None:
    """Refuse requests to this computer or the local network, redirects included."""
    try:
        from core.shared_config import validate_job_url
    except ImportError:  # pragma: no cover
        from backend.core.shared_config import validate_job_url
    if not validate_job_url(str(request.url)) or _private_address(request.url.host):
        raise httpx.InvalidURL(f"not a public web address: {request.url.host}")


def fetch(url: str) -> Posting | None:
    """Fetch and read the posting; None when the page can't be fetched (never raises).

    Only public web addresses: a link (or a redirect from it) to this computer
    or the home network is never followed.
    """
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS,
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                          event_hooks={"request": [_public_only]}) as client:
            with client.stream("GET", url) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return parse(str(response.url), response.status_code, body, original_url=url)
    except Exception:
        return None
