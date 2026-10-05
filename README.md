<div align="center">
  <img src="src-tauri/icons/icon.png" alt="LangHireV2" width="120" />
</div>

<h1 align="center">LangHireV2</h1>

<p align="center">
  AI-powered job application automation for Windows.<br />
  An independent fork of <a href="https://github.com/jaimaann/LangHire">LangHire</a> (MIT).
</p>

---

## What this is

LangHireV2 drives a real Chrome window with an LLM agent to collect job listings
and fill in applications. It is a **personal fork**, not a distribution:

- **Windows x64 only.** The installer is an NSIS `.exe`; macOS and Linux targets
  were removed from the bundle config and CI. Cross-platform code paths inside
  the app were deliberately left intact.
- **Not synced with upstream.** Changes from the original project are not merged
  in automatically; picking one up means doing it by hand.
- **English only.** The other 12 languages, the language picker and the
  translated READMEs were removed. UI strings still live in
  `src/locales/en/*.json` and are read through `t()`.

## Install

Build it yourself (below) and run:

```
src-tauri/target/release/bundle/nsis/LangHireV2_1.0.0_x64-setup.exe
```

You do **not** need Python installed — it is bundled into the backend sidecar.
On first launch the app downloads Chromium (~150 MB) automatically.

## First-run setup

1. **LLM API key** — required; nothing works without it. OpenRouter is the
   default provider; OpenAI, Anthropic, Gemini, Ollama and Bedrock also work.

   A **ChatGPT Plus/Pro plan** works without an API key: pick "ChatGPT plan" and
   click Sign in with ChatGPT. It uses the same sign-in and backend as OpenAI's
   Codex app, so each AI step counts toward the plan's Codex usage limit instead
   of being billed per token. That backend isn't a published API and could
   change. The sign-in is stored in `chatgpt_auth.json` in the app data folder,
   separate from the Codex CLI's own login.
2. **Resume** — Settings → Resume, a PDF path.
3. **Profile** — name, contact details, work authorization, target job titles
   and locations. Collection uses the target titles as its search terms.
4. **Logins** — LinkedIn and Gmail have buttons in the app; log in there before
   a run. Other sites (Handshake, Intern Insider, …) are signed into in the
   agent's window: when a sign-in page appears the agent waits for you, up to
   2 minutes, then stops that run with a "sign-in required" message. Nothing
   checks logins up front, because every check is a paid LLM request.

Gmail matters beyond email: when a site sends a verification code, the agent
reads it off the Gmail page in its own browser. There is no IMAP or API
integration. Gmail is only opened when a code is actually needed.

## Getting jobs in

**Paste or upload links** — Jobs → Collect → *Paste Links*. One URL per line,
or upload a `.txt`/`.csv`. Duplicates and invalid or internal URLs are skipped
and counted. Pasted jobs are treated as external applications.

**Collect from job boards** — pick one or more sources and a job title. Each
title is collected from every selected source in turn.

| Source | Availability | Login |
|---|---|---|
| LinkedIn | everywhere | yes |
| Indeed, Glassdoor | everywhere | yes |
| Handshake | everywhere (one domain for all universities) | yes, via your school's SSO |
| Intern Insider | US / CA | yes |
| ZipRecruiter | US / GB / CA | yes |
| SEEK (AU/NZ), Reed (GB), StepStone (DE), Naukri (IN) | region-locked | yes |

Sources are declarative YAML in `backend/sources/plugins/`. Each supplies its
own search URL, filters and navigation prompt. Generic safety rules — prompt
injection defence, honeypot avoidance, language filter, duplicate skipping —
are appended in code, so no plugin can ship without them.

## Settings

| Setting | Default | What it does |
|---|---|---|
| Job board collection | **on** | Off disables collecting from job boards, in the app and the CLI. Pasting links still works. |
| Stealth mode | off | Hides automation signals (`navigator.webdriver`, plugin list, WebGL fingerprint) via CDP injection, and drops the automation launch flags. |
| Human-like delays | off | Randomised pause (default 5–15s) between applications, between collection sources, and between worker launches. |
| Solve CAPTCHAs with 2Captcha | off | Sends CAPTCHAs to 2Captcha, max 2 attempts. Needs your API key. |
| Account email / password | empty | Used to create and log into accounts on employer sites. Passed to the agent as secret placeholders, never as prompt text. The account email is also used on forms when the Profile email isn't a valid address. |
| Browser | Automatic (Chrome) | Which installed Chromium browser (Chrome, Edge, Brave, Vivaldi, or a custom path) the agent drives. Each gets its own automation profile. |
| Max failures per job | 8 | Consecutive failed steps before the agent gives up on a job. |

All defaults reproduce the app's behaviour with none of the new features on.

### CAPTCHA behaviour

A visible challenge (reCAPTCHA v2, hCaptcha, Turnstile, image) that cannot be
solved ends that application: it is marked **failed** with the reason, and the
next job starts. Nothing ever waits for you.

reCAPTCHA **v3 / Enterprise is deliberately not treated as blocking** — it is
invisible and scores the session in the background rather than gating
submission, and it appears on a large share of ordinary career sites.
Abandoning on its mere presence would throw away applications that would
otherwise succeed. It is still solved when the solver is on.

Failed jobs stay in the Pending list with their error and can be re-run.

## Building from source

Requires Node 20+, Rust (MSVC toolchain), Visual Studio Build Tools with the
**Desktop development with C++** workload, and `uv`.

```
npm install
uv sync
powershell -ExecutionPolicy Bypass -File scripts/build-backend.ps1   # sidecar first
npm run tauri build
```

The sidecar must be built before the Tauri bundle — `tauri.conf.json` declares
it as an `externalBin` and the build fails without it.

Tests:

```
npm run test:coverage
uv run pytest tests/
```

## Known limitations

- **Handshake and Intern Insider collection is unproven.** Those plugins were
  written from the sites' public pages; their logged-in layouts were never
  observed and the navigation steps may need adjusting.
- **2Captcha has never been run against the live service.** The decision logic
  is unit-tested against a mocked SDK; no real solve has happened.
- **Secrets are stored in plaintext.** The account password and the 2Captcha
  API key live in `%APPDATA%\langhirev2\settings.json` as plain JSON. The code
  attempts a restrictive `chmod`, which does almost nothing on Windows.
- **LinkedIn collection costs one request per job.** The LinkedIn plugin reads
  each job's URL from the address bar after clicking it, so collecting 25 jobs
  takes 25+ LLM requests; on Gemini's free tier (20 requests/day per model)
  collect in small batches.

## Automation and terms of service

Stealth mode and automated CAPTCHA solving exist to defeat bot detection. Most
major job boards prohibit automated access in their terms. Using these features
is a deliberate choice with real consequences for the accounts involved — that
is the operator's call to make, knowingly.

## Credit and licence

Built on [LangHire](https://github.com/jaimaann/LangHire) by jaimaann, MIT
licensed. The original `LICENSE` and copyright notice are retained unchanged.
