"""What the app checks and asks when the user presses Start, before the first job.

The user presses Start and walks away, so whatever would stop the run is said then, once:

* THE CHECK. The AI sign-in and the room left on its plan, Gmail (verification codes and
  confirmation emails), the CAPTCHA solver's balance, the resume, the job-site password. One
  message lists what is missing, and the run doesn't start (the alternative: jobs failing one
  after another, as many did once when the AI plan's limit ran out).
* THE QUESTIONS. What the queued jobs' sites are known to ask that nothing answers yet: asked
  together, before the run. An answer is saved as the user's own (Q&A page) and used on every
  site in every later run: nothing is asked twice.

Nothing here spends an AI request. The only network request is the CAPTCHA solver's free
balance call.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

from . import captcha, dates, usage_meter

# Things a family of hiring sites asks every applicant and resumes don't carry: tal.net sites ask
# for "Pre-University Education" on every application. The answer is free text; the agent fits it
# to that site's own fields.
SITE_NEEDS: tuple[tuple[str, str, str], ...] = (
    ("tal.net",
     "Your school before university (high school): its name, the qualification or system you followed (for "
     "example \"US High School Diploma\"), your final grade or GPA, and the month and year you finished",
     "pre-university education"),
)
MAX_QUESTIONS = 12


def _host(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _family(host: str) -> str:
    """The part of a host that hiring sites of one system share: 'acme.tal.net' -> 'tal.net'."""
    parts = [p for p in (host or "").split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def _same_site(a: str, b: str) -> bool:
    return bool(a and b) and (a == b or a.endswith("." + b) or b.endswith("." + a))


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", "", (text or "").lower())).strip()


def _same_question(a: str, b: str, store) -> bool:
    if _norm(a) == _norm(b):
        return True
    try:
        return bool(store is not None and store.same_meaning(_norm(a), _norm(b), 0.85))
    except Exception:
        return False


def group_questions(jobs: dict, store=None, statuses=("needs_you",)) -> list[dict]:
    """The questions jobs are waiting on, each once, with the jobs that wait on it.

    [{"question", "section", "options", "field_type", "jobs": [{"url", "title", "company"}]}],
    the most-awaited first. A job that may already have been sent isn't listed: an answer never
    sends it again.
    """
    groups: list[dict] = []
    for url, job in (jobs or {}).items():
        if job.get("status") not in statuses or job.get("submit_attempted_at"):
            continue
        for q in job.get("pending_questions") or []:
            if not isinstance(q, dict) or not str(q.get("question") or "").strip():
                continue
            text = str(q["question"]).strip()
            group = next((g for g in groups if _same_question(g["question"], text, store)), None)
            if group is None:
                group = {"question": text, "section": str(q.get("section") or ""),
                         "options": [str(o) for o in q.get("options") or [] if str(o).strip()],
                         "field_type": str(q.get("field_type") or "text"), "jobs": []}
                groups.append(group)
            if not any(j["url"] == url for j in group["jobs"]):
                group["jobs"].append({"url": url, "title": job.get("title") or "", "company": job.get("company") or ""})
    groups.sort(key=lambda g: len(g["jobs"]), reverse=True)
    return groups


def answer_everywhere(jobs: dict, question: str, store=None) -> dict[str, list[dict] | None]:
    """{url: the job's pending questions without `question`} for every waiting job that asks it
    (None when nothing is left: the job is ready again). Jobs that may have been sent are left out."""
    changed: dict[str, list[dict] | None] = {}
    for url, job in (jobs or {}).items():
        pending = [q for q in job.get("pending_questions") or [] if isinstance(q, dict)]
        if job.get("status") != "needs_you" or job.get("submit_attempted_at") or not pending:
            continue
        left = [q for q in pending if not _same_question(str(q.get("question") or ""), question, store)]
        if len(left) != len(pending):
            changed[url] = left or None
    return changed


def start_questions(queued: list[dict], jobs: dict, store=None, profile: dict | None = None) -> list[dict]:
    """What the queued jobs' sites are known to ask that nothing answers yet.

    From: questions other jobs on the same sites are waiting on; questions asked on those sites
    in earlier runs that never got an answer (the Q&A page's unanswered ones); and SITE_NEEDS.
    A question the app answers itself (a date that follows from the profile) or the user already
    answered is left out. Each: {"question", "section", "options", "field_type", "jobs": n,
    "why"}.
    """
    hosts = [_host(job.get("url") or "") for job in queued]
    hosts = [h for h in hosts if h]
    if not hosts:
        return []

    def waiting_on(site: str) -> int:
        return sum(1 for h in hosts if _same_site(h, site) or _family(h) == _family(site))

    def answered(text: str, field_type: str = "text") -> bool:
        if dates.own_answer(text, field_type, profile) is not None:
            return True
        try:
            return bool(store is not None and store.qa_user_answer(text))
        except Exception:
            return False

    found: list[dict] = []

    def add(text: str, why: str, count: int, section: str = "", options=None, field_type: str = "text") -> None:
        if count <= 0 or answered(text, field_type) or any(_same_question(f["question"], text, store) for f in found):
            return
        found.append({"question": text, "section": section, "options": list(options or []),
                      "field_type": field_type or "text", "jobs": count, "why": why})

    for suffix, text, about in SITE_NEEDS:
        count = sum(1 for h in hosts if h == suffix or h.endswith("." + suffix))
        add(text, f"{suffix} sites ask every applicant about {about}", count)
    queued_urls = {job.get("url") for job in queued}
    for group in group_questions({u: j for u, j in (jobs or {}).items() if u not in queued_urls}, store):
        sites = {_host(j["url"]) for j in group["jobs"]}
        count = max((waiting_on(site) for site in sites if site), default=0)
        add(group["question"], "another job on the same site is waiting for this answer", count,
            group["section"], group["options"], group["field_type"])
    try:
        unanswered = store.qa_list(unanswered_only=True) if store is not None else []
    except Exception:
        unanswered = []
    for row in unanswered:
        if (row.get("answer") or "").strip():
            continue                    # a guess to check, not a question without an answer
        site = (row.get("source_domain") or "").lower().removeprefix("www.")
        if site:
            add(str(row.get("question") or ""), "this site asked it in an earlier run", waiting_on(site),
                options=[o for o in (row.get("options") or "").splitlines() if o.strip()],
                field_type=row.get("question_type") or "text")
    found.sort(key=lambda f: f["jobs"], reverse=True)
    return found[:MAX_QUESTIONS]


def start_problems(*, llm_ready: bool, provider: str, settings: dict, profile: dict, resume_ok: bool,
                   gmail_signed_in: bool, balance=None) -> list[dict]:
    """What is missing for a run, each {"id", "message", "required"}.

    `required`: a run can't work at all without it (the AI model, room on its plan, the resume,
    the profile's name). The others (Gmail, the CAPTCHA solver, the job-site password) leave jobs
    waiting for the user instead; Start says so and doesn't begin, and the user may start anyway.
    """
    problems: list[dict] = []

    def add(ident: str, message: str, required: bool) -> None:
        problems.append({"id": ident, "message": message, "required": required})

    if not llm_ready:
        add("ai", "No AI model is set up (Settings → AI model)." if provider != "chatgpt"
            else "ChatGPT isn't signed in (Settings → AI model).", True)
    else:
        stop = usage_meter.stop_reason()
        if stop is not None and not usage_meter.resets_soon(stop):      # one that resets within minutes is waited for
            add("ai_limit", stop["message"][0].upper() + stop["message"][1:] + ".", True)
    if not resume_ok:
        add("resume", "No resume is chosen (Settings → Applications).", True)
    if not (profile.get("name") or "").strip():
        add("name", "Your name is missing (Profile).", True)
    if not gmail_signed_in:
        add("gmail", "Gmail isn't connected (Settings → Accounts & email): verification codes, verification links "
            "and confirmation emails can't be read.", False)
    if not str((settings.get("sensitive_data") or {}).get("password") or "").strip():
        add("password", "No password for job sites is saved (Settings → Accounts & email): sites that need an "
            "account can't be applied to.", False)
    key = str(settings.get("captcha_api_key") or "").strip()
    if not key:
        add("captcha", "No 2Captcha key is saved (Settings → Applications): a CAPTCHA will wait for you to solve "
            "it in the browser.", False)
    elif captcha.solver_enabled(settings):
        try:
            left = (balance or captcha.balance)(key)
        except captcha.SolverRejected as refused:
            add("captcha", "2Captcha: " + captcha._SOLVER_ERRORS.get(refused.code, f"it refused the request "
                f"({refused.code})") + ".", False)
        except Exception:
            left = None
        else:
            if left is not None and left <= 0:
                add("captcha", "Your 2Captcha balance is empty (add funds at 2captcha.com): a CAPTCHA will wait "
                    "for you to solve it in the browser.", False)
    return problems
