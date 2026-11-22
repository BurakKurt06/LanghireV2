<div align="center">
  <img src="src-tauri/icons/icon.png" alt="LangHireV2" width="120" />
</div>

<h1 align="center">LangHireV2</h1>

<p align="center">
  AI-powered job application automation for Windows.<br />
  An independent fork of <a href="https://github.com/jaimaann/LangHire">LangHire</a> (MIT).
</p>

<p align="center">
  <a href="https://github.com/BurakKurt06/LanghireV2/releases/latest/download/LangHireV2-setup.exe"><b>⬇ Download LangHireV2 for Windows (LangHireV2-setup.exe)</b></a><br />
  <sub>Always the newest release · <a href="https://github.com/BurakKurt06/LanghireV2/releases/latest">release notes</a> · see <a href="#install">Install</a> before running it</sub>
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

Download **[LangHireV2-setup.exe](https://github.com/BurakKurt06/LanghireV2/releases/latest/download/LangHireV2-setup.exe)** (Windows 10/11, x64). The link always gives the newest
release; its notes are on the [Releases page](https://github.com/BurakKurt06/LanghireV2/releases/latest). Installing a newer version over an older one
keeps your settings, profile and jobs. You can also build the installer yourself (see
[Building from source](#building-from-source)); it lands in `src-tauri/target/release/bundle/nsis/`.

The installer isn't code-signed. Before running a downloaded copy, right-click the `.exe` →
**Properties**, tick **Unblock** at the bottom of the General tab and click **OK**. Windows
SmartScreen checks files marked as downloaded, and Unblock removes that mark, so the warning
normally doesn't appear. This isn't guaranteed: Windows 11's Smart App Control, where it is
turned on, can still stop an unsigned installer. An installer you built yourself isn't marked
and has no Unblock box. Signing wouldn't remove the warning at once either: a new certificate
has to build up SmartScreen reputation first.

You do **not** need Python installed — it is bundled into the backend sidecar.
On first launch the app downloads Chromium (~150 MB) automatically.

## First-run setup

1. **AI model** — Settings → AI model; required, nothing works without it. OpenRouter is the
   default provider; OpenAI, Anthropic, Gemini, Ollama and Bedrock also work.

   A **ChatGPT Plus/Pro plan** works without an API key: pick "ChatGPT plan" and
   click Sign in with ChatGPT. It uses the same sign-in and backend as OpenAI's
   Codex app, so each AI step counts toward the plan's Codex usage limit instead
   of being billed per token. That backend isn't a published API and could
   change. The sign-in is stored in `chatgpt_auth.json` in the app data folder,
   separate from the Codex CLI's own login.
2. **Resume** — Profile → Resume, a PDF path (the same file as Settings → Applications).
3. **Profile** — name, contact details and work authorization, on the Profile page.
4. **What to look for** — Settings → Job search: target job titles and locations, the search
   area and the default search. Collection uses the target titles as its search terms. Your
   notes for the AI (what you want and don't want) are in Settings → Applications.
5. **Logins** — Select your boards in Jobs → Collect, then use their **Sign in**
   buttons before collecting. All ten built-in boards open a normal browser window
   for password login, SSO or verification. Close that window when finished; the
   collector reuses its saved session in the same app-selected browser profile.
   If a session expires, sign in again here and retry. Gmail's separate button is
   in Settings → Accounts & email. Sign-in and automation never use the profile at the
   same time. Imported plugins cannot launch unrestricted sign-in windows.

6. **Accounts on employer sites** — Settings → Accounts & email: the account email and one
   **Password for job sites**. The agent signs in wherever the job's link leads (Workday,
   SuccessFactors, iCIMS, Taleo, Greenhouse, ...), or creates the account the site needs, with
   that password. When a site's rules reject it, the app adds characters to its end and keeps
   that version for that site; Settings → Accounts & email lists those sites.

Gmail verification is read by app code in a separate browser profile. The application
agent cannot open Gmail; it receives only a checked code or link. Optional Inbox
checks classify job emails, but those classifications are not verified receipts.

## Getting jobs in

**Paste or upload links** — Jobs → Collect → *Paste Links*. One URL per line,
or upload a `.txt`/`.csv`. Duplicates and invalid or internal URLs are skipped
and counted. Pasted jobs are treated as external applications and wait as **Ready to
apply**: nothing starts until you press **Start**, which applies to every ready job, one after another. Their real title and employer are read in
the background, first with a plain page read and, for pages that are built in the browser
(Oracle's Candidate Experience sites, for example), with one read in a temporary headless
browser profile. That second read only fills in what is still missing. A posting whose
title says it is done ("FILLED: ...", "[Closed] ...") is marked closed. Some pages never
name their employer; the agent names those jobs when it opens them.

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
- **Settings → Job search** sets what to look for: job type, work arrangement,
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
| Solve CAPTCHAs with 2Captcha | **on** once a key is saved | Sends CAPTCHAs to 2Captcha, at most 2 attempts per application. The key field is in Settings → Applications. When it can't solve one, or no key is saved, the run waits up to 5 minutes for you to solve it in the open browser window. |
| Account email / Password for job sites | empty | One password for every employer site the job leads to: the agent signs in, or creates the account. A site whose rules reject it gets the password with characters added to its end, kept for that site. Passed to the agent as secret placeholders, never as prompt text, and typed only on sites the job's link led to (never Gmail, a job board or a local address). The account email is also used on forms when the Profile email isn't a valid address. |
| Browser | Automatic (bundled Chromium) | Which Chromium browser (bundled Chromium, Chrome, Edge, Brave, Vivaldi, or a custom path) the agent drives. Login and automation use the same executable and profile; each explicit browser choice has its own automation profile. |
| Max failures per job | 8 | A page that stays blank, stops responding or doesn't change for 5 minutes counts as a failure: the app reloads it and tries again, up to this many times, then skips the job with "The page didn't load". Consecutive failed agent steps count too. |

Settings has five categories: **AI model**, **Job search** (with the job sources),
**Applications** (resume, instructions for the AI, CAPTCHAs, cover letters, failures),
**Accounts & email**, and **App & browser** (browser, stealth, delays, theme, usage analytics,
then **Logs** and **Send feedback**). Links to the earlier categories open the merged one. Each
job card has **View log** for that job's own log. The sidebar reads Dashboard, Profile, Jobs,
Inbox, Memory, Q&A, Guide, Settings; Ctrl+1 to Ctrl+8 open them in that order, Ctrl+L opens the
logs, Ctrl+, opens Settings, and F11 switches real fullscreen on and off (Esc leaves it).

There are two application modes. **Full auto** (the default) applies on the live site,
final Submit included. **Trial run** works on the live site the same way (login, uploads,
saves and Next run automatically) but the agent never presses the final Submit application
control; the job stays ready for you to check. In either mode the site can still send data
during an intermediate step or from its own scripts. Ambiguous submit labels such as Done,
Confirm or Submit resume count as possible submit attempts. An attempt that didn't end on a
confirmation page while the flow went on is cleared; one that may have gone out is kept, and
such an application is never submitted again automatically.

**When the agent needs you.** Ordinary questions (authorized to work, referred by someone,
how did you hear about this job, ...) are answered without asking, from your profile, resume
and saved answers, then from the **Default answers** on the Q&A page, which you can change;
your own answers always win, and every inferred answer is saved on the Q&A page. The run asks
only when nothing can answer (a long essay with nothing usable, a required salary when none
is saved), a CAPTCHA can't be solved, or a verification code can't be read. The run panel
shows the question whole (form section, full text, choices) and waits at most 5 minutes; then
the job stays **Needs you** with that question, answerable on its card, and is tried again on
the next run. Click or type in the agent's browser at any moment and the agent stops at once,
waits until you've been idle for about 25 seconds (or you press Resume), then goes on from
your page and keeps every field you changed. Your Instructions for the AI come before the
built-in rules and default answers, except where one would clearly break the application or a
protection below; a conflict is noted in the job's log.

The per-site request contracts from the earlier request gate (Workday, Greenhouse, Lever,
Ashby, Workable, Oracle Candidate Experience, ...) stay in the code with their tests. The gate
now serves collection only, so normal applications don't use them.
Candidate sign-in also supports observed iCIMS email gates and its delegated Auth0
login/verification forms, Taleo's JSF login, and SAP SuccessFactors' candidate login.
Candidate preparation also covers Teamtailor's signed file uploads, Breezy's uploads
and upload-bound résumé parser, Jobvite's resource uploads and Personio's documents.
BrassRing supports its observed candidate login and metadata-bound non-review
SaveAndNextPage requests; the strict diagnostic refuses its Submit flag.
Delegated credentials stay tied to the original employer; social identity providers
and unreviewed application dispatchers do not inherit those login permissions.

### CAPTCHA behaviour

Both modes use the same CAPTCHA handling and email verification. With a 2Captcha key saved,
a challenge is sent to 2Captcha (at most 2 attempts per application); a challenge that is
already answered is left alone. When it can't be solved, or no key is saved, the run waits up
to 5 minutes for you to solve it in the open browser window, then keeps the job as Needs you
and moves on. A run that may already have submitted keeps that mark and is not retried
automatically. Invisible reCAPTCHA scores alone are not treated as proof that a challenge is
blocking the application. The live 2Captcha service remains untested.

## Building from source

Requires Node 22.22+ (also supports the website build), Rust (MSVC toolchain), Visual Studio Build Tools with the
**Desktop development with C++** workload, and `uv`.

```
powershell -ExecutionPolicy Bypass -File scripts/build-windows.ps1
```

That installs the locked dependencies, builds the backend sidecar and then the installer. By hand:

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

The whole Windows CI job (`.github/workflows/test.yml`, real-browser tests and the built backend
included) also runs on your own computer, for free, step by step and in the same order:

```
uv run python scripts/run-ci-locally.py
```

### Releasing

The download link above always points to the newest release of the public repository
([BurakKurt06/LanghireV2](https://github.com/BurakKurt06/LanghireV2)). To ship a new installer, raise
the version in `package.json` (and its lockfile) and `src-tauri/tauri.conf.json` (and the matching
strings in `backend/main.py` and `src/main.tsx`), then push it to the public repository's `main`.
When the **Integration Tests** run for that push passes, **Build & Release** builds the installer
from that exact commit and publishes release `v<version>` with two copies of the same installer:
`LangHireV2_<version>_x64-setup.exe` and `LangHireV2-setup.exe` (the fixed name the link uses).
A push that keeps the version builds nothing, and nothing is released from a commit whose tests failed.

You can also run **Build & Release** by hand from the Actions page: it only builds (the installer is
a 7-day download on the run's page), or with **release** ticked it publishes `v<version>` or
replaces that release's files. Pushing a `v<version>` tag publishes that release too.

## Safety boundaries and remaining limits

- **Trial never presses the final Submit.** Login, saves, Next and site requests work as in
  Full auto, without approval prompts. The action guard refuses recognized final controls, and
  refuses agent scripts or unreadable targets that could get around that check. Site scripts and
  intermediate steps may send data or submit by themselves; this is deliberately allowed. This
  is not a guarantee that a website can never submit.
- **Full auto submits on its own.** It asks you only for what nothing else can answer, and waits
  at most 5 minutes. A page saying the application was received marks it Applied. Trial never
  marks an application Applied.
- **Application browsers connect directly, with a rule script.** Applications use the browser as
  it connected before the request gate (October 2026, when applications went through): no proxy
  in between, so new tabs, workers, WebSockets and service workers work. A rule script in the
  launch flags (no proxy server, no warning bar) keeps Gmail, private addresses and local names
  unreachable, and bare IP addresses aren't opened. This computer itself (localhost) is not
  covered by the script; account details are never typed on local addresses. Collection keeps
  the stricter request gate: a deny-all local SOCKS listener, every request fetched by the
  backend through the checked-IP transport. Neither is an OS-wide firewall.
- **No page or step can hang.** A page that stays blank, stops responding or doesn't change for
  5 minutes counts as a failure and is retried up to "Max failures per job", then skipped with
  "The page didn't load". Uploads must match explicitly available local files; ambiguous or
  unreadable uploads are refused. Some employer flows will need manual completion.
- **Account details go only where the job's link led.** Your account email and job-site
  password are typed only on HTTPS sites that the job's link led to (redirects and Apply buttons
  included), never on Gmail, a job board, a private or local address, or a site reached some
  other way. The agent never opens Gmail; the app's own code reads verification codes and
  links, from emails tied to that application.
- **Uncertain outcomes are never retried blindly.** A run that may have submitted but didn't
  end on a confirmation page keeps its mark; retrying it requires acknowledging the duplicate risk.
- **Collection keeps exact limits.** Imported plugins get only their exact configured HTTPS
  hosts, with no domain-wide wildcard. Description reads use anonymous public HTTP rather than
  another signed-in browser agent. Console runs require an interactive terminal for
  approvals; non-interactive runs refuse to proceed.
- **Dependencies are checked, with explicit exceptions.** The pinned Python and
  both npm dependency trees have been remediated. Two narrow upstream Rust cases
  remain documented in `security/dependency-exceptions.json`: an unmaintained
  hashing crate and an old RNG used only while building, not at runtime. The audit
  checks active Windows features and rejects new or expired exceptions.
- **Live services remain unvalidated.** Tests use local pages/listeners, isolated
  profiles and fake LLM responses (real-browser tests run whole applications on local fake
  hiring sites). Real employer compatibility, Gmail cookie-copy sign-in, Handshake/Intern
  Insider layouts and 2Captcha have not been exercised by the tests. Do not infer universal
  compatibility from passing local tests.
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
