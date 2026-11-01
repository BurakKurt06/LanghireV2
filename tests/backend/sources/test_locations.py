"""Glassdoor's own place IDs: its search ignores a typed place (locKeyword) and shows the whole US."""
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from sources import locations
from sources.registry import PluginRegistry


def place(kind, ident, name, city="", state="", abbr="", country="United States", iso="US", long=""):
    return {"locationType": kind, "locationId": ident, "locationName": name, "label": name,
            "longName": long or f"{name} ({iso})", "cityName": city, "stateName": state,
            "stateAbbreviation": abbr, "countryName": country, "country2LetterIso": iso}


# The lookup's shape (ids made up for the tests).
TORONTO = [place("C", 101, "Toronto, OH", "Toronto", "Ohio", "OH"),
           place("C", 102, "Toronto, KS", "Toronto", "Kansas", "KS"),
           place("C", 103, "Toronto, ON", "Toronto", "Ontario", "ON", "Canada", "CA", "Toronto, ON (Canada)")]
BOSTON = [place("C", 201, "Boston, MA", "Boston", "Massachusetts", "MA"),
          place("C", 202, "South Boston, MA", "South Boston", "Massachusetts", "MA")]
STATE = [place("S", 301, "Massachusetts", long="Massachusetts, US"),
         place("C", 302, "Massachusetts Avenue Heights, DC", "Massachusetts Avenue Heights", "District of Columbia", "DC")]


@pytest.fixture(autouse=True)
def fresh_cache():
    locations._cache.clear()
    yield
    locations._cache.clear()


@pytest.mark.parametrize("typed, results, ident", [
    ("Toronto, ON", TORONTO, 103),          # not Toronto, Ohio, which the lookup lists first
    ("toronto,  on", TORONTO, 103),
    ("Boston, MA", BOSTON, 201),
    ("Boston", BOSTON, 201),
    ("Massachusetts", STATE, 301),
    ("Toronto, Ontario", TORONTO, 103),
])
def test_the_place_the_user_typed_is_picked(typed, results, ident):
    assert locations.best_match(typed, results)["locationId"] == ident


@pytest.mark.parametrize("typed, results", [
    ("Boston, CA", BOSTON),                  # a Boston, but not in California
    ("Springfield", BOSTON),
    ("", BOSTON),
    ("Boston, MA", [{"locationType": "X", "locationId": 1, "locationName": "Boston, MA"}, "junk", None]),
])
def test_nothing_that_fits_is_never_guessed(typed, results):
    assert locations.best_match(typed, results) is None


def glassdoor():
    return PluginRegistry().get_by_name("glassdoor")


def test_glassdoors_search_gets_its_place_id(monkeypatch):
    asked = []
    monkeypatch.setattr(locations, "_glassdoor_lookup", lambda typed: asked.append(typed) or BOSTON)
    plugin = glassdoor()
    url = plugin.build_search_url(["Data Analyst"], ["Boston, MA"], 0, None, search={"posted_within": 7})
    assert "locKeyword=Boston" in url
    new, note = locations.localize(plugin, url, "Boston, MA")
    query = parse_qs(urlparse(new).query)
    assert query["locId"] == ["201"] and query["locT"] == ["C"] and "locKeyword" not in query
    assert query["sc.keyword"] == ["Data Analyst"] and query["fromAge"] == ["7"]
    assert urlparse(new).netloc == "www.glassdoor.com" and "Boston, MA (US)" in note
    locations.localize(plugin, url, "Boston, MA")
    assert asked == ["Boston, MA"]          # asked once while the app runs


def test_an_unknown_place_keeps_the_search_and_says_so(monkeypatch):
    monkeypatch.setattr(locations, "_glassdoor_lookup", lambda typed: [])
    url = glassdoor().build_search_url(["Analyst"], ["Atlantis"], 0, None)
    new, note = locations.localize(glassdoor(), url, "Atlantis")
    assert new == url and "doesn't know" in note


def test_other_boards_and_imported_lookalikes_are_left_alone(monkeypatch):
    monkeypatch.setattr(locations, "_glassdoor_lookup", lambda typed: pytest.fail("no lookup"))
    linkedin = PluginRegistry().get_by_name("linkedin")
    url = linkedin.build_search_url(["Analyst"], ["Boston, MA"], 0, None)
    assert locations.localize(linkedin, url, "Boston, MA") == (url, "")
    imported = SimpleNamespace(name="glassdoor", is_builtin=False)
    assert locations.localize(imported, "https://x.example/?locKeyword=a", "Boston") == ("https://x.example/?locKeyword=a", "")
    assert locations.localize(glassdoor(), "https://www.glassdoor.com/Job/jobs.htm", "  ") == ("https://www.glassdoor.com/Job/jobs.htm", "")


def test_a_failed_lookup_is_an_empty_answer(monkeypatch):
    import httpx

    def refuse(self, request):
        raise httpx.ConnectError("offline")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", refuse)
    import socket
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert locations._glassdoor_lookup("Boston, MA") == []
