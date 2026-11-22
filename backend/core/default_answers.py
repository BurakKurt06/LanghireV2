"""Default answers: what the agent answers when the profile and notes say nothing special.

The user's rule (2026-11): if the profile and notes don't mention something special, the
answer is the ordinary one; whoever has something unusual to declare puts it in the profile.
The agent never asks the user these. Each default is one editable line, shown in one place in
the app (Q&A → Default answers); the user's own saved answers and edits always win over them.

Stored in settings.json under "default_answers" as {id: the user's text}: only what the user
changed, so a default improved in a later version reaches everyone who kept it.
"""
from __future__ import annotations

HOURS_PER_YEAR = 2080      # 40 hours a week
MONTHS_PER_YEAR = 12

# (id, the kind of question as the app shows it, the default answer)
DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("how_heard", "How did you hear about us? / Where did you find this job?",
     "The company's own careers website. Never LinkedIn, Indeed, another job board or an employee referral. "
     "If the list has no such option, pick 'Company website', 'Careers page' or 'Other' (type 'Company careers "
     "website' if it asks)."),
    ("work_authorization", "Are you legally authorized to work in the job's country / the country you live in?",
     "Yes, when Profile → Work authorization says I am authorized to work there."),
    ("sponsorship", "Will you now or in the future need visa sponsorship?",
     "What Profile → Visa sponsorship needed says."),
    ("relocation", "Are you willing to relocate, commute or work on site?",
     "What Profile → Willing to relocate and Preferred work mode say; when they say nothing, Yes."),
    ("ordinary_yes", "Are you at least 18? Can you do the essential duties of the job? Will you follow company policies?",
     "Yes."),
    ("referred", "Were you referred by an employee? / Do you have a reference at this company?", "No."),
    ("worked_here", "Have you worked for this company (or its affiliates) before?", "No."),
    ("applied_before", "Have you applied to this company before?", "No."),
    ("relatives", "Do you have relatives or know anyone who works here?", "No."),
    ("non_compete", "Are you bound by a non-compete or any other restrictive agreement?", "No."),
    ("other_special",
     "Any other \"do you have / are you / have you ever\" question about something unusual that the profile and "
     "notes don't mention (criminal record, conflict of interest, government or military service, security "
     "clearance, discipline, ...)",
     "No."),
    ("certify", "Certifications and declarations that the application is true, complete and accurate",
     "Agree: tick the box (or type my name where it asks for a signature). The same one asked twice gets the same answer."),
    ("consent_required", "Required consent: privacy policy, terms, data processing, background or reference checks, drug test",
     "Agree."),
    ("marketing", "Optional marketing emails, text messages, newsletters or talent communities",
     "Opt out: answer No or leave the box unticked."),
    ("demographics", "Voluntary questions: gender, race / ethnicity, veteran status, disability",
     "My answers under Profile → Screening answers. Where the profile has none: the 'I don't wish to answer' / "
     "'Decline to self-identify' option."),
    ("salary", "Desired salary / pay expectation",
     "My salary from Profile → Salary. A number-only field gets my minimum. A range gets my minimum to my maximum. "
     "A yearly or monthly field gets the amount converted from my rate (40 hours a week). With no maximum set: my "
     "minimum, the upper end left open; only if the form forces an upper number, my minimum there too. Never 0."),
    ("start_date", "When can you start? / Available start date",
     "From the posting when it names a period (\"Summer 2027\" → June 2027); otherwise Profile → Start date."),
    ("history_dates", "Dates, titles, employers, schools and skills in work or education history",
     "From my resume and profile. A form that wants a whole date where they give only the month and year gets the "
     "15th (May 2028 → 05/15/2028). A job that is still going on: 'I currently work here', or today's date when "
     "an end date is required."),
    ("cover_letter", "Cover letter", "Optional: skip it. Required: write one from my resume, profile and notes."),
    ("gpa", "GPA", "Optional: leave it blank. Required: the GPA in my profile or resume."),
    ("anything_else", "Anything else that nothing above, my profile, my resume or my saved answers tells you",
     "The most sensible answer worked out from what they do say, logged as a guess (with what it was worked out "
     "from) for me to check. Never invent skills, experience, certifications, or a date with nothing behind it."),
)
IDS = tuple(d[0] for d in DEFAULTS)
MAX_ANSWER_LENGTH = 600


def answers(settings: dict | None) -> list[dict]:
    """[{id, question, answer, default, changed}] in display order, with the user's edits applied."""
    saved = (settings or {}).get("default_answers")
    saved = saved if isinstance(saved, dict) else {}
    rows = []
    for ident, question, default in DEFAULTS:
        mine = saved.get(ident)
        mine = mine.strip()[:MAX_ANSWER_LENGTH] if isinstance(mine, str) else ""
        rows.append({"id": ident, "question": question, "answer": mine or default, "default": default,
                     "changed": bool(mine) and mine != default})
    return rows


def with_edit(settings: dict | None, ident: str, answer: str | None) -> dict:
    """The "default_answers" value after one edit (answer None or "" puts the default back)."""
    if ident not in IDS:
        raise KeyError(ident)
    saved = (settings or {}).get("default_answers")
    saved = dict(saved) if isinstance(saved, dict) else {}
    text = (answer or "").strip()[:MAX_ANSWER_LENGTH]
    default = next(d[2] for d in DEFAULTS if d[0] == ident)
    if text and text != default:
        saved[ident] = text
    else:
        saved.pop(ident, None)
    return {k: v for k, v in saved.items() if k in IDS}


def _number(value) -> float:
    try:
        return max(0.0, float(value or 0))
    except (TypeError, ValueError):
        return 0.0


def _fmt(amount: float) -> str:
    return f"{amount:,.0f}" if abs(amount - round(amount)) < 0.005 else f"{amount:,.2f}"


def salary_amounts(salary: dict | None) -> dict | None:
    """The user's pay per hour, month and year: {"hourly": (min, max), "monthly": ..., "yearly": ...}.

    max is 0.0 when no maximum is set (0, empty or below the minimum): "no maximum", never a
    salary of 0. None when no minimum is set either.
    """
    salary = salary or {}
    low, high = _number(salary.get("min")), _number(salary.get("max"))
    if not low and high:
        low, high = high, 0.0       # only one figure saved: that is the figure
    if not low:
        return None
    if high <= low:
        high = 0.0
    period = str(salary.get("period") or "annual").lower()
    if period.startswith(("hour", "per hour")):
        per_year = HOURS_PER_YEAR
    elif period.startswith("month"):
        per_year = MONTHS_PER_YEAR
    elif period.startswith("week"):
        per_year = 52
    elif period.startswith(("day", "daily")):
        per_year = 260
    else:
        per_year = 1
    yearly = (low * per_year, high * per_year)
    # Monthly and yearly amounts are whole numbers (20 an hour is 3,467 a month, not 3,466.67).
    return {"hourly": tuple(round(v / HOURS_PER_YEAR, 2) for v in yearly),
            "monthly": tuple(float(round(v / MONTHS_PER_YEAR)) for v in yearly),
            "yearly": tuple(float(round(v)) for v in yearly)}


def salary_lines(salary: dict | None) -> str:
    """The numbers to type into salary fields, worked out here so the agent does no arithmetic."""
    amounts = salary_amounts(salary)
    if amounts is None:
        return ("SALARY NUMBERS: none saved in the profile. An optional salary field stays empty; a REQUIRED one "
                "is the one thing to ask the user about (ask_for_answers).")
    currency = str((salary or {}).get("currency") or "USD")

    def row(name: str) -> str:
        low, high = amounts[name]
        if high:
            return f"{name}: number-only field {_fmt(low)} | range {_fmt(low)} to {_fmt(high)}"
        return (f"{name}: number-only field {_fmt(low)} | range: minimum {_fmt(low)}, maximum left empty "
                f"(\"{_fmt(low)}+\"; only if a maximum is forced, {_fmt(low)} again)")

    return (f"SALARY NUMBERS ({currency}; type digits only into number fields, no currency sign, and never 0):\n"
            f"- {row('hourly')}\n- {row('monthly')}\n- {row('yearly')}")


def prompt_block(settings: dict | None, profile: dict | None) -> str:
    """The DEFAULT ANSWERS section of the agent's instructions."""
    lines = "\n".join(f"- {row['question']}: {row['answer']}" for row in answers(settings))
    return ("DEFAULT ANSWERS (the ordinary answer applies whenever the profile, the notes and the saved answers "
            "don't say otherwise; never ask the user about these):\n"
            f"{lines}\n{salary_lines((profile or {}).get('salary_expectation'))}")
