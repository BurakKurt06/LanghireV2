"""Real local-browser collection attacks; no accounts, models, or external pages."""
import asyncio
import os

import pytest

import conftest
import httpx

from cli import collect_jobs
from core import browser_policy
from tests.backend.core.test_request_gate_live import browser_and_gate, wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]


@pytest.mark.asyncio
async def test_clipping_overlays_and_repeated_links_only_return_clickable_anchors(wire_site):
    url, seen, source = wire_site
    source["html"] = """<style>body{margin:0}a{display:block;width:150px;height:24px}</style>
      <div><a href="/job/1">Real first</a><span>Company</span><span style="opacity:0">Invisible salary $999</span></div>
      <div style="position:relative;width:200px;height:40px">
        <a href="/job/2">Covered in own card</a>
        <div style="position:absolute;inset:0;background:white;z-index:9">Overlay</div></div>
      <div style="clip-path:circle(0%)"><a href="/job/3">Clipped trap</a></div>
      <div style="position:relative;height:40px"><a href="/job/4">Covered duplicate</a>
        <div style="position:absolute;inset:0;background:white;z-index:9"></div></div>
      <div><a href="/job/4">Visible duplicate</a></div>
      <div style="opacity:.1"><div style="opacity:.1"><a href="/job/6">Cumulative opacity trap</a></div></div>
      <div style="visibility:hidden"><a style="visibility:visible" href="/job/7">Visible child override</a></div>
      <div id="shadow"></div><script>
        document.querySelector('#shadow').attachShadow({mode:'open'}).innerHTML=
          '<div><a href="/job/5">Visible shadow listing</a></div>';
      </script>"""
    browser, gate = await browser_and_gate("trial", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        result = await browser_policy.evaluate(browser, collect_jobs.LISTING_JS, [r"/job/(\d+)"], 60)
        assert {c["id"] for c in result["cards"]} == {"1", "4", "5", "7"}, result
        assert next(c for c in result["cards"] if c["id"] == "4")["title"] == "Visible duplicate"
        assert "Invisible salary" not in next(c for c in result["cards"] if c["id"] == "1")["text"]
        assert not any(r[0] == "POST" for r in seen)
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("trap", [
    '<form id="application" method="post" action="/sent"><input name="answer" value="local"></form>'
    '<button form="application">Next</button>',
    '<button form="application" formaction="/sent" formmethod="post">Show more jobs</button>'
    '<form id="application"><input name="answer" value="local"></form>',
])
async def test_pagination_never_submits_an_externally_owned_form(wire_site, trap):
    url, seen, source = wire_site
    source["html"] = trap
    browser, gate = await browser_and_gate("trial", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        await browser_policy.evaluate(browser, collect_jobs.NEXT_JS)
        await asyncio.sleep(.3)
        assert not any(r[0] == "POST" or (r[0] == "GET" and "/sent" in r[1]) for r in seen), seen
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_real_load_more_button_still_works(wire_site):
    url, seen, source = wire_site
    source["html"] = '<button type="button" onclick="document.body.dataset.more=\'yes\'">Show more jobs</button>'
    browser, gate = await browser_and_gate("trial", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        result = await browser_policy.evaluate(browser, collect_jobs.NEXT_JS)
        assert result.get("clicked") == "Show more jobs"
        assert await browser_policy.evaluate(browser, "() => document.body.dataset.more") == "yes"
        assert not any(r[0] == "POST" for r in seen)
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_imported_board_redirect_cannot_load_a_document_on_another_host(wire_site, data_dir, monkeypatch):
    from browser_use import BrowserSession
    from core import request_gate, shared_config
    from tests.backend.cli.test_collect_review_1018 import plugin
    url, seen, site = wire_site
    site["responses"] = {"/start": (302, {"Location": "https://outside.example/target"}, b"")}
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings: (
        conftest.live_browser(), data_dir/"browser"))
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(collect_jobs, "BrowserSession", lambda **kw: BrowserSession(headless=True, **kw))
    real = request_gate.RequestGate
    def bridge(req):
        with httpx.Client(trust_env=False) as c:
            response = c.request(req.method, url+req.url.path.lstrip("/"), content=req.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)
    monkeypatch.setattr(request_gate, "RequestGate",
                        lambda *a, **k: real(*a, **k, transport_factory=lambda: httpx.MockTransport(bridge)))
    browser, gate = collect_jobs._open_browser(plugin(), "https://board.example/start", "Analyst")
    try:
        await browser.start()
        await gate.start()
        try:
            await asyncio.wait_for(browser.navigate_to("https://board.example/start"), 10)
        except Exception:
            pass  # a refused navigation may surface as the SDK's navigation error
        await asyncio.sleep(.3)
        assert ("GET", "/start") in seen
        assert ("GET", "/target") not in seen, seen
    finally:
        await gate.close()
