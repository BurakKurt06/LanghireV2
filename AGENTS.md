# LangHireV2: working rules for AI agents (Claude, Codex)

LangHireV2 is a Windows desktop app (Tauri + React frontend, FastAPI + browser-use Python
backend) that collects job listings and fills in applications in a real browser.

## Repositories

- **This repo (private)** is the working base. All work happens here.
- **Public repo** `https://github.com/BurakKurt06/LanghireV2` gets updates only when the
  user explicitly requests publication or pushing/synchronizing both repositories.
  Before publishing: run the checks below and scan for personal data.
  Leave out internal review files (`REMEDIATION.md`, `CODEX_RESPONSE.md`,
  `CODEX_RESUME_CHECKPOINT.md`, `SETTINGS_REORG_PLAN.md`, `security/`).
- **Installer releases:** both READMEs link to the public repo's
  `releases/latest/download/LangHireV2-setup.exe`. A publish that changes the app raises the version
  (`package.json` + lockfile, `src-tauri/tauri.conf.json`, `backend/main.py`, `src/main.tsx`); after
  the public Integration Tests pass on that push, `build.yml` builds and publishes release
  `v<version>` automatically (README → Releasing).

## Hard rules

- Pushing (user's rule, 2026-10-31, clarified 2026-11-04): Claude may commit and push to this
  private repo (`langhireV2old`) without asking. Other agents: commit or push only when the
  user asks. Public pushes still require the explicit request above. Never force-push public.
- For Codex-authored work in either LangHireV2 repository, use the commit author
  `Codex <codex@openai.com>` (for example, `git commit --author="Codex <codex@openai.com>"`).
  When commits/pushes are authorized, finish the requested synchronization on `main` with
  a clean working tree and no pending PRs (user's instruction, 2026-11-04). Do not create a
  PR unless the user asks or repository protection requires one; finish required checks
  and authorized merges rather than leaving a manual merge waiting. Never bypass required
  checks. Preserve the original commit author when merging. This standing
  attribution instruction does not authorize a commit, push, merge, or public publication.
- Tests and probes must not sign in to the user's accounts, read their Gmail, submit real
  applications, make production POST requests, call paid AI providers, or overwrite personal
  data. Use temporary data folders, fake pages, fake accounts and fake AI responses.
- Never put personal details (name, email, phone, address, resume, real job searches) in the
  repo. Test data is always made up. `tmp/` and `output/` are git-ignored for this reason.
- Never type or store the user's passwords.
- Don't weaken guards or tests to make something pass. Don't lower the Python requirement or
  edit installed dependency files.
- Never call the app "secure" just because tests pass.
- English only: edit only `src/locales/en`.
- Prove fixes: reproduce a bug first (a failing test or a real local page), then fix it.
  Real-browser tests are opt-in with `LANGHIRE_REAL_BROWSER=1`.
- Keep scratch scripts and evidence inside the repo (for example under `scripts/`), not in
  temp folders.

## Product decisions (don't reverse without asking)

The user reversed several earlier decisions on 2026-11-10 ("I paste job links. The app opens
each link, handles CAPTCHAs, creates any account the hiring site needs, fills in my information
correctly and submits the application."). These are the current ones:

- **Full auto** (default) submits. **Trial run** stays an option: it goes through every page
  like Full auto (login, uploads, saves, Next, site scripts may send data) but never activates
  the agent's final Submit application control. No approval prompts. A possibly-sent
  application is never submitted again automatically.
- **Waiting for the user**: at most 5 minutes (a question nothing answers, an unsolved CAPTCHA,
  an unread verification code), then the run moves on; one wait per job (after one nobody
  answered, that job doesn't wait again); the job stays "Needs you" with the exact
  question saved, answerable on its card; the answer is remembered and the job is tried again
  on the next run.
- **Answers**: resume/profile/saved answers/memory first, then the AI, the user last. Ordinary
  questions get the editable **Default answers** (Q&A page; core/default_answers.py) and are never
  asked; every inferred answer is saved on the Q&A page; the user's own answers always win.
  Required consent/certifications are agreed to, optional marketing declined. Ask only for a long
  essay with nothing usable, a required salary when none is set, or a fact with no basis at all
  (e.g. the school before university); Start asks such known questions once, before the run, and
  an answer is never asked again (user, 2026-11-16). Smart guesses fill in whatever follows from
  the resume/profile/saved answers (formats, years of experience, list names), each saved on the
  Q&A page with what it was worked out from. Dates may be completed from known ones (missing day
  -> the 15th; only a year -> June 15, May 15 for a graduation); never invent experience, skills,
  credentials, or dates with no basis.
- **CAPTCHAs**: every CAPTCHA goes to 2Captcha once a key is saved, including invisible ones and
  ones after Submit (an invisible one, hCaptcha or reCAPTCHA v3, is first left to pass by itself; the solver takes over when
  the form is refused or a puzzle opens); on failure or without a key, the user solves it in the
  open browser (within the 5-minute wait).
- **Runs** (user, 2026-11-16): "already applied" / "application received" on the site itself =
  Applied, never retried. Only problems that might go away (page didn't load, timeout, crash) are
  retried. Two jobs on one site ending the same way skip that site's other jobs for the run
  (they stay ready). The ChatGPT usage meters in each reply decide whether the next job fits; jobs
  the limit cuts off go back to ready, never Failed; no reserve setting. A full plan pauses the run
  (user, 2026-11-20): the run panel counts down to the reset, the run goes on by itself then, and
  "Continue now" goes on at once (a free or early reset); cut-off jobs are tried again in the same run. A failed/Needs-you job
  keeps one picture of its last page (at most 500). Not wanted: one job per site at a time as a new
  rule (the existing one stays), stopping a job that repeats itself, skipping a link because it
  looks like an earlier application, a Windows notification, fewer screenshots per step.
- **Accounts**: wherever the job's link leads, sign in or create the account with the ONE
  "Password for job sites" from Settings; a site that rejects it for its rules gets characters
  added to the END (that version is saved for the site and listed in Settings). Verification
  codes/links come from Gmail through the app's own reader only.
- **The user steps in**: any click/typing/page change the agent didn't make stops the agent at
  once; it waits until the user is idle (about 25 s) or Resume, continues from the user's page and
  keeps every field the user changed. The user's own edits always win over the agent's answers.
- **Instructions for the AI** come before built-in rules and default answers, unless one would
  clearly break the application or a protection; the conflict is noted in the job's log.
- **Protections that stay**: the agent never opens Gmail; account details go only to sites the
  job's link led to (never Gmail, job boards, private or local addresses); Trial never presses the
  final Submit; a possibly-sent application is never submitted twice automatically; tests never
  touch real accounts, never read Gmail and never send real applications.
- **Application browser**: connects directly (no request gate, no proxy, no
  "--host-resolver-rules" warning flag); a PAC rule script keeps Gmail and private addresses out.
  The request gate serves collection only. No page or step may hang: 5 minutes blank or without
  progress is a failure, retried up to "Max failures per job", then "The page didn't load".
- **Collection**: Quick (default; about one AI request per results page) and Detailed (one
  per job). Built-in boards may make any public HTTPS GET; every write is refused; private
  addresses and Gmail are always blocked. Imported plugins keep exact-host limits.
- Notes-driven AI matching. Jobs below the minimum pay aren't collected; with a minimum set,
  jobs without pay need a higher match (halfway from the bar to 10).
- AI calls are costly (with the ChatGPT sign-in provider each AI step counts toward the plan's
  usage limits): avoid extra AI calls.

## Setup

```bash
uv sync --locked          # Python 3.13 from uv.lock (never --upgrade)
npm ci
```

## Checks

```bash
.venv/Scripts/python -B -m pytest tests/backend -p no:cacheprovider   # Linux: .venv/bin/python
LANGHIRE_REAL_BROWSER=1 .venv/Scripts/python -B -m pytest tests/backend -k "collect or trial_action_mode or user_steps_in or whole_application or second_run or fourth_run or posting_reader"
npx vitest run && npx tsc -b --noEmit && npx eslint .
```

pyproject already adds `-q`; a second `-q` hides pytest's final pass/fail count.

`uv run python scripts/run-ci-locally.py` runs every step of the Windows CI job (`test.yml`) on this
computer, in order (useful where hosted Actions minutes aren't available). Full output goes to
`tmp/ci-local.log`.

The Windows installer (`scripts/build-windows.ps1`) only builds on Windows. In the cloud, use
the GitHub Actions `build.yml` workflow. Packaged checks: `scripts/smoke-backend.py` and
`scripts/integration-test.py --binary <absolute sidecar path>`.

In the Linux cloud container: four Windows-only backend tests fail (DPAPI encryption, and resume
paths under `/tmp`, which non-Windows allows); `test.yml` on Windows is the real check. Chromium
won't start as root, so run browser tests as a regular user with a clean environment, e.g.
`su - tester -c 'cd <repo> && PLAYWRIGHT_BROWSERS_PATH=/opt/pw-browsers LANGHIRE_REAL_BROWSER=1 .venv/bin/python -B -m pytest ...'`
(create the user with `useradd -m tester`).

## Status and history

`REMEDIATION.md` (top section is current) and `CODEX_RESPONSE.md` (later sections supersede
earlier ones) record every review round. `TESTING.md` has the full testing guide.
