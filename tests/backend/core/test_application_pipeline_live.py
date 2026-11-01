"""The actual application pipeline, real SDK agent/browser, local HTTP, fake LLM."""
import asyncio
import os
from pathlib import Path

import httpx
import pytest

import conftest

from cli import apply_jobs
from core import request_gate, shared_config, job_page
from tests.backend.core.test_request_gate_live import wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in browser")]


@pytest.mark.asyncio
async def test_real_application_startup_installs_the_gate_and_a_trial_never_counts_a_page_claim_as_applied(wire_site, data_dir, monkeypatch):
    import fitz
    from browser_use import Agent, BrowserSession
    from browser_use.llm import ChatOpenAI
    from browser_use.llm.views import ChatInvokeCompletion
    url, seen, source = wire_site
    virtual = "https://acme.test/"
    source["html"] = "<title>Application</title><h1>Thank you for applying! Your application has been received.</h1>"
    resume = data_dir / "resume.pdf"
    pdf = fitz.open()
    pdf.new_page().insert_text((72, 72), "Ada Test\nPython")
    pdf.save(resume)
    pdf.close()
    logs = data_dir / "logs"
    logs.mkdir()
    monkeypatch.setattr(apply_jobs, "LOGS_DIR", logs)
    monkeypatch.setattr(shared_config, "LOGS_DIR", logs)
    monkeypatch.setattr(apply_jobs, "JOBS_FILE", shared_config.JOBS_FILE)
    monkeypatch.setattr(apply_jobs, "QA_FILE", shared_config.QA_FILE)
    monkeypatch.setattr(apply_jobs, "CANDIDATE_PROFILE", shared_config.CANDIDATE_PROFILE)
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings: (
        conftest.live_browser(), data_dir / "isolated-browser"))
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"fit_check_enabled": False, "cover_letter_boxes": False})
    monkeypatch.setattr(shared_config, "refresh_posting_details", lambda *a: job_page.Posting(title="Intern", company="Acme"))
    monkeypatch.setattr(shared_config, "validate_job_url", lambda u: u == virtual)
    monkeypatch.setattr(apply_jobs, "current_settings", lambda: (str(resume), {"email": "candidate@example.com"}, []))
    monkeypatch.setattr(apply_jobs, "refresh_credentials", lambda: None)
    monkeypatch.setattr(shared_config, "get_llm", lambda: ChatOpenAI(model="test-local-model", api_key="unused-local-test-key"))
    monkeypatch.setattr(shared_config, "get_fallback_llm", lambda: None)
    async def local_completion(self, messages, output_format=None, **kwargs):
        value = output_format.model_validate({"evaluation_previous_goal": "Loaded the local test page",
            "memory": "Only a local test", "next_goal": "Finish", "action": [{"done": {"text": "Application received", "success": True}}]})
        return ChatInvokeCompletion(completion=value, usage=None)
    monkeypatch.setattr(ChatOpenAI, "ainvoke", local_completion)
    monkeypatch.setattr(apply_jobs, "BrowserSession", lambda **kw: BrowserSession(headless=True, **kw))
    monkeypatch.setattr(apply_jobs, "Agent", lambda **kw: Agent(**{**kw, "calculate_cost": False}))
    real_gate = request_gate.RequestGate
    def bridge(request):
        with httpx.Client(trust_env=False) as client:
            response = client.request(request.method, url + request.url.path.lstrip("/"), headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    def gate_factory(*args, **kwargs):
        return real_gate(*args, **kwargs, transport_factory=lambda: httpx.MockTransport(bridge))
    monkeypatch.setattr(request_gate, "RequestGate", gate_factory)
    job = {"url": virtual, "title": "Intern", "company": "Acme", "status": "pending"}
    shared_config.write_jobs({virtual: job})
    apply_jobs.set_run_mode("trial")
    task = asyncio.create_task(apply_jobs.apply_to_job(job, {"name": "Ada Test", "email": "candidate@example.com"}, {}, [], False, 1))
    approvals = []
    collecting = None
    try:
        for _ in range(600):
            rows = request_gate.pending()
            if rows:
                approvals.extend(rows)
                assert request_gate.decide([r["id"] for r in rows], True)
            if task.done():
                break
            await asyncio.sleep(.05)
        result = await asyncio.wait_for(task, 10)
        assert approvals == [] and ("GET", "/") in seen   # the page loaded through the gate, without questions
        assert result == "practised"  # even a fake success/receipt can never mark Trial applied
        saved = shared_config.read_jobs()[virtual]
        assert not saved.get("submit_attempted_at") and "submission_evidence" not in saved
        assert not any(r[0] == "POST" for r in seen)
        # Collection uses the same real SDK and broker, including the newly
        # separated proxy purpose; its approvals must remain visible in the app.
        from cli import collect_jobs
        from types import SimpleNamespace
        monkeypatch.setattr(collect_jobs, "LOGS_DIR", logs)
        monkeypatch.setattr(collect_jobs, "refresh_credentials", lambda: None)
        monkeypatch.setattr(collect_jobs, "BrowserSession", lambda **kw: BrowserSession(headless=True, **kw))
        monkeypatch.setattr(collect_jobs, "Agent", lambda **kw: Agent(**{**kw, "calculate_cost": False}))
        plugin = SimpleNamespace(name="local_test", display_name="Local test board", is_builtin=False,
            website=virtual, search_url=virtual, login_url="", render_collection_prompt=lambda *a, **k: "Read only this local test board and finish.",
            build_search_url=lambda *a, **k: virtual)
        collecting = asyncio.create_task(collect_jobs.collect_for_title("Intern", {}, {"languages": ["English"]}, plugin=plugin))
        collected_reviews = []
        for _ in range(600):
            rows = request_gate.pending()
            if rows:
                collected_reviews.extend(rows)
                assert request_gate.decide([r["id"] for r in rows], True)
            if collecting.done():
                break
            await asyncio.sleep(.05)
        assert await asyncio.wait_for(collecting, 10) == []
        assert collected_reviews == []   # collection loads its pages without questions too
        assert seen.count(("GET", "/")) >= 2
    finally:
        if collecting is not None and not collecting.done():
            collecting.cancel()
            await asyncio.gather(collecting, return_exceptions=True)
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        apply_jobs.set_run_mode("auto")
