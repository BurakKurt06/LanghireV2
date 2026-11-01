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
    pay: object = None                  # core.pay.Pay when the posting states it
    job_types: list[str] = field(default_factory=list)   # core.job_search.JOB_TYPES
    work_mode: str = ""                 # remote / hybrid / onsite, when the posting says
    posted_at: str = ""                 # datePosted from the posting data
    seniority: str = ""
    company_stated: bool = False        # explicit employer data, not the publisher's site name


def _meta(page: str, prop: str) -> str | None:
    """Content of <meta property|name=prop>; "" when the tag is there but empty, None when absent."""
    for tag in re.findall(r"<meta\b[^<>]*>", page, re.I):     # no "<" in a tag: linear on unclosed tags
        names = re.findall(r'(?:property|name)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        if prop in (n.lower() for n in names):
            m = re.search(r'content\s*=\s*"([^"]*)"', tag, re.I) or re.search(r"content\s*=\s*'([^']*)'", tag, re.I)
            return html.unescape(m.group(1)).strip() if m else ""
    return None


def _json_ld_postings(page: str) -> list[dict]:
    out = []
    low = page.lower()
    # Tag scans stop at "<" and ">", and each closing tag is found once (no pattern rescanning the
    # rest of the page per opening tag): unclosed tags at a cut-off page's end can't make this quadratic.
    for m in re.finditer(r'<script\b[^<>]*application/ld\+json[^<>]*>', page, re.I):
        end = low.find("</script>", m.end())
        if end < 0:
            break
        block = page[m.end():end]
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        out += [i for i in items if isinstance(i, dict) and
                (i.get("@type") == "JobPosting" or "hiringOrganization" in i or "HiringOrganization" in i)]
    return out


def _greenhouse_post(page: str) -> dict:
    """A job-boards.greenhouse.io posting as JSON-LD-like data. Those pages carry no JSON-LD (their
    og:description is just the place); the posting is in the page's route data."""
    m = re.search(r"window\.__remixContext\s*=\s*(\{.*?\});?\s*</script>", page, re.S)
    try:
        routes = json.loads(m.group(1))["state"]["loaderData"] if m else {}
    except (ValueError, KeyError, TypeError):
        return {}
    post = next((r["jobPost"] for r in (routes.values() if isinstance(routes, dict) else [])
                 if isinstance(r, dict) and isinstance(r.get("jobPost"), dict)), None)
    if post is None or not isinstance(post.get("title"), str):
        return {}
    text = lambda key: post.get(key) if isinstance(post.get(key), str) else ""
    item = {"title": post["title"], "hiringOrganization": {"name": text("company_name")},
            "description": "\n".join(text(k) for k in ("introduction", "content", "conclusion") if text(k)),
            "jobLocation": [{"address": p.strip()} for p in re.split(r"\s*[•;]\s*", text("job_post_location")) if p.strip()],
            "datePosted": text("published_at")}
    # Pay ranges ("$150,000" to "$180,000", one per location zone): the span of those in one currency.
    ranges = [r for r in post.get("pay_ranges") or [] if isinstance(r, dict)]
    amounts = []
    for r in ranges:
        try:
            amounts.append((float(re.sub(r"[^\d.]", "", str(r.get("min")))), float(re.sub(r"[^\d.]", "", str(r.get("max"))))))
        except ValueError:
            continue
    currencies = {str(r.get("currency_type") or "").upper() for r in ranges}
    if amounts and len(currencies) == 1:
        item["baseSalary"] = {"currency": currencies.pop(), "value": {"minValue": min(a for a, _ in amounts),
                                                                       "maxValue": max(b for _, b in amounts)}}
    return item


def _microdata_posting(page: str) -> dict:
    """The places and date of a schema.org JobPosting marked up as microdata (SmartRecruiters), as
    JSON-LD-like data: its pages carry no JSON-LD."""
    if not re.search(r"""itemtype\s*=\s*["']https?://schema\.org/JobPosting""", page, re.I):
        return {}

    def props(fragment):
        found = {}
        # A tag holds no "<": bounded so a hostile page can't make this scan quadratic.
        for tag in re.findall(r"<[a-z][^<>]{0,2000}>", fragment, re.I):
            if "itemprop" not in tag.lower():
                continue
            name = re.search(r"""itemprop\s*=\s*["']([^"']+)""", tag, re.I)
            value = re.search(r'\bcontent\s*=\s*"([^"]*)"', tag, re.I) or re.search(r"\bcontent\s*=\s*'([^']*)'", tag, re.I)
            if name and value:
                found.setdefault(name.group(1), html.unescape(value.group(1)).strip())
        return found

    places = []
    for chunk in re.split(r"""itemprop\s*=\s*["']jobLocation["']""", page, flags=re.I)[1:21]:
        found = props(chunk[:3000])
        address = {k: found[k] for k in ("addressLocality", "addressRegion", "addressCountry") if found.get(k)}
        if address:
            places.append({"address": address})
    item = {"jobLocation": places} if places else {}
    date = props(page).get("datePosted")
    if date:
        item["datePosted"] = date
    return item


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


def _without_blocks(fragment: str) -> str:
    """`fragment` without its <script> and <style> elements (an unclosed one stays, as text)."""
    low, out, pos, unclosed = fragment.lower(), [], 0, set()
    for m in re.finditer(r"<(script|style)\b[^<>]*>", fragment, re.I):
        kind = m.group(1).lower()
        if m.start() < pos or kind in unclosed:
            continue
        end = low.find(f"</{kind}>", m.end())
        if end < 0:
            unclosed.add(kind)       # no later one of this kind closes either
            continue
        out += [fragment[pos:m.start()], " "]
        pos = end + len(kind) + 3
    return "".join(out) + fragment[pos:]


def _text(fragment: str) -> str:
    fragment = _without_blocks(fragment or "")
    fragment = re.sub(r"<br\s*/?>|</p>|</li>|</div>", "\n", fragment, flags=re.I)
    fragment = html.unescape(re.sub(r"<[^<>]+>", " ", fragment))
    fragment = html.unescape(fragment)  # some sites escape twice
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n", fragment)).strip()


def _page_title(page: str) -> str:
    m = re.search(r"<title\b[^<>]*>", page, re.I)
    end = page.lower().find("</title>", m.end()) if m else -1
    return re.sub(r"\s+", " ", html.unescape(page[m.end():end])).strip() if end >= 0 else ""


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


def _on(host: str, *domains: str) -> bool:
    """`host` is one of `domains` or under it (never just containing the text: x.greenhouse.io.evil.net)."""
    host = (host or "").lower().rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains)


def tenant(url: str) -> str:
    """The company's own id in an applicant-tracking link: northwind, wernham, hooli, initrodeamer, ..."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    parts = [p for p in parsed.path.split("/") if p]
    if host in ("sjobs.brassring.com", "krb-sjobs.brassring.com", "jobs.brassring.com"):
        from urllib.parse import parse_qsl
        values = {}
        for k, v in parse_qsl(parsed.query, keep_blank_values=True):
            key = k.lower()
            if key in ("partnerid", "siteid"):
                if key in values or not re.fullmatch(r"[0-9]{1,12}", v):
                    return ""
                values[key] = v
        return values['partnerid'] + ':' + values['siteid'] if set(values) == {'partnerid', 'siteid'} else ""
    if host == "recruiting.ultipro.com":
        return parts[0].lower() if parts and re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]) else ""
    if host == "jobs.jobvite.com":
        return parts[0].lower() if parts and re.fullmatch(r"[A-Za-z0-9_-]+", parts[0]) else ""
    if _on(host, "myworkdaysite.com"):
        if parts and re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]):
            parts = parts[1:]  # locale prefix: en-US, en-GB, fr-FR, ...
        # /recruiting/<tenant>/...; any other shape names no tenant (never a generic segment).
        return parts[1] if len(parts) >= 2 and parts[0] == "recruiting" else ""
    if _on(host, "myworkdayjobs.com", "csod.com"):
        return host.split(".")[0]
    if _on(host, "successfactors.com", "successfactors.eu", "sapsf.com", "sapsf.eu"):
        from urllib.parse import parse_qsl
        values = [v for k,v in parse_qsl(parsed.query, keep_blank_values=True) if k == "company"]
        return values[0].lower() if len(values)==1 and re.fullmatch(r"[A-Za-z0-9_-]+", values[0]) else ""
    if _on(host, "greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com", "workable.com"):
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
    # An employer's career site naming the employer: "Careers at Airbnb", "Datadog Careers | Datadog"
    # (a bare "Acme Careers" may be a portal's name, not the employer's).
    m = (re.match(r"^Careers at (.{2,40}?)(?:\s+[-–—|·]\s+.*)?$", site_name or "", re.I)
         or re.match(r"^(.{2,40}?) Careers\s+[-–—|·]\s+\1$", site_name or "", re.I))
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


def _without_site_name(title: str, site_name: str) -> str:
    """'Accounts Payable Analyst | Datadog Careers' (og:site_name 'Datadog Careers | Datadog') -> 'Accounts
    Payable Analyst'; 'Data Scientist at bunq | bunq Careers' (og:site_name 'bunq') -> 'Data Scientist'.
    Only the page's own site name is dropped, never words of the role."""
    if len(title) > 300:     # no real title is this long; these patterns aren't for megabytes
        return title
    names = [n.strip() for n in [site_name] + re.split(r"\s+[-–—|·]\s+", site_name) if len(n.strip()) >= 3]
    for name in list(names):
        names += [f"{name} Careers", f"Careers at {name}", f"{name} Jobs", f"Jobs at {name}"]
    for name in names:
        m = re.fullmatch(r"(.+?)\s+[-–—|·]\s+" + re.escape(name), title, re.I)
        if m:
            title = m.group(1)
            break
    for name in names[:1]:
        m = re.fullmatch(r"(.{2,}?)\s+at\s+" + re.escape(name), title, re.I)
        if m:
            title = m.group(1)
    return title


def _title_from_url(url: str) -> str:
    """iCIMS links carry the role: /jobs/14653/global-real-estate-summer-intern/job -> 'Global Real Estate Summer Intern'."""
    m = re.search(r"/jobs/\d+/([a-z0-9-]{6,})/job", urlparse(url).path, re.I)
    return m.group(1).replace("-", " ").title() if m else ""


def _url_location(url: str) -> str:
    """Workday links carry a place: /job/New-York-NY/... or /job/United-States-Virginia-Reston/..."""
    m = re.search(r"/job/([^/]+)/[^/]+", urlparse(url).path)
    return m.group(1) if m and re.search(r"[A-Za-z]", m.group(1)) else ""


def _element_html(page: str, start: int) -> str:
    """The inner HTML of the <div> whose opening tag starts at `start` (nested divs included)."""
    open_end = page.find(">", start)
    if open_end < 0:
        return ""
    depth, pos = 1, open_end + 1
    for m in re.finditer(r"<(/?)div\b", page[pos:pos + 400_000], re.I):
        depth += -1 if m.group(1) else 1
        if depth == 0:
            return page[pos:pos + m.start()]
    return page[pos:pos + 400_000]


def _linkedin_details(page: str) -> dict:
    """What LinkedIn's public job page says outside its meta tags: the pay range, the
    criteria list (Employment type, Seniority level) and the full description."""
    details = {}
    m = re.search(r'class="[^"<>]{0,500}\bcompensation__salary\b[^"<>]{0,500}"[^<>]*>', page, re.I)
    end = page.lower().find("</div>", m.end()) if m else -1
    if end >= 0:
        details["salary"] = _text(page[m.end():end])
    for m in re.finditer(r'description__job-criteria-subheader[^<>]*>(.{0,1000}?)</h3>\s*<span[^<>]*>(.{0,1000}?)</span>',
                         page, re.S):
        details[_text(m.group(1)).lower()] = _text(m.group(2))
    m = re.search(r'<div\b[^<>]*class="[^"<>]{0,500}\bshow-more-less-html__markup\b', page, re.I)
    if m:
        details["description"] = _text(_element_html(page, m.start()))
    return details


def _ld_types(item: dict) -> list[str]:
    try:
        from core.job_search import job_types_in
    except ImportError:  # pragma: no cover
        from backend.core.job_search import job_types_in
    kinds = item.get("employmentType") or []
    kinds = kinds if isinstance(kinds, list) else [kinds]
    return job_types_in(" ".join(str(k) for k in kinds if isinstance(k, str)))


def _details(url: str, page: str, ld: dict, title: str, locations: list[str], remote: bool,
             description: str, currency: str) -> dict:
    """Pay, job type, work arrangement, posted date and seniority, from the posting data,
    LinkedIn's page, or (pay only) the description."""
    try:
        from core import pay as pay_mod
        from core.job_search import job_types_in, work_mode_in
    except ImportError:  # pragma: no cover
        from backend.core import pay as pay_mod
        from backend.core.job_search import job_types_in, work_mode_in
    linkedin = _on((urlparse(url).hostname or "").lower(), "linkedin.com")
    li = _linkedin_details(page) if linkedin else {}
    if li.get("description") and len(li["description"]) > len(description):
        description = li["description"][:DESCRIPTION_LIMIT]
    found_pay = pay_mod.from_json_ld(ld, currency) if ld else None
    if found_pay is None and li.get("salary"):
        found_pay = pay_mod.parse(li["salary"], currency)
    if found_pay is None:
        found_pay = pay_mod.from_description(description, currency)
    # Never from the title alone: "Contract Specialist" and "Remote Sensing Analyst" name no job type.
    types = (_ld_types(ld) if ld else []) or job_types_in(li.get("employment type", ""))
    mode = "remote" if remote else work_mode_in(" ".join(locations))
    posted = ld.get("datePosted") if isinstance(ld.get("datePosted"), str) else ""
    return {"description": description, "pay": found_pay, "job_types": types, "work_mode": mode,
            "posted_at": posted.strip()[:30], "seniority": li.get("seniority level", "")[:60]}


def is_placeholder(title: str | None) -> bool:
    title = (title or "").strip()
    return title.lower() in PLACEHOLDER_TITLES or bool(GENERIC_TITLES.match(title))


def is_workday(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return _on(host, "myworkdayjobs.com", "myworkdaysite.com")


_LINKEDIN_TITLES = (
    # "Acme hiring Data Analyst in Boston, MA | LinkedIn" (older pages)
    re.compile(r"^(?P<company>.{2,120}?) hiring (?P<title>.{2,200}?) in (?P<place>.{2,120}?) \| LinkedIn$"),
    # "Data Analyst at Acme — Boston, MA | LinkedIn Jobs" (2026 pages; "·" on some)
    re.compile(r"^(?P<title>.{2,200}) at (?P<company>.{2,120}?) [—·] (?P<place>[^|]{2,120}?) \| LinkedIn(?: Jobs)?$"),
)


def _linkedin_title(text: str):
    text = html.unescape(text or "")    # LinkedIn escapes "&" twice in og:title ("&amp;amp;")
    return next((m for m in (rx.match(text) for rx in _LINKEDIN_TITLES) if m), None)


# As collect_jobs._LOGIN_PATH (the results page), plus sign-up pages; LinkedIn's /checkpoint/ is its challenge.
def _with_additions(page_title: str, ld_title: str, org: str) -> bool:
    """The page's title is the posting data's title plus a board's words after it ("... - Feste
    Anstellung, Vollzeit Job in Köln" on StepStone) or the employer before it ("Palantir
    Technologies - ..." on Lever): the data's title is the job's."""
    if len(page_title) > 300:    # no real title is this long; these patterns aren't for megabytes
        return False
    sep = r"\s+[-–—|·]\s+"
    if re.match(re.escape(ld_title) + sep, page_title):
        return True
    m = re.fullmatch(r"(.+?)" + sep + re.escape(ld_title), page_title)
    return bool(m and org and _letters(clean_company(m.group(1))) == _letters(clean_company(org)))


_SIGN_IN_PATHS = {"login", "log-in", "signin", "sign-in", "sign_in", "signup", "sign-up", "authwall", "auth", "access",
                  "checkpoint"}


def _sign_in_instead(url: str, asked: str) -> bool:
    """The request for `asked` was redirected to a sign-in page: /access, /authwall, /nlogin/login, ..."""
    landed, start = urlparse(url), urlparse(asked)
    if (landed.hostname, landed.path) == (start.hostname, start.path):
        return False
    return any(re.sub(r"\.\w{2,5}$", "", part.lower()) in _SIGN_IN_PATHS for part in landed.path.split("/"))


def parse(url: str, status_code: int, page: str, original_url: str | None = None, currency: str = "USD") -> Posting:
    """Read a fetched page (pure function, for tests). `url` is where it ended up after redirects.

    `currency`: what a bare "$" means (the user's dollar currency)."""
    host = (urlparse(url).hostname or "").lower()
    if status_code in (404, 410):
        return Posting(closed=True, reason=f"The posting is gone (HTTP {status_code}).")
    if status_code >= 400:
        return Posting()
    asked = original_url or url
    if _sign_in_instead(url, asked):
        # The site sent the anonymous request to its sign-in page (Handshake, LinkedIn's authwall):
        # its title is not the job's, and a sign-in wall says nothing about the posting being closed.
        return Posting()
    if _on((urlparse(asked).hostname or "").lower(), "greenhouse.io") and "/jobs/" in urlparse(asked).path \
            and _on(host, "greenhouse.io") and "/jobs/" not in urlparse(url).path and "gh_jid" not in url:
        return Posting(closed=True, reason="The posting is closed (the link now opens the company's job board).")

    og_title = _meta(page, "og:title")
    page_title = _page_title(page)
    workday = is_workday(url)
    linkedin_title = _linkedin_title(og_title or page_title) if _on(host, "linkedin.com") else None
    if linkedin_title:
        og_title = linkedin_title.group("title")
    if workday and og_title == "":
        return Posting(closed=True, reason="The posting was taken down (Workday shows its not-found page).")
    for text in (og_title or "", page_title):
        low = text.lower()
        if low and any(p in low for p in _CLOSED_PHRASES) and len(low) < 120:
            return Posting(closed=True, reason=f'The posting is closed (the page says "{text}").')
    if _on(host, "linkedin.com"):
        # A closed LinkedIn posting keeps its title; its top card says "No longer accepting applications".
        m = re.search(r'<figcaption\b[^<>]*class="[^"<>]{0,500}\bclosed-job__flavor--closed\b[^"<>]{0,500}"[^<>]*>'
                      r'(.{0,500}?)</figcaption>', page, re.S)
        if m:
            return Posting(closed=True, reason=f'The posting is closed (LinkedIn says "{_text(m.group(1))[:80]}").')

    postings = _json_ld_postings(page)
    ld = postings[0] if postings else {}
    if not ld and _on(host, "greenhouse.io"):
        ld = _greenhouse_post(page)
    if not ld:
        ld = _microdata_posting(page)

    title = og_title or ""
    ld_title = html.unescape(ld["title"]).strip() if isinstance(ld.get("title"), str) else None
    if (not title or is_placeholder(title)) and ld_title is not None:
        title = ld_title
    elif ld_title and not is_placeholder(ld_title) and _with_additions(title, ld_title, _org_name(ld)):
        title = ld_title
    title = _without_site_name(title, _meta(page, "og:site_name") or "")
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
    if not company and linkedin_title:
        company = clean_company(linkedin_title.group("company"))
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
    if linkedin_title and not locations:
        locations.append(linkedin_title.group("place"))
    description = _text(ld.get("description") or "") if isinstance(ld.get("description"), str) else ""
    if not description:
        description = _meta(page, "og:description") or _meta(page, "description") or ""
    details = _details(url, page, ld, title, locations, remote, description[:DESCRIPTION_LIMIT], currency)
    return Posting(title=title[:200], company=company[:120], company_stated=bool(org or linkedin_title),
                   locations=locations[:20], remote=remote,
                   description=details.pop("description")[:DESCRIPTION_LIMIT], **details)


def _public_only(request: httpx.Request) -> None:
    """Refuse requests to this computer or the local network, redirects included."""
    try:
        from core.shared_config import validate_job_url
    except ImportError:  # pragma: no cover
        from backend.core.shared_config import validate_job_url
    if not validate_job_url(str(request.url)):
        raise httpx.InvalidURL(f"not a public web address: {request.url.host}")


def _public_address(host: str, port: int) -> str:
    """The address to connect to for `host`: resolved once, and only when every answer is public."""
    import ipaddress
    import socket
    try:
        from core.shared_config import is_private_host
    except ImportError:  # pragma: no cover
        from backend.core.shared_config import is_private_host
    try:
        ipaddress.ip_address(host.strip("[]"))
        literal = True
    except ValueError:
        literal = False
    if literal:
        if is_private_host(host, resolve=False):
            raise httpx.InvalidURL(f"not a public web address: {host}")
        return host
    try:
        answers = {info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as e:
        raise httpx.ConnectError(f"couldn't resolve {host}: {e}") from e
    if not answers or any(is_private_host(a, resolve=False) for a in answers):
        raise httpx.InvalidURL(f"not a public web address: {host}")
    return sorted(answers, key=lambda a: ":" in a)[0]  # IPv4 first


class _PinnedTransport(httpx.BaseTransport):
    """Connects to the very address that was checked (each redirect too).

    Checking a name and then letting the connection look it up again left a gap:
    a DNS server can answer with a public address for the check and a private one
    for the connection (DNS rebinding). TLS still verifies the certificate against
    the real host name (sni_hostname), and the Host header keeps the name.
    """

    def __init__(self, *args, **kwargs):
        import threading
        self._args, self._kwargs = args, kwargs
        self._transports = {}
        self._lock = threading.Lock()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        address = _public_address(host, port)
        named = request.url
        extensions = request.extensions
        # The physical URL is an IP; pooling by that IP would share a TLS
        # connection across names with different certificate requirements.
        key = (named.scheme, host, port)
        with self._lock:
            if key not in self._transports:
                self._transports[key] = httpx.HTTPTransport(*self._args, **self._kwargs)
            transport = self._transports[key]
        # Always replace inherited SNI, including redirects to IP literals.
        request.extensions = {**extensions, "sni_hostname": host}
        request.url = named.copy_with(host=address)
        try:
            return transport.handle_request(request)
        finally:
            # The response (and a redirect built from it) keeps the site's name, not the address:
            # the page is read as linkedin.com, and a relative redirect is checked and pinned again.
            request.url = named
            request.extensions = extensions

    def close(self):
        with self._lock:
            transports = list(self._transports.values())
            self._transports.clear()
        for transport in transports:
            transport.close()


def fetch(url: str, currency: str = "USD", *, allowed_hosts=None) -> Posting | None:
    """Fetch and read the posting; None when the page can't be fetched (never raises).

    Only public web addresses: a link (or a redirect from it) to this computer
    or the home network is never followed, and the connection goes to the address
    that was checked (no second lookup).
    """
    def check(request):
        _public_only(request)
        if allowed_hosts is not None and (request.url.scheme != "https" or request.url.port not in (None, 443)
                                         or request.url.host not in allowed_hosts or request.url.username or request.url.password):
            raise httpx.InvalidURL("Posting redirect left this source's configured hosts")
    try:
        with httpx.Client(follow_redirects=True, timeout=TIMEOUT_SECONDS, transport=_PinnedTransport(),
                          headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
                          event_hooks={"request": [check]}) as client:
            with client.stream("GET", url) as response:
                chunks, size = [], 0
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BYTES:
                        break
                body = b"".join(chunks).decode(response.encoding or "utf-8", errors="replace")
                return parse(str(response.url), response.status_code, body, original_url=url, currency=currency)
    except Exception:
        return None
