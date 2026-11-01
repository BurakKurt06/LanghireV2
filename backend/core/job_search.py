"""What the user is looking for, and which collected listings fit it.

Profile -> Job search holds the defaults; the Collect page can change any of them
for one search. Every listing a collector finds goes through `screen()` before it
is saved: the employer and title exclusions, duplicates (the same job on another
board), recently applied companies, job type, work arrangement, minimum pay,
dealbreaker phrases and the AI's match score. A listing that doesn't fit is not
collected; it is kept in a small "filtered out" list with the reason, so the user
can see what was left out and keep one anyway.

The match bar rises when pay is missing: with a minimum pay set, a posting that
doesn't say what it pays must be a better match, halfway from the normal bar to 10
(a 5/10 bar becomes 7.5/10).
"""
from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

try:
    from core import pay as pay_mod
except ImportError:  # pragma: no cover
    from backend.core import pay as pay_mod

JOB_TYPES = ("full_time", "part_time", "contract", "temporary", "internship")
WORK_MODES = ("remote", "hybrid", "onsite")
EXPERIENCE = ("internship", "entry", "associate", "mid", "senior", "director")
POSTED_WITHIN = (0, 1, 3, 7, 14, 30)        # days; 0 = any time
MODES = ("quick", "detailed")
MAX_LIST = 50
MAX_WORDS = 80

DEFAULTS = {
    "mode": "quick",
    "job_types": [],
    "work_modes": [],
    "experience": [],
    "posted_within": 7,
    "min_pay": {"amount": 0, "period": "year"},
    "part_time_hours": 20,
    "min_match": 5,
    "notes": "",
    "exclude_companies": [],
    "exclude_title_words": [],
    "dealbreakers": [],
    "skip_staffing_agencies": False,
    "company_cooldown_days": 0,
}

LABELS = {
    "full_time": "Full-time", "part_time": "Part-time", "contract": "Contract", "temporary": "Temporary",
    "internship": "Internship", "remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site",
    "entry": "Entry level", "associate": "Associate", "mid": "Mid-senior", "senior": "Senior",
    "director": "Director",
}

# Words on a listing or in its posting data that name the job type / work arrangement.
_TYPE_WORDS = (
    ("internship", r"\bintern(ship)?s?\b|\bco-?op\b|\bpraktik(um|ant)\b|\bwerkstudent|\bINTERN\b"),
    ("part_time", r"\bpart[- _]?time\b|\bteilzeit\b|\bcasual\b"),
    ("temporary", r"\btemp(orary)?\b|\bseasonal\b|\bbefristet\b|\bfixed[- ]term\b"),
    ("contract", r"\bcontract(or)?\b|\bfreelance\b|\bper[- _]diem\b"),
    ("full_time", r"\bfull[- _]?time\b|\bvollzeit\b|\bpermanent\b"),
)
_TYPE_RES = tuple((t, re.compile(rx, re.I)) for t, rx in _TYPE_WORDS)
_MODE_WORDS = (
    ("hybrid", r"\bhybrid\b|teilweise home[- ]?office"),
    ("remote", r"\bremote\b|\bwork from home\b|\bwfh\b|\btelecommute|\bhome[- ]?office\b|\bfully remote\b"),
    ("onsite", r"\bon[- ]?site\b|\bin[- ]office\b|\bin[- ]person\b|\bvor ort\b"),
)
_MODE_RES = tuple((m, re.compile(rx, re.I)) for m, rx in _MODE_WORDS)
# Recruiting and staffing agencies (posting for a client, not hiring themselves).
_AGENCIES = (
    "robert half", "randstad", "adecco", "kelly services", "manpower", "manpowergroup", "aerotek", "teksystems",
    "insight global", "kforce", "apex systems", "allegis", "express employment", "spherion", "volt",
    "hays", "michael page", "page group", "pagegroup", "harvey nash", "modis", "akkodis", "beacon hill",
    "robert walters", "jobot", "cybercoders", "motion recruitment", "experis", "collabera", "mondo", "vaco",
    "addison group", "lhh", "aston carter", "actalent", "staffmark", "peopleready", "tandym", "yoh",
    "judge group", "medix", "amn healthcare", "cross country", "aya healthcare", "hirequest", "atrium",
    "itol recruit", "harnham", "hudson", "korn ferry", "talentbridge", "russell tobin", "solomon page",
)
_AGENCY_WORDS = re.compile(r"\b(staffing|recruit(ing|ment|ers?)|personnel|employment (agency|services)|"
                           r"talent (solutions|partners)|search partners|headhunt)", re.I)


def _short_list(value, limit: int = MAX_LIST, length: int = MAX_WORDS) -> list[str]:
    if not isinstance(value, (list, tuple)):
        return []
    out = []
    for item in value:
        text = re.sub(r"\s+", " ", str(item or "")).strip()[:length]
        if text and text.lower() not in (o.lower() for o in out):
            out.append(text)
    return out[:limit]


def _number(value, low: float, high: float, default: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number:   # NaN
        return default
    return min(high, max(low, number))


def normalize(raw: dict | None) -> dict:
    """The preferences with every value checked: anything malformed falls back to its default."""
    raw = raw if isinstance(raw, dict) else {}
    out = {k: (list(v) if isinstance(v, list) else dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    out["mode"] = raw.get("mode") if raw.get("mode") in MODES else DEFAULTS["mode"]
    for key, allowed in (("job_types", JOB_TYPES), ("work_modes", WORK_MODES), ("experience", EXPERIENCE)):
        values = raw.get(key)
        out[key] = [v for v in allowed if isinstance(values, list) and v in values]
    days = raw.get("posted_within", DEFAULTS["posted_within"])
    out["posted_within"] = days if isinstance(days, int) and not isinstance(days, bool) and days in POSTED_WITHIN \
        else DEFAULTS["posted_within"]
    minimum = raw.get("min_pay") if isinstance(raw.get("min_pay"), dict) else {}
    out["min_pay"] = {
        "amount": round(_number(minimum.get("amount"), 0, 100_000_000, 0), 2),
        "period": minimum.get("period") if minimum.get("period") in pay_mod.PERIODS else "year",
    }
    if isinstance(minimum.get("currency"), str) and re.fullmatch(r"[A-Za-z]{3}", minimum["currency"]):
        out["min_pay"]["currency"] = minimum["currency"].upper()
    out["part_time_hours"] = int(_number(raw.get("part_time_hours"), 4, 39, DEFAULTS["part_time_hours"]))
    out["min_match"] = round(_number(raw.get("min_match"), 0, 10, DEFAULTS["min_match"]) * 2) / 2
    out["notes"] = str(raw.get("notes") or "")[:2000].strip()
    out["exclude_companies"] = _short_list(raw.get("exclude_companies"))
    out["exclude_title_words"] = _short_list(raw.get("exclude_title_words"))
    out["dealbreakers"] = _short_list(raw.get("dealbreakers"))
    out["skip_staffing_agencies"] = raw.get("skip_staffing_agencies") is True
    out["company_cooldown_days"] = int(_number(raw.get("company_cooldown_days"), 0, 365, 0))
    return out


def for_search(profile: dict | None, override: dict | None = None) -> dict:
    """Profile -> Job search, with this search's changes on top; the minimum pay in the
    profile's salary currency unless it names its own."""
    profile = profile if isinstance(profile, dict) else {}
    base = profile.get("job_search") if isinstance(profile.get("job_search"), dict) else {}
    merged = {**base, **(override if isinstance(override, dict) else {})}
    if isinstance(override, dict) and isinstance(override.get("min_pay"), dict):
        merged["min_pay"] = {**(base.get("min_pay") if isinstance(base.get("min_pay"), dict) else {}),
                             **override["min_pay"]}
    prefs = normalize(merged)
    if "currency" not in prefs["min_pay"]:
        currency = str((profile.get("salary_expectation") or {}).get("currency") or "USD").upper()
        prefs["min_pay"]["currency"] = currency if re.fullmatch(r"[A-Z]{3}", currency) else "USD"
    return prefs


def currency_of(prefs: dict) -> str:
    return (prefs.get("min_pay") or {}).get("currency") or "USD"


def job_types_in(text: str) -> list[str]:
    """The job types a listing's words name ("Permanent, full-time" -> ["full_time"])."""
    text = (text or "").replace("_", " ")
    return [t for t, rx in _TYPE_RES if _positive_mention(rx, text)]


def _positive_mention(rx, text):
    for match in rx.finditer(text or ""):
        before = (text or "")[max(0,match.start()-45):match.start()]
        after = (text or "")[match.end():match.end()+45]
        negative_before = re.search(r"\b(?:no|not|non|without|isn't|aren't)\s+(?:(?:a|an|fully|entirely)\s+)*$",before,re.I)
        negative_after = re.match(r"\s*(?:(?:work|working|roles?|positions?|options?|opportunities)\s+)?(?:(?:is|are)\s+)?(?:not\b|unavailable\b)",after,re.I)
        if not negative_before and not negative_after:
            return True
    return False


def work_mode_in(text: str) -> str:
    modes = [mode for mode,rx in _MODE_RES if _positive_mention(rx,text)]
    return "hybrid" if "hybrid" in modes else modes[0] if len(modes)==1 else ""


def posted_at_in(text: str) -> str:
    """An explicit relative age on a card; unknown formats remain unknown."""
    match = re.search(r"\b(\d{1,3})\+?\s*(hours?|days?|weeks?|months?)\s+ago\b", text or "", re.I)
    if not match:
        return ""
    count, unit = int(match[1]), match[2].lower()
    days = count / 24 if unit.startswith("hour") else count * (7 if unit.startswith("week") else 30 if unit.startswith("month") else 1)
    return (datetime.now(timezone.utc)-timedelta(days=days)).date().isoformat()


def _letters(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def company_key(name: str) -> str:
    try:
        from core.job_page import clean_company
    except ImportError:  # pragma: no cover
        from backend.core.job_page import clean_company
    return _letters(clean_company(name or ""))


def title_key(title: str) -> str:
    title = re.sub(r"\((?:m|w|f|d|x|h)(?:\s*/\s*(?:m|w|f|d|x|h))+\)", " ", title or "", flags=re.I)  # (m/w/d)
    return _letters(title)


def city_key(location: str) -> str:
    parts = re.split(r"[,·•|]", location or "")
    place = _letters(parts[0].split("(")[0])
    if place in ("", "remote", "united states", "us", "usa", "anywhere"):
        return ""
    region = _letters(parts[1].split("(")[0]) if len(parts) > 1 else ""
    return place + ("|" + region if region else "")


def _words_in(phrase: str, text: str) -> bool:
    """`phrase` as whole words in `text` ("Meta" isn't in "Metalworks"; "Amazon" is in "Amazon Web Services")."""
    phrase = _letters(phrase)
    return bool(phrase) and f" {phrase} " in f" {_letters(text)} "


def is_agency(company: str) -> bool:
    key = company_key(company)
    ambiguous = {"volt", "hays", "mondo", "hudson", "atrium", "cross country", "beacon hill", "yoh", "medix"}
    return bool(_AGENCY_WORDS.search(company or "")) or any(
        key == a if a in ambiguous else f" {a} " in f" {key} " for a in _AGENCIES)


def match_bar(prefs: dict, has_pay: bool) -> float:
    """The lowest match score that is collected (0: every score). With a minimum pay set,
    a posting without pay must be a better match: halfway from the normal bar to 10."""
    bar = float(prefs.get("min_match") or 0)
    if bar and not has_pay and (prefs.get("min_pay") or {}).get("amount"):
        return bar + (10 - bar) / 2
    return bar


def source_match(search_title: str, listing: dict, prefs: dict, profile: dict) -> tuple[float, str]:
    """Reproducible search fit; untrusted model scores never decide collection.

    This measures stated title/type/arrangement/skills, not employer legitimacy or
    subjective notes. Unknown facts earn no extra points. Exact-title fit starts
    at 8, so an unknown-pay job must still meet the user's raised bar.
    """
    tokens = lambda text: set(re.findall(r"[^\W_]+", str(text).casefold())) - {"a", "an", "the", "and", "of", "for"}
    wanted, title = tokens(search_title), tokens(listing.get("title", ""))
    coverage = len(wanted & title) / len(wanted) if wanted else 0
    score = 8.0 if coverage == 1 else 6.0 if coverage >= .5 else 3.0 if coverage else 0.0
    if coverage == 1:
        if set(prefs.get("job_types") or []) & set(listing.get("job_types") or []):
            score += .5
        if listing.get("work_mode") and listing["work_mode"] in (prefs.get("work_modes") or []):
            score += .5
        text = f"{listing.get('title') or ''}\n{listing.get('text') or ''}\n{listing.get('description') or ''}"
        if any(_words_in(str(skill), text) for skill in (profile.get("skills") or [])[:50]):
            score += .5
        if any(_words_in(LABELS.get(level,level), listing.get("seniority") or "") for level in prefs.get("experience") or []):
            score += .5
    return min(10, score), f"Source-based fit: title words {len(wanted & title)}/{len(wanted)}; stated preferences and skills only"


@dataclass
class Context:
    """What the screen compares against: jobs already in the list, and recent applications."""
    duplicates: dict = field(default_factory=dict)     # (title, company) -> set of cities
    cooldown: set = field(default_factory=set)          # company keys applied to recently

    @classmethod
    def from_jobs(cls, jobs: dict, prefs: dict, now: datetime | None = None) -> "Context":
        ctx = cls()
        now = now or datetime.now(timezone.utc)
        days = int(prefs.get("company_cooldown_days") or 0)
        for job in (jobs or {}).values():
            if not isinstance(job, dict):
                continue
            ctx.add(job)
            if days and job.get("status") == "applied" and job.get("applied_at"):
                try:
                    applied = datetime.fromisoformat(str(job["applied_at"]).replace("Z", "+00:00"))
                    if applied.tzinfo is None:
                        applied = applied.replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
                if now - applied <= timedelta(days=days):
                    ctx.cooldown.add(company_key(job.get("company", "")))
        ctx.cooldown.discard("")
        return ctx

    def add(self, job: dict) -> None:
        key = (title_key(job.get("title", "")), company_key(job.get("company", "")))
        if all(key):
            self.duplicates.setdefault(key, set()).add(city_key(job.get("location", "")))

    def duplicate(self, job: dict) -> bool:
        key = (title_key(job.get("title", "")), company_key(job.get("company", "")))
        if not all(key) or key not in self.duplicates:
            return False
        city = city_key(job.get("location", ""))
        cities = self.duplicates[key]
        return not city or "" in cities or any(
            city == other or ("|" not in city or "|" not in other) and city.split("|")[0] == other.split("|")[0]
            for other in cities)


def screen(listing: dict, prefs: dict, ctx: Context, final: bool = True) -> tuple[str, str] | None:
    """(kind, why) when this listing shouldn't be collected; None to keep it.

    `listing`: title, company, location, text (the listing's words), job_types, work_mode,
    pay (a Pay or None), description, match (a 0-10 score or None). With `final=False` only
    what a listing card tells is checked; the minimum pay, dealbreakers and the match bar
    wait for the posting itself.
    """
    title, company = listing.get("title") or "", listing.get("company") or ""
    for name in prefs.get("exclude_companies") or ():
        if _words_in(name, company):
            return "company", f"Excluded employer ({name})"
    for word in prefs.get("exclude_title_words") or ():
        if _words_in(word, title):
            return "title", f'Title has "{word}"'
    if prefs.get("skip_staffing_agencies") and company and is_agency(company):
        return "agency", f"Staffing or recruiting agency ({company[:60]})"
    if ctx.duplicate(listing):
        return "duplicate", "Already in your list (same title and employer)"
    if company_key(company) in ctx.cooldown:
        return "cooldown", f"You applied to {company[:60]} in the last {prefs.get('company_cooldown_days')} days"
    wanted_types = prefs.get("job_types") or []
    types = listing.get("job_types") or []
    if wanted_types and types and not set(types) & set(wanted_types):
        return "job_type", f"{', '.join(LABELS.get(t, t) for t in types)} (you want " \
                           f"{', '.join(LABELS.get(t, t) for t in wanted_types)})"
    wanted_modes = prefs.get("work_modes") or []
    mode = listing.get("work_mode") or ""
    if wanted_modes and mode and mode not in wanted_modes:
        return "work_mode", f"{LABELS.get(mode, mode)} (you want {', '.join(LABELS.get(m, m) for m in wanted_modes)})"
    pay = listing.get("pay")
    minimum = prefs.get("min_pay") or {}
    if pay is not None and minimum.get("amount") and pay_mod.compare(pay, minimum) == "below":
        wanted = f"{minimum.get('currency', '')} {minimum['amount']:,.0f}/{minimum.get('period', 'year')}".strip()
        return "pay", f"Pays {pay_mod.describe(pay.to_dict())}, below your minimum ({wanted})"
    if not final:
        return None
    seniority = _letters(listing.get("seniority") or "")
    levels = {"internship": {"internship"}, "entry level": {"entry"}, "associate": {"associate"},
              "mid senior level": {"mid", "senior"}, "mid senior": {"mid", "senior"},
              "mid": {"mid"}, "senior": {"senior"}, "director": {"director"}, "executive": {"director"}}
    known_levels = levels.get(seniority, set())
    experience = prefs.get("experience") or []
    if experience and known_levels and not known_levels.intersection(experience):
        return "experience", f"Experience level: {listing['seniority']} (does not match your search)"
    days = prefs.get("posted_within") or 0
    if days and listing.get("posted_at"):
        try:
            posted = datetime.fromisoformat(str(listing["posted_at"]).replace("Z", "+00:00"))
            if posted.date() < datetime.now(timezone.utc).date() - timedelta(days=days):
                return "posted", f"Posted more than {days} days ago"
        except (ValueError, TypeError, OverflowError):
            pass  # unknown dates do not establish that a posting is too old
    haystack = f"{title}\n{listing.get('text') or ''}\n{listing.get('description') or ''}"
    for phrase in prefs.get("dealbreakers") or ():
        if _words_in(phrase, haystack):
            return "dealbreaker", f'Mentions "{phrase}"'
    match = listing.get("match")
    comparable = pay is not None and (not minimum.get("amount") or pay_mod.compare(pay, minimum) != "unknown")
    bar = match_bar(prefs, comparable)
    if bar and not comparable and minimum.get("amount") and not isinstance(match, (int, float)):
        return "match", f"Match not available; a listing with unknown pay needs {bar:g}/10"
    if isinstance(match, (int, float)):
        if bar and match < bar:
            why = (listing.get("match_reason") or "").strip()
            extra = "" if comparable or bar == prefs.get("min_match") else " (no pay listed in your currency, so the bar is higher)"
            return "match", f"Match {match:g}/10, below {bar:g}{extra}" + (f": {why[:120]}" if why else "")
    return None


# ── Filtered-out listings and saved searches (small JSON files beside jobs.json) ──
_store_lock = threading.Lock()
FILTERED_LIMIT = 1000


def _paths():
    try:
        from core import shared_config as config
    except ImportError:  # pragma: no cover
        from backend.core import shared_config as config
    return config, config.DATA_DIR / "filtered_jobs.json", config.DATA_DIR / "saved_searches.json"


def _locked(path):
    from filelock import FileLock
    return FileLock(str(path) + ".lock")


def record_filtered(url: str, entry: dict) -> None:
    config, filtered, _ = _paths()
    with _store_lock, _locked(filtered):
        data = config.load_json(filtered, {})
        if not isinstance(data, dict):
            data = {}
        data[url] = {**entry, "filtered_at": datetime.now(timezone.utc).isoformat()}
        if len(data) > FILTERED_LIMIT:
            for old in sorted(data, key=lambda u: str(data[u].get("filtered_at", "")))[:len(data) - FILTERED_LIMIT]:
                del data[old]
        config.save_json(filtered, data)


def filtered_jobs() -> dict:
    config, filtered, _ = _paths()
    with _store_lock, _locked(filtered):
        data = config.load_json(filtered, {})
    return data if isinstance(data, dict) else {}


def take_filtered(url: str) -> dict | None:
    """Remove one listing from the filtered-out list and return it (None if it isn't there)."""
    config, filtered, _ = _paths()
    with _store_lock, _locked(filtered):
        data = config.load_json(filtered, {})
        if not isinstance(data, dict) or url not in data:
            return None
        entry = data.pop(url)
        config.save_json(filtered, data)
    return entry


def keep_filtered(url: str, save) -> int | None:
    """Save to jobs durably before deleting; failures leave the original recoverable.

    The lock order is filtered -> jobs. No jobs mutation holds its lock while
    acquiring this lock. Holding it through save also linearizes concurrent Keep.
    """
    config, filtered, _ = _paths()
    with _store_lock, _locked(filtered):
        data = config.load_json(filtered, {})
        if not isinstance(data, dict) or url not in data:
            return None
        added = save(data[url])
        del data[url]
        config.save_json(filtered, data)
        return added


def clear_filtered() -> int:
    config, filtered, _ = _paths()
    with _store_lock, _locked(filtered):
        data = config.load_json(filtered, {})
        config.save_json(filtered, {})
    return len(data) if isinstance(data, dict) else 0


MAX_SEARCHES = 50


def clean_saved_search(raw) -> dict | None:
    if not isinstance(raw, dict):
        return None
    name = re.sub(r"\s+", " ", str(raw.get("name") or "")).strip()[:60]
    if not name:
        return None
    sources = [s for s in _short_list(raw.get("sources"), 20, 40) if re.fullmatch(r"[A-Za-z0-9_.-]+", s)]
    filters = raw.get("filters") if isinstance(raw.get("filters"), dict) else {}
    filters = {str(k)[:40]: str(v)[:100] for k, v in list(filters.items())[:20]
               if isinstance(v, (str, int, float)) and not isinstance(v, bool)}
    try:
        max_jobs = int(raw.get("max_jobs") or 0)
    except (TypeError, ValueError, OverflowError):
        max_jobs = 0
    search_id = str(raw.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,40}", search_id):
        search_id = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40] or "search"
    return {"id": search_id, "name": name, "title": str(raw.get("title") or "").strip()[:200],
            "sources": sources, "filters": filters, "max_jobs": min(500, max(0, max_jobs)),
            "search": normalize(raw.get("search"))}


def saved_searches() -> list[dict]:
    config, _, searches = _paths()
    with _store_lock, _locked(searches):
        data = config.load_json(searches, [])
    return [s for s in (clean_saved_search(x) for x in (data if isinstance(data, list) else [])) if s]


def save_searches(items: list) -> list[dict]:
    config, _, searches = _paths()
    cleaned, ids = [], set()
    for item in (items if isinstance(items, list) else [])[:MAX_SEARCHES]:
        search = clean_saved_search(item)
        if not search:
            continue
        base, n = search["id"], 2
        while search["id"] in ids:
            suffix = f"-{n}"
            search["id"] = base[:40-len(suffix)] + suffix
            n += 1
        ids.add(search["id"])
        cleaned.append(search)
    with _store_lock, _locked(searches):
        config.save_json(searches, cleaned)
    return cleaned
