## Current Trial definition — user clarification, 2026-10-26

Normal application Trial uses the same network behavior as Full Auto: login,
saves, Next, unknown ATS commands and site scripts can send data or submit. Only
the agent's recognized final-submit action is blocked. No approval prompt is
added. Common private-network/Gmail, checked-IP proxy, scoped credentials and
file protections remain. Collection keeps its separate unchanged policy.

The historical strict ATS contract tests below now explicitly use
`strict_trial_protocols=True` in their diagnostic harness. That internal option
is **not enabled by application runs**. Their no-send assertions are retained,
but are not advertised as the normal Trial guarantee. Normal production defaults
are tested in `test_trial_action_mode.py` and `test_trial_action_mode_live.py`.
The latter exercises local native iCIMS/SAP/Taleo-shaped forms, actual HTTP POSTs,
login/profile/Next redirects, final-button refusal with Full Auto positive controls,
and the explicitly allowed site-script request. No real accounts or public writes.
The stricter private-ATS protocol research limitations recorded below are no
longer reasons to block ordinary Trial requests under this user-selected behavior.

Arbitrary agent evaluate scripts and unreadable action targets cannot bypass
final-control checking. Intermediate native Submit profile/resume/code and Next
are allowed; explicit final controls are checked for click/Enter/Space/type
activation. Whole-form approval scanning is unnecessary for this mode.

---

# Testing Guide

LangHire has two test suites — a **Python/pytest** suite for the FastAPI backend and a
**Vitest + React Testing Library** suite for the React frontend. Both produce coverage reports
and run in CI (`.github/workflows/test.yml`) on every push/PR for Windows x64.

## Environment (Windows x64, reproducible)

```powershell
uv python install 3.13      # the interpreter pyproject.toml requires (>=3.13)
uv sync --locked            # .venv from uv.lock; --locked refuses to change the lock
npm ci                      # Node 22.22+ recommended; also required by the website
                            # node_modules from package-lock.json
```

`uv sync --locked --dry-run` prints "Would make no changes" when `.venv` already
matches the lockfile. If `.venv` points at a Python that no longer exists (an
error about a missing uv-managed interpreter), delete `.venv` and run
`uv sync --locked` again: it is recreated from `uv.lock`.

## Quick start

```bash
# Frontend (Vitest)
npm test                 # run once
npm run test:watch       # watch mode
npm run test:coverage    # with coverage report (./coverage)
npm run typecheck        # tsc project build (app + node + test projects)

# Backend (pytest) — run via uv
uv run pytest                                   # run all backend tests
uv run pytest --cov --cov-report=term-missing   # with coverage
uv run pytest tests/backend/core -v             # a subset
```

## Layout

```
tests/                          # backend pytest suite
  conftest.py                   # sys.path setup + `data_dir` fixture (isolated tmp HOME)
  backend/
    core/      test_config.py, test_country_config.py, test_llm_factory.py,
               test_shared_config.py, test_agent_logger.py
    memory/    test_store.py, test_extractors.py, test_metrics.py
    resume/    test_tailor.py
    sources/   test_registry.py
    test_models.py, test_main_api.py

src/**/*.test.{ts,tsx}          # frontend tests, co-located with the code they cover
src/test/setup.ts               # global Vitest setup (mocks Tauri, jest-dom matchers)
```

## Conventions

### Backend (pytest)
- Configuration lives in `pyproject.toml` (`[tool.pytest.ini_options]`, `[tool.coverage.*]`).
- `asyncio_mode = "auto"` — `async def test_*` functions run without extra decorators.
- The **`data_dir` fixture** (in `tests/conftest.py`) redirects the OS app-data directory into a
  per-test temp `HOME`, so tests never touch your real `~/.config/langhire`. Use it for anything
  that reads/writes settings, profile, jobs, or the memory DB.
- **No network, no AWS, no real browser.** External calls (LLMs, boto3, subprocess, Playwright)
  are mocked with `unittest.mock` / `monkeypatch`.
- **Safety net for every test** (`_safety_net` in `tests/conftest.py`, applied before any
  application module is imported, plus per test):
  - `HOME`/`USERPROFILE` point at a throwaway folder before imports, so settings, jobs,
    profile, the API token, logs, inbox and browser profiles are all temporary;
  - the memory/metrics database lives in the app-data folder in source and packaged
    runs, and tests additionally redirect it to each test's own folder;
  - sockets may only connect to this machine (fake LLMs, fake pages, fake Gmail);
  - launching a browser (`subprocess.Popen` of chrome/brave/edge/..., or a
    `BrowserSession.start`) raises unless a test is marked `real_browser`
    (none are in the default run);
  - browser clean-up only matches profiles under the (throwaway) data folder, so it
    can never close your own browsers or the real app's.
  `tests/backend/test_safety_net.py` checks these guards.
- **Opt-in real-browser tests** (skipped by default, marked `real_browser`):
  `tests/backend/cli/test_submit_guard_live.py` (the submit guard against local forms),
  `tests/backend/cli/test_review_round5_live.py` (native input/label bypass regressions), and
  `tests/backend/core/test_browser_network_live.py` (the agent browser's network limits: a
  HTTP, STUN UDP and TURN TCP requests to this computer, explicit forwarding,
  and refusal to secure an already-running profile). They start Brave (or Chrome)
  headless with a throwaway profile and talk only to local servers:
  ```powershell
  $env:LANGHIRE_REAL_BROWSER = "1"; .venv\Scripts\python.exe -m pytest tests/backend/cli/test_submit_guard_live.py tests/backend/cli/test_review_round5_live.py tests/backend/core/test_browser_network_live.py tests/backend/core/test_request_gate_live.py tests/backend/core/test_application_pipeline_live.py; Remove-Item Env:LANGHIRE_REAL_BROWSER
  ```

### Frontend (Vitest)
- Configuration lives in the `test` block of `vite.config.ts` (jsdom env, globals, v8 coverage).
- `src/test/setup.ts` mocks `@tauri-apps/api/core` (`invoke`) and `@tauri-apps/plugin-dialog`,
  registers `@testing-library/jest-dom` matchers, and cleans up the DOM + mocks after each test.
- Tests mock `../lib/api`, `react-i18next`, `../lib/analytics`, and `fetch` — **no real network
  or Tauri runtime** is used.
- Test files are type-checked under `tsconfig.test.json` (with `vitest/globals` + jest-dom types)
  and excluded from the production build (`tsconfig.app.json`).

## Coverage status

Coverage is measured by the commands above. The latest frontend coverage run
measured 85.11% statements and 87.02% lines with Vitest 5; compare current artifacts
rather than earlier estimates from Vitest 3. The default backend run skips all
opt-in browser cases and two POSIX-only checks on Windows. CI now also runs the
browser cases explicitly, including the real SDK application/collection pipeline
with a fake local LLM and only local HTTP traffic.

The request-gate live suite verifies real POSTs, GET side effects, complete uploaded
bytes, cookies, redirects, new documents, cancelled requests, recording failures,
credential approvals, automatic grants, and refused WebSocket/TURN/worker traffic.
It also holds human reviews beyond the SDK's short defaults and requires a fresh
decision for repeated writes and retries after an HTTP error.
No test uses a real Gmail inbox, employer account, application, or paid provider.
The application-pipeline test injects a transport for a reserved test hostname to
talk to its local server; production has no setting that bypasses private-IP checks.

Dependency checks:

```powershell
uv run --locked python scripts/audit-dependencies.py
npm audit --audit-level=low
npm --prefix website audit --audit-level=low
```

The OSV report records pinned Python packages and the active Windows Cargo graph.
Accepted Rust findings remain visible with reasons and expiration dates. Optional
Cargo packages not enabled for this Windows build are not called shipped defects.
Use `uv sync --locked` to install Python: its explicit security overrides supersede
browser-use's older exact pins. `backend/requirements.txt` is the complete exported
set; a pip-only installation must use `--no-deps` with that file.

## Integration script (optional)

```powershell
uv run python scripts/integration-test.py            # backend from source
uv run python scripts/integration-test.py --binary src-tauri\binaries\langhirev2-backend-x86_64-pc-windows-msvc.exe
```

It starts the backend on a free loopback port with its own throwaway home folder (deleted
afterwards), so the real settings, jobs, profile and API token are never read or
written. The "collection dry run" switches collection off in those throwaway
settings and checks the request is refused: no collection starts. The Chromium
checks launch a headless browser on an empty profile and visit only a local page; the LLM checks are skipped
unless `TEST_OPENROUTER_API_KEY` is set.

After building the Windows sidecar, run its security smoke checks:

```powershell
uv run --locked python scripts/smoke-backend.py --binary src-tauri/binaries/langhirev2-backend-x86_64-pc-windows-msvc.exe
```

This uses a fake token, an empty provider and its own temporary data folder. It
checks actual HTTP authentication, malformed/rebound/duplicate Host values,
preflight ordering, strict request decisions, refusal of stale approvals, and
per-job retry-note isolation, the 10,000-character limit, clearing and failed-save
preservation. Seeded dummy jobs never trigger external posting requests.
It stops only its own processes and leaves the temporary process log for diagnosis.

## Collector review checks

With LANGHIRE_REAL_BROWSER=1, run the collector modules through the same pytest
command: test_collect_search.py, test_collect_quick_live.py,
test_collect_review_1018.py and test_collect_review_1018_live.py under
tests/backend/cli, plus tests/backend/core/test_pinned_transport_review_1018.py.
The full tests/backend invocation includes all of them. The 2026-10-19 follow-up adds
tests/backend/cli/test_collect_remaining_live.py (real outgoing-request refusals,
cross-origin/nested/rotated/closed-shadow readers, scrolling, cookie/referrer payloads),
test_collect_source_match.py (adversarial model opinions, evidence/pay checks) and
tests/backend/core/test_collection_policy.py (capability scope).

The TLS cases create temporary certificates and verify actual hostname checks
across redirects. Production certificate verification stays enabled.
CODEX_RESPONSE.md retains the older probes as historical evidence and records the
new permanent tests that supersede those pagination/frame limitations. Remote page
truth and live board compatibility are not inferred from local tests.

## Windows build

The screenshot/compatibility follow-up adds
`tests/backend/core/test_application_compat_live.py`: all eight built-in Quick
link recipes under the real request gate, CSS-hidden negative controls, inert
bootstrap recovery, blocked-retry-loop progress, actual Auto/Trial step/final
POST evidence, password/upload/Workday-picker completeness and CAPTCHA lifecycle
plus SDK URL-dispatch refusal. Run with `LANGHIRE_REAL_BROWSER=1`; only local
test pages, dummy files and fake solver answers are used. Public anonymous board
observations are recorded separately in CODEX_RESPONSE.md; they are not treated
as authenticated or provider validation.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-windows.ps1
```

## Live Trial contracts and regressions

Trial is live again, superseding the earlier offline preview implementation. It uses the
same pinned transport, scoped browser sessions and credential checks as Full Auto. The
separate collection policy is unchanged. There is no final-submit approval or credential
approval prompt. Unknown operations end the step rather than granting a broad write exception.

`backend/core/trial_policy.py` allows reads and explicitly identified intermediate writes.
Workday evidence: public first-party JavaScript fetched read-only on 2026-10-20:
- https://wd5.myworkdaycdn.com/wday/asset/candidate-experience-jobs/2026.40.17/cx-jobs.min.js
- https://wd5.myworkdaycdn.com/wday/asset/candidate-experience-apply-flow/2026.40.17/candidate-experience-apply-flow.min.js

The client uses a strictly typed `/wday/cxs/{tenant}/{site}/jobs` POST for a read-only
search lookup (observed refused in the first public Workday Trial probe, then fixed).
Only search filters/limit/offset/text are permitted; extra submit/control fields are refused.
The client separates `/en-US/{site}/login` and register (URL-encoded credentials),
`/wday/cxs/{tenant}/jobpostings/{posting}/jobapplications` (`{}` creates a draft),
dedicated `/jobapplication/{id}/{section}` JSON saves, separate single-file upload/scan
endpoints, and `/jobapplication/{id}/finalize`. Trial refuses finalize in every method.
Allowed sections are enumerated in code, not inferred from the page or an AI judgement.
Tenant path must match its exact Workday host. Unknown actions/batches, duplicate keys,
method overrides, unreadable bodies, and extra auth fields are refused. Terminal paths
and query operation fields are checked for live GETs as well. No generic `/draft` allowance.

Oracle evidence: [create a draft](https://docs.oracle.com/en/cloud/saas/human-resources/farws/op-recruitingcejobapplicationdrafts-post.html),
[update a draft](https://docs.oracle.com/en/cloud/saas/human-resources/farws/op-recruitingcejobapplicationdrafts-appdraftid-patch.html),
[submit an application](https://docs.oracle.com/en/cloud/saas/human-resources/farws/op-recruitingcejobapplications-post.html).
The 2026-10-20 hosted-client check below extends the original numeric-version draft
contract. Only reviewed draft actions and login-token exchanges are permitted; final
application/confirmation operations, unbound tokens and arbitrary batches remain refused.

`tests/backend/core/test_trial_policy.py` exercises the contracts and ambiguous payloads.
`tests/backend/core/test_trial_no_send_live.py` uses a real local HTTP server and separate
headless browsers: native login and Next, cookies and redirects, supported saves, final
buttons, forged old approval, and POST/GET/beacon/image/popup/frame/redirect/RPC/mixed-save
attacks sent directly by page scripts. Every attack has a Full Auto positive control.
`test_application_compat_live.py` retains collection positives for all fixture recipes.
Run browser modules with `LANGHIRE_REAL_BROWSER=1`; fixtures use dummy data only.

These tests verify transport behavior against local contract fixtures, not signed-in
production applications or every ATS variant. Public GET compatibility probes are separate.
Ordinary GETs can mutate a nonstandard server, and changed ATS endpoint semantics remain
an external assumption. No universal no-submit or all-site compatibility claim is made.

### Hosted ATS compatibility expansion (2026-10-20)

Protocol sources were downloaded as data from public first-party pages and bundles,
without accounts, forms sent to employers, real applications or paid providers:

- [Lever demo application](https://jobs.lever.co/leverdemo/58db3f8e-b108-47d1-9e6e-87a1712497cd/apply),
  [current résumé parser](https://jobs.lever.co/js/parseResume.js): `/parseResume` receives
  exactly `accountId` from the observed employer form and the authorized résumé bytes.
- [Greenhouse public application](https://job-boards.greenhouse.io/anthropic/jobs/5183044008),
  [current client](https://job-boards.cdn.greenhouse.io/assets/entry.client-DnzCgKgt.js):
  signing uses `https://boards.greenhouse.io/uncacheable_attributes/presigned_fields`,
  repeated `fields[]`, and a returned S3 URL/fields/key template. The vendor appends
  fixed `utf8`, `authenticity_token`, and octet-stream fields, and substitutes timestamp
  and random key components. Tests exercise this exact shape, not a simpler fake upload.
- [Ashby public application](https://jobs.ashbyhq.com/ashby/044cc683-be16-471b-9af1-99cf70607735/application),
  [reviewed client](https://cdn.ashbyprd.com/frontend_non_user/76439344781599c026ec4e9d3aeba8af45755362/assets/index-CkI05tC5.js):
  14 reviewed GraphQL operations. Full document hashes retain the audit evidence;
  dispatch hashes pin operation/variable declarations and every root field/alias/argument.
  Nested read selections may grow without disabling a newer UI. Extra operations,
  root fragments, renamed aliases, spoofed operationName, batches and foreign tenants
  are refused. Returned form IDs and upload handles must belong to this run.
- [Workable public board](https://apply.workable.com/betterup/),
  [reviewed client](https://dcvxs6ggqztsa.cloudfront.net/candidate/releases/careers.6901cd820c44d775.js):
  `/api/v3/accounts/{tenant}/jobs` is a read-only POST, including the actual `worktype`
  filter. The dedicated form upload lookup can mint an exact S3 upload grant; the
  separate `/api/v1/jobs/{id}/apply` receives no Trial sending authority.
- [Oracle custom careers domain](https://jobs.navyfederal.org/hcmUI/CandidateExperience/en/sites/nfcu),
  [reviewed Oracle runtime](https://static.oracle.com/cdn/fa/oj-hcm-ce/2607.26.262300322/js/main-minimal.js):
  actual APIs use `resources/latest` and `application/vnd.oracle.adf.resourceitem+json`.
  Trusted initial HTML pins its Oracle API origin/site number; this is protocol
  binding, never blanket credential trust. Draft saves/generate-number/update-channel,
  separate file parsing and draft attachments are supported. `JOB_APPLY`/`CSS_ACCESS`
  open authentication; only login tokens/channels observed from those successful
  exchanges may be verified. `CONFIRM`, `JOB_APPLY_CONFIRM`, unbound email tokens,
  final `recruitingCEJobApplications`, mixed controls and arbitrary batches are held.
- Existing Workday contracts extend to `/recruiting/{tenant}/{site}` shared hosts;
  the first trusted tenant remains pinned even when another tenant shares the origin.

Signed upload permissions are short-lived, bounded, one-use and atomically consumed.
They require the exact broker-observed signed fields and explicitly supplied file
bytes/name. Repeated metadata cannot reset a spent grant. Multipart duplicates,
extra controls/files, transfer encoding tricks, changed keys, foreign destinations,
expired/replayed grants and unauthorized local files are refused.

Native buttons in other languages, keyboard progression, unreadable frame metadata
and model scripts may advance live Trial only while the independent broker is healthy.
Explicit final buttons remain refused even after an old approval; final/unknown writes
from ordinary-looking Next handlers are independently refused before transport.

Chromium dedicated workers do not expose `Fetch.enable`, and some reject `closeTarget`.
These workers remain paused with `Network.setBlockedURLs(["*"])` and the independent
deny-all proxy; they are never resumed/detached as an unguarded fallback. A rejected
worker-close command no longer kills the parent page. Worker-dependent functionality
is not claimed supported. Exact hCaptcha/reCAPTCHA proof routes and Cloudflare's
challenge-platform proof routes have separate non-terminal request contracts. This
does not grant iframe/page scripts any permission to send an application; tests use
dummy local proofs, not real CAPTCHA tokens or paid solver calls.

`test_trial_contracts.py` covers protocol positives, dispatch changes and adversarial
payloads. `test_trial_ats_live.py` proves progression, complete uploads, final refusals,
worker network refusal with a usable parent page, challenge/submit separation and
canonical tool actions through real local HTTP and Chromium. Auto controls actually
send their final dummy payloads, proving the listener would observe a leaked Trial post.

The ten collection recipes remain separate and unchanged. Their local fixtures cover
all recipes plus dynamic resources, hidden links, pagination, notes-driven relevance
and write refusals. Host recognition in memory covers more ATS names than the reviewed
write contracts above. The candidate authentication contracts below extend login
support; unreviewed external SSO and iCIMS/SAP/Taleo/custom combined application
save-and-submit protocols are not silently enabled; an unknown operation still stops
without an approval prompt. These are explicit compatibility limits, not verified
production sign-in/application successes.

### Candidate identity contracts (2026-10-21)

`core/trial_identity.py` separates candidate authentication from ATS application
authority. iCIMS' literal authorization redirect is read only from a trusted
employer document. The redirect URI must name that employer's reviewed login
callback and the same job ID; client, audience, scope, organization, tenant,
nonce and state are validated. An actual broker-observed provider redirect
then binds the opaque Auth0 state. Only served forms on reviewed `/u/login`,
signup, password and verification routes can post. Hidden state and immutable
fields stay exact, duplicate/unknown/control fields fail, and a successful
reviewed login may rotate state for the next verification form. Arbitrary HTML
cannot grant a write route. Social-provider buttons are excluded. These
capabilities expire after 15 minutes and are bounded per run.

The same observer operates in Auto for credential delegation. Auth0 pages do
not enter the origin-wide credential approval cache. The typing pipeline maps
the exact bound provider form back to the original employer's encrypted vault
key, retaining actual document/form destination checks and refusing the earlier
shared password on the provider. Verification-code lookup retains the original
employer, sender and recipient restrictions; no generic provider mailbox search
or cross-provider verification-link permission is granted.

Native iCIMS email gates, Taleo `login.jsf`/`iamSecuredLogin.jss` and SAP
SuccessFactors candidate login forms have separate reviewed contracts. JSF
state/CSRF/hidden dispatcher fields are pinned; only the observed login/register
command IDs vary. SAP's company is pinned, is now part of the credential tenant,
and its shared `/career` endpoint gets no general application-write permission.
Board-to-employer redirects use the observed employer scope, not a board password.

Real browser tests found that a button named `action` shadows `form.action`,
refusing ordinary Auth0 credential entry. The effective destination now resolves
the actual action attribute against the document base URI, without reading the
clobbered named property. The test types fake credentials through the real
application tools and verifies the decoded password at the local HTTP listener.

Wire tests also demonstrated that Workday's old multipart branch accepted
duplicate boundary/name parameters and unapproved file bytes. It now shares
the strict multipart parser and exact authorized-file-byte check. Auto positive
controls still reach the local listener, Trial negatives do not, and native
authorized resume uploads remain covered by browser regressions.

Protocol evidence: read-only public GETs of the
[iCIMS candidate portal](https://careers-hines.icims.com/jobs/search?in_iframe=1),
its served `login.icims.com/authorize` flow, the
[Taleo candidate login](https://drhorton.taleo.net/careersection/iam/accessmanagement/login.jsf),
and the [SAP candidate login](https://career5.successfactors.eu/career?company=SAP&loginFlowRequired=true&lang=en_US).
All three actual served forms bind and accept their reconstructed login payloads
**in memory only**; no public POST, credentials or account/application were sent.
Auth0 describes its internal browser routes in
[its credential request explanation](https://support.auth0.com/center/s/article/visibility-of-credentials-in-the-u-login-password-post-request-payload)
and [state-engine testing guidance](https://support.auth0.com/center/s/article/automated-testing-with-identifier-first-universal-login).
These routes are internal and may change; the tests demonstrate local behavior
and observed current schemas, not every authenticated production flow.

New regressions: `tests/backend/core/test_trial_identity.py` and
`test_trial_identity_live.py`. CI includes the latter in its opt-in browser run.
Use `LANGHIRE_REAL_BROWSER=1` for the browser suite. No real accounts, Gmail,
external write requests, paid model calls or solver calls are required.


### Security dependency refresh (2026-10-21)

A fresh OSV audit found 19 unaccepted Python advisory records in the previous
lock (aiohttp 3.14.3, certifi 2026.7.22, Pillow 12.3.0); certifi's two records
refer to the same underlying issue. npm reported three Vite advisories under
one high-severity package finding. Updated to aiohttp 3.14.5, certifi 2026.10.19,
Pillow 13.0.0 and the minimal patched Vite 8.1.6 line. Explicit uv overrides
are necessary because upstream browser-use pins otherwise retain the older
packages. `uv sync --locked` and the exported pip-only requirements agree.

Primary patch references: [aiohttp cookie fix](https://github.com/aio-libs/aiohttp/security/advisories/GHSA-gx76-c99x-2c96),
[Pillow security fix](https://github.com/python-pillow/Pillow/security/advisories/GHSA-8g5g-8vmr-9782),
[Pillow 13 release notes](https://pillow.readthedocs.io/en/stable/releasenotes/13.0.0.html),
[certifi CA removal](https://github.com/advisories/GHSA-6q48-6c7w-9gw2),
[Vite/launch-editor Windows advisory](https://github.com/advisories/GHSA-587h-7wmj-h5pm).

The refreshed Python/Rust audit checks 612 locked/active packages and reports
zero unaccepted findings. Three advisory records remain under the two unchanged,
expiring exceptions in `security/dependency-exceptions.json`: fxhash maintenance
and rand's build-only legacy dependency. Root and website npm audits report zero.
This is an audit result with explicit exceptions, not a zero-vulnerability proof.
Final backend, browser, frontend and frozen-package results are in the latest
CODEX_RESPONSE.md section. The frozen verifier also checks patched package code,
the CA bundle and Pillow's native extensions against the actual build environment.


### Additional candidate preparation and shared dispatchers (2026-10-22)

`core/trial_vendors.py` and `core/trial_brassring.py` extend the live Trial
contracts. Sources below were retrieved by public GET only, without credentials,
real applications, production POSTs or paid calls. Exact downloaded-source hashes
and permissions are recorded in `security/ats-protocols-2026-10-22.json`.

| ATS | Reviewed preparation permission | Application boundary retained |
| --- | --- | --- |
| Teamtailor | Trusted careers document; POST `/uploads/presigned_data` with an explicitly available filename (or the client's empty-body variant); returned S3 fields with the exact lowercase/sanitized filename substitution; exact authorized file bytes and atomic one-use grant | Separate POST `/applications` stays refused |
| Breezy | Observed `portal-data` JSON pins company `friendly_id`; multipart `/api/portal/{company}/upload` and `/upload/file`, one explicitly available file and no extra fields; GET `/resume/parse` can fetch only the just-uploaded broker-returned S3 URL for that company, expiring after ten minutes | `/apply/{id}`, `/portal/fragment/{id}`, references/consents and other writes receive no blanket permission |
| Jobvite | Served `preloadedData.companyEId` pins company on `jobs.jobvite.com`; `/careers/resource/resume`, `/coverletter`, `/portfolio` accept exact supplied file and reviewed company/parser fields | No application-write permission; remote Dropbox/LinkedIn file URLs receive no upload exception |
| Personio | Trusted `{company}.jobs.personio.de` document; multipart POST `/api/v1/documents` with one explicitly supplied file | Separate `/api/v1/jobs/{id}/application` stays refused |
| BrassRing | Partner/site pinned from trusted URL; observed encrypted login session and client settings pin `/TgNewUI/Search/Ajax/CheckLoginDetails`; no social/create-account/extra operation exception. Served `pagemetadata`, RFT, page/next-page/section/requisition/questionnaire/application/session IDs pin `/GQWeb/SaveAndNextPage` | Only previous page type 1 or 2 with boolean `Submit=false`; review type 3, forged false flag, unknown next page/session/tenant, duplicate JSON and mixed operations stay refused |

Primary protocol evidence:
- [Teamtailor public application](https://career.teamtailor.com/jobs/8546033-revenue-systems-engineer/applications/new),
  [upload controllers](https://assets-aws.teamtailor-cdn.com/assets/chunks/chunk-ML2C2TJ4.digested.js),
  [POST request helper](https://assets-aws.teamtailor-cdn.com/assets/chunks/chunk-3ODYOQTC.digested.js).
- [Breezy public application and bootstrap](https://jobs.breezy.hr/p/493d0eedba0f-sales-executive/apply),
  [candidate client](https://assets-cdn.breezy.hr/breezy-portal/react/assets/portal-SYa-2SqQ.js).
- [Jobvite public company page](https://jobs.jobvite.com/jobvite),
  [resource/native upload client](https://d3igejkwe1ucjd.cloudfront.net/__assets__/concat/careersite/public/jv.careersite.desktop.app.js?v=261019193052).
- [Personio public application](https://pm-team.jobs.personio.de/job/2602413/apply),
  [application client](https://assets.cdn.personio.de/artifact-service/personio-jobs/_next/static/chunks/app/company/%5Bsubdomain%5D/job/%5BpositionId%5D/apply/page-232c8832c11bd400.js).
- [BrassRing public client](https://sjobs.brassring.com/GQWeb/Scripts/app/v-639275469907003986/apply.js).
  Its `submitmode=(pagetype==review)` is sent as `Submit`. The response is the
  next HTML fragment containing `pagemetadata`; this is observed at the broker,
  not trusted from a page-script assertion or a button label.

New regression files: `test_trial_vendors.py`, `test_trial_brassring.py`,
`test_vendor_scope.py`, `test_trial_vendors_live.py`, `test_vendor_scope_live.py`.
Live tests use actual Chromium requests and actual local HTTP connections, with
Auto controls proving final payloads would reach the listener. They cover login,
Next, uploads, parser URL binding, shared-endpoint final disguises and exact
credential typing through `build_tools`; Auto really posts the fake password on
the original employer and posts an empty password after cross-tenant or
missing/duplicated-tenant input was refused. No approval queues are opened.
BrassRing partner/site, Jobvite company path and UKG company path now separate
credential vault keys. Old unscoped entries are preserved, not reused implicitly.
The collector and collection policy are unchanged.

**Scope of this evidence:** public client review and local protocol tests are not
signed-in production completion tests. They do not establish every employer's
private/custom dispatcher. iCIMS/SAP/Taleo application-step dispatchers beyond the
reviewed login contracts, other unreviewed APIs, external SSO and worker-dependent
functions are still refused rather than being granted broad writes. This pass
must not be described as universal ATS parity. No remaining work is delegated to
Claude or hidden behind an approval prompt.

A concrete semantic limit: [JazzHR's public two-stage client](https://gatelesis.applytojob.com/js/apply/submit-resume.js?master)
posts candidate data to `/apply/job/{code}/stage-one` and redirects to an
application confirmation. A first application submission cannot be permitted in
Trial merely because the vendor has later questions/screens. Its ordinary
client-side filling remains possible; that server submission stays refused.

### Collection sign-in — 2026-10-26

`test_collection_login.py` checks all ten bundled login destinations, existing
API authentication, refusal of imported plugins/caller URLs/invalid destinations,
and the existing profile ownership lock. `test_collection_login_live.py` uses
fake password and SSO forms on a local HTTP listener, closes the normal login
browser, and starts a new guarded collection browser using the same saved profile.
Both automatic and custom browser selection are covered. The collection request
must carry the persisted session cookie and display the authenticated listing;
the page's application POST, mail request and outside-host request must not reach
the listener, and no approval queue may open. No real accounts or public login
POSTs are used. These local tests establish the app's login/session plumbing,
not acceptance of every real board's CAPTCHA, SSO policy or future login changes.

The final test uses actual Automatic selection, with no executable override in
the collection phase, and reopens the normal browser after collection too. This
initially reproduced two Automatic startup timeouts: normal login found the
modern bundled Chromium while browser-use's older cache patterns selected a
different executable. Login and automation now share discovery of modern/legacy
Playwright layouts and configured cache paths, with numeric version ordering.
The existing profile is retained. Explicit installed/custom choices still win.

Collector UI tests cover selecting and opening the right board without a dialog,
launch failures, disabling sign-in during collection, and enabling collection
after the sign-in window closes. Existing notes-driven AI matching, no-pay
threshold and collection network regression tests remain in place.

### Browser resource ownership and SAP return login (2026-10-26)

`core.browser_lifecycle` owns the asyncio subprocess that browser-use otherwise
discards after converting it to a psutil handle. It drains stdout/stderr with
bounded tails, retains the exact process identities, waits for descendants and
pipe EOF, and closes on normal kill, startup failure or cancellation. The shim
is confined to the vendor module's asyncio binding and browser-launch context;
it does not replace global asyncio, capture installer subprocesses, change
network policy or scan/kill browsers by executable name. Repeated cancellation
finishes the bounded cleanup before propagating. Embedded backend imports also
avoid constructing a discarded RotatingFileHandler when logging is configured.

`test_browser_lifecycle.py` uses real child processes and output exceeding pipe
capacity; it covers bounded drainage, concurrent owners, repeated close and
cancellation, PID reuse and exclusion of non-browser subprocesses.
`test_browser_lifecycle_live.py` starts actual Chromium with a dead local proxy,
forces failure/cancellation after CDP is ready, and asserts process exit,
descendant exit, both stream EOFs and closed transport. No network/account is
needed. CI includes it and treats ResourceWarning and
PytestUnraisableExceptionWarning as errors for the browser suite; warnings are
not suppressed. `test_main_logging.py` checks an actual isolated backend import
under preconfigured logging.

The wider strict run also exposed worker metrics connections created on the
caller thread and discarded from the worker thread. The backend now initializes
and closes each run's MetricsStore inside its worker, serializing concurrent
first-time WAL/schema initialization. `test_worker_metrics_lifecycle.py` asserts
real SQLite closure on the creating thread for success, failure and cancellation,
and verifies persisted logs. The existing simultaneous-run isolation test stays
unchanged; SQLite's thread-affinity checks remain enabled.

An anonymous GET of the current
[SAP application sign-in page](https://career5.successfactors.eu/career?company=gieseckede&career_job_req_id=27483&career_ns=job_application)
exposed a compatibility gap in the earlier login fixture: the served form has
two identical `career_ns=job_application` fields, with `career_os` and `login_ns`
empty. Trial now recognizes that exact return route while pinning the company,
job ID, full rendered field multiplicity and opaque state. The sanitized field
structure is `tests/backend/core/fixtures/sap-login-return.html`; it contains no
live state/credentials. Unit and native-form browser/wire tests verify login
and refuse changed operation, namespace, company and job selectors; Auto
controls verify that refused Trial payloads really can reach the HTTP listener.

Research evidence and hashes: `security/ats-protocols-2026-10-26.json`. Public
iCIMS profile and Taleo profile entry pages lead to login. Following Taleo's
literal redirect in the same anonymous session exposes the native login form;
fetching its bare login URL alone can instead return the unsupported page.
The named public iCIMS candidate-matching sandbox returned 404. SAP's exposed
profile model and Taleo's generic FTL ButtonComponent/PageComponent dispatchers
do not establish nonterminal application commands. Administrative OData/Web
Services APIs use different authorization and are not candidate browser grants.
No public POST or account creation was attempted. Authenticated iCIMS/SAP/Taleo
application-save/next dispatchers beyond reviewed authentication remain
**unverified and refused**. No claim of complete post-login parity is made.
