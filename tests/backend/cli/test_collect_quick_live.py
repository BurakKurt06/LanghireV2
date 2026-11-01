"""Quick collection in a real browser: a fake local job board, a fake AI, nothing real.

The board is served by a local test server under a made-up https name through the
request gate (as every collection page is). It has a scrolling results list, a second
page, listings that each of the user's filters must leave out, and hidden trap links
that a person couldn't see or click.
"""
import asyncio
import os
import re

import httpx
import pytest

import conftest

from cli import collect_jobs
from core import job_search, request_gate, shared_config
from sources.registry import _parse_plugin_yaml
from tests.backend.core.test_request_gate_live import wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in browser")]

BOARD = "https://jobs.boardtest.example"


def card(job_id, *lines):
    title, rest = lines[0], lines[1:]
    return (f'<li><a href="/job/{job_id}?trk=list">{title}</a>'
            + "".join(f"<div>{line}</div>" for line in rest) + "</li>")


STYLE = """<style>
body { margin: 0; font: 14px sans-serif; }
ul { height: 320px; overflow-y: auto; margin: 0; padding: 0; list-style: none; }
li { height: 110px; border-bottom: 1px solid #ccc; }
</style>"""

TRAPS = """
<a style="display:none" href="/job/900001">Hidden trap</a>
<a style="position:absolute;left:-9999px;top:0" href="/job/900002">Offscreen trap</a>
<span style="display:block;width:0;height:0;overflow:hidden"><a href="/job/900003">Tiny trap</a></span>
<a style="opacity:0" href="/job/900004">Invisible trap</a>
<a style="position:fixed;top:600px;left:10px" href="/job/900005">Covered trap</a>
<div style="position:fixed;top:590px;left:0;width:600px;height:60px;background:#fff;z-index:5"></div>
"""

PAGE_1 = f"""<!doctype html><html><head><title>Data Analyst jobs</title>{STYLE}</head><body><main><ul>
{card(100001, "Data Analyst", "Acme Test Co", "Boston, MA (Hybrid)", "$25/hr", "Part-time")}
{card(100002, "Senior Data Analyst", "Globex Test", "Remote", "$60/hr", "Part-time")}
{card(100003, "Data Analyst", "Blocked Test Co", "Austin, TX", "Part-time")}
{card(100004, "Data Analyst Intern", "Initech Test", "Denver, CO", "$15/hr", "Part-time")}
{card(100005, "Junior Data Analyst", "Umbrella Test", "Remote")}
{card(100006, "Data Analyst I", "Hooli Test", "Remote")}
{card(100007, "Data Analyst", "Wayne Test", "Chicago, IL", "Applied")}
{card(100008, "Data Analyst", "Stark Test", "Full-time", "$90,000 a year")}
{card(100009, "Data Analyst", "Cyberdyne Test", "Remote", "$30/hr", "Part-time")}
</ul></main>{TRAPS}</body></html>"""

PAGE_2 = f"""<!doctype html><html><head><title>Data Analyst jobs</title>{STYLE}</head><body><main><ul>
{card(100010, "Data Analyst", "Vandelay Test", "Remote", "$28/hr", "Part-time")}
{card(100011, "Data Analyst", "Acme Test Co", "Boston, MA", "$26/hr", "Part-time")}
</ul></main></body></html>"""

PAGE_3 = f"<!doctype html><html><head><title>Data Analyst jobs</title>{STYLE}</head><body><main><ul></ul></main></body></html>"

SCORES = {"Umbrella Test": 7, "Hooli Test": 9}


def _board(wire_site, data_dir, monkeypatch):
    """The fake two-page board behind the request gate, a fake AI recording its prompts, the plugin."""
    from browser_use import BrowserSession
    from browser_use.llm import ChatOpenAI
    from browser_use.llm.views import ChatInvokeCompletion
    url, seen, site = wire_site
    site["html"] = PAGE_3
    site["responses"] = {"/search?q=Data+Analyst": (200, {}, PAGE_1.encode()),
                         "/search?q=Data+Analyst&page=2": (200, {}, PAGE_2.encode())}
    logs = data_dir / "logs"
    logs.mkdir()
    monkeypatch.setattr(collect_jobs, "LOGS_DIR", logs)
    monkeypatch.setattr(collect_jobs, "refresh_credentials", lambda: None)
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings: (
        conftest.live_browser(), data_dir / "isolated-browser"))
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(shared_config, "validate_job_url", lambda u, resolve=True: str(u).startswith(BOARD + "/"))
    monkeypatch.setattr(collect_jobs, "BrowserSession", lambda **kw: BrowserSession(headless=True, **kw))
    monkeypatch.setattr(shared_config, "get_llm", lambda: ChatOpenAI(model="test-local-model", api_key="unused-local-test-key"))
    monkeypatch.setattr(shared_config, "get_fallback_llm", lambda: None)
    prompts = []

    async def label(self, messages, output_format=None, **kwargs):
        prompt = messages[-1].content
        prompts.append(prompt)
        rows = []
        for n, row in re.findall(r"^\[(\d+)\] (.*)$", prompt.split("LISTINGS:")[1], re.M):
            parts = row.split(" | ")
            rows.append({"n": int(n), "title": parts[0], "company": parts[1], "location": parts[2] if len(parts) > 2 else "",
                         "pay": next((p for p in parts if "$" in p), ""),
                         "job_type": next((p for p in parts if p.endswith("time")), ""),
                         "match": SCORES.get(parts[1], 8), "why": "test score"})
        return ChatInvokeCompletion(completion=output_format.model_validate({"listings": rows}), usage=None)
    monkeypatch.setattr(ChatOpenAI, "ainvoke", label)
    real_gate = request_gate.RequestGate

    def bridge(request):
        with httpx.Client(trust_env=False) as client:
            response = client.request(request.method, url + request.url.raw_path.decode().lstrip("/"),
                                      headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(request_gate, "RequestGate",
                        lambda *a, **k: real_gate(*a, **k, transport_factory=lambda: httpx.MockTransport(bridge)))
    plugin = _parse_plugin_yaml({
        "name": "boardtest", "display_name": "Board Test", "version": "1", "author": "tests", "countries": ["US"],
        "website": BOARD, "search_url": BOARD + "/search?q={title}", "collection_prompt": "unused", "apply_prompt": "unused",
        "listing": {"job_link": r"/job/(\d{6})", "next_page": {"param": "page", "step": 1, "first": 1},
                    "skip_lines": ["Applied"]},
    }, "boardtest.yaml", is_builtin=False)
    return plugin, prompts


@pytest.mark.asyncio
async def test_quick_collection_reads_whole_pages_with_one_ai_request_each(wire_site, data_dir, monkeypatch):
    plugin, prompts = _board(wire_site, data_dir, monkeypatch)
    seen = wire_site[1]
    existing = {}
    profile = {"job_search": {"mode": "quick", "job_types": ["part_time"], "min_match": 5,
                              "min_pay": {"amount": 20, "period": "hour", "currency": "USD"},
                              "exclude_companies": ["Blocked Test Co"], "exclude_title_words": ["Senior"]}}
    saved = await asyncio.wait_for(collect_jobs.collect_for_title("Data Analyst", existing, profile, plugin=plugin), 240)

    def job(n):
        return f"{BOARD}/job/{n}"
    assert [j["url"] for j in saved] == [job(100001), job(100006), job(100009), job(100010)]
    stored = shared_config.read_jobs()
    assert set(stored) == {job(100001), job(100006), job(100009), job(100010)}
    assert stored[job(100001)]["pay"]["high"] == 25 and stored[job(100001)]["job_types"] == ["part_time"]
    assert stored[job(100001)]["work_mode"] == "hybrid" and stored[job(100001)]["company"] == "Acme Test Co"
    assert stored[job(100006)].get("pay") is None and stored[job(100006)]["match"] == 9
    left = {u: e["kind"] for u, e in job_search.filtered_jobs().items()}
    assert left == {job(100002): "title", job(100003): "company", job(100004): "pay", job(100005): "match",
                    job(100008): "job_type", job(100011): "duplicate"}
    # One AI request per results page (the empty third page needs none), and no trap ever reached it.
    assert len(prompts) == 2
    assert not any("trap" in p.lower() for p in prompts)
    assert not any("/job/9000" in u for u in list(stored) + list(left))
    assert ("GET", "/search?q=Data+Analyst&page=2") in seen and ("GET", "/search?q=Data+Analyst&page=3") in seen


@pytest.mark.asyncio
async def test_a_repeated_search_spends_no_ai_request_on_listings_already_handled(wire_site, data_dir, monkeypatch):
    """Listings already in the list (or left out before) were labelled again on every repeated search,
    though the intake drops them: a page of them cost an AI request (the user has about 20 a day)."""
    plugin, prompts = _board(wire_site, data_dir, monkeypatch)

    def job(n):
        return f"{BOARD}/job/{n}"
    existing = {job(n): {"title": "Earlier listing", "company": f"Earlier Co {n}", "status": "pending"}
                for n in range(100001, 100010)}     # every listing on page 1
    saved = await asyncio.wait_for(collect_jobs.collect_for_title(
        "Data Analyst", existing, {"job_search": {"mode": "quick"}}, plugin=plugin), 240)
    assert [j["url"] for j in saved] == [job(100010), job(100011)]
    assert len(prompts) == 1, prompts    # page 2 only
    assert "Vandelay Test" in prompts[0] and "Globex Test" not in prompts[0]
