"""Optional AI helpers around an application: the fit check and cover-letter text.

Both are small single requests to the user's own model. The fit check is off
in performance mode; cover-letter boxes are off unless the user turns them on.
"""
from __future__ import annotations

import asyncio
import re

from pydantic import BaseModel, Field

FIT_TIMEOUT_SECONDS = 60


class FitVerdict(BaseModel):
    skip: bool = Field(description="true only when a HARD requirement clearly rules the candidate out")
    title: str = Field(description="Max 8 words, e.g. 'Requires an MBA'. Empty when not skipping.")
    reason: str = Field(description="One or two sentences quoting the posting's requirement. Empty when not skipping.")


async def ask_text(llm, prompt: str, timeout: float, settings: dict | None = None) -> str:
    """One plain-text request. With the LLM settings, backup models take over on capacity errors."""
    from browser_use.llm.messages import UserMessage
    try:
        from core.llm_factory import ainvoke_with_fallback, completion_text
    except ImportError:  # pragma: no cover
        from backend.core.llm_factory import ainvoke_with_fallback, completion_text
    response = await asyncio.wait_for(
        ainvoke_with_fallback(llm, settings or {}, [UserMessage(content=prompt)]), timeout=timeout)
    return completion_text(response)


def _profile_facts(profile: dict) -> str:
    try:
        from core.shared_config import _describe_education
    except ImportError:  # pragma: no cover
        from backend.core.shared_config import _describe_education
    screening = profile.get("screening") or {}
    edu = profile.get("education") or {}
    return "\n".join(line for line in (
        f"Education: {_describe_education(edu)}",
        f"Graduation date: {edu['graduation']}" if (edu.get("graduation") or "").strip() else "",
        f"Work authorization: {profile.get('work_authorization', '')}",
        f"Needs visa sponsorship: {profile.get('visa_sponsorship_needed', '')}",
        f"Nationality: {profile.get('nationality', '')}" if profile.get("nationality") else "",
        f"GPA: {screening.get('gpa', '')}" if screening.get("gpa") else "",
        "Security clearance: none",
    ) if line)


def fit_prompt(title: str, company: str, description: str, profile: dict, location_note: str = "") -> str:
    return (
        "Decide whether this candidate should SKIP applying to this job because of a hard requirement they "
        "clearly don't meet. Be conservative: when in doubt, don't skip.\n\n"
        "Skip ONLY for:\n"
        "- Degree level: the posting is only for MBA, Master's, PhD or JD students/graduates, and the candidate "
        "is in a bachelor's program.\n"
        "- Graduation window: the posting requires graduating in a specific window that excludes the "
        "candidate's graduation date.\n"
        "- Citizenship or security clearance the candidate doesn't have (an active clearance, 'US citizens "
        "only' when the candidate isn't one). Permanent residents count as authorized to work.\n"
        "- Location: only if a SEARCH AREA note is given below and the job is clearly outside it and not remote.\n"
        "NEVER skip for: GPA minimums, years of experience, skills, majors or 'preferred' qualifications.\n\n"
        f"JOB: {title} at {company}\n"
        f"POSTING:\n{description[:6000]}\n\n"
        f"CANDIDATE:\n{_profile_facts(profile)}\n"
        + (f"\nSEARCH AREA: {location_note}\n" if location_note else "")
    )


async def fit_check(llm, title: str, company: str, description: str, profile: dict,
                    location_note: str = "") -> FitVerdict | None:
    """The model's verdict, or None when it can't be had (never blocks an application)."""
    if not description.strip():
        return None
    from browser_use.llm.messages import UserMessage
    try:
        response = await asyncio.wait_for(
            llm.ainvoke([UserMessage(content=fit_prompt(title, company, description, profile, location_note))],
                        output_format=FitVerdict),
            timeout=FIT_TIMEOUT_SECONDS,
        )
    except Exception:
        return None
    verdict = getattr(response, "completion", None)
    return verdict if isinstance(verdict, FitVerdict) else None


# ── Cover letters: plain text, nothing that reads as AI ─────────────────────
_AI_PHRASES = re.compile(
    r"\b(as an ai|language model|i am (thrilled|excited|delighted) to|i'?m (thrilled|excited|delighted) to|"
    r"delve|leverag(e|ing)|synerg\w*|tapestry|testament to|in today'?s (fast-paced|ever-evolving)|"
    r"passionate about leveraging|dynamic team|i am writing to express my (strong |keen )?interest)\b", re.I)


def plain_cover_letter(text: str, name: str = "") -> str:
    """Strip formatting and AI tells: markdown, bullets, em dashes, placeholders, stock phrases.

    Works paragraph by paragraph so the greeting and paragraph breaks survive;
    a sentence with a stock AI phrase is dropped from its paragraph.
    """
    text = (text or "").replace("\r", "")
    text = text.replace("\u2014", ", ").replace("\u2013", "-").replace("\u2026", "...")
    text = text.replace("\u201c", '"').replace("\u201d", '"').replace("\u2018", "'").replace("\u2019", "'")
    text = re.sub(r"\[[^\]]{1,40}\]", "", text)                 # [Company], [Your Name]
    blocks, bullets = [], []
    for raw in text.split("\n"):
        line = re.sub(r"[*_#`>]+", "", raw).strip()             # markdown
        if re.match(r"^([-\u2022\u00b7]|\d+[.)])\s+", line):   # a bullet: becomes a sentence
            bullets.append(re.sub(r"^([-\u2022\u00b7]|\d+[.)])\s+", "", line).rstrip(".") + ".")
            continue
        if bullets:
            blocks.append(" ".join(bullets))
            bullets = []
        if not line:
            blocks.append("")
            continue
        if re.fullmatch(r"(cover letter|subject:.*|re:.*|date:.*)", line, re.I):
            continue
        blocks.append(line)
    if bullets:
        blocks.append(" ".join(bullets))
    paragraphs, current = [], []
    for block in blocks + [""]:
        if block:
            current.append(block)
        elif current:
            # "Sincerely," / "Best regards," keep their own line above the name.
            joined = current[0]
            for prev, line in zip(current, current[1:]):
                joined += ("\n" if prev.endswith(",") and len(prev) < 30 else " ") + line
            paragraphs.append(joined)
            current = []
    cleaned = []
    for para in paragraphs:
        if re.match(r"^(dear|hello|hi)\b", para, re.I) and len(para) < 60:
            cleaned.append(para)
            continue
        sentences = re.split(r"(?<=[.!?])\s+", para)
        kept = [x for x in sentences if not _AI_PHRASES.search(x)]
        para = re.sub(r"\s{2,}", " ", " ".join(kept)).replace(" ,", ",").strip()
        if para:
            cleaned.append(para)
    if name and cleaned and name.lower() not in cleaned[-1].lower():
        cleaned.append(name)
    return "\n\n".join(cleaned)


def cover_letter_prompt(profile: dict, resume: str, title: str, company: str, description: str) -> str:
    return (
        f"Write a short cover letter for {profile.get('name', 'the candidate')} applying to {title} at {company}.\n\n"
        f"POSTING:\n{description[:4000]}\n\n"
        f"RESUME:\n{resume[:5000]}\n\n"
        "RULES:\n"
        "- Plain text only: no markdown, bullets, headings, subject line or date. At most 3 short paragraphs, "
        "then the candidate's name on its own line.\n"
        "- Sound like a student writing it themselves: simple, specific, concrete. Mention one or two real "
        "things from the resume that fit the role.\n"
        "- Don't use em dashes, 'I am thrilled/excited to', 'delve', 'leverage', 'passionate', 'dynamic', "
        "or any placeholder like [Company].\n"
        "- Never invent experience, skills or numbers that aren't in the resume.\n"
    )


async def write_cover_letter(llm, profile: dict, resume: str, title: str, company: str, description: str) -> str:
    text = await ask_text(llm, cover_letter_prompt(profile, resume, title, company, description), timeout=90)
    return plain_cover_letter(text, profile.get("name", ""))


def write_pdf(text: str, path) -> None:
    """The letter as a simple one-page PDF (for upload fields)."""
    import fitz
    doc = fitz.open()
    page = doc.new_page()
    page.insert_textbox(fitz.Rect(72, 72, page.rect.width - 72, page.rect.height - 72), text,
                        fontsize=11, fontname="helv")
    doc.save(str(path))
    doc.close()
