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

- **Trial run** (default): goes through every page like Full Auto (login, uploads, saves,
  Next, site scripts may send data) but never activates the agent's final Submit
  application control. No approval prompts. **Full auto** submits.
- The user's own edits during a pause or review win over the agent's answers.
- **Collection**: Quick (default; about one AI request per results page) and Detailed (one
  per job). Built-in boards may make any public HTTPS GET; every write is refused; private
  addresses and Gmail are always blocked. Imported plugins keep exact-host limits.
- Notes-driven AI matching. Jobs below the minimum pay aren't collected; with a minimum set,
  jobs without pay need a higher match (halfway from the bar to 10).
- The user is cost-sensitive (Gemini free tier, about 20 requests per day per model): avoid
  extra AI calls.

## Setup

```bash
uv sync --locked          # Python 3.13 from uv.lock (never --upgrade)
npm ci
```

## Checks

```bash
.venv/Scripts/python -B -m pytest tests/backend -p no:cacheprovider   # Linux: .venv/bin/python
LANGHIRE_REAL_BROWSER=1 .venv/Scripts/python -B -m pytest tests/backend -k "collect or trial_action_mode"
npx vitest run && npx tsc -b --noEmit && npx eslint .
```

pyproject already adds `-q`; a second `-q` hides pytest's final pass/fail count.

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
