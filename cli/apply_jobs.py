
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
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_use import Agent, BrowserSession

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
    from memory import extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from memory.metrics import MetricsStore
    from core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
    import core.captcha as _captcha
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
    from backend.memory import extract_learnings_from_markers, extract_learnings_via_llm, store_learnings
    from backend.memory.metrics import MetricsStore
    from backend.core.agent_logger import on_step as _agent_on_step, on_done as _agent_on_done, log_run_start as _agent_log_start
    import backend.core.captcha as _captcha

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
    """Pause or resume every running application agent; new ones follow suit."""
    _pause_state["paused"] = paused
    for agent in list(_running_agents):
        _set_agent_paused(agent, paused)


def is_paused() -> bool:
    return _pause_state["paused"]


FORM_RULES = (
    "FORM ANSWER RULES (these override saved answers and learned tips):\n"
    "- 'How did you hear about us' / referral source / job source: the company's own careers website. "
    "Pick the option closest to 'Company Website' or 'Careers Site'; never LinkedIn, Indeed, Handshake "
    "or another job board, and never an employee referral. If nothing fits, pick 'Other' and type "
    "'Company careers website'.\n"
    "- Salary fields that take only a number: enter one number, with no currency sign, range or words "
    "(the minimum of the candidate's range). If the field asks for a different period than the "
    "candidate's, convert it: hourly x 2080 = yearly, hourly x 173 = monthly, yearly / 12 = monthly.\n"
    "- Address fields: copy them from the profile's Address line. Address Line 1 is the street address "
    "(never the town or city, even if the resume shows only a town); City, State and ZIP go in their own "
    "fields; leave Address Line 2 empty unless the profile has one.\n"
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


def resume_instructions(text: str) -> str:
    """Task text with the resume and the rules for work history sections."""
    if not text:
        return ""
    return (
        "RESUME (the file you upload; the source of truth for work history, education and projects):\n"
        f"<resume>\n{text}\n</resume>\n"
        "WORK EXPERIENCE AND EDUCATION SECTIONS:\n"
        "- Fill them from the resume above, never from guesses.\n"
        "- Only real positions (jobs, internships, research or teaching roles) go under Work Experience. "
        "Projects, coursework and clubs are NOT jobs: leave them out of Work Experience "
        "(use a Projects section if the form has one).\n"
        "- Many sites (Workday, iCIMS, Taleo, ...) pre-fill these sections from the uploaded resume and "
        "often get them wrong. Check every pre-filled entry against the resume: the employer goes in "
        "Company and the role in Job Title (a resume line like 'CORNER MARKET | Cashier' means "
        "Company = Corner Market, Job Title = Cashier), fill missing required fields from the resume, "
        "and delete entries that are projects or duplicates.\n\n"
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


async def save_job_status(url: str, status: str, error: str | None = None):
    fields: dict = {"status": status}
    if error:
        fields["error"] = _friendly_error(error)
    else:
        fields["error"] = None
    if status == "applied":
        fields["applied_at"] = datetime.now(timezone.utc).isoformat()
    update_job(url, **fields)


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

    agent_sensitive_data = build_agent_secrets(profile, settings_sensitive)
    if "email" not in agent_sensitive_data:
        # Nearly every form asks for an email; don't spend a run typing a bad one.
        await save_job_status(url, "failed", NO_VALID_EMAIL_ERROR)
        print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — no valid email address")
        return "failed"

    # Always refresh credentials before each job to avoid mid-run expiry
    refresh_credentials()

    llm = config.get_llm()
    # Use the shared browser profile in OS data dir (same as login endpoint)
    browser = BrowserSession(
        chromium_sandbox=(sys.platform != "linux"),
        **config.build_browser_kwargs(),
    )
    await config.maybe_apply_stealth(browser)
    mem_store = get_memory_store()
    # Count memories injected for metrics tracking
    domain = mem_store.extract_domain(url)
    memories_before = mem_store.get_domain_memories(url, limit=50)
    memories_injected_count = len(memories_before) if memories_before else 0
    # Show the agent the email it will actually type (see build_agent_secrets).
    memory = build_memory_context(
        {**profile, "email": agent_sensitive_data["email"]}, qa,
        same_company_applications(applied_labels, company), job_url=url,
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
        f"{signin_wait} the text 'LinkedIn sign-in required: log in to LinkedIn from LangHire, "
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
        "'Gmail sign-in required to read the verification code: log in to Gmail from LangHire, "
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

    MAX_STEPS = 70
    _step_count = {"n": 0}
    _agent_ref = {"agent": None}
    # Set when a blocking CAPTCHA cannot be solved, so the run can be ended and
    # reported accurately after agent.run() returns.
    _captcha_failure = {"error": None}

    async def _on_step_with_limit(browser_state, agent_output, step_num):
        _agent_on_step(browser_state, agent_output, step_num)
        _step_count["n"] += 1
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
        except _captcha.CaptchaUnsolved as e:
            _captcha_failure["error"] = e
            if _agent_ref["agent"]:
                _agent_ref["agent"].stop()
        except Exception:
            # Detection problems must never break an otherwise-fine run.
            pass

    agent = Agent(
        task=(
            f"{apply_instructions}\n\n"
            f"{resume_instructions(resume_text(resume_path))}"
            f"{FORM_RULES}"
            f"PERSISTENCE & EFFICIENCY:\n"
            f"- Try at least 3 DIFFERENT approaches before reporting failure.\n"
            f"- If an element doesn't respond after 2-3 clicks, try a completely different method (keyboard, scrolling, different selector).\n"
            f"- Do NOT repeat the same failing action more than 3 times — switch strategies.\n"
            f"- If you've been stuck on the same form field for more than 5 steps, skip it or call done with success=false.\n"
            f"- You have a maximum of 70 steps total. Budget your steps wisely.\n\n"
            f"TRACKING: Include in memory field after submission:\n"
            f'@@JOB_APPLIED: {{"title": "{title}", "company": "{company}", "location": "{job.get("location", "")}"}}\n'
            f"For each form question: @@QUESTION: {{\"question\": \"...\", \"answer\": \"...\", \"type\": \"...\"}}"
        ),
        llm=llm,
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
        step_timeout=300,
        max_failures=_max_failures(),
        loop_detection_enabled=True,
        loop_detection_window=5,
        browser_session=browser,
        sensitive_data=agent_sensitive_data,
        extend_system_message=memory,
        available_file_paths=[resume_path],
        save_conversation_path=str(LOGS_DIR / f"apply_{company.replace(' ', '_')}_{title.replace(' ', '_')[:30]}"),
        calculate_cost=True,
        register_new_step_callback=_on_step_with_limit,
        register_done_callback=_agent_on_done,
    )
    _agent_ref["agent"] = agent
    agent._langhire_loop = asyncio.get_running_loop()
    _running_agents.add(agent)
    if is_paused():
        agent.pause()

    try:
        result = await agent.run()

        # A blocking CAPTCHA we could not solve ends the application here,
        # regardless of what the agent reported, so the next job can start.
        if _captcha_failure["error"] is not None:
            e = _captcha_failure["error"]
            await save_job_status(
                url, "failed", f"CAPTCHA could not be solved ({e.challenge_type}): {e.reason}"
            )
            print(f"  🧩 [W{worker_id}] Abandoned (CAPTCHA): {title} at {company} — {e.reason}")
            return "failed"

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

        if success:
            await save_job_status(url, "applied")
            print(f"  ✅ [W{worker_id}] Applied: {title} at {company}")
            return "applied"
        else:
            error_msg = failure_reason(result)
            await save_job_status(url, "failed", error_msg[:2000])
            print(f"  ❌ [W{worker_id}] Failed: {title} at {company} — {error_msg[:100]}")
            return "failed"

    except _captcha.CaptchaUnsolved as e:
        # Abandon this application and move on — never pause or wait.
        await save_job_status(url, "failed", f"CAPTCHA could not be solved ({e.challenge_type}): {e.reason}")
        print(f"  🧩 [W{worker_id}] Abandoned (CAPTCHA): {title} at {company} — {e.reason}")
        return "failed"

    except Exception as e:
        error_str = str(e)
        if "security token" in error_str.lower() or "expired" in error_str.lower():
            refresh_credentials()
            await save_job_status(url, "pending", "credentials_expired_retry")
            print(f"  🔑 [W{worker_id}] Credentials expired on: {title} at {company} — refreshed, will retry")
            return "retry"
        await save_job_status(url, "failed", error_str[:2000])
        print(f"  ❌ [W{worker_id}] Error: {title} at {company} — {error_str[:100]}")
        return "failed"
    finally:
        _running_agents.discard(agent)
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
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            break

        job_easy_apply = job.get("easy_apply", easy_apply) if job.get("easy_apply") is not None else easy_apply
        # Re-read the profile for each job, so edits saved during a long run
        # (a fixed address, new instructions) apply to the next application.
        profile = load_json(CANDIDATE_PROFILE, profile) or profile
        status = await apply_to_job(job, profile, qa, applied_labels, job_easy_apply, worker_id)
        stats[status] = stats.get(status, 0) + 1

        # Randomised pause so applications are not evenly spaced. Off by
        # default; only runs between jobs, never mid-application.
        if not (cancel_flag and cancel_flag.get("cancel_requested")):
            await config.maybe_human_delay("the next application")

        # On retry, put back in queue
        if status == "retry":
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
