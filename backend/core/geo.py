"""Is a job inside the user's search area? Worked out in code, with no AI and no internet.

Uses the bundled US postal-code table (data/us_places.csv.gz, GeoNames,
CC BY 4.0). Job locations come in many shapes: "New York, NY",
"100 Main St - Worcester - USA - MA", "Trenton, New Jersey, United States
of America", Workday link slugs like "New-York-NY", or just "Remote".
"""
from __future__ import annotations

import csv
import gzip
import math
import re
import sys
from functools import lru_cache
from pathlib import Path

STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado",
    "CT": "Connecticut", "DE": "Delaware", "DC": "District of Columbia", "FL": "Florida", "GA": "Georgia",
    "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas",
    "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts",
    "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana",
    "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico",
    "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota",
    "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington",
    "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming", "PR": "Puerto Rico",
}
_NAME_TO_ABBR = {name.lower(): abbr for abbr, name in STATES.items()}
# Places that are not in the US (a short list: enough to not call them "unknown").
_FOREIGN = re.compile(
    r"\b(canada|toronto|vancouver|montreal|united kingdom|\buk\b|london|england|ireland|dublin|india|bangalore|"
    r"bengaluru|hyderabad|mumbai|pune|germany|berlin|munich|france|paris|netherlands|amsterdam|spain|madrid|"
    r"poland|warsaw|singapore|hong kong|japan|tokyo|china|shanghai|beijing|australia|sydney|mexico|brazil|"
    r"switzerland|zurich|israel|tel aviv|philippines|manila)\b", re.I)
_REMOTE = re.compile(r"\b(remote|work from home|wfh|telecommute|virtual|anywhere)\b", re.I)

RADII = {"25": 25, "50": 50, "100": 100, "any": None}


def _data_path() -> Path:
    base = Path(getattr(sys, "_MEIPASS", "")) / "core" / "data" if getattr(sys, "frozen", False) else None
    if base and (base / "us_places.csv.gz").exists():
        return base / "us_places.csv.gz"
    return Path(__file__).resolve().parent / "data" / "us_places.csv.gz"


@lru_cache(maxsize=1)
def _table():
    """(zips: zip -> (lat, lon, city, state), cities: (city, state) -> (lat, lon), by_state: state -> [(lat, lon)])."""
    zips, sums, by_state = {}, {}, {}
    with gzip.open(_data_path(), "rt", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            lat, lon = float(row["lat"]), float(row["lon"])
            zips[row["zip"]] = (lat, lon, row["city"], row["state"])
            key = (row["city"].lower(), row["state"])
            s = sums.setdefault(key, [0.0, 0.0, 0])
            s[0] += lat
            s[1] += lon
            s[2] += 1
            by_state.setdefault(row["state"], []).append((lat, lon))
    cities = {k: (v[0] / v[2], v[1] / v[2]) for k, v in sums.items()}
    return zips, cities, by_state


@lru_cache(maxsize=64)
def _city_pattern(state: str):
    """A regex matching any place name of a state, longest names first."""
    _, cities, _ = _table()
    names = sorted({c for c, s in cities if s == state}, key=len, reverse=True)
    if not names:
        return None
    return re.compile(r"\b(" + "|".join(re.escape(n) for n in names) + r")\b", re.I)


def miles(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def zip_point(zip_code: str) -> tuple[float, float] | None:
    hit = _table()[0].get((re.sub(r"\D", "", zip_code or "") or "")[:5])
    return (hit[0], hit[1]) if hit else None


def _states_in(text: str) -> list[str]:
    found = []
    low = text.lower()
    for name, abbr in sorted(_NAME_TO_ABBR.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + re.escape(name) + r"\b", low) and abbr not in found:
            found.append(abbr)
            low = low.replace(name, " ")  # "West Virginia" must not also count as "Virginia"
    for abbr in re.findall(r"\b([A-Z]{2})\b", text):
        if abbr in STATES and abbr not in found:
            found.append(abbr)
    return found


def resolve(text: str) -> dict:
    """One location text -> {"remote", "foreign", "point", "place", "state"} (best effort)."""
    text = (text or "").replace("_", " ")
    text = re.sub(r"(?<=[A-Za-z])-(?=[A-Za-z])", " ", text)  # Workday slugs: New-York-NY
    out = {"remote": bool(_REMOTE.search(text)), "foreign": False, "point": None, "place": "", "state": ""}
    zips, cities, _ = _table()
    for z in re.findall(r"\b(\d{5})(?:-\d{4})?\b", text):
        if z in zips:
            lat, lon, city, state = zips[z]
            out.update(point=(lat, lon), place=f"{city}, {state}", state=state)
            return out
    states = _states_in(text)
    for state in states:
        pattern = _city_pattern(state)
        # Search the text without the state's own name ("New York, NY": the city is "New York").
        m = pattern.search(text) if pattern else None
        if m:
            city = m.group(1).lower()
            out.update(point=cities[(city, state)], place=f"{m.group(1).title()}, {state}", state=state)
            return out
    if states:
        out["state"] = states[0]
    elif _FOREIGN.search(text) and not re.search(r"\b(united states|usa|u\.s\.)\b", text, re.I):
        out["foreign"] = True
    return out


def _state_min_miles(state: str, center: tuple[float, float]) -> float:
    return min(miles(center, p) for p in _table()[2].get(state, [center]))


def check(location_texts: list[str], center_zip: str, radius: str | int | None) -> tuple[str, str]:
    """("in" | "out" | "unknown", explanation) for a posting's locations against the search area.

    In if any location is remote or within the radius; out only when every
    location is clearly outside (or abroad); unknown when it can't be told.
    """
    limit = RADII.get(str(radius), None) if not isinstance(radius, int) else radius
    texts = [t for t in (location_texts or []) if t and t.strip()]
    if limit is None:
        return "in", "Search area: anywhere"
    center = zip_point(center_zip)
    if center is None:
        return "unknown", f"Search-area ZIP {center_zip!r} not found"
    if not texts:
        return "unknown", "No location on the posting"
    outside, unknown = [], []
    for text in texts:
        place = resolve(text)
        if place["remote"]:
            return "in", f"Remote ({text.strip()[:60]})"
        if place["point"]:
            d = miles(center, place["point"])
            if d <= limit:
                return "in", f"{place['place']} is {d:.0f} mi away"
            outside.append(f"{place['place']} ({d:.0f} mi)")
        elif place["state"]:
            d = _state_min_miles(place["state"], center)
            if d > limit:
                outside.append(f"{STATES[place['state']]} (at least {d:.0f} mi)")
            else:
                unknown.append(text.strip()[:60])
        elif place["foreign"]:
            outside.append(f"{text.strip()[:40]} (outside the US)")
        else:
            unknown.append(text.strip()[:60])
    if outside:
        # Placed somewhere, and nowhere inside: out (a job's unplaceable office
        # codes like "VADU01" usually repeat a place that was found).
        return "out", f"Outside your {limit}-mile search area around {center_zip}: " + "; ".join(dict.fromkeys(outside))
    return "unknown", "Couldn't place: " + "; ".join(unknown)
