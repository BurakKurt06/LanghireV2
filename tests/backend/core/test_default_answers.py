"""Default answers and the salary numbers the agent types (the user's decision 6, 2026-11).

Salary from the app's salary setting: a number-only field gets the minimum; yearly or monthly
fields get the rate converted at 40 hours a week (20 an hour is about 41,600 a year, about 3,467
a month); a range is minimum to maximum; no maximum (0) leaves the upper end open, never 0.
"""
import pytest

from core import default_answers as da


def test_an_hourly_rate_is_converted_at_forty_hours_a_week():
    amounts = da.salary_amounts({"min": 20, "max": 0, "period": "hourly", "currency": "USD"})
    assert amounts["hourly"] == (20.0, 0.0)
    assert amounts["yearly"] == (41600.0, 0.0)
    assert amounts["monthly"] == (3467.0, 0.0)          # whole numbers: 3,467, not 3,466.67


def test_a_range_gives_minimum_to_maximum_in_every_period():
    amounts = da.salary_amounts({"min": 20, "max": 30, "period": "hourly"})
    assert amounts["yearly"] == (41600.0, 62400.0)
    assert amounts["monthly"] == (3467.0, 5200.0)
    lines = da.salary_lines({"min": 20, "max": 30, "period": "hourly", "currency": "USD"})
    assert "hourly: number-only field 20 | range 20 to 30" in lines
    assert "yearly: number-only field 41,600 | range 41,600 to 62,400" in lines


@pytest.mark.parametrize("salary", [{"min": 20, "max": 0, "period": "hourly"}, {"min": 20, "max": 15, "period": "hourly"}])
def test_no_maximum_leaves_the_upper_end_open_never_zero(salary):
    lines = da.salary_lines(salary)
    assert "maximum left empty" in lines and "never 0" in lines
    assert " 0 " not in lines.replace("never 0", "")


def test_a_yearly_salary_is_shown_per_month_and_hour_too():
    amounts = da.salary_amounts({"min": 52000, "period": "annual"})
    assert amounts["yearly"] == (52000.0, 0.0) and amounts["monthly"] == (4333.0, 0.0) and amounts["hourly"] == (25.0, 0.0)


def test_only_a_maximum_saved_is_taken_as_the_figure():
    assert da.salary_amounts({"min": 0, "max": 25, "period": "hourly"})["hourly"] == (25.0, 0.0)


def test_no_salary_saved_means_the_one_question_worth_asking():
    assert da.salary_amounts({}) is None
    text = da.salary_lines(None)
    assert "none saved" in text and "ask_for_answers" in text and "optional salary field stays empty" in text


def test_every_default_has_an_answer_and_the_users_edit_wins():
    rows = da.answers({})
    assert [r["id"] for r in rows] == list(da.IDS) and all(r["answer"] for r in rows)
    edited = da.with_edit({}, "how_heard", "A career fair")
    rows = {r["id"]: r for r in da.answers({"default_answers": edited})}
    assert (rows["how_heard"]["answer"], rows["how_heard"]["changed"]) == ("A career fair", True)
    # Putting the default text back (or nothing) stores nothing: a better default later reaches the user.
    assert da.with_edit({"default_answers": edited}, "how_heard", "") == {}
    assert da.with_edit({}, "how_heard", rows["how_heard"]["default"]) == {}
    with pytest.raises(KeyError):
        da.with_edit({}, "no_such_default", "x")


def test_the_standing_answers_the_user_asked_for():
    text = {r["id"]: r["answer"] for r in da.answers({})}
    assert text["how_heard"].startswith("The company's own careers website") and "Never LinkedIn" in text["how_heard"]
    for ident in ("referred", "worked_here", "applied_before", "relatives", "non_compete", "other_special"):
        assert text[ident] == "No."
    assert text["consent_required"] == "Agree." and text["marketing"].startswith("Opt out")
    assert "Summer 2027" in text["start_date"] and "June 2027" in text["start_date"]
    assert text["gpa"].startswith("Optional: leave it blank")
    assert "Required: write one" in text["cover_letter"]
    assert "Never invent" in text["anything_else"]


def test_the_prompt_lists_every_default_and_the_salary_numbers():
    block = da.prompt_block({}, {"salary_expectation": {"min": 20, "max": 30, "period": "hourly"}})
    assert block.startswith("DEFAULT ANSWERS") and "never ask the user about these" in block
    for row in da.answers({}):
        assert row["answer"] in block
    assert "41,600 to 62,400" in block
