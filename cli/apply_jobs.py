
"""
Script 2: Apply to collected jobs with multiple concurrent workers.
One agent per job. Tracks status in jobs.json.

Usage:
  uv run python apply_jobs.py                          # 1 worker, easy apply
  uv run python apply_jobs.py --workers 3              # 3 concurrent workers
  uv run python apply_jobs.py --workers 2 --no-easy-apply  # non-easy apply
  uv run python apply_jobs.py --limit 10               # apply to max 10 jobs
"""
import argparse
import asyncio
import hashlib
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession, Tools
# The real class, for action signatures (tests swap BrowserSession for a fake).
from browser_use import BrowserSession as _BrowserSessionType
from browser_use.agent.views import ActionResult
from browser_use.filesystem.file_system import FileSystem
from browser_use.tools.views import DoneAction
from pydantic import BaseModel, Field

try:
    import core.shared_config as config
    from core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        JOBS_FILE, QA_FILE, CANDIDATE_PROFILE, LOGS_DIR, RESUME_PATH, SENSITIVE_DATA, BLOCKED_DOMAINS,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from memory import ATS_DOMAINS, JOB_BOARDS, extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from memory.metrics import MetricsStore
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
    import core.captcha as _captcha
    from core import ai_extras, click_patch, form_check, form_snapshot, geo, job_page
except ImportError:
    import backend.core.shared_config as config
    from backend.core.shared_config import (
        BASE_DIR, BROWSER_PROFILE_DIR,
        JOBS_FILE, QA_FILE, CANDIDATE_PROFILE, LOGS_DIR, RESUME_PATH, SENSITIVE_DATA, BLOCKED_DOMAINS,
        AWS_PROFILE, AWS_REGION, MODEL_ID,
        load_json, save_json, refresh_credentials, credential_refresh_loop,
        build_memory_context, extract_from_history, normalize_question,
        read_jobs, claim_job, update_job, get_memory_store,
    )
    from backend.memory import ATS_DOMAINS, JOB_BOARDS, extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from backend.memory.metrics import MetricsStore
    from backend.core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
    import backend.core.captcha as _captcha
    from backend.core import ai_extras, click_patch, form_check, form_snapshot, geo, job_page

# Workday's checkboxes, radio buttons and buttons need real mouse clicks.
click_patch.apply()

_log = logging.getLogger("langhire.apply")

# Jobs already retried once after an unexpected crash (url -> count).
_crash_retries: dict[str, int] = {}

# Lock for thread-safe QA file writes
_qa_lock = asyncio.Lock()

# Runs at least this long are worth an LLM call to summarise what was learned.
LEARN_FROM_RUNS_OVER_STEPS = 15


MIN_SECRET_LENGTH = 6
NO_VALID_EMAIL_ERROR = (
    "No valid email address to apply with: the Profile email isn't a valid address and "
    "no account email is set in Settings. Fix Profile → Email, then retry this job."
)


def _looks_like_email(value: str) -> bool:
    return bool(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value or ""))


def build_agent_secrets(profile: dict, sensitive: dict) -> dict:
    """Secrets the agent may type, keyed by the placeholder names in the task.

    email          address for application form fields: the Profile email, or the
                   Settings account email when the Profile one isn't a valid address
    account_email  login / account-creation email for employer sites
    password       login / account-creation password for employer sites

    browser-use masks every occurrence of each value in everything sent to the
    LLM. A short value (e.g. a 4-letter typo in the email field) would therefore
    turn ordinary page text into <secret> placeholders, and would be typed into
    forms as the "email". Such values, and empty ones, are left out.
    """
    profile_email = (profile.get("email") or "").strip()
    account_email = (sensitive.get("email") or "").strip()
    form_email = profile_email if _looks_like_email(profile_email) else (
        account_email if _looks_like_email(account_email) else "")
    secrets = {
        "email": form_email,
        "account_email": account_email if _looks_like_email(account_email) else form_email,
        "password": sensitive.get("password") or "",
    }
    return {k: v for k, v in secrets.items() if len(v.strip()) >= MIN_SECRET_LENGTH}


def same_company_applications(applied_labels: list[str], company: str) -> list[str]:
    """The earlier applications ("<title> at <company>") worth showing for this job.

    The list goes into every LLM step's prompt. Each run targets one posting,
    so only earlier applications at the same company can matter (the agent
    may meet an "already applied" page there); the full history just grew the
    prompt with every application.
    """
    name = (company or "").strip().lower()
    if not name or name == "unknown":
        return []
    return [label for label in applied_labels or [] if label.lower().endswith(f" at {name}")]


def _max_failures() -> int:
    """Settings -> "Max Failures Per Job" (1-50, default 8)."""
    try:
        return max(1, min(int(config.load_runtime_settings().get("max_failures", 8)), 50))
    except (TypeError, ValueError):
        return 8


def current_settings() -> tuple[str, dict, list[str]]:
    """(resume path, account email/password, blocked domains) as saved right now.

    RESUME_PATH, SENSITIVE_DATA and BLOCKED_DOMAINS are read once, when this
    module is first imported, so a new resume or password saved in Settings
    was ignored until the app restarted. Read settings.json for every job
    instead, falling back to those constants only when a key is missing.
    """
    settings = config.load_runtime_settings()
    resume = settings.get("resume_path", RESUME_PATH) or ""
    sensitive = settings.get("sensitive_data") or SENSITIVE_DATA or {}
    blocked = settings.get("blocked_domains", BLOCKED_DOMAINS) or []
    return resume, sensitive, blocked


def failure_reason(result) -> str:
    """Why a finished run failed: the agent's own done text, else the last step error.

    AgentHistoryList.errors() has a None for every step without an error, so
    errors()[-1] was usually None, and slicing it raised "'NoneType' object is
    not subscriptable", which replaced the agent's explanation with a generic
    "unexpected page state" message.
    """
    if result.is_done() and result.final_result():
        return result.final_result()
    errors = [e for e in result.errors() if e]
    return errors[-1] if errors else "Agent reported failure"


# ── What is running, for the sidebar run controls ───────────────────────────
# worker id -> {"url", "title", "company", "step", "max_steps", "started_at"}
_current: dict[int, dict] = {}
# Jobs the user chose to skip mid-run (they stay pending, with a note).
_skip_requested: set[str] = set()
SKIPPED_BY_YOU_NOTE = "Skipped by you during the run. It stays in the list; apply again any time."

# Sites whose CAPTCHA couldn't be passed in this run (host -> first job's title).
# Their other jobs are marked Needs you without opening them: a Pied Piper run
# spent 38 jobs' worth of AI steps and 2Captcha waits on the same unsolvable
# hCaptcha.
_captcha_blocked: dict[str, str] = {}


# worker id -> site of the job it's on. Two workers never use the same site at
# once: simultaneous account creation on one Workday tenant clashes, and a burst
# of applications to one site is what brings out its CAPTCHA.
_busy_sites: dict[int, str] = {}


COVER_LETTER_DAYS = 30


def _remove_old_cover_letters(days: int = COVER_LETTER_DAYS) -> None:
    """Cover-letter PDFs are only needed while their application runs; keep a month of them."""
    import time
    cutoff = time.time() - days * 86400
    for pdf in (LOGS_DIR.parent / "cover_letters").glob("*.pdf"):
        try:
            if pdf.stat().st_mtime < cutoff:
                pdf.unlink()
        except OSError:
            pass


def new_run() -> None:
    """Forget the previous run's per-site state (called when a run starts)."""
    _remove_old_cover_letters()
    _captcha_blocked.clear()
    _busy_sites.clear()
    _skip_requested.clear()


def take_job(queue: asyncio.Queue, worker_id: int):
    """The first queued job on a site no other worker is using, in queue order.

    None when the queue is empty; False when every queued job is on a site
    another worker is busy with (try again shortly).
    """
    waiting = []
    while True:
        try:
            waiting.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    busy = {site for w, site in _busy_sites.items() if w != worker_id}
    pick = next((j for j in waiting if _site(j["url"]) not in busy), None)
    for j in waiting:
        if j is not pick:
            queue.put_nowait(j)
    if pick is None:
        return False if waiting else None
    _busy_sites[worker_id] = _site(pick["url"])
    return pick


def _site(url: str) -> str:
    """One employer's application site: the host, plus the company id on shared hosts (Greenhouse, Lever, ...)."""
    from urllib.parse import urlparse
    host = (urlparse(url).hostname or "").removeprefix("www.")
    company = job_page.tenant(url)
    return f"{host}/{company}" if company and company not in host else host


def captcha_note(reason: str) -> str:
    """Needs-you text for a CAPTCHA the solver couldn't pass (title line, then the paragraph)."""
    return ("CAPTCHA the solver couldn't pass\n"
            f"{reason} Open the link and apply yourself, or apply again later: these checks "
            "don't always appear.")


_CAPTCHA_BLOCKED = re.compile(r"block|stuck|stopp|prevent|unable|can'?t|cannot|could ?not|couldn'?t|unsolved|"
                              r"not (been )?solved|remain|still|again", re.I)
_CAPTCHA_PASSED = re.compile(r"\b(was|were|got|is|been|automatically) (solved|passed|cleared)\b|\bno captcha\b|"
                             r"without (a |any )?captcha", re.I)
_NEGATED = re.compile(r"\b(not|n't|never)\b[^.]{0,20}\b(solved|passed|cleared)\b", re.I)


def blocked_by_captcha(text: str) -> bool:
    """The agent's failure report says a CAPTCHA stopped it (not merely that one appeared, or was solved)."""
    for sentence in re.split(r"(?<=[.!?])\s+|\n", text or ""):
        if "captcha" not in sentence.lower() or not _CAPTCHA_BLOCKED.search(sentence):
            continue
        if _CAPTCHA_PASSED.search(sentence) and not _NEGATED.search(sentence):
            continue
        return True
    return False


def _block_site(url: str, title: str) -> None:
    _captcha_blocked.setdefault(_site(url), title)


def current_jobs() -> list[dict]:
    return [dict(job, worker=w) for w, job in sorted(_current.items())]


def skip_job(url: str) -> bool:
    """Stop the application to `url` and move on to the next job (called from the app)."""
    agent = next((a for a in list(_running_agents) if getattr(a, "_langhire_url", None) == url), None)
    if agent is None:
        return False
    _skip_requested.add(url)
    # A run waiting for the user's help or review ends at once.
    answer_help(url, "", True)
    decide_review(url, False)
    loop = getattr(agent, "_langhire_loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(agent.stop)
    else:
        agent.stop()
    return True


# ── Pause / resume from the app ─────────────────────────────────────────────
# Lets the user take over the browser to fix something: a paused agent stops
# after its current action, drops the rest of that step's plan, and reads the
# page afresh when resumed, so it sees (and keeps) the user's changes.
_running_agents: set = set()
_pause_state = {"paused": False}


def _set_agent_paused(agent, paused: bool) -> None:
    # The agent's pause event belongs to the worker's event loop, and the app
    # calls this from a request thread.
    action = agent.pause if paused else agent.resume
    loop = getattr(agent, "_langhire_loop", None)
    if loop is not None and loop.is_running():
        loop.call_soon_threadsafe(action)
    else:
        action()


def set_paused(paused: bool) -> None:
    """Pause or resume every running application agent; new ones follow suit.

    While paused the user may fix fields by hand. The page's form values are
    read when pausing and again before resuming; the fields that changed are
    pinned for the agent (it must not change them back) and saved as the
    user's answers for later applications.
    """
    _pause_state["paused"] = paused
    for agent in list(_running_agents):
        loop = getattr(agent, "_langhire_loop", None)
        if paused:
            _set_agent_paused(agent, True)
            if loop is not None and loop.is_running():
                asyncio.run_coroutine_threadsafe(_snapshot_before_edits(agent), loop)
        elif loop is not None and loop.is_running():
            asyncio.run_coroutine_threadsafe(_resume_keeping_user_edits(agent), loop)
        else:
            _set_agent_paused(agent, False)


# Let the agent's last action finish before reading the page's "before" state.
PAUSE_SETTLE_SECONDS = 2


async def _snapshot_before_edits(agent) -> None:
    await asyncio.sleep(PAUSE_SETTLE_SECONDS)
    agent._langhire_before_edits = await form_snapshot.snapshot(agent.browser_session)


async def _resume_keeping_user_edits(agent) -> None:
    try:
        before = getattr(agent, "_langhire_before_edits", None)
        if before is not None:
            after = await form_snapshot.snapshot(agent.browser_session)
            keep_user_edits(agent, form_snapshot.changed_fields(before, after))
    finally:
        agent._langhire_before_edits = None
        agent.resume()


# A label that looks like an element id ("input-12", "workExperience_3") isn't
# a question worth saving as an answer.
def _looks_like_question(label: str) -> bool:
    return bool(re.search(r"[A-Za-z]{3,}", label)) and (" " in label.strip() or label.strip().endswith("?"))


# Longer text is a job-specific essay, not an answer to reuse elsewhere.
USER_ANSWER_MAX_LENGTH = 150


def keep_user_edits(agent, changes: list[tuple[str, str, str]]) -> None:
    """Pin fields the user set by hand for the rest of this run, and save them as answers."""
    if not changes:
        return
    pinned = getattr(agent, "_langhire_user_fields", {})
    for label, _old, new in changes:
        pinned[label] = new
    agent._langhire_user_fields = pinned
    _pin_in_system_message(agent)

    domain = getattr(agent, "_langhire_domain", "")
    store = get_memory_store()
    if store is None:
        return
    for label, _old, new in changes:
        if _looks_like_question(label) and len(new) <= USER_ANSWER_MAX_LENGTH:
            try:
                store.qa_set_answer(label, new, source="user", source_domain=domain)
            except Exception as e:
                print(f"    ⚠️  Couldn't save your answer for '{label}': {e}")


def keep_user_message(agent, message: str) -> None:
    """Pin a message the user typed for the agent, for the rest of this run."""
    message = (message or "").strip()
    if not message:
        return
    agent._langhire_user_messages = getattr(agent, "_langhire_user_messages", []) + [message]
    _pin_in_system_message(agent)


def _pin_in_system_message(agent) -> None:
    """Add the user's values and messages to the system message, which persists across steps.

    browser-use rebuilds the per-step messages every step, so a note there would
    be gone by the next one.
    """
    try:
        manager = agent._message_manager
        current = manager.state.history.system_message
        base = getattr(agent, "_langhire_base_system", None)
        if base is None:
            base = current.text
            agent._langhire_base_system = base
        note = ""
        pinned = getattr(agent, "_langhire_user_fields", {})
        if pinned:
            lines = "\n".join(f'- "{label}" = "{value}"' for label, value in pinned.items())
            note += ("\n\nFIELDS THE USER SET BY HAND (keep exactly these values; never change them back, "
                     "even if you would have answered differently):\n" + lines)
        messages = getattr(agent, "_langhire_user_messages", [])
        if messages:
            note += ("\n\nMESSAGES FROM THE USER DURING THIS RUN (follow them):\n"
                     + "\n".join(f"- {m}" for m in messages))
        manager._set_message_with_type(current.model_copy(update={"content": base + note}), "system")
    except Exception as e:
        print(f"    ⚠️  Couldn't pass your edits to the agent: {e}")


# ── Review before submitting, and questions the agent can't answer ─────────
# Chosen when starting a run in the app: "auto" (Full auto) submits on its
# own, "trial" waits for the user's approval before every submit, "training"
# fills everything in but never submits (practice: answers and site tips are
# still saved).
RUN_MODES = ("auto", "trial", "training")
_run_options = {"mode": "auto"}
REVIEW_WAIT_SECONDS = 25 * 60
# A step waiting for the user's decision must not hit the step timeout.
REVIEW_STEP_TIMEOUT = REVIEW_WAIT_SECONDS + 5 * 60

_reviews: dict[str, dict] = {}  # job url -> {title, company, summary, loop, future}
# Trial runs never give up on their own: a stuck agent waits for the user.
_help_requests: dict[str, dict] = {}  # job url -> {title, company, problem, loop, future}


def set_run_mode(mode: str) -> None:
    """The mode of the next run: "auto", "trial" or "training"."""
    _run_options["mode"] = mode if mode in RUN_MODES else "auto"


def set_trial_run(review: bool) -> None:
    set_run_mode("trial" if review else "auto")


def is_trial_run() -> bool:
    return _run_options["mode"] == "trial"


def is_training_run() -> bool:
    return _run_options["mode"] == "training"


NAME_JOB_RULE = (
    "JOB NAME: when you first see the posting, call set_job_details with the role and the employer's "
    "everyday name as the page shows it (e.g. 'Contoso', not 'Contoso Brokerage Services LLC'), in the "
    "same step as your other actions.\n\n"
)
COVER_LETTER_RULE = (
    "COVER LETTERS: when a form has a cover letter text box, or asks to upload a cover letter, call "
    "write_cover_letter once and use its text (paste it as is) or its PDF path. Never write one yourself.\n\n"
)

TRAINING_RULES = (
    "TRAINING RUN (practice): fill in every page exactly as for a real application, but NEVER click "
    "the final Submit / Apply button. When only that button is left, call done with success=true and a "
    "short summary of what you filled in, without clicking it. Reaching that point is a success even if "
    "you noted small doubts: list them in the summary.\n\n"
)


def pending_reviews() -> list[dict]:
    return [{"url": url, "title": r["title"], "company": r["company"], "summary": r["summary"]}
            for url, r in list(_reviews.items())]


def pending_help() -> list[dict]:
    return [{"url": url, "title": r["title"], "company": r["company"], "problem": r["problem"]}
            for url, r in list(_help_requests.items())]


def answer_help(url: str, message: str, give_up: bool) -> bool:
    """The user's reply to an agent waiting for help (called from the app)."""
    request = _help_requests.get(url)
    if request is None:
        return False
    future = request["future"]
    reply = {"message": message or "", "give_up": bool(give_up)}
    request["loop"].call_soon_threadsafe(lambda: future.done() or future.set_result(reply))
    return True


def decide_review(url: str, approve: bool) -> bool:
    """The user's decision on an application waiting for review (called from the app)."""
    review = _reviews.get(url)
    if review is None:
        return False
    future = review["future"]
    review["loop"].call_soon_threadsafe(lambda: future.done() or future.set_result(approve))
    return True


class JobDetails(BaseModel):
    role: str = Field(description="The job title as the posting shows it")
    company: str = Field(description="The employer's everyday name, e.g. 'Contoso' (not the legal entity)")


class GuessedAnswer(BaseModel):
    question: str = Field(description="The question exactly as the form shows it")
    answer: str = Field(description="The answer you gave")
    options: list[str] = Field(default_factory=list, description="The choices, for a dropdown, radio or checkbox question")
    field_type: str = Field(default="text", description="text, dropdown, radio, checkbox or date")


class GuessedAnswers(BaseModel):
    guesses: list[GuessedAnswer]


class HelpRequest(BaseModel):
    problem: str = Field(description="What you're stuck on and what you tried, in one or two sentences")


class SubmitReview(BaseModel):
    summary: str = Field(description="One short paragraph: what you filled in and anything you were unsure of")


def guard_next_clicks(tools: Tools, agent_ref: dict, worker_id: int) -> None:
    """Don't let Next / Continue / Submit be clicked while required questions are unanswered.

    The agent is told which ones are missing instead. The same set of missing
    questions only blocks once, so a field misread as required can never stop a run.
    """
    click = tools._click_by_index

    async def guarded(params, browser_session):
        try:
            node = await browser_session.get_element_by_index(params.index) if params.index is not None else None
            label = ""
            if node is not None:
                label = (node.get_all_children_text() or node.attributes.get("aria-label")
                         or node.attributes.get("value") or "").strip()
            if form_check.is_advance_button(label):
                data = await form_check.scan(browser_session)
                missing = form_check.missing_required(data)
                key = tuple(f["label"] for f in missing)
                if missing and len(data.get("fields", [])) >= form_check.MIN_FIELDS and agent_ref.get("blocked_next") != key:
                    agent_ref["blocked_next"] = key
                    names = "; ".join(form_check._describe(f) for f in missing)
                    print(f"  ✋ [W{worker_id}] Held back '{label[:30]}': {len(missing)} required question(s) unanswered")
                    return ActionResult(error=(
                        f"Not clicked '{label[:40]}': these required questions on this page are still unanswered "
                        f"(top to bottom): {names}. Answer them, then click it again."))
        except Exception:
            pass
        return await click(params, browser_session)

    tools._click_by_index = guarded


def add_form_check_each_step(agent, profile: dict | None = None) -> None:
    """Put the page's FORM CHECK note into every step's prompt (after the page; cheap, cache-friendly)."""
    from browser_use.llm.messages import UserMessage
    original = getattr(agent, "_inject_budget_warning", None)
    if original is None:
        return

    async def with_form_check(step_info=None):
        await original(step_info)
        try:
            note = form_check.checklist_text(await form_check.scan(agent.browser_session), profile)
            if note:
                agent._message_manager._add_context_message(UserMessage(content=note))
        except Exception:
            pass

    agent._inject_budget_warning = with_form_check


def build_tools(job: dict, domain: str, review: bool, agent_ref: dict, worker_id: int) -> Tools:
    """The agent's actions for one application: the defaults plus ours."""
    tools = Tools()
    url, title, company = job["url"], job.get("title", ""), job.get("company", "")
    guard_next_clicks(tools, agent_ref, worker_id)

    saved = read_jobs().get(url) or {}
    # Only when the page didn't already give a good name (saves the tokens otherwise).
    unnamed = job_page.is_placeholder(job.get("title")) or not (job.get("company") or "").strip()
    if unnamed and not performance_mode() and saved.get("name_source") != "user":
        @tools.action(
            "Record the job's role and the employer's everyday name, as the posting shows them. Call it once.",
            param_model=JobDetails,
        )
        async def set_job_details(params: JobDetails):
            changed = await asyncio.to_thread(config.set_job_names, url, params.role, params.company, "agent")
            if changed:
                print(f"  🏷️  [W{worker_id}] Named: {params.role} at {params.company}")
            typed = saved.get("user_named") or []
            _current.get(worker_id, {}).update(
                title=title if "title" in typed else (params.role or title),
                company=company if "company" in typed else (params.company or company))
            return ActionResult(extracted_content="Noted.")

    if cover_letter_boxes_on():
        @tools.action(
            "Write a cover letter for this job (plain text, the candidate's own voice). Returns the text to "
            "paste into a cover letter box and the path of a PDF version for upload fields.",
        )
        async def write_cover_letter():
            if agent_ref.get("cover_letter"):
                return ActionResult(extracted_content=agent_ref["cover_letter"])
            try:
                text = await ai_extras.write_cover_letter(
                    config.get_llm(), agent_ref.get("profile") or {}, agent_ref.get("resume_text") or "",
                    title, company, agent_ref.get("posting_description") or "")
                ai_extras.write_pdf(text, agent_ref["cover_letter_pdf"])
            except Exception as e:
                return ActionResult(error=f"Couldn't write a cover letter: {e}")
            print(f"  ✉️  [W{worker_id}] Wrote a cover letter for {title} at {company}")
            agent_ref["cover_letter"] = (f"COVER LETTER TEXT (paste exactly):\n{text}\n\n"
                                         f"PDF for upload fields: {agent_ref['cover_letter_pdf']}")
            return ActionResult(extracted_content=agent_ref["cover_letter"])

    @tools.action(
        "Log the answers you had to guess: questions the profile, resume, saved answers and the "
        "user's instructions don't answer. Call it in the same step as filling them in; the "
        "application continues. The user checks the guesses later on the Q&A page.",
        param_model=GuessedAnswers,
    )
    async def record_guesses(params: GuessedAnswers):
        store = get_memory_store()
        guesses = [g for g in params.guesses if g.question.strip() and g.answer.strip()]
        for g in guesses:
            if store is not None:
                store.qa_set_answer(g.question.strip(), g.answer.strip(), source="guess",
                                    question_type=g.field_type, source_domain=domain, options=g.options)
        if guesses:
            print(f"  🤔 [W{worker_id}] Guessed {len(guesses)} answer(s) at {company}: "
                  + " | ".join(f"{g.question.strip()} → {g.answer.strip()}" for g in guesses))
        return ActionResult(extracted_content=f"Logged {len(guesses)} guessed answer(s) for the user to check.")

    if review:
        async def wait_for_help(problem: str, browser_session) -> ActionResult:
            """Keep the browser open and wait for the user; then continue, or end if they give up."""
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            before = await form_snapshot.snapshot(browser_session)
            _help_requests[url] = {"title": title, "company": company, "problem": problem,
                                   "loop": loop, "future": future}
            print(f"  ❗ [W{worker_id}] Needs your help: {title} at {company}: {problem}")
            try:
                reply = await asyncio.wait_for(future, REVIEW_WAIT_SECONDS)
            except asyncio.TimeoutError:
                reply = None
            finally:
                _help_requests.pop(url, None)
            changes = form_snapshot.changed_fields(before, await form_snapshot.snapshot(browser_session))
            agent = agent_ref.get("agent")
            if agent is not None:
                keep_user_edits(agent, changes)
            if reply is None or reply["give_up"]:
                why = "no reply within 25 minutes" if reply is None else "you chose Give up"
                message = f"{problem} ({why})"
                return ActionResult(is_done=True, success=False, extracted_content=message, long_term_memory=message)
            said = reply["message"].strip()
            if said:
                if agent is not None:
                    keep_user_message(agent, said)
                save_job_note(url, said)
            parts = ["The user has helped."]
            if said:
                parts.append(f'Their message: "{said}".')
            if changes:
                parts.append("They changed these fields; keep them:\n" + form_snapshot.describe_changes(changes))
            parts.append("Look at the page again and continue the application from where it is now.")
            return ActionResult(extracted_content="\n".join(parts))

        @tools.action(
            "Ask the user for help when you are stuck (an element won't respond, a page won't advance, "
            "something is unclear). The browser stays open; the user fixes it or tells you what to do.",
            param_model=HelpRequest,
        )
        async def ask_user_for_help(params: HelpRequest, browser_session: _BrowserSessionType):
            return await wait_for_help(params.problem, browser_session)

        # In a trial run, finishing with success=false asks for help instead.
        finish = tools.registry.registry.actions["done"].function

        @tools.action(
            "Complete task. Only report actions you performed and data you extracted in this session. "
            "In this trial run, success=false does not end the application: the user is asked to help.",
            param_model=DoneAction,
        )
        async def done(params: DoneAction, file_system: FileSystem, browser_session: _BrowserSessionType):
            if params.success:
                return await finish(params=params, file_system=file_system)
            return await wait_for_help(params.text, browser_session)

        @tools.action(
            "Call this INSTEAD of clicking the final Submit/Apply button, once every page is done. "
            "It waits for the user to check the page and approve.",
            param_model=SubmitReview,
        )
        async def request_submit_review(params: SubmitReview, browser_session: _BrowserSessionType):
            if agent_ref.get("approved"):
                return ActionResult(extracted_content="Already approved: click the final Submit button now.")
            loop = asyncio.get_running_loop()
            future = loop.create_future()
            before = await form_snapshot.snapshot(browser_session)
            _reviews[url] = {"title": title, "company": company, "summary": params.summary,
                             "loop": loop, "future": future}
            print(f"  👀 [W{worker_id}] Ready to submit, waiting for your review: {title} at {company}")
            try:
                approve = await asyncio.wait_for(future, REVIEW_WAIT_SECONDS)
            except asyncio.TimeoutError:
                approve = None
            finally:
                _reviews.pop(url, None)
            changes = form_snapshot.changed_fields(before, await form_snapshot.snapshot(browser_session))
            if agent_ref.get("agent") is not None:
                keep_user_edits(agent_ref["agent"], changes)
            if approve is None:
                message = "Not submitted: no review decision within 25 minutes."
                return ActionResult(is_done=True, success=False, extracted_content=message, long_term_memory=message)
            if not approve:
                message = "Not submitted: you chose Skip when reviewing it."
                return ActionResult(is_done=True, success=False, extracted_content=message, long_term_memory=message)
            agent_ref["approved"] = True
            note = ("\nThe user changed these fields while reviewing; keep them:\n"
                    + form_snapshot.describe_changes(changes)) if changes else ""
            return ActionResult(extracted_content=(
                "The user reviewed the application and approved it." + note
                + "\nNow click the final Submit button, check that it went through, then call done."))
    else:
        finish = tools.registry.registry.actions["done"].function

        @tools.action(
            "Complete task. Only report actions you performed and data you extracted in this session.",
            param_model=DoneAction,
        )
        async def done(params: DoneAction, file_system: FileSystem):
            agent = agent_ref.get("agent")
            used = agent.state.n_steps if agent is not None else EARLY_GIVE_UP_STEPS
            if (not params.success and used < EARLY_GIVE_UP_STEPS and not agent_ref.get("pushed_back")
                    and not is_hard_stop(params.text)):
                agent_ref["pushed_back"] = True
                print(f"  💪 [W{worker_id}] Agent wanted to give up at step {used}: sent it back to keep trying")
                return ActionResult(extracted_content=GIVE_UP_PUSHBACK.format(used=used, limit=agent_ref.get("max_steps", 70)))
            return await finish(params=params, file_system=file_system)

    return tools


PAGE_CHECK_RULES = (
    "FILL TOP TO BOTTOM: on each page start at the first question and work down in order, one question "
    "after another; don't jump between the top and the bottom. Each step's FORM CHECK note lists the "
    "required questions still unanswered on this page (a red * means required even when the element list "
    "doesn't say so): answer all of them before moving on.\n"
    "PAGE BY PAGE: before clicking Next, Continue, Save and Continue or Submit, check every field on the "
    "current page:\n"
    "- Every answer must come from the candidate profile, the resume, the saved answers, the user's "
    "instructions or these rules. Correct anything that doesn't.\n"
    "- A REQUIRED question none of those answer: never stop the application over it. Give the most "
    "reasonable, conservative answer and log it with record_guesses (question, your answer, its options):\n"
    "  - About this company specifically (referred by an employee, worked or applied here before, "
    "relatives working here, agreements with other employers, conflicts of interest): No / None.\n"
    "  - Consent, and willingness to have a background check or drug test or to follow policies: Yes.\n"
    "  - A skill, certification, license, clearance or tool the resume doesn't show: No. Years of "
    "experience with something: from the resume, else 0.\n"
    "  - Anything else: the safest plausible answer. Never invent experience, employers, degrees or dates.\n"
    "- An OPTIONAL question you can't answer from those sources: leave it blank.\n"
    "- Voluntary self-identification (gender, race/ethnicity, veteran status, disability): the "
    "screening answer if there is one, otherwise the 'decline to answer' option.\n\n"
)

# A page shorter than this (visible text) is still loading: Workday and other
# single-page apps show a blank root for a few seconds after every navigation.
PAGE_READY_TEXT = 200
PAGE_WAIT_SECONDS = 10

_PAGE_TEXT_JS = "() => ((document.body && document.body.innerText) || '').trim().length"


# Employer application sites (Oracle's careers pages, for one) ask for the
# location, and the agent can't click the browser's own Allow popup. Job boards
# and every other page keep the browser's normal behaviour.
LOCATION_SITES = tuple(host for host, platform in ATS_DOMAINS.items() if platform not in JOB_BOARDS)


def location_origin(url: str) -> str:
    """The origin to let read the location ("" for none): known application sites only."""
    from urllib.parse import urlparse
    parsed = urlparse(url or "")
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        return ""
    if not any(host == s or host.endswith("." + s) for s in LOCATION_SITES):
        return ""
    try:
        port = parsed.port
    except ValueError:
        return ""
    default = 443 if parsed.scheme == "https" else 80
    return f"{parsed.scheme}://{host}" + (f":{port}" if port and port != default else "")


# id(browser session) -> (the session, origins already granted); dropped when the
# job ends. Holding the session keeps its id from being reused while listed.
_location_granted: dict[int, tuple[object, set[str]]] = {}


async def allow_location(browser_session, url: str) -> None:
    """Grant the location to this page's site if it's an employer application site (once per site)."""
    origin = location_origin(url)
    if not origin:
        return
    granted = _location_granted.setdefault(id(browser_session), (browser_session, set()))[1]
    if origin in granted:
        return
    try:
        await browser_session.cdp_client.send.Browser.setPermission(
            params={"permission": {"name": "geolocation"}, "setting": "granted", "origin": origin})
        granted.add(origin)
    except Exception:
        pass


async def before_step(agent) -> None:
    """Before each AI step: let a blank page finish loading, then tick a simple
    "I'm not a robot" check, so the AI doesn't spend a request on either."""
    try:
        await allow_location(agent.browser_session, await agent.browser_session.get_current_page_url())
    except Exception:
        pass
    await wait_for_page_content(agent)
    try:
        found = await _captcha.detect(agent.browser_session)
        if found is not None and found.type == _captcha.HUMAN_CHECK:
            await _captcha.solve_human_check(agent.browser_session)
    except Exception:
        pass


async def wait_for_page_content(agent) -> None:
    """Before each step: give a blank, still-loading page a few seconds, in code.

    Otherwise the AI spends a whole request (~15K tokens) seeing a blank page
    and deciding to wait; most Workday runs did that twice.
    """
    try:
        page = await agent.browser_session.get_current_page()
        if page is None:
            return
        deadline = asyncio.get_running_loop().time() + PAGE_WAIT_SECONDS
        while asyncio.get_running_loop().time() < deadline:
            length = await page.evaluate(_PAGE_TEXT_JS)
            if int(length or 0) >= PAGE_READY_TEXT:
                return
            await asyncio.sleep(0.5)
    except Exception:
        pass


def performance_mode() -> bool:
    """Settings -> Performance mode: optional AI work off (answers stay the same)."""
    return bool(config.load_runtime_settings().get("performance_mode"))


def fit_check_on() -> bool:
    settings = config.load_runtime_settings()
    return settings.get("fit_check_enabled", True) is not False and not settings.get("performance_mode")


def cover_letter_boxes_on() -> bool:
    return bool(config.load_runtime_settings().get("cover_letter_boxes"))


def search_area(profile: dict) -> tuple[str, str]:
    """(centre ZIP, radius "25" | "50" | "100" | "any") from Profile -> Search area."""
    area = profile.get("search_area") or {}
    center = (area.get("center_zip") or (profile.get("address") or {}).get("zip") or "").strip()
    return center, str(area.get("radius") or "any")


def previous_failure_instructions(text: str) -> str:
    if not text:
        return ""
    return ("THE LAST ATTEMPT AT THIS JOB FAILED. Its own report:\n"
            f"{text[:1500]}\n"
            "Read it, work out what went wrong, and get past that point a different way this time (another "
            "control, the keyboard, Edit links, Sign In instead of Create Account, ...).\n\n")


def safe_folder_name(name: str) -> str:
    """A folder name Windows accepts: "apply_X_Early Careers: Health" -> "apply_X_Early_Careers__Health"."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).replace(" ", "_").strip(". ")
    return cleaned[:80] or "apply"


def other_documents() -> list[dict]:
    """Extra files from Settings -> Other documents ([{"label", "path"}]) that exist on disk."""
    from pathlib import Path as _Path
    docs = config.load_runtime_settings().get("other_documents") or []
    return [d for d in docs if isinstance(d, dict) and d.get("path") and _Path(d["path"]).is_file()]


def documents_instructions(documents: list[dict]) -> str:
    if not documents:
        return ""
    lines = "\n".join(f"- {d.get('label') or 'Document'}: {d['path']}" for d in documents)
    return ("OTHER DOCUMENTS you may upload when a form asks for them (use the exact path):\n"
            f"{lines}\n\n")


def user_note_instructions(note: str) -> str:
    if not note:
        return ""
    return ("NOTE FROM THE USER ABOUT THIS JOB (written after an earlier attempt; follow it):\n"
            f"{note}\n\n")


def save_job_note(url: str, note: str) -> None:
    """Keep the user's message on the job, so a retry gets it too."""
    try:
        update_job(url, user_note=note)
    except Exception as e:
        print(f"    ⚠️  Couldn't save your note on the job: {e}")


REVIEW_RULES = (
    "REVIEW BEFORE SUBMITTING: the user checks this application before it is sent. When every page is "
    "done and only the final Submit / Apply button is left, do NOT click it: call request_submit_review "
    "with a short summary. If it says the user approved, click Submit. If it says not submitted, stop.\n"
    "TRIAL RUN: never give up on your own. When you're stuck (an element won't respond, a page won't "
    "advance, something is unclear), call ask_user_for_help with the problem; the browser stays open "
    "and the user fixes it or tells you what to do. The same for a page asking you to confirm you're a "
    "real person ('I'm not a robot', 'Quick Check Needed'): the app ticks it for you; if it's still there, ask the user.\n\n"
)

# A failed done() whose text starts with this marks the job "closed", not "failed".
POSTING_CLOSED_PREFIX = "POSTING CLOSED:"
# ... and this one marks it "needs_you" (something only the user can provide).
NEEDS_YOU_PREFIX = "NEEDS YOU:"

# The agent's done text starting with one of these ends the job with that status
# (prefix, status, reason if none given, log label).
_AGENT_ENDINGS = (
    (NEEDS_YOU_PREFIX, "needs_you", "This job needs something from you.", "🙋 Needs you"),
    (POSTING_CLOSED_PREFIX, "closed", "The posting is closed.", "🔒 Closed"),
)

# A first done(success=false) before this many steps is sent back once with
# ideas to try: agents gave up after 10-16 steps, one claiming "this is the
# final allowed step" with hundreds left.
EARLY_GIVE_UP_STEPS = 25

# Reasons that end a run however early they come.
_HARD_STOPS = tuple(prefix.lower() for prefix, *_ in _AGENT_ENDINGS) + (
    "sign-in required", "real person", "not a robot", "human check", "captcha")


def is_hard_stop(text: str) -> bool:
    low = (text or "").lower()
    return any(marker in low for marker in _HARD_STOPS)


GIVE_UP_PUSHBACK = (
    "Not yet: you have used only {used} of {limit} steps, so don't give up. Before reporting failure, try:\n"
    "- An element that doesn't respond: click its visible label text, use the keyboard (Tab to it, Space "
    "or Enter), or scroll it into the middle of the page and click again.\n"
    "- A page that doesn't advance after Create Account / Sign In / Next: look for a red error message or "
    "an empty required field; if the account already exists, use Sign In; if a verification email was "
    "sent, get it from Gmail.\n"
    "- Wait a few seconds and look at the page again: it may still be loading.\n"
    "Then continue the application. If it still can't go on after that, call done with success=false again."
)


def is_paused() -> bool:
    return _pause_state["paused"]


FORM_RULES = (
    "FORM ANSWER RULES (these override saved answers and learned tips):\n"
    "- 'How did you hear about us' / referral source / job source: the company's own website or careers "
    "portal; if that isn't offered or can't be selected quickly, LinkedIn. Never an employee referral. If "
    "neither exists, pick 'Other' (type 'Company careers website' if asked).\n"
    "- Searchable list fields (Workday fields with a list icon, 'Select One' prompts): click the field, "
    "type a keyword of the option you want (e.g. 'Website' or 'LinkedIn'), press Enter, then click the "
    "matching option. If the list shows categories, open the one that fits (e.g. 'Company Website', "
    "'Job Board', 'Social Media') and pick inside it. Check the field shows your choice. After 2 failed "
    "attempts, pick any reasonable option and move on.\n"
    "- Salary fields that take only a number: enter one number, with no currency sign, range or words "
    "(the minimum of the candidate's range). If the field asks for a different period than the "
    "candidate's, convert it: hourly x 2080 = yearly, hourly x 173 = monthly, yearly / 12 = monthly.\n"
    "- Address fields: copy them from the profile's Address line. Address Line 1 is the street address "
    "(never the town or city, even if the resume shows only a town); City, State and ZIP go in their own "
    "fields; leave Address Line 2 empty unless the profile has one.\n"
    "- Field of study / major / discipline: the profile's Education major (it wins over the resume). If that "
    "isn't one of the options, use the "
    "profile's 'Field of study if yours isn't listed'; if there is none, the closest broad field. Log it with "
    "record_guesses. Never leave it blank or 'No Response'.\n"
    "- Desired / available start date: follow the profile's 'Start date' line exactly.\n"
    "- Type phone numbers, emails and ZIP codes exactly as the profile shows them, and clear the field first "
    "if it already holds something else.\n"
    "- AT THE REVIEW PAGE: if something is wrong, use that section's Edit link (or Back), fix it, return and "
    "submit. Small imperfections are never a reason to stop: a dropdown without the exact option gets the "
    "closest one (logged with record_guesses), a resume pre-fill detail gets corrected or left if harmless. "
    "Only stop when the application truly can't be finished (a required document you don't have, a page that "
    "won't load).\n"
    "- A required upload you don't have (transcript, writing sample, portfolio, ...) and no matching file among "
    "your available files: call done with success=false and text starting with 'NEEDS YOU: missing document: "
    "<what>'.\n"
    "- The user may be watching and fixing fields by hand. Never change a field that already holds a "
    "valid, sensible value just because you would have entered something else. Only change fields that "
    "are empty, show a validation error, or clearly contradict the profile or resume. If the page moved "
    "on without you, continue from where it is now.\n\n"
)


# About 1,500 tokens: room for a two-page resume.
RESUME_TEXT_LIMIT = 6000


def resume_text(path: str, limit: int = RESUME_TEXT_LIMIT) -> str:
    """The resume PDF's text, or "" when it can't be read.

    The agent only uploads the file, so without this it has no idea of the
    candidate's work history, and can't fix the entries that Workday and
    similar sites pre-fill from the upload (projects listed as jobs,
    "COMPANY | Title" pasted whole into Job Title).
    """
    try:
        import fitz  # pymupdf
        with fitz.open(path) as doc:
            text = "\n".join(page.get_text() for page in doc)
    except Exception:
        return ""
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


# Resume headings, by what their entries are. Only "work" entries are jobs.
_SECTION_KINDS = {
    "work": ("experience", "work experience", "professional experience", "relevant experience",
             "additional experience", "other experience", "employment", "employment history",
             "work history", "internships", "internship experience"),
    "not_job": ("projects", "personal projects", "academic projects", "technical projects",
                "selected projects", "activities", "extracurricular activities", "leadership",
                "volunteer", "volunteering", "volunteer experience", "community service",
                "awards", "honors", "honors & awards", "certifications", "coursework",
                "relevant coursework", "involvement", "research projects"),
}


def resume_sections(text: str) -> dict[str, list[str]]:
    """{"work": [...], "not_job": [...]}: the resume's section headings, by kind."""
    found: dict[str, list[str]] = {"work": [], "not_job": []}
    for line in text.splitlines():
        heading = line.strip().rstrip(":")
        key = heading.lower()
        for kind, names in _SECTION_KINDS.items():
            if key in names and heading not in found[kind]:
                found[kind].append(heading)
    return found


def resume_instructions(text: str) -> str:
    """Task text with the resume and the rules for work history sections."""
    if not text:
        return ""
    sections = resume_sections(text)
    section_rule = ""
    if sections["work"] or sections["not_job"]:
        section_rule = (
            f"- The resume's job sections are: {', '.join(sections['work']) or 'none'}. Only entries under "
            f"these go under Work Experience / Employment History.\n"
            f"- Entries under {', '.join(sections['not_job']) or 'other sections'} are NOT jobs: never put "
            f"them under Work Experience and never make up a company or job title for them.\n"
        )
    return (
        "RESUME (the file you upload; the source of truth for work history, education and projects):\n"
        f"<resume>\n{text}\n</resume>\n"
        "WORK EXPERIENCE AND EDUCATION SECTIONS:\n"
        f"{section_rule}"
        "- Fill them from the resume above, never from guesses.\n"
        "- Add EVERY entry under the job sections (one Work Experience entry each, all of them), not just "
        "the first one.\n"
        "- Education: always fill it in (school, degree, field of study, dates) from the profile and resume; "
        "never delete the education section. A required date the profile and resume don't give (e.g. the "
        "start date): use the most likely value (a bachelor's degree usually starts in September four years "
        "before graduation) and log it with record_guesses.\n"
        "- Only real positions (jobs, internships, research or teaching roles) go under Work Experience. "
        "Projects, coursework and clubs are NOT jobs: leave them out of Work Experience "
        "(use a Projects section if the form has one).\n"
        "- Many sites (Workday, iCIMS, Taleo, ...) pre-fill these sections from the uploaded resume and "
        "often get them wrong. Check every pre-filled entry against the resume: the employer goes in "
        "Company and the role in Job Title (a resume line like 'CORNER MARKET | Cashier' means "
        "Company = Corner Market, Job Title = Cashier), fill missing required fields from the resume, "
        "and DELETE entries that are projects, activities or duplicates (use the entry's Delete button) "
        "instead of inventing a company or title to satisfy a required field.\n\n"
    )


_ERROR_MAP = [
    ("'NoneType' object is not subscriptable", "Browser agent encountered an unexpected page state. The page may have changed or timed out."),
    ("'NoneType' object has no attribute", "Browser agent lost track of a page element. The site may have redirected or loaded slowly."),
    ("net::ERR_", "Network error — the page failed to load. Check your internet connection."),
    ("Timeout", "Operation timed out. The page took too long to respond."),
    ("ERR_CONNECTION_REFUSED", "Could not connect to the website. It may be temporarily down."),
    ("security token", "AWS credentials expired. They will be refreshed automatically on retry."),
    ("rate limit", "API rate limit reached. Wait a moment and try again."),
    ("context was destroyed", "Browser page closed unexpectedly during the application."),
    ("Target page, context or browser has been closed", "Browser closed unexpectedly during the application."),
]


def _friendly_error(raw: str) -> str:
    """Translate raw Python/browser errors into user-readable messages."""
    for pattern, friendly in _ERROR_MAP:
        if pattern.lower() in raw.lower():
            return friendly
    if len(raw) > 200 and ("Traceback" in raw or "Error:" in raw):
        return "Application failed due to an unexpected error. Check the Logs page for details."
    return raw


_MARKER = re.compile(r"\s*@@(?:LEARNING|QUESTION|JOB_APPLIED):\s*\{[^{}]*\}")


def strip_markers(text: str) -> str:
    """The agent's text without its @@LEARNING / @@QUESTION notes (those are for the app, not the user)."""
    cleaned = _MARKER.sub("", text or "")
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


async def save_job_status(url: str, status: str, error: str | None = None, agent_words: bool = False):
    """agent_words: the error is the agent's own explanation, kept as written.

    _friendly_error is for raw exceptions; applied to the agent's text it
    replaced any long explanation quoting a page's "Error: ..." with a generic
    message, and one mentioning a timeout with "Operation timed out".
    """
    fields: dict = {"status": status}
    if error:
        fields["error"] = strip_markers(error) if agent_words else _friendly_error(error)
    else:
        fields["error"] = None
    if status == "applied":
        fields["applied_at"] = datetime.now(timezone.utc).isoformat()
    update_job(url, **fields)


async def _captcha_needs_you(url: str, title: str, company: str, worker_id: int, reason: str, label: str) -> str:
    """End the job as Needs you over a CAPTCHA, and skip the site's other jobs this run."""
    _block_site(url, title)
    await save_job_status(url, "needs_you", captcha_note(reason), agent_words=True)
    print(f"  🙋 [W{worker_id}] Needs you ({label}): {title} at {company}")
    return "needs_you"


def _unsolved_reason(e) -> str:
    kind = "hCaptcha" if "hcaptcha" in e.challenge_type else e.challenge_type.replace("_", " ")
    return f"The application site showed a {kind} check and the CAPTCHA solver couldn't solve it ({e.reason})."


async def save_new_qa(new_questions: dict, source_domain: str = ""):
    if not new_questions:
        return
    async with _qa_lock:
        store = get_memory_store()
        if store:
            for q, a in new_questions.items():
                store.qa_add(question=q, answer=a or "", source_domain=source_domain)
        else:
            qa = load_json(QA_FILE, {})
            existing_norms = {normalize_question(k) for k in qa}
            for q, a in new_questions.items():
                if normalize_question(q) not in existing_norms:
                    qa[q] = a
                    existing_norms.add(normalize_question(q))
            save_json(QA_FILE, qa)


async def check_fit(posting, profile: dict, title: str, company: str) -> str | None:
    """Why to skip this job ("<title>\n<reason>"), or None to apply.

    The location check is code (free; on in every mode). The AI fit check is
    one small request, off in performance mode or when turned off in Settings.
    """
    center, radius = search_area(profile)
    location_note = ""
    if posting is not None and radius != "any" and center:
        texts = posting.locations + (["Remote"] if posting.remote else [])
        verdict, detail = geo.check(texts, center, radius)
        if verdict == "out":
            return f"Outside your search area\n{detail}"
        if verdict == "unknown":
            location_note = f"within {radius} miles of ZIP {center} (remote jobs are fine)"
    if posting is None or not fit_check_on() or not (posting.description or "").strip():
        return None
    result = await ai_extras.fit_check(config.get_llm(), title, company, posting.description, profile, location_note)
    if result is not None and result.skip and result.reason.strip():
        return f"{(result.title or 'Not a fit').strip()}\n{result.reason.strip()}"
    return None


async def apply_to_job(job: dict, profile: dict, qa: dict, applied_labels: list[str], easy_apply: bool, worker_id: int, resume_path_override: str | None = None) -> str:
    """Apply to a single job. Returns final status."""
    url = job["url"]
    title = job.get("title", "Unknown")
    company = job.get("company", "Unknown")
    print(f"  🚀 [W{worker_id}] Starting: {title} at {company}")

    if not config.validate_job_url(url):
        await save_job_status(url, "blocked", "Invalid or internal URL")
        print(f"  🚫 [W{worker_id}] Blocked (invalid URL): {title} at {company}")
        return "blocked"

    settings_resume, settings_sensitive, blocked_domains = current_settings()

    if any(domain in url for domain in blocked_domains):
        await save_job_status(url, "blocked", "Blocked domain")
        print(f"  🚫 [W{worker_id}] Blocked: {title} at {company}")
        return "blocked"

    if not claim_job(url):
        print(f"  ⏭️  [W{worker_id}] Skipped (already claimed): {title} at {company}")
        return "skipped"

    blocked_by = _captcha_blocked.get(_site(url))
    if blocked_by is not None:
        return await _captcha_needs_you(url, title, company, worker_id, (
            f"Not opened: earlier in this run the same site ({_site(url)}) showed a CAPTCHA the solver "
            f"couldn't pass, on \"{blocked_by}\"."), "same site's CAPTCHA")

    agent_sensitive_data = build_agent_secrets(profile, settings_sensitive)
    if "email" not in agent_sensitive_data:
        # Nearly every form asks for an email; don't spend a run typing a bad one.
        await save_job_status(url, "failed", NO_VALID_EMAIL_ERROR)
        print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — no valid email address")
        return "failed"

    # One plain request before any browser or AI: a taken-down posting ends
    # here, and a pasted link gets its real title and company.
    posting = await asyncio.to_thread(config.refresh_posting_details, url, None, False)
    if posting is not None and posting.closed:
        await save_job_status(url, "closed", posting.reason, agent_words=True)
        print(f"  🔒 [W{worker_id}] Closed: {title} at {company} — {posting.reason}")
        return "closed"
    saved = read_jobs().get(url) or {}
    title = job["title"] = saved.get("title") or title
    company = job["company"] = saved.get("company") or company
    if posting is not None:
        if posting.title and job_page.is_placeholder(title):
            title = job["title"] = posting.title
        if posting.company and (company or "").strip() in ("", "Unknown"):
            company = job["company"] = posting.company

    # A retry of the same job in this run doesn't ask again.
    if not (saved.get("fit_override") or job.get("fit_override") or job.get("_fit_checked")):
        skip = await check_fit(posting, profile, title, company)
        job["_fit_checked"] = True
        if skip is not None:
            await save_job_status(url, "skipped", skip, agent_words=True)
            print(f"  🚫 [W{worker_id}] Skipped: {title} at {company} — {skip.splitlines()[0]}")
            return "skipped"

    # Always refresh credentials before each job to avoid mid-run expiry
    refresh_credentials()

    llm = config.get_llm()
    # Use the shared browser profile in OS data dir (same as login endpoint)
    browser = BrowserSession(
        chromium_sandbox=(sys.platform != "linux"),
        **config.build_browser_kwargs(worker=worker_id),
    )
    # (started inside the try below, whose finally always closes it)
    mem_store = get_memory_store()
    # Count memories injected for metrics tracking
    domain = mem_store.extract_domain(url)
    memories_before = mem_store.get_domain_memories(url, limit=50)
    memories_injected_count = len(memories_before) if memories_before else 0
    # Show the agent the email it will actually type (see build_agent_secrets).
    memory = build_memory_context(
        {**profile, "email": agent_sensitive_data["email"]}, qa,
        same_company_applications(applied_labels, company), job_url=url,
        learnings=not performance_mode(),
    )
    run_started_at = datetime.now(timezone.utc)

    resume_path = resume_path_override or settings_resume

    # Use tailored resume if available for this job
    try:
        from resume.tailor import get_tailored_resume_path
        tailored_path = get_tailored_resume_path(url)
        if tailored_path:
            resume_path = tailored_path
            print(f"  📄 [W{worker_id}] Using tailored resume: {tailored_path}")
    except ImportError:
        pass


    # Jobs no longer come only from LinkedIn: pasted links, Handshake and Intern
    # Insider all land here. Telling the agent a greenhouse.io URL is "on
    # LinkedIn" sends it to the wrong site, so the wording follows the URL.
    from urllib.parse import urlparse as _urlparse
    _host = (_urlparse(url).hostname or "").lower()
    is_linkedin = _host.endswith("linkedin.com")

    # No up-front LinkedIn/Gmail login checks: they cost 2-4 LLM steps on every
    # job (on two of the heaviest pages on the web), and a signed-out Gmail made
    # the agent re-check every 15 s for 5 minutes, about 20 requests before the
    # job was even opened. Sign-in pages are now handled only when one actually
    # appears, with a short, bounded wait. The run opens the job URL itself
    # (initial_actions below), so the task starts on the posting.
    signin_wait = (
        "wait for the user to sign in in this browser window: use the wait action "
        "(30 seconds) at most 4 times, checking the page after each wait. If it is still "
        "signed out, call done with success=false and"
    )
    linkedin_signin_note = (
        f"LINKEDIN SIGN-IN: If LinkedIn shows a sign-in or join page instead of the job, "
        f"{signin_wait} the text 'LinkedIn sign-in required: sign in to LinkedIn in Settings → Accounts, "
        f"then retry this job.'\n\n"
    ) if is_linkedin else ""

    otp_instructions = (
        "\n\nOTP/VERIFICATION CODES: If ANY site asks for a verification code, OTP, or 2FA token:\n"
        "1. Choose the EMAIL option if given a choice\n"
        "2. Open a new tab and go to https://mail.google.com\n"
        "3. Find the most recent email with the verification/OTP code\n"
        "4. Copy the code, switch back to the application tab, and enter it\n"
        "5. This is NOT a blocker — always attempt to retrieve the code from Gmail before giving up\n"
        f"6. If Gmail shows a Google sign-in page instead of the inbox, {signin_wait} the text "
        "'Gmail sign-in required to read the verification code: sign in to Gmail in Settings → Accounts, "
        "then retry this job.'"
    )

    has_password = bool(agent_sensitive_data.get("password", "").strip())

    # When a password is configured it is the one account password for every
    # site — the agent must never invent its own.
    configured_password_note = (
        "\n\nACCOUNT CREDENTIALS: A password is configured in Settings.\n"
        "- To LOG IN to any site, use <secret>account_email</secret> and <secret>password</secret>.\n"
        "- To CREATE an account on any site, use <secret>account_email</secret> and "
        "<secret>password</secret> as the new password. Do NOT generate or invent a password.\n"
        "- If the site rejects that password for its complexity rules, append characters to it "
        "rather than replacing it, and report what you used.\n"
        "- Accounts are per company (each Workday site has its own). If Create Account says the email is "
        "already registered or in use, click Sign In and sign in instead. If signing in says the email isn't "
        "verified, or the site says it sent a verification email, verify it through Gmail (see below) and "
        "sign in. If the password is wrong, use 'Forgot your password?' and set it to "
        "<secret>password</secret> through the emailed link.\n"
    ) if has_password else ""

    if easy_apply:
        apply_instructions = (
            f"You are on a LinkedIn job posting: {url}. Click Easy Apply and complete the application. "
            f"Use resume at {resume_path}. Auto-fill all fields from candidate profile.\n\n"
            f"{linkedin_signin_note}"
            f"EMAIL USAGE:\n"
            f"- For APPLICATION FORM fields (contact email, email address, etc.): use <secret>email</secret>\n"
            f"- For LOGGING IN or CREATING ACCOUNTS: use <secret>account_email</secret> and <secret>password</secret>\n"
            f"{configured_password_note}"
            f"{otp_instructions}"
        )
    else:
        password_note = ""
        if not has_password:
            password_note = (
                "\n\nIMPORTANT: No password is configured in Settings. If the external site requires "
                "account creation or login:\n"
                "1. First check if you can apply as a guest (without creating an account)\n"
                "2. Try 'Sign in with LinkedIn' or 'Sign in with Google' buttons\n"
                "3. If no SSO option, CREATE A NEW ACCOUNT using <secret>account_email</secret> and a generated password "
                "(create a strong password like 'JobApp2026!')\n"
                "4. If account creation also fails, try 'Forgot password' → reset via email\n"
                "5. If nothing works after 3 attempts, report failure and move to the next job\n"
            )

        # A LinkedIn posting needs the extra hop through its Apply button; a
        # pasted link or a Handshake/Intern Insider posting is already on (or one
        # click from) the employer's own form.
        opening = (
            f"You are on a LinkedIn job posting: {url}. Click Apply and follow through to the "
            f"external application page. "
            if is_linkedin else
            f"You are on a job posting: {url}. It is NOT on LinkedIn, so do "
            f"not go to LinkedIn. Find the application form on this site, following "
            f"an 'Apply' button through to the employer's own form if needed. "
        )
        apply_instructions = (
            f"{opening}"
            f"Use resume at {resume_path}. Auto-fill all fields from candidate profile.\n\n"
            f"{linkedin_signin_note}"
            f"NAVIGATING EXTERNAL SITES:\n"
            f"- The LinkedIn 'Apply' button often opens a company careers page, NOT the application form directly.\n"
            f"- You MUST explore the landing page: look for 'Apply Now', 'Submit Application', or similar buttons.\n"
            f"- Scroll down — the apply button is often below the job description.\n"
            f"- If you see a job listing page, click on the specific job title first, then look for the apply button.\n"
            f"- Some sites require you to click through 2-3 pages before reaching the actual form.\n"
            f"- If the page looks blank or is loading, wait 3-5 seconds and try scrolling.\n"
            f"- NEVER give up just because the form isn't immediately visible — always explore the page first.\n\n"
            f"EMAIL USAGE:\n"
            f"- For APPLICATION FORM fields (contact email, email address, etc.): use <secret>email</secret>\n"
            f"- For LOGGING IN or CREATING ACCOUNTS on external ATS sites: use <secret>account_email</secret> and <secret>password</secret>\n"
            f"{configured_password_note}"
            f"{password_note}"
            f"SAFE LINK TARGETING — only interact with links and buttons a human could actually see "
            f"in the job listing or the application form itself. Ignore anything hidden, zero-sized, "
            f"positioned off-screen, or marked aria-hidden: those are honeypot traps used to detect bots.\n\n"
            f"If it's a video funnel or recruitment pitch, report failure and stop. "
            f"If the external form is broken after 3 attempts, report failure and stop.\n\n"
            f"BLOCKED SITES — if redirected to any of these, immediately call done with success=false: {', '.join(blocked_domains)}"
            f"{otp_instructions}"
        )

    _agent_log_start("apply", f"{title} at {company}")

    # A trial run has the user at hand to get it unstuck, so it gets more room.
    MAX_STEPS = 150 if is_trial_run() else (100 if job_page.is_workday(url) else 70)
    _step_count = {"n": 0}
    _agent_ref = {"agent": None, "max_steps": MAX_STEPS}
    # Set when a blocking CAPTCHA cannot be solved, so the run can be ended and
    # reported accurately after agent.run() returns.
    _captcha_failure = {"error": None}

    async def _on_step_with_limit(browser_state, agent_output, step_num):
        _agent_on_step(browser_state, agent_output, step_num)
        _step_count["n"] += 1
        if worker_id in _current:
            _current[worker_id]["step"] = _step_count["n"]
        if _step_count["n"] >= MAX_STEPS:
            raise Exception(f"Reached maximum of {MAX_STEPS} steps — stopping to avoid wasting tokens")

        # CAPTCHA handling runs as code on every step, not as prompt text.
        # captcha.handle() solves when the solver is on and raises
        # CaptchaUnsolved when a blocking challenge cannot be dealt with.
        #
        # We must NOT let that exception escape: browser-use catches everything
        # raised from a step callback in Agent.step() and routes it to
        # _handle_step_error(), which only counts a failure and carries on. The
        # run would then burn up to max_failures more LLM steps and finish with
        # a generic error. Calling agent.stop() ends the run promptly instead,
        # and the stored error gives an accurate reason afterwards.
        try:
            await _captcha.handle(browser, config.load_runtime_settings())
        except _captcha.HumanCheckNeeded as e:
            # In a trial run the agent asks the user to tick it; otherwise skip
            # the job and leave it for the user ("Needs you").
            if not is_trial_run():
                _captcha_failure["error"] = e
                if _agent_ref["agent"]:
                    _agent_ref["agent"].stop()
        except _captcha.CaptchaUnsolved as e:
            _captcha_failure["error"] = e
            if _agent_ref["agent"]:
                _agent_ref["agent"].stop()
        except Exception:
            # Detection problems must never break an otherwise-fine run.
            pass

    documents = other_documents()
    user_note = (job.get("user_note") or "").strip()
    job_domain = _host.removeprefix("www.")
    review = is_trial_run()
    training = is_training_run()
    if training:
        print(f"  🎓 [W{worker_id}] Training run: fills everything in, never submits")
    if review:
        print(f"  👀 [W{worker_id}] Trial run: it will wait for your review before submitting")
    # Unique per job: two jobs with the same company and title can run at the same time.
    _agent_ref["cover_letter_pdf"] = LOGS_DIR.parent / "cover_letters" / (
        safe_folder_name(f"{company}_{title}")[:60] + f"_{hashlib.sha1(url.encode()).hexdigest()[:8]}.pdf")
    _agent_ref["cover_letter_pdf"].parent.mkdir(parents=True, exist_ok=True)
    _agent_ref["profile"] = profile
    _agent_ref["posting_description"] = getattr(posting, "description", "") if posting is not None else ""
    resume = _agent_ref["resume_text"] = resume_text(resume_path)
    tools = build_tools(job, job_domain, review, _agent_ref, worker_id)
    actions = tools.registry.registry.actions

    agent = Agent(
        task=(
            f"{apply_instructions}\n\n"
            f"{user_note_instructions(user_note)}"
            f"{documents_instructions(documents)}"
            f"{previous_failure_instructions(job.get('previous_failure', ''))}"
            f"{NAME_JOB_RULE if 'set_job_details' in actions else ''}"
            f"{COVER_LETTER_RULE if 'write_cover_letter' in actions else ''}"
            f"{resume_instructions(resume)}"
            f"{FORM_RULES}"
            f"{PAGE_CHECK_RULES}"
            f"{REVIEW_RULES if review else ''}"
            f"{TRAINING_RULES if training else ''}"
            f"PERSISTENCE & EFFICIENCY:\n"
            f"- Try at least 3 DIFFERENT approaches before reporting failure.\n"
            f"- If an element doesn't respond after 2-3 clicks, try a completely different method (keyboard, scrolling, different selector).\n"
            f"- Do NOT repeat the same failing action more than 3 times — switch strategies.\n"
            f"- If you've been stuck on the same form field for more than 5 steps, switch to a completely different approach (or skip the field if it's optional).\n"
            f"- If the posting itself is gone (page not found, job closed, no longer accepting applications), call done with success=false and text starting with '{POSTING_CLOSED_PREFIX}'.\n"
            f"- You have a maximum of {MAX_STEPS} steps total. Budget your steps wisely.\n"
            f"- When you report a failure (done with success=false), start the text with a short title of at most "
            f"8 words on its own line (e.g. 'Stopped at Review: Field of Study'), then the explanation. Never "
            f"mention @@ tracking markers in it.\n\n"
            f"TRACKING: Include in memory field after submission:\n"
            f'@@JOB_APPLIED: {{"title": "{title}", "company": "{company}", "location": "{job.get("location", "")}"}}\n'
            f"For each form question: @@QUESTION: {{\"question\": \"...\", \"answer\": \"...\", \"type\": \"...\"}}"
        ),
        llm=llm,
        tools=tools,
        fallback_llm=config.get_fallback_llm(),
        # Open the posting before the first LLM step. browser-use only does this
        # itself when the task contains a single URL, and ours has several.
        initial_actions=[{"navigate": {"url": url, "new_tab": False}}],
        # browser-use's judge re-sends the task, every step and up to 10
        # screenshots after each run, purely for logging; LangHire never reads it.
        use_judge=False,
        # Fill up to 10 fields per step instead of 5. Actions after a page change
        # are skipped automatically, and every step saved is a whole request.
        max_actions_per_step=10,
        use_vision=True,
        # A slow reply (retries, long thinking) can take longer than the old
        # default of 75 s.
        llm_timeout=180,
        # A review step waits for the user, up to REVIEW_WAIT_SECONDS.
        step_timeout=REVIEW_STEP_TIMEOUT if review else 300,
        max_failures=_max_failures(),
        loop_detection_enabled=True,
        loop_detection_window=5,
        browser_session=browser,
        sensitive_data=agent_sensitive_data,
        extend_system_message=memory,
        available_file_paths=[resume_path] + [d["path"] for d in documents]
        + ([str(_agent_ref["cover_letter_pdf"])] if "write_cover_letter" in actions else []),
        save_conversation_path=str(LOGS_DIR / safe_folder_name(f"apply_{company}_{title[:30]}")),
        calculate_cost=True,
        register_new_step_callback=_on_step_with_limit,
        register_done_callback=_agent_on_done,
    )
    _agent_ref["agent"] = agent
    # The emails the agent is told to type: the form email, and the account email on account pages.
    add_form_check_each_step(agent, {**profile, "email": agent_sensitive_data.get("email", ""),
                                     "account_email": agent_sensitive_data.get("account_email", "")})
    agent._langhire_loop = asyncio.get_running_loop()
    agent._langhire_domain = job_domain
    agent._langhire_url = url
    _current[worker_id] = {"url": url, "title": title, "company": company, "step": 0, "max_steps": MAX_STEPS,
                           "started_at": datetime.now(timezone.utc).isoformat()}
    _running_agents.add(agent)
    if is_paused():
        agent.pause()

    try:
        await config.maybe_apply_stealth(browser)
        try:
            await browser.start()  # no-op when stealth already started it
            await allow_location(browser, url)  # before the posting's first page loads
        except Exception:
            pass
        result = await agent.run(max_steps=MAX_STEPS, on_step_start=before_step)

        if url in _skip_requested:
            _skip_requested.discard(url)
            await save_job_status(url, "pending", SKIPPED_BY_YOU_NOTE, agent_words=True)
            print(f"  ⏭️  [W{worker_id}] Skipped by you: {title} at {company}")
            return "skipped"

        # A blocking CAPTCHA we could not solve ends the application here,
        # regardless of what the agent reported, so the next job can start.
        if isinstance(_captcha_failure["error"], _captcha.HumanCheckNeeded):
            await save_job_status(url, "needs_you", _captcha_failure["error"].reason, agent_words=True)
            print(f"  🙋 [W{worker_id}] Needs you (human check): {title} at {company}")
            return "needs_you"
        if _captcha_failure["error"] is not None:
            return await _captcha_needs_you(url, title, company, worker_id,
                                            _unsolved_reason(_captcha_failure["error"]), "CAPTCHA not solved")

        # Extract Q&A from history
        _, new_questions = extract_from_history(result)
        domain = ""
        try:
            from urllib.parse import urlparse
            domain = urlparse(url).hostname or ""
            if domain.startswith("www."):
                domain = domain[4:]
        except Exception:
            pass
        await save_new_qa(new_questions, source_domain=domain)

        # Determine success/failure
        success = result.is_successful()

        # ── Memory extraction (self-learning) ─────────────────────────────
        mem_store = get_memory_store()
        # 1. Marker-based: extract @@LEARNING tags the agent emitted
        marker_learnings = extract_learnings_from_markers(result)
        if marker_learnings:
            store_learnings(mem_store, marker_learnings, url, success)
        # 2. LLM-based: use the configured LLM to summarise the run into procedural
        # learnings. It costs a request, so only for runs with something to learn:
        # failures, and long flows (e.g. multi-page Workday forms).
        llm_learnings = []
        if not success or len(result.history) >= LEARN_FROM_RUNS_OVER_STEPS:
            try:
                def _llm_call(prompt):
                    """Use the user's configured LLM for memory extraction.

                    Runs in a worker thread (see asyncio.to_thread below), which
                    has no event loop of its own, so it can run one here. Called
                    from the agent's thread this raised "Cannot run the event loop
                    while another loop is running" and never produced a learning.
                    """
                    from browser_use.llm.messages import UserMessage
                    extraction_llm = config.get_llm()
                    loop = asyncio.new_event_loop()
                    try:
                        resp = loop.run_until_complete(
                            asyncio.wait_for(
                                extraction_llm.ainvoke([UserMessage(content=prompt)]),
                                timeout=60,
                            )
                        )
                        return resp.completion if hasattr(resp, 'completion') else (resp.content if hasattr(resp, 'content') else str(resp))
                    finally:
                        loop.close()

                llm_learnings = await asyncio.to_thread(
                    extract_learnings_via_llm,
                    result, job_url=url, job_title=title, success=success,
                    llm_call=_llm_call,
                )
                if llm_learnings:
                    store_learnings(mem_store, llm_learnings, url, success)
            except Exception as mem_err:
                print(f"    ⚠️  [W{worker_id}] Memory extraction failed (non-fatal): {mem_err}")

        # ── Record metrics ────────────────────────────────────────────────
        run_finished_at = datetime.now(timezone.utc)
        memories_extracted_count = len(marker_learnings) + len(llm_learnings)
        try:
            MetricsStore().record_run(
                job_url=url, job_title=title, company=company,
                website_domain=domain, ats_platform=mem_store.detect_ats_platform(domain),
                success=success, started_at=run_started_at, finished_at=run_finished_at,
                step_count=len(result.history),
                memories_injected=memories_injected_count,
                memories_extracted=memories_extracted_count,
                error_message=None if success else failure_reason(result)[:2000],
            )
        except Exception as metrics_err:
            print(f"    ⚠️  [W{worker_id}] Metrics recording failed (non-fatal): {metrics_err}")

        if success and training:
            # Practice only: the job stays pending for a real run.
            update_job(url, status="pending", error=None, practised_at=datetime.now(timezone.utc).isoformat())
            print(f"  🎓 [W{worker_id}] Practised (not submitted): {title} at {company}")
            return "practised"
        if success:
            await save_job_status(url, "applied")
            print(f"  ✅ [W{worker_id}] Applied: {title} at {company}")
            return "applied"
        else:
            error_msg = failure_reason(result)
            text = strip_markers(error_msg)
            for prefix, status, fallback, label in _AGENT_ENDINGS:
                if text.startswith(prefix):
                    reason = text[len(prefix):].strip() or fallback
                    await save_job_status(url, status, reason[:2000], agent_words=True)
                    print(f"  {label} [W{worker_id}]: {title} at {company} — {reason[:100]}")
                    return status
            if result.is_done() and blocked_by_captcha(text):
                return await _captcha_needs_you(url, title, company, worker_id, text[:1800], "CAPTCHA")
            await save_job_status(url, "failed", error_msg[:2000], agent_words=result.is_done())
            # The agent's own report of a problem it might get past on a second
            # try (not a closed posting, a missing document, a sign-in, ...).
            if result.is_done() and not is_hard_stop(error_msg) and not job.get("previous_failure"):
                job["_retry_with"] = text
            print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — {error_msg[:100]}")
            return "failed"

    except _captcha.CaptchaUnsolved as e:
        # Abandon this application and move on — never pause or wait.
        return await _captcha_needs_you(url, title, company, worker_id, _unsolved_reason(e), "CAPTCHA not solved")

    except Exception as e:
        error_str = str(e)
        # The full traceback goes to backend.log; the job card only gets one line.
        _log.error(f"Application crashed: {title} at {company} ({url})", exc_info=True)
        if "security token" in error_str.lower() or "expired" in error_str.lower():
            refresh_credentials()
            await save_job_status(url, "pending", "credentials_expired_retry")
            print(f"  🔑 [W{worker_id}] Credentials expired on: {title} at {company} — refreshed, will retry")
            return "retry"
        if _crash_retries.get(url, 0) < 1:
            # An unexpected internal error is often a one-off (a page changing
            # mid-step): try the job once more before calling it failed.
            _crash_retries[url] = _crash_retries.get(url, 0) + 1
            await save_job_status(url, "pending", None)
            print(f"  🔁 [W{worker_id}] Unexpected error on {title} at {company} — retrying once: {error_str[:100]}")
            return "retry"
        await save_job_status(url, "failed", error_str[:2000])
        print(f"  ❌ [W{worker_id}] Error: {title} at {company} — {error_str[:100]}")
        return "failed"
    finally:
        _running_agents.discard(agent)
        _current.pop(worker_id, None)
        _location_granted.pop(id(browser), None)
        _skip_requested.discard(url)
        try:
            await browser.close()
        except Exception as close_err:
            print(f"    ⚠️  [W{worker_id}] Browser cleanup error: {close_err}")


async def worker(name: str, worker_id: int, queue: asyncio.Queue, profile: dict, qa: dict, applied_labels: list, easy_apply: bool, stats: dict, cancel_flag: dict | None = None):
    """Worker that pulls jobs from queue and applies."""
    while True:
        if cancel_flag and cancel_flag.get("cancel_requested"):
            print(f"  🛑 [{name}] Stop requested — halting")
            break
        job = take_job(queue, worker_id)
        if job is None:
            break
        if job is False:
            await asyncio.sleep(2)  # the rest are on a site another worker is using
            continue

        job_easy_apply = job.get("easy_apply", easy_apply) if job.get("easy_apply") is not None else easy_apply
        # Re-read the profile for each job, so edits saved during a long run
        # (a fixed address, new instructions) apply to the next application.
        profile = load_json(CANDIDATE_PROFILE, profile) or profile
        try:
            status = await apply_to_job(job, profile, qa, applied_labels, job_easy_apply, worker_id)
        finally:
            _busy_sites.pop(worker_id, None)
        stats[status] = stats.get(status, 0) + 1

        # Randomised pause so applications are not evenly spaced. Off by
        # default; only runs between jobs, never mid-application.
        # (No pause after a closed posting: no browser was opened.)
        if status not in ("closed", "skipped") and not (cancel_flag and cancel_flag.get("cancel_requested")):
            await config.maybe_human_delay("the next application")

        # On retry, put back in queue
        if status == "retry":
            queue.put_nowait(job)
        # Self-retry: one more attempt that reads the failure report first.
        # Off in performance mode (a whole extra run).
        elif status == "failed" and job.get("_retry_with") and not performance_mode() \
                and not (cancel_flag and cancel_flag.get("cancel_requested")):
            job["previous_failure"] = job.pop("_retry_with")
            update_job(job["url"], status="pending", error=f"Retrying with what went wrong last time.\n{job['previous_failure'][:500]}")
            print(f"  🔁 [{name}] Retrying {job.get('title', '')} once, using its failure report")
            queue.put_nowait(job)

        queue.task_done()


async def main():
    parser = argparse.ArgumentParser(description="Apply to collected jobs")
    parser.add_argument("--workers", type=int, default=1, help="Number of concurrent workers")
    parser.add_argument("--limit", type=int, help="Max jobs to process")
    parser.add_argument("--easy-apply", dest="easy_apply", action="store_true", default=True)
    parser.add_argument("--no-easy-apply", dest="easy_apply", action="store_false")
    args = parser.parse_args()

    jobs = load_json(JOBS_FILE, {})
    profile = load_json(CANDIDATE_PROFILE, {})
    qa = load_json(QA_FILE, {})
    LOGS_DIR.mkdir(exist_ok=True)
    new_run()
    args.workers = max(1, min(args.workers, config.MAX_PARALLEL_JOBS))  # one prepared profile per worker
    if args.workers > 1:
        config.prepare_worker_profiles(args.workers)

    # Filter pending jobs by type
    pending = [
        j for j in jobs.values()
        if j.get("status") == "pending"
        and (j.get("easy_apply") is True) == args.easy_apply
    ]

    if args.limit:
        pending = pending[:args.limit]

    if not pending:
        print("No pending jobs to apply to. Run collect_jobs.py first.")
        return

    applied_labels = [
        f"{j.get('title','')} at {j.get('company','')}"
        for j in jobs.values() if j.get("status") == "applied"
    ]

    mode = "Easy Apply" if args.easy_apply else "Non-Easy Apply"
    print(f"Applying to {len(pending)} {mode} jobs with {args.workers} worker(s)\n")

    queue = asyncio.Queue()
    for job in pending:
        queue.put_nowait(job)

    stats = {}
    num_workers = min(args.workers, len(pending))

    # Background credential refresh every 14 min — auto-cancelled when workers finish
    cred_task = asyncio.create_task(credential_refresh_loop(14))

    workers = []
    for i in range(num_workers):
        if i > 0:
            await config.worker_stagger()  # stagger browser launches
        workers.append(
            asyncio.create_task(worker(f"W{i+1}", i+1, queue, profile, qa, applied_labels, args.easy_apply, stats))
        )
    await asyncio.gather(*workers)
    cred_task.cancel()

    print(f"\n{'='*60}")
    print(f"Results: {stats}")
    total_applied = sum(1 for j in load_json(JOBS_FILE, {}).values() if j.get("status") == "applied")
    print(f"Total applied across all runs: {total_applied}")

    # Memory stats
    mem_stats = get_memory_store().get_stats()
    print(f"🧠 Agent memory: {mem_stats['total_memories']} memories across {mem_stats['unique_domains']} domains")


if __name__ == "__main__":
    asyncio.run(main())
