"""Dates the app completes itself: the 15th for a missing day, never a question to the user.

The user's rule (2026-11), after three applications each waited five minutes on "Please input
your anticipated graduation date" although the profile and the resume already gave the month and year.
"""
from datetime import date

import pytest

from core import dates

PROFILE = {"education": {"school": "State University", "degree": "B.S.", "major": "Finance", "graduation": "May 2028"}}
TODAY = date(2026, 11, 16)


@pytest.mark.parametrize("written,expected,missing", [
    ("May 2028", date(2028, 5, 15), "day"),
    ("Expected May 2028", date(2028, 5, 15), "day"),
    ("05/2028", date(2028, 5, 15), "day"),
    ("2028-05", date(2028, 5, 15), "day"),
    ("Sept. 2023", date(2023, 9, 15), "day"),
    ("Spring 2028", date(2028, 5, 15), "day"),
    ("Fall 2027", date(2027, 12, 15), "day"),
    ("2028", date(2028, 6, 15), "month and day"),
    ("May 20, 2028", date(2028, 5, 20), ""),
    ("20 May 2028", date(2028, 5, 20), ""),
    ("05/20/2028", date(2028, 5, 20), ""),
    ("2028-05-20", date(2028, 5, 20), ""),
])
def test_a_date_is_completed_from_what_is_written(written, expected, missing):
    assert dates.complete(written) == (expected, missing)


def test_only_a_year_is_may_for_a_graduation_and_june_otherwise():
    assert dates.complete("2028", graduation=True) == (date(2028, 5, 15), "month and day")
    assert dates.complete("2028") == (date(2028, 6, 15), "month and day")


@pytest.mark.parametrize("written", ["", "soon", "Present", "room 2028B", "next year", "12345", None])
def test_nothing_is_made_up_where_no_date_is_written(written):
    assert dates.complete(written) is None


def test_one_date_in_every_form_a_site_asks_for():
    shown = dates.formats(date(2028, 5, 15))
    assert "05/15/2028 (MM/DD/YYYY)" in shown and "15/05/2028 (DD/MM/YYYY)" in shown
    assert "2028-05-15" in shown and "May 15, 2028" in shown


def test_the_graduation_date_comes_from_the_profile():
    assert dates.graduation(PROFILE) == (date(2028, 5, 15), "May 2028", "day")
    assert dates.graduation({"education": {"graduation": ""}}) is None and dates.graduation({}) is None


# ── what the agent is told instead of the user being asked ─────────────────────
@pytest.mark.parametrize("question", [
    "Please input your anticipated graduation date", "Expected graduation date", "Graduation Date (MM/DD/YYYY)",
    "When do you graduate?", "Degree completion date",
])
def test_a_graduation_date_question_is_answered_by_the_app(question):
    told = dates.own_answer(question, "date", PROFILE, TODAY)
    assert told is not None and "05/15/2028" in told and "Not asked" in told
    assert 'the profile says "May 2028"' in told and "record_guesses" in told


def test_control_without_a_graduation_in_the_profile_it_is_the_users_to_answer():
    assert dates.own_answer("Please input your anticipated graduation date", "date", {"education": {}}, TODAY) is None


@pytest.mark.parametrize("question", ["Employment Date To", "Start date", "End date (month and year)",
                                      "Date from", "Dates attended", "When did you start this position?"])
def test_a_history_date_question_is_answered_from_the_resume_rule(question):
    told = dates.own_answer(question, "date", PROFILE, TODAY)
    assert told is not None and "use the 15th" in told and "11/16/2026" in told       # today, for a job still going on
    assert "I currently work here" in told


@pytest.mark.parametrize("question,field_type", [
    ("What pre-university education system did you follow?", "dropdown"),
    ("High school graduation date", "date"),                    # the resume rarely lists it: nothing to complete
    ("Secondary education: date from", "date"),
    ("Why do you want to work here?", "text"),
    ("What is your desired salary?", "text"),
    ("Name of your high school", "text"),
])
def test_what_has_no_basis_is_still_the_users_to_answer(question, field_type):
    assert dates.own_answer(question, field_type, PROFILE, TODAY) is None


def test_the_agents_date_rules_name_the_graduation_date_and_today():
    block = dates.prompt_block(PROFILE, TODAY)
    assert block.startswith("DATES (never ask the user for a date that follows from the resume or the profile)")
    assert "the day is the 15th (May 2028 -> 05/15/2028)" in block and "June 15 of that year (May 15 for a graduation)" in block
    assert "Graduation / expected graduation / degree completion date: 05/15/2028 (MM/DD/YYYY)" in block
    assert "Today's date is 11/16/2026 (MM/DD/YYYY)" in block and "tick 'I currently work here'" in block
    assert "is never made up" in block


def test_control_no_graduation_line_without_one_in_the_profile():
    assert "Graduation / expected graduation" not in dates.prompt_block({}, TODAY)
