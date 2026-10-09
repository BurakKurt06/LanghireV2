"""Fit-check prompt and the plain cover-letter cleaner."""
from core import ai_extras


def test_fit_prompt_is_conservative_and_never_skips_for_gpa():
    text = ai_extras.fit_prompt("MBA Associate", "Cyberdyne", "First-year MBA students only.", {
        "education": {"degree": "B.S.", "major": "Statistics", "graduation": "May 2028"},
        "work_authorization": "US Citizen"}, "within 50 miles of ZIP 10001")
    assert "NEVER skip for: GPA minimums" in text and "when in doubt, don't skip" in text
    assert "Education: B.S. in Statistics from  (May 2028)" in text and "Graduation date: May 2028" in text and "SEARCH AREA: within 50 miles" in text


def test_cover_letter_cleaner_removes_formatting_and_ai_tells():
    raw = ("**Cover Letter**\n\nDear Hiring Manager,\n\nI am thrilled to apply for the [Position] role. "
           "It fits me well \u2014 really. At the campus library I rebuilt the website.\n\n"
           "- I leverage synergy daily\n- I built an admin panel\n\nSincerely,\nAda Lovelace")
    out = ai_extras.plain_cover_letter(raw, "Ada Lovelace")
    assert out.startswith("Dear Hiring Manager,\n\nIt fits me well, really.")
    for tell in ("**", "Cover Letter", "thrilled", "[Position]", "\u2014", "leverage", "- I"):
        assert tell not in out
    assert "I built an admin panel." in out and out.endswith("Sincerely,\nAda Lovelace")


def test_cover_letter_gets_the_name_when_missing():
    assert ai_extras.plain_cover_letter("Dear Hiring Manager,\n\nI like data.", "Ada Lovelace").endswith("Ada Lovelace")
