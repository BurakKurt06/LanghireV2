"""The free location check: places from real postings, distances, the search area."""
import pytest

from core import geo


@pytest.mark.parametrize("text,place", [
    ("New York, NY (1271 AOA/6th Ave)", "New York, NY"),
    ("100 Main St - Worcester - USA - MA", "Worcester, MA"),
    ("Trenton, New Jersey, United States of America", "Trenton, NJ"),
    ("New-York-NY", "New York, NY"),                       # Workday link slug
    ("United-States-Virginia-Reston", "Reston, VA"),
    ("Florham-Park-NJ-US", "Florham Park, NJ"),
    ("University Park, Pennsylvania", "University Park, PA"),
    ("100 State St., Springfield IL 62701", "Springfield, IL"),     # a ZIP wins
])
def test_places_from_real_postings(text, place):
    assert geo.resolve(text)["place"] == place


def test_west_virginia_is_not_virginia():
    assert geo.resolve("Charleston, West Virginia")["state"] == "WV"


@pytest.mark.parametrize("text,key", [("Remote - US", "remote"), ("London, United Kingdom", "foreign")])
def test_remote_and_abroad(text, key):
    assert geo.resolve(text)[key] is True


def test_distance_is_roughly_right():
    nyc, trenton = geo.zip_point("10001"), geo.zip_point("08608")
    assert 50 < geo.miles(nyc, trenton) < 65


@pytest.mark.parametrize("radius,verdict", [("25", "out"), ("50", "out"), ("100", "in"), ("any", "in")])
def test_search_area_radius(radius, verdict):
    assert geo.check(["Philadelphia, PA"], "10001", radius)[0] == verdict


def test_any_location_inside_or_remote_passes():
    assert geo.check(["Lowell, MA", "Hoboken, NJ"], "10001", "25")[0] == "in"
    assert geo.check(["Lowell, MA", "Remote"], "10001", "25")[0] == "in"


def test_outside_explains_where_and_how_far():
    verdict, detail = geo.check(["Worcester, MA", "Worcester, MA"], "10001", "50")
    assert verdict == "out" and detail.count("Worcester, MA") == 1 and "mi" in detail


def test_a_far_state_without_a_city_is_out_but_a_near_one_is_unknown():
    assert geo.check(["California"], "10001", "100")[0] == "out"
    assert geo.check(["New Jersey"], "10001", "100")[0] == "unknown"


def test_unplaceable_and_missing_locations_are_unknown():
    assert geo.check(["Chanin Corporate Headquarters"], "10001", "50")[0] == "unknown"
    assert geo.check([], "10001", "50")[0] == "unknown"
    assert geo.check(["New York, NY"], "00000", "50")[0] == "unknown"


def test_a_far_place_plus_an_unplaced_town_is_not_out():
    """One location outside and one the table can't place: the second may be inside, so unknown."""
    status, why = geo.check(["Los Angeles, CA", "Smallville Township"], "10001", "50")
    assert status == "unknown" and "Smallville" in why


def test_office_codes_next_to_a_far_place_are_still_out():
    assert geo.check(["Los Angeles, CA", "VADU01"], "10001", "50")[0] == "out"
    assert geo.check(["Los Angeles, CA", "US-LAX-12"], "10001", "50")[0] == "out"
