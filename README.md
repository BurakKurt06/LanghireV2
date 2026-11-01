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
4. **Logins** — Select your boards in Jobs → Collector, then use their **Sign in**
   buttons before collecting. All ten built-in boards open a normal browser window
   for password login, SSO or verification. Close that window when finished; the
   collector reuses its saved session in the same app-selected browser profile.
   If a session expires, sign in again here and retry. Gmail's separate button is
   in Settings → Accounts. Sign-in and automation never use the profile at the
   same time. Imported plugins cannot launch unrestricted sign-in windows.

Gmail verification is read by app code in a separate browser profile. The application
agent cannot open Gmail; it receives only a checked code or link. Optional Inbox
checks classify job emails, but those classifications are not verified receipts.

## Getting jobs in

**Paste or upload links** — Jobs → Collect → *Paste Links*. One URL per line,
or upload a `.txt`/`.csv`. Duplicates and invalid or internal URLs are skipped
and counted. Pasted jobs are treated as external applications.

**Collect from job boards** — pick one or more sources and a job title. Each
title is collected from every selected source in turn.

- **Quick** (default) reads each results page as a whole: job links and listing text
  come from the page, and one AI request labels and advises on the page's listings
  (about 25 jobs per request). Links a person couldn't see or click are ignored.
  ZipRecruiter's results have no job links, so it is always collected one job at a time.
- **Detailed** has the agent open each job in turn; a job can require several AI steps.
  Quick also reads nested cross-origin frames and closed shadow roots through Chrome's
  DOM protocol, checking visibility through ancestor frames and finishing embedded scrolling first.
- **Glassdoor** ignores a typed place and shows the whole US, so the app asks Glassdoor
  for its own ID for your first target location ("Toronto, ON" is Ontario, not Ohio).
  If Glassdoor doesn't know the place, the log says its results may come from anywhere.
- **Profile → Job search** sets what to look for: job type, work arrangement,
  experience, posting age, minimum pay, a lowest match score, notes for the AI, and
  what to leave out (employers, title words, phrases, staffing agencies, employers
  applied to recently). The Collect page can change any of them for one search, and
  searches can be saved. Boards take what they can in their own search; every listing
  is also checked by the app.
- **Pay** is read from the listing, the posting data or the description, and hourly,
  weekly or monthly pay gets a yearly estimate (40 hours a week, or your part-time
  hours). Jobs below your minimum aren't collected. With a minimum set, a job that
  doesn't list pay needs a higher match: halfway from your bar to 10.
  Different or missing currency, a guessed pay period and a board salary estimate also count as unknown pay. With that raised bar
  enabled, a missing match score cannot qualify; check the Left out list.
- Left-out listings are kept with the reason (Collect → *Left out*), and any of them
  can be added after all. Review & Apply can sort by match or pay.

Quick normally makes one classifier invocation per nonempty page; a fallback
can add another. Provider retries and token usage are not a hard budget.
Collection uses GET/HEAD without bodies. Built-in boards can load documents, runtime chunks,
images, styles, fonts and GET APIs on configured board/CDN hosts. Other destinations need exact
declared resources; imported plugins keep the stricter exact-capability policy and network sealing.
Write methods, upload bodies and beacons are refused without asking. Private addresses, Gmail,
unguarded connections, workers and WebSockets remain blocked by the broker. Collection reuses an
existing signed-in session; use the app's separate browser login when that session expires.
Board API/session headers are scoped to configured read hosts, referrers are removed and only
startup/server cookie values are forwarded. Automatic board GETs can have server-side effects;
this compatibility policy is not an assertion that arbitrary scripts cannot send data to their board.
When a failed bootstrap hides public server-rendered listings, Quick can reload the same search
with scripts disabled, retaining CSS visibility, clipping and overlay checks.

Automatic match filtering uses AI relevance from your notes, profile and desired role, including
related titles such as BI Analyst for Data Analyst. Explicit exclusions, source facts and pay
comparisons are checked separately. Without stated/comparable pay, a minimum-pay search raises
the required match halfway from your match threshold to10. AI relevance is fallible; explicit
exclusions are the way to enforce requirements without relying on a model. Guessed or estimated pay
is labelled approximate. Source text and structured posting data remain statements by the publisher,
not independent proof of employer identity or compensation.

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
| Browser | Automatic (bundled Chromium) | Which Chromium browser (bundled Chromium, Chrome, Edge, Brave, Vivaldi, or a custom path) the agent drives. Login and automation use the same executable and profile; each explicit browser choice has its own automation profile. |
| Max failures per job | 8 | Consecutive failed steps before the agent gives up on a job. |

There are two application modes. **Trial run** (the default) works on the live site:
login, assets, redirects, uploads, saves and Next run automatically like Full auto.
The agent stops before pressing the final Submit application control, without approval.
The site can still send data or submit during an intermediate step or its own scripts;
Trial does not refuse a request merely because its ATS operation is unknown.
Completed Trial leaves the job pending for the user to check.
**Full auto** applies on the live site, final Submit included. Collection keeps its separate policy.

Previously reviewed ATS contracts remain available for explicit protocol diagnostics,
including Workday, Greenhouse, Lever, Ashby, Workable and Oracle Candidate Experience.
Those diagnostics are stricter than normal Trial; they do not limit normal login,
Next or saves. This is not a claim that every employer customization works.
Within the optional strict diagnostics, recognizing an ATS name alone does not
authorize its server writes; normal Trial has no such ATS write allowlist.
Candidate sign-in also supports observed iCIMS email gates and its delegated Auth0
login/verification forms, Taleo's JSF login, and SAP SuccessFactors' candidate login.
Candidate preparation also covers Teamtailor's signed file uploads, Breezy's uploads
and upload-bound résumé parser, Jobvite's resource uploads and Personio's documents.
BrassRing supports its observed candidate login and metadata-bound non-review
SaveAndNextPage requests; the strict diagnostic refuses its Submit flag.
Delegated credentials stay tied to the original employer; social identity providers
and unreviewed application dispatchers do not inherit those login permissions.

### CAPTCHA behaviour

Both modes use the configured CAPTCHA handling and scoped email verification. An unsolved
challenge ends the job for later attention, without a Trial approval wait. A run
that may already have submitted retains its uncertain outcome and is not retried
automatically. Invisible reCAPTCHA scores alone are not treated as proof that a
challenge is blocking the application. The live 2Captcha service remains untested.

## Building from source

Requires Node 22.22+ (also supports the website build), Rust (MSVC toolchain), Visual Studio Build Tools with the
**Desktop development with C++** workload, and `uv`.

```
npm ci
uv sync --locked
powershell -ExecutionPolicy Bypass -File scripts/build-backend.ps1   # sidecar first
npm run tauri build
```

The sidecar must be built before the Tauri bundle — `tauri.conf.json` declares
it as an `externalBin` and the build fails without it.

Tests (see TESTING.md for the isolated environment and what the safety net blocks):

```
npm run test:coverage
uv run pytest tests/
```

## Safety boundaries and remaining limits

- **Trial stops the agent's final-submit action.** Login, saves, Next and site requests use
  the same transport as Full auto, without an approval prompt or an ATS write allowlist.
  The action guard refuses recognized final controls even after an old approval, and refuses
  arbitrary agent scripts/unreadable targets that could bypass that check. Site scripts and
  intermediate steps may send data or submit automatically; this is deliberately allowed.
  Strict request contracts can be explicitly enabled for protocol diagnostics, but are not
  the normal application mode. This is not a guarantee that a website can never submit.
- **Full auto submits on its own.** No questions during the run. A page saying the application
  was received marks it Applied. Trial never marks an application Applied.
- **Browser egress is denied.** Application and collection browsers use a deny-all
  local SOCKS listener, disabled browser DNS/prefetch/QUIC and secured WebRTC
  preferences. Every request is fetched by the backend through the checked-IP
  transport and handed to the browser. Private destinations and Gmail stay blocked;
  one unreachable or refused resource fails on its own. Other
  processes and arbitrary managed/custom browser policies are outside this
  boundary; this is not an OS-wide firewall.
- **Unsupported features stop safely.** Workers, WebSockets and non-HTTP transports
  are not supported for applications. Ambiguous or unreadable uploads are refused.
  Uploads must match explicitly available local files. Limits include 25 MB uploads
  and 15 MB responses. Some employer flows will need manual completion.
- **Credentials go only to the employer's site.** The job's own site, or its applicant-system
  tenant, gets account details without a question in either mode; any other site gets nothing.
  Trial never opens a credential approval prompt.
  New passwords are scoped to the exact origin and tenant. Reuse of an older
  saved password remains restricted to its original site; the old vault entry is preserved.
  Inputs and form destinations must match, and a job's credentials stay pinned to
  its first approved origin. The earlier shared password remains restricted to
  the original job origin/tenant.
- **Uncertain outcomes are never retried blindly.** A run that may have submitted but didn't
  end on a confirmation page keeps its mark; retrying it requires acknowledging the duplicate risk.
- **Collection uses the same browser network.** Imported plugins get only their exact configured
  HTTPS hosts, with no domain-wide wildcard. Description reads use anonymous public
  HTTP rather than another signed-in browser agent. Console runs require an
  interactive terminal for approvals; non-interactive runs refuse to proceed.
- **Dependencies are checked, with explicit exceptions.** The pinned Python and
  both npm dependency trees have been remediated. Two narrow upstream Rust cases
  remain documented in `security/dependency-exceptions.json`: an unmaintained
  hashing crate and an old RNG used only while building, not at runtime. The audit
  checks active Windows features and rejects new or expired exceptions.
- **Live services remain unvalidated.** Tests use local pages/listeners, isolated
  profiles and fake LLM responses. Real employer compatibility, Gmail cookie-copy
  sign-in, Handshake/Intern Insider layouts and 2Captcha have not been exercised.
  Do not infer universal compatibility from passing local tests.
- **Review generated resumes and answers.** Tailoring is optional and creates a
  separate PDF with its own layout; original styling is not guaranteed. Preview
  the output. The app cannot guarantee an LLM's semantic correctness or that an
  employer posting is legitimate; review your answers and approved destinations.

## Automation and terms of service

Stealth mode and automated CAPTCHA solving exist to defeat bot detection. Most
major job boards prohibit automated access in their terms. Using these features
is a deliberate choice with real consequences for the accounts involved — that
is the operator's call to make, knowingly.

## Credit and licence

Built on [LangHire](https://github.com/jaimaann/LangHire) by jaimaann, MIT
licensed. The original `LICENSE` and copyright notice are retained unchanged.
