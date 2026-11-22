"""Press Pause, Resume, Skip and Stop during a real two-job run. Local pages and a fake AI only.

Runs the app's own backend (the same code path as the desktop app: POST /apply/start, then the
run controls) against a local test page, with a scripted AI that keeps the agent busy, and
reports what each control actually did:

  pause    the agent stops taking steps within PAUSE_SECONDS
  resume   the same job goes on taking steps within RESUME_SECONDS
  skip     the current job ends and the next one starts within SKIP_SECONDS; the skipped job stays in the list
  stop     the run ends within STOP_SECONDS and no further job starts afterwards

Usage:  python scripts/repro-run-controls.py [--legacy-gate] [--scenario busy|newtab]
  --scenario      busy (default): the page is healthy and the agent keeps working.
                  newtab: the agent clicks an "Apply Now" link that opens the form in a new tab
                  (as on real career sites), then the same controls are pressed.
  --legacy-gate   for v1.2.0, whose application browser only loads pages through the request gate:
                  the gate's backend fetch is pointed at the local test page (as its own tests do).
Nothing here leaves this computer; the data folder is a throwaway one.
"""
import argparse
import asyncio
import http.server
import json
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

_HOME = tempfile.mkdtemp(prefix="langhire-controls-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME
os.environ["JOB_APPLICANT_TOKEN"] = "repro-controls-token"
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

PAUSE_SECONDS = 20
RESUME_SECONDS = 40
SKIP_SECONDS = 40
STOP_SECONDS = 20

_PAGE = """<!doctype html><title>Test Analyst at Example Works</title>
<h1>Test Analyst</h1><p>{filler}</p>
<a id="open" href="/form/{job}" target="_blank">Apply Now</a>
<form><label>First name <input name="first"></label><label>City <input name="city"></label></form>"""
_FORM = """<!doctype html><title>Application form</title><h1>Application form</h1><p>{filler}</p>
<form><label>Last name <input name="last"></label><label>Phone <input name="phone"></label></form>"""


def start_site():
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            job = self.path.rstrip("/").split("/")[-1]
            page = _FORM if "/form/" in self.path else _PAGE
            body = page.format(filler="A made-up posting for a local test. " * 20, job=job).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--legacy-gate", action="store_true")
    parser.add_argument("--scenario", choices=("busy", "newtab"), default="busy")
    parser.add_argument("--out", default="")
    args = parser.parse_args()

    import fitz
    import httpx
    from browser_use import Agent, BrowserSession
    from browser_use.llm import ChatOpenAI
    from browser_use.llm.views import ChatInvokeCompletion
    from fastapi.testclient import TestClient

    import main as backend
    import core.shared_config as config
    from cli import apply_jobs
    from core import job_page
    from core.browsers import find_playwright_chromium

    site = start_site()
    port = site.server_port
    if args.legacy_gate:
        jobs = [f"https://careers.example.test/job/{n}" for n in (1, 2)]
    else:
        jobs = [f"http://localhost:{port}/job/{n}" for n in (1, 2)]

    data = Path(_HOME) / "AppData" / "Roaming" / "langhirev2"
    data.mkdir(parents=True, exist_ok=True)
    resume = data / "resume.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Ada Test\nPython")
    pdf.save(resume)
    pdf.close()
    logs = data / "logs"
    logs.mkdir(exist_ok=True)
    apply_jobs.LOGS_DIR = config.LOGS_DIR = logs
    config.write_jobs({url: {"url": url, "title": f"Test Analyst {i}", "company": "Example Works",
                             "status": "pending", "source": "pasted"} for i, url in enumerate(jobs, 1)})
    (data / "candidate_profile.json").write_text(json.dumps({"name": "Ada Test", "email": "ada@example.com"}))

    settings = {"fit_check_enabled": False, "cover_letter_boxes": False, "resume_path": str(resume),
                "sensitive_data": {"email": "ada@example.com", "password": ""}, "max_failures": 8,
                "browser_allowed_local_hosts": ["localhost"]}
    config.load_runtime_settings = lambda: dict(settings)
    backend.load_settings = lambda: dict(settings)
    backend.load_llm_settings = lambda: {}
    backend._start_problem = lambda apply: ""
    config.selected_browser = lambda s=None: (find_playwright_chromium(), data / "browser_profile")
    config.refresh_posting_details = lambda url, *a, **k: job_page.Posting(title="Test Analyst", company="Example Works")
    config.validate_job_url = lambda url, *a, **k: True
    apply_jobs.refresh_credentials = lambda: None
    config.get_llm = lambda: ChatOpenAI(model="test-local-model", api_key="unused-local-test-key")
    config.get_fallback_llm = lambda: None
    apply_jobs.BrowserSession = lambda **kw: BrowserSession(headless=True, **kw)
    apply_jobs.Agent = lambda **kw: Agent(**{**kw, "calculate_cost": False})

    clicked: set[str] = set()

    def _text_of(messages) -> str:
        parts = []
        for message in messages:
            content = getattr(message, "content", "")
            if isinstance(content, str):
                parts.append(content)
            else:
                parts.extend(getattr(part, "text", "") or "" for part in content or [])
        return "\n".join(parts)

    async def scripted(self, messages, output_format=None, **kwargs):
        # Keeps the agent busy without ever finishing: each step waits two seconds.
        await asyncio.sleep(0.3)
        if output_format is None:
            return ChatInvokeCompletion(completion="ok", usage=None)
        action = {"wait": {"seconds": 2}}
        if args.scenario == "newtab":
            import re
            text = _text_of(messages)
            link = re.search(r"\[(\d+)\]<a[^\n]*\n?[^\n]*Apply Now", text)
            job = re.search(r"Current url: \S*/job/(\d+)", text) or re.search(r"/job/(\d+)", text)
            key = job.group(1) if job else "?"
            if link and key not in clicked and "Application form" not in text.split("Current url:")[-1][:400]:
                clicked.add(key)
                action = {"click": {"index": int(link.group(1))}}
        value = output_format.model_validate({
            "evaluation_previous_goal": "Waited", "memory": "Local run-control test", "next_goal": "Keep going",
            "action": [action]})
        return ChatInvokeCompletion(completion=value, usage=None)

    ChatOpenAI.ainvoke = scripted

    if args.legacy_gate:
        from core import request_gate
        real_gate = request_gate.RequestGate

        def bridge(request):
            with httpx.Client(trust_env=False) as client:
                reply = client.request(request.method, f"http://127.0.0.1:{port}" + request.url.raw_path.decode("ascii"),
                                       headers=dict(request.headers), content=request.content)
            return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)

        request_gate.RequestGate = lambda *a, **kw: real_gate(*a, **kw, transport_factory=lambda: httpx.MockTransport(bridge))

    client = TestClient(backend.app, base_url="http://127.0.0.1")
    client.headers.update({"Authorization": "Bearer repro-controls-token"})

    def status():
        return client.get("/apply/status").json()

    def current():
        jobs_now = status().get("current") or []
        return jobs_now[0] if jobs_now else None

    def wait_for(predicate, seconds, every=0.5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            time.sleep(every)
        return None

    report = {}
    try:
        started = client.post("/apply/start", json={"mode": "all", "run_mode": "trial", "workers": 1}).json()
        report["start"] = started
        if args.scenario == "newtab":
            first = wait_for(lambda: clicked and current(), 120)   # the Apply Now click has been made
        else:
            first = wait_for(lambda: (current() or {}).get("step", 0) >= 2 and current(), 120)
        if not first:
            report["error"] = "The run never reached step 2: " + json.dumps(status().get("log", [])[-6:])
            return _finish(report, args.out)
        first_url = first["url"]
        if args.scenario == "newtab":
            # After the click the form is in a new tab: does the agent keep taking steps?
            reached = (current() or {}).get("step", 0)
            later = wait_for(lambda: ((current() or {}).get("step") or 0) >= reached + 3 and current(), 90)
            report["after_new_tab"] = {"works": bool(later), "step_at_click": reached,
                                       "step_90s_later": (current() or {}).get("step"), "clicked_jobs": sorted(clicked)}

        # ── Pause ──
        client.post("/apply/pause")
        time.sleep(PAUSE_SECONDS / 2)          # let the action in flight finish
        before = (current() or {}).get("step")
        time.sleep(PAUSE_SECONDS / 2)
        after = (current() or {}).get("step")
        report["pause"] = {"works": before is not None and before == after, "step_before": before, "step_after": after,
                           "status_says_paused": bool(status().get("paused"))}

        # ── Resume ──
        at_resume = (current() or {}).get("step") or 0
        t0 = time.monotonic()
        client.post("/apply/resume")
        resumed = wait_for(lambda: (current() or {}).get("url") == first_url
                           and ((current() or {}).get("step") or 0) > at_resume, RESUME_SECONDS)
        report["resume"] = {"works": bool(resumed), "seconds": round(time.monotonic() - t0, 1),
                            "same_job": bool(current() and current()["url"] == first_url)}

        # ── Skip ──
        t0 = time.monotonic()
        skipped = client.post("/apply/skip", json={"url": first_url}).json()
        moved = wait_for(lambda: current() and current()["url"] != first_url, SKIP_SECONDS)
        saved = config.read_jobs().get(first_url) or {}
        report["skip"] = {"works": bool(moved), "seconds": round(time.monotonic() - t0, 1), "api": skipped,
                          "skipped_job_status": saved.get("status"), "skipped_job_note": (saved.get("error") or "")[:80]}

        # ── Stop ──
        t0 = time.monotonic()
        client.post("/apply/stop")
        ended = wait_for(lambda: not status().get("running"), STOP_SECONDS)
        seconds = round(time.monotonic() - t0, 1)
        time.sleep(15)                          # does anything start again by itself?
        again = status()
        report["stop"] = {"works": bool(ended) and not again.get("running") and not again.get("current"),
                          "seconds_until_not_running": seconds if ended else None,
                          "running_15s_later": bool(again.get("running")),
                          "job_statuses": {u[-1]: (config.read_jobs().get(u) or {}).get("status") for u in jobs}}
    except Exception as error:
        report["error"] = f"{type(error).__name__}: {error}"
    finally:
        try:
            client.post("/apply/stop")
        except Exception:
            pass
        site.shutdown()
    return _finish(report, args.out)


def _finish(report: dict, out: str) -> int:
    print("\n=== Run controls ===")
    for name in ("after_new_tab", "pause", "resume", "skip", "stop"):
        entry = report.get(name)
        if entry is None and name == "after_new_tab":
            continue
        if entry is None:
            print(f"{name:13} not reached")
        else:
            print(f"{name:13} {'WORKS ' if entry.get('works') else 'BROKEN'} {json.dumps({k: v for k, v in entry.items() if k != 'works'})}")
    if report.get("error"):
        print("error:", report["error"])
    if out:
        Path(out).write_text(json.dumps(report, indent=1), encoding="utf-8")
    time.sleep(2)
    shutil.rmtree(_HOME, ignore_errors=True)
    return 0 if all((report.get(n) or {}).get("works") for n in ("pause", "resume", "skip", "stop")) else 1


if __name__ == "__main__":
    code = main()
    os._exit(code)   # leftover non-daemon worker threads must not keep the script alive
