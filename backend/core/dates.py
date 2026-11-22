"""Dates the app completes itself, so a form asking for a whole date never stops a run.

The user's rule (2026-11): when a form wants a full date and the resume or profile gives only the
month and year, the day is the 15th ("May 2028" -> 05/15/2028). Only a year: June 15, or May 15
for a graduation. Nobody is asked for a graduation date, or for any date that follows from what
the resume and profile already say. A date with no basis at all is never made up.
"""
from __future__ import annotations

import re
from datetime import date

MIDDLE_DAY = 15
MONTH_WHEN_ONLY_YEAR = 6
GRADUATION_MONTH_WHEN_ONLY_YEAR = 5

_MONTHS = {name: number for number, names in enumerate((
    ("january", "jan"), ("february", "feb"), ("march", "mar"), ("april", "apr"), ("may",), ("june", "jun"),
    ("july", "jul"), ("august", "aug"), ("september", "sep", "sept"), ("october", "oct"), ("november", "nov"),
    ("december", "dec")), start=1) for name in names}
# When a term ends: "Spring 2028" is a May graduation.
_SEASONS = {"spring": 5, "summer": 8, "fall": 12, "autumn": 12, "winter": 12}
_MONTH_WORD = "|".join(sorted(_MONTHS, key=len, reverse=True))


def _valid(year: int, month: int, day: int) -> date | None:
    if not 1900 <= year <= 2100:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None


def complete(text: str, graduation: bool = False) -> tuple[date, str] | None:
    """(the date, what was missing) for a date as a resume or profile writes it.

    What was missing is "" (a whole date), "day" (month and year: the 15th) or "month and day"
    (only a year). None when the text holds no date.
    """
    text = (text or "").strip().lower()
    if not text:
        return None
    # Whole dates: 2028-05-20, 05/20/2028, May 20, 2028, 20 May 2028.
    found = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", text)
    if found and (whole := _valid(int(found[1]), int(found[2]), int(found[3]))):
        return whole, ""
    found = re.search(r"\b(\d{1,2})[/.](\d{1,2})[/.](\d{4})\b", text)
    if found:
        first, second, year = int(found[1]), int(found[2]), int(found[3])
        whole = _valid(year, first, second) or _valid(year, second, first)     # month first, as the profile's default
        if whole:
            return whole, ""
    found = re.search(rf"\b({_MONTH_WORD})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", text)
    if found and (whole := _valid(int(found[3]), _MONTHS[found[1]], int(found[2]))):
        return whole, ""
    found = re.search(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTH_WORD})\.?,?\s+(\d{{4}})\b", text)
    if found and (whole := _valid(int(found[3]), _MONTHS[found[2]], int(found[1]))):
        return whole, ""
    # Month and year: May 2028, 05/2028, 2028-05, Spring 2028.
    found = re.search(rf"\b({_MONTH_WORD})\.?,?\s+(?:of\s+)?(\d{{4}})\b", text)
    if found and (middle := _valid(int(found[2]), _MONTHS[found[1]], MIDDLE_DAY)):
        return middle, "day"
    found = re.search(r"\b(\d{1,2})[/.-](\d{4})\b", text) or None
    if found and 1 <= int(found[1]) <= 12 and (middle := _valid(int(found[2]), int(found[1]), MIDDLE_DAY)):
        return middle, "day"
    found = re.search(r"\b(\d{4})[/.-](\d{1,2})\b", text)
    if found and 1 <= int(found[2]) <= 12 and (middle := _valid(int(found[1]), int(found[2]), MIDDLE_DAY)):
        return middle, "day"
    found = re.search(r"\b(spring|summer|fall|autumn|winter)\s+(?:of\s+)?(\d{4})\b", text)
    if found and (middle := _valid(int(found[2]), _SEASONS[found[1]], MIDDLE_DAY)):
        return middle, "day"
    # Only a year.
    found = re.search(r"\b((?:19|20)\d{2})\b", text)
    if found:
        month = GRADUATION_MONTH_WHEN_ONLY_YEAR if graduation else MONTH_WHEN_ONLY_YEAR
        if (middle := _valid(int(found[1]), month, MIDDLE_DAY)):
            return middle, "month and day"
    return None


def formats(day: date) -> str:
    """One date in the forms sites ask for."""
    return (f"{day:%m/%d/%Y} (MM/DD/YYYY), {day:%d/%m/%Y} (DD/MM/YYYY), {day:%Y-%m-%d}, "
            f"{day:%B} {day.day}, {day.year}")


def graduation(profile: dict | None) -> tuple[date, str, str] | None:
    """(date, what the profile says, what was missing) for the profile's graduation, or None."""
    written = str(((profile or {}).get("education") or {}).get("graduation") or "").strip()
    found = complete(written, graduation=True)
    return (found[0], written, found[1]) if found else None


_GRADUATION_QUESTION = re.compile(
    r"\bgraduat\w*|\b(degree|program(me)?|studies|course)\s+(completion|end)\b|\bcompletion\s+date\b"
    r"|\b(expected|anticipated)\s+(completion|end)\b", re.I)
_PRE_UNIVERSITY = re.compile(r"\b(high\s*school|secondary|pre-?university|a-?levels?|gcse|baccalaureate)\b", re.I)
_HISTORY_DATE = re.compile(
    r"\b(start|end|from|to|begin\w*|finish\w*)\s+(date|month|year)\b|\bdate\s+(from|to|started|ended|of\s+(joining|leaving))\b"
    r"|\b(employment|work|job|position|internship|education|attendance|enrol\w*)\s+dates?\b|\bdates?\s+(attended|employed|worked)\b"
    r"|\bwhen\s+did\s+you\s+(start|finish|leave|join|begin|end)\b", re.I)


def own_answer(question: str, field_type: str, profile: dict | None, today: date | None = None) -> str | None:
    """What to tell the agent instead of asking the user this question; None when it is the user's to answer.

    A graduation date comes from the profile, with the 15th for a missing day. A date inside the
    work or education history comes from the resume the same way. A question about school before
    university (which resumes rarely list) is still the user's, like any date with no basis.
    """
    text = question or ""
    if _PRE_UNIVERSITY.search(text):
        return None
    is_date = (field_type or "").lower() == "date" or bool(re.search(r"\bdate\b|\bwhen\b|\bmonth\b|\byear\b", text, re.I))
    if not is_date:
        return None
    today = today or date.today()
    if _GRADUATION_QUESTION.search(text):
        found = graduation(profile)
        if found is None:
            return None
        day, written, missing = found
        how = f" (the profile says \"{written}\"; the {missing} it doesn't give is filled in as the middle of the " \
              f"month)" if missing else f" (the profile says \"{written}\")"
        return (f"Not asked: the graduation date is {formats(day)}{how}. Enter it in the form the field wants and "
                f"log it with record_guesses (basis 'guess', source 'profile: {written}').")
    if _HISTORY_DATE.search(text):
        return ("Not asked: dates in the work and education history come from the resume and profile. Where they "
                f"give only a month and year, use the 15th (May 2028 -> 05/15/2028); only a year: June 15. A job "
                f"that is still going on ('Present'): tick 'I currently work here', or when an end date is required "
                f"use today's date, {formats(today)}. Log each completed date with record_guesses (basis 'guess', "
                "source 'resume: <what it says>').")
    return None


def prompt_block(profile: dict | None, today: date | None = None) -> str:
    """The DATES section of the agent's instructions."""
    today = today or date.today()
    lines = [
        "DATES (never ask the user for a date that follows from the resume or the profile):",
        "- A field wants a whole date and the resume or profile gives only the month and year: the day is the 15th "
        "(May 2028 -> 05/15/2028). Only a year: June 15 of that year (May 15 for a graduation).",
        "- Write every date in the form the field wants (MM/DD/YYYY, DD/MM/YYYY, separate day / month / year boxes, "
        "a month list): the same date, converted.",
    ]
    found = graduation(profile)
    if found is not None:
        day, written, missing = found
        note = f"; the profile says \"{written}\"" if missing else ""
        lines.append(f"- Graduation / expected graduation / degree completion date: {formats(day)}{note}. Never ask for it.")
    lines += [
        f"- Today's date is {formats(today)}. A job or study that is still going on ('Present'): tick 'I currently "
        "work here' / 'Current'. When the form still requires an end date, use today's date (this month and year).",
        "- Years of experience with something: worked out from the resume's dates.",
        "- Log each date you completed this way with record_guesses (basis 'guess', source naming what the resume or "
        "profile says, e.g. 'resume: May 2028').",
        "- A date with nothing behind it in the resume, profile or saved answers (the dates of a school the resume "
        "doesn't list) is never made up: that is one of the few things to ask the user.",
    ]
    return "\n".join(lines)
