"""Board location IDs: some boards ignore a typed place and need their own ID for it.

Glassdoor's search address with locKeyword=Boston shows jobs from the whole US (it
redirects to "Data analyst jobs in United States"). With Glassdoor's own ID for the
place (locT=C&locId=1154532, from its location lookup) it shows Boston. The lookup
is one plain request per place (remembered while the app runs); when it fails or
finds nothing that fits, the search keeps the typed place and the log says so.
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import httpx

GLASSDOOR_LOOKUP = "https://www.glassdoor.com/autocomplete/location"
_LOCATION_TYPES = {"C", "S", "N", "M"}      # city, state, country, metro
_cache: dict[str, dict | None] = {}
_cache_lock = threading.Lock()


def _norm(text) -> str:
    text = re.sub(r"[.()]", " ", str(text or "").lower())
    return re.sub(r"\s*,\s*", ", ", re.sub(r"\s+", " ", text)).strip(" ,")


def best_match(place: str, results: list) -> dict | None:
    """The lookup result that is the place the user typed ("Toronto, ON" is Toronto in Ontario,
    not Toronto, OH, even when that comes first); None when nothing fits well."""
    want = _norm(place)
    parts = [p for p in want.split(", ") if p]
    if not parts:
        return None
    first, regions = parts[0], parts[1:]
    best, best_score = None, 0
    for item in results[:10]:
        if not isinstance(item, dict) or str(item.get("locationType")) not in _LOCATION_TYPES \
                or not str(item.get("locationId") or "").isdigit():
            continue
        names = {_norm(item.get(k)) for k in ("locationName", "label", "longName")}
        if want in names:
            score = 10
        else:
            main = _norm(item.get("cityName")) or _norm(item.get("locationName")).split(", ")[0]
            if main != first:
                continue
            score = 5
            known = {_norm(item.get(k)) for k in ("stateAbbreviation", "stateName", "countryName", "country2LetterIso")}
            if any(region not in known for region in regions):
                continue
            score += 3 * len(regions)
        if score > best_score:
            best, best_score = item, score
    return best if best_score >= 5 else None


def _glassdoor_lookup(place: str) -> list:
    """Glassdoor's location suggestions for `place` ([] when it can't be asked)."""
    try:
        from core import job_page
    except ImportError:  # pragma: no cover
        from backend.core import job_page
    try:
        with httpx.Client(follow_redirects=False, timeout=10, transport=job_page._PinnedTransport(),
                          headers={"User-Agent": job_page.USER_AGENT, "Accept": "application/json",
                                   "Accept-Language": "en-US,en;q=0.9"},
                          event_hooks={"request": [job_page._public_only]}) as client:
            response = client.get(GLASSDOOR_LOOKUP, params={
                "locationTypeFilters": "CITY,STATE,COUNTRY", "caller": "jobs", "term": place[:100]})
            if response.status_code != 200 or len(response.content) > 300_000:
                return []
            data = response.json()
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)][:10] if isinstance(data, list) else []


def glassdoor_location(place: str) -> dict | None:
    key = _norm(place)
    if not key:
        return None
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    found = best_match(place, _glassdoor_lookup(place))
    with _cache_lock:
        _cache[key] = found
    return found


def localize(plugin, url: str, place: str) -> tuple[str, str]:
    """(the search address with the board's own ID for `place`, a line for the log).

    Only for the built-in Glassdoor board; any other board, or no place, is unchanged."""
    if getattr(plugin, "name", "") != "glassdoor" or not getattr(plugin, "is_builtin", False) or not (place or "").strip():
        return url, ""
    found = glassdoor_location(place)
    if found is None:
        return url, f"📍 Glassdoor doesn't know \"{place}\", so its results may come from anywhere."
    parsed = urlparse(url)
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if k not in ("locKeyword", "locT", "locId")]
    query += [("locT", str(found["locationType"])), ("locId", str(found["locationId"]))]
    name = found.get("longName") or found.get("label") or place
    return urlunparse(parsed._replace(query=urlencode(query))), f"📍 Glassdoor location: {name}"
