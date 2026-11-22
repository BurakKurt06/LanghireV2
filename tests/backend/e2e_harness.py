"""A local fake hiring site, a scripted AI and the real application engine in a real browser.

For the opt-in real-browser tests of whole applications: the app's own code path
(cli.apply_jobs.apply_to_job, the real browser-use Agent, a real Chromium), with
- pages served from this computer only (made-up content);
- a scripted AI: each test gives a function that reads what the agent sees (the page as text)
  and returns the next action(s), so no AI provider is ever called;
- a throwaway data folder (the `data_dir` fixture).
"""
import asyncio
import http.server
import json
import os
import re
import threading
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

FILLER = "A made-up posting for a local test. " * 12


class Site:
    """http://localhost:<port>/ with pages given as {path: html or function(request) -> html}."""

    def __init__(self, pages: dict):
        self.pages = dict(pages)
        self.requests: list[tuple[str, str, dict]] = []       # (method, path, form or query)
        site = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def _answer(self, method: str, data: dict):
                parts = urlsplit(self.path)
                site.requests.append((method, parts.path, data))
                page = site.pages.get(parts.path)
                if page is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                body = page(method, data) if callable(page) else page
                if isinstance(body, tuple):                    # (status, headers, body)
                    status, headers, body = body
                else:
                    status, headers = 200, {}
                raw = body.encode() if isinstance(body, str) else body
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                for key, value in headers.items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                self._answer("GET", {k: v[0] for k, v in parse_qs(urlsplit(self.path).query).items()})

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                self._answer("POST", {k: v[0] for k, v in parse_qs(raw.decode(errors="replace")).items()})

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.base = f"http://localhost:{self.server.server_port}"

    def url(self, path: str = "/") -> str:
        return self.base + path

    def posted(self, path: str) -> list[dict]:
        return [data for method, where, data in self.requests if method == "POST" and where == path]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def page_text(messages) -> str:
    """Everything the agent was sent for this step, as one text."""
    parts = []
    for message in messages:
        content = getattr(message, "content", "")
        if isinstance(content, str):
            parts.append(content)
        else:
            parts.extend(getattr(part, "text", "") or "" for part in content or [])
    return "\n".join(parts)


def browser_state(text: str) -> str:
    """The part of the step's text that describes the current page (the last <browser_state> block)."""
    end = text.rfind("</browser_state>")
    start = text.rfind("<browser_state>", 0, end if end >= 0 else len(text))
    if start < 0:
        return text
    return text[start:end if end > start else len(text)]


def index_of(text: str, snippet: str) -> int | None:
    """The agent's index of the first interactive element whose line contains `snippet`
    (an attribute such as name=city, or its visible text on the same or the next line)."""
    state = browser_state(text)
    lines = state.splitlines()
    for number, line in enumerate(lines):
        match = re.search(r"\[(\d+)\]<", line)
        if not match:
            continue
        following = lines[number + 1] if number + 1 < len(lines) and "[" not in lines[number + 1][:6] else ""
        if snippet in line or snippet in following:
            return int(match.group(1))
    return None


class Script:
    """The scripted AI. `decide(text, step)` returns one action dict, a list of them, or None to wait."""

    def __init__(self, decide):
        self.decide = decide
        self.calls = 0
        self.seen: list[str] = []

    def install(self, monkeypatch):
        from browser_use.llm import ChatOpenAI
        from browser_use.llm.views import ChatInvokeCompletion
        script = self

        async def ainvoke(self, messages, output_format=None, **kwargs):
            text = page_text(messages)
            if output_format is None:
                return ChatInvokeCompletion(completion="ok", usage=None)
            script.calls += 1
            script.seen.append(text)
            if os.environ.get('LANGHIRE_E2E_DUMP'):
                Path(os.environ['LANGHIRE_E2E_DUMP']).write_text(text, encoding='utf-8')
            result = script.decide(text, script.calls)
            if asyncio.iscoroutine(result):
                result = await result
            actions = result if isinstance(result, list) else [result or {"wait": {"seconds": 1}}]
            value = output_format.model_validate({
                "evaluation_previous_goal": "Looked at the page", "memory": "Local scripted test",
                "next_goal": "Next scripted action", "action": actions})
            return ChatInvokeCompletion(completion=value, usage=None)

        monkeypatch.setattr(ChatOpenAI, "ainvoke", ainvoke)
        return ChatOpenAI(model="test-local-model", api_key="unused-local-test-key")


def prepare(monkeypatch, data_dir, site: Site, script: Script, *, settings: dict | None = None,
            profile: dict | None = None, mode: str = "auto", password: str = "Made-Up-Pass-77"):
    """Point the real application engine at the local site, the scripted AI and the throwaway data
    folder. Returns (apply_jobs module, shared_config module)."""
    import fitz
    from browser_use import Agent, BrowserSession

    import conftest
    from cli import apply_jobs
    from core import job_page, shared_config

    resume = data_dir / "resume.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Ada Test\nEXPERIENCE\nExample Works | Intern | June 2025 - August 2025\n"
                                         "EDUCATION\nState University, B.S. Computer Science, expected May 2028")
    pdf.save(resume)
    pdf.close()
    logs = data_dir / "logs"
    logs.mkdir(exist_ok=True)
    jobs_file = data_dir / "jobs.json"
    for module in (apply_jobs, shared_config):
        monkeypatch.setattr(module, "LOGS_DIR", logs)
        monkeypatch.setattr(module, "JOBS_FILE", jobs_file)
        monkeypatch.setattr(module, "QA_FILE", data_dir / "qa_repository.json")
        monkeypatch.setattr(module, "CANDIDATE_PROFILE", data_dir / "candidate_profile.json")
    monkeypatch.setattr(shared_config, "DATA_DIR", data_dir)
    monkeypatch.setattr(shared_config, "JOBS_LOCK", data_dir / "jobs.json.lock")
    saved_settings = {"fit_check_enabled": False, "max_failures": 3, "resume_path": str(resume),
                      "sensitive_data": {"email": "ada.accounts@example.com", "password": password},
                      **(settings or {})}
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: dict(saved_settings))
    monkeypatch.setattr(shared_config, "selected_browser", lambda s=None: (conftest.live_browser(), data_dir / "browser_profile"))
    monkeypatch.setattr(shared_config, "refresh_posting_details",
                        lambda url, *a, **k: job_page.Posting(title="Test Analyst", company="Example Works",
                                                              description="Summer 2027 internship in Springfield."))
    monkeypatch.setattr(shared_config, "validate_job_url", lambda url, *a, **k: True)
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: None)
    llm = script.install(monkeypatch)
    monkeypatch.setattr(shared_config, "get_llm", lambda: llm)
    monkeypatch.setattr(shared_config, "get_fallback_llm", lambda: None)
    monkeypatch.setattr(apply_jobs, "BrowserSession",
                        lambda **kw: BrowserSession(headless=True, enable_default_extensions=False, **kw))
    monkeypatch.setattr(apply_jobs, "Agent", lambda **kw: Agent(**{**kw, "calculate_cost": False}))
    # The local site stands in for a public https hiring site: only its own address counts as one.
    real_refusal = apply_jobs.credential_refusal
    monkeypatch.setattr(apply_jobs, "credential_refusal",
                        lambda url: "" if str(url).startswith(site.base) else real_refusal(url))
    (data_dir / "candidate_profile.json").write_text(json.dumps(profile or PROFILE))
    apply_jobs.set_run_mode(mode)
    apply_jobs.new_run()
    return apply_jobs, shared_config


PROFILE = {"name": "Ada Test", "email": "ada@example.com", "phone": "5550100",
           "address": {"street": "12 Example St", "city": "Springfield", "state": "IL", "zip": "62701", "country": "USA"},
           "work_authorization": "Authorized to work in the USA", "visa_sponsorship_needed": "No",
           "salary_expectation": {"currency": "USD", "min": 20, "max": 30, "period": "hourly"}}


def add_job(shared_config, url: str, **fields) -> dict:
    job = {"url": url, "title": "Test Analyst", "company": "Example Works", "status": "pending",
           "source": "pasted", **fields}
    jobs = shared_config.read_jobs()
    jobs[url] = job
    shared_config.write_jobs(jobs)
    return job


async def run_job(apply_jobs, job: dict, profile: dict | None = None, worker_id: int = 1) -> str:
    return await apply_jobs.apply_to_job(job, profile or PROFILE, {}, [], False, worker_id)


async def running_agent(apply_jobs, url: str, seconds: float = 60):
    """The live agent working on `url` (waits until it exists and its browser has a page)."""
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        agent = next((a for a in list(apply_jobs._running_agents) if getattr(a, "_langhire_url", "") == url), None)
        if agent is not None and getattr(agent.browser_session, "agent_focus_target_id", None):
            return agent
        await asyncio.sleep(0.1)
    raise AssertionError("the application never started")


async def as_the_user(agent, script: str) -> None:
    """Do something in the agent's browser as a person would: real input events (a mouse click,
    typed keys), sent while the agent itself is not acting. `script` is a tiny list of steps:
    "click #selector", "type some text", "clear" (select all + delete), one per line."""
    browser = agent.browser_session
    cdp = await browser.get_or_create_cdp_session()
    send, sid = cdp.cdp_client.send, cdp.session_id
    for line in [l.strip() for l in script.splitlines() if l.strip()]:
        command, _, argument = line.partition(" ")
        if command == "click":
            result = await send.Runtime.evaluate(params={"returnByValue": True, "expression": (
                "(() => { const el = document.querySelector(%s); el.scrollIntoView({block: 'center'});"
                " const r = el.getBoundingClientRect(); return [r.left + r.width / 2, r.top + r.height / 2]; })()"
                % json.dumps(argument))}, session_id=sid)
            x, y = result["result"]["value"]
            for kind in ("mouseMoved", "mousePressed", "mouseReleased"):
                await send.Input.dispatchMouseEvent(params={"type": kind, "x": x, "y": y, "button": "left",
                                                            "clickCount": 1}, session_id=sid)
        elif command == "clear":
            for kind in ("keyDown", "keyUp"):
                await send.Input.dispatchKeyEvent(params={"type": kind, "key": "a", "code": "KeyA", "modifiers": 2,
                                                          "windowsVirtualKeyCode": 65}, session_id=sid)
            for kind in ("keyDown", "keyUp"):
                await send.Input.dispatchKeyEvent(params={"type": kind, "key": "Delete", "code": "Delete",
                                                          "windowsVirtualKeyCode": 46}, session_id=sid)
        elif command == "type":
            for char in argument:
                await send.Input.dispatchKeyEvent(params={"type": "keyDown", "key": char, "text": char}, session_id=sid)
                await send.Input.dispatchKeyEvent(params={"type": "keyUp", "key": char}, session_id=sid)
        await asyncio.sleep(0.15)


async def field_value(agent, selector: str) -> str:
    browser = agent.browser_session
    cdp = await browser.get_or_create_cdp_session()
    result = await cdp.cdp_client.send.Runtime.evaluate(params={"returnByValue": True, "expression": (
        "(() => { const el = document.querySelector(%s); return el ? String(el.type === 'checkbox' ? el.checked : el.value) : null; })()"
        % json.dumps(selector))}, session_id=cdp.session_id)
    return result["result"].get("value")
