<div align="center">
  <img src="src-tauri/icons/icon.png" alt="LangHireV2" width="120" />
</div>

<h1 align="center">LangHireV2</h1>

<p align="center">
  AI-powered job application automation for Windows.<br />
  A private, independent fork of <a href="https://github.com/jaimaann/LangHire">LangHire</a> (MIT).
</p>

---

## What this is

LangHireV2 drives a real Chrome window with an LLM agent to collect job listings
and fill in applications. It is a **personal fork**, not a distribution:

- **Windows x64 only.** The installer is an NSIS `.exe`; macOS and Linux targets
  were removed from the bundle config and CI. Cross-platform code paths inside
  the app were deliberately left intact.
- **Not connected to upstream.** There is no `origin` remote and no merge path
  configured. Picking up a future upstream change means doing it by hand.
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
2. **Resume** — Settings → Resume, a PDF path.
3. **Profile** — name, contact details, work authorization, target job titles
   and locations. Collection uses the target titles as its search terms.
4. **Logins** — LinkedIn and Gmail have buttons in the app. Handshake and
   Intern Insider are signed into manually when the agent pauses for you (it
   waits up to 5 minutes, once per run).

Gmail matters beyond email: the agent reads OTP and verification codes off the
Gmail page in its own browser. There is no IMAP or API integration.

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
| Account email / password | empty | Used to create and log into accounts on employer sites. Passed to the agent as secret placeholders, never as prompt text. |

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
- **ESLint scans `.venv`.** Most of the reported errors come from Playwright's
  bundled type definitions, not from this codebase.

## Automation and terms of service

Stealth mode and automated CAPTCHA solving exist to defeat bot detection. Most
major job boards prohibit automated access in their terms. Using these features
is a deliberate choice with real consequences for the accounts involved — that
is the operator's call to make, knowingly.

## Credit and licence

Built on [LangHire](https://github.com/jaimaann/LangHire) by jaimaann, MIT
licensed. The original `LICENSE` and copyright notice are retained unchanged.
