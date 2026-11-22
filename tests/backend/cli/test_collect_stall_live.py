"""A board that stops answering, in a real browser: it ends, and the next board is collected.

Codex's note (2026-11-17): on 1.3.2 a board's browser started and the collection then sat for four
and a half hours, still "running", with nothing in its log. Here a local fake board (behind the
request gate, as every collection page is) answers its results page only after the time limit;
the board ends with the phase named, its browser is closed, and the next board is read normally.
"""
import asyncio
import os
import time

import pytest

from cli import collect_jobs
from core import shared_config
from tests.backend.cli.test_collect_quick_live import BOARD, _board
from tests.backend.core.test_request_gate_live import wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in browser")]


@pytest.mark.asyncio
async def test_a_board_that_never_loads_ends_and_the_next_board_is_collected(wire_site, data_dir, monkeypatch, capsys):
    import httpx
    from core import request_gate
    real_gate = request_gate.RequestGate                      # before _board wraps it with its own bridge
    plugin, prompts = _board(wire_site, data_dir, monkeypatch)
    url, seen, site = wire_site
    stalled = {"on": True}
    import threading
    release = threading.Event()

    def slow_bridge(request):
        # The first board's results page never answers (until the test ends): on 1.3.2 the board
        # then waited 26 minutes per browser command, i.e. hours.
        if stalled["on"] and request.url.raw_path.decode().startswith("/search"):
            release.wait(300)
        with httpx.Client(trust_env=False) as client:
            response = client.request(request.method, url + request.url.raw_path.decode().lstrip("/"),
                                      headers=dict(request.headers), content=request.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    monkeypatch.setattr(request_gate, "RequestGate", lambda *a, **k: real_gate(
        *a, **{**k, "transport_factory": lambda: httpx.MockTransport(slow_bridge)}))
    monkeypatch.setattr(collect_jobs, "OPEN_PAGE_SECONDS", 4)
    profile = {"job_search": {"mode": "quick"}}

    try:
        started = time.monotonic()
        first = await asyncio.wait_for(collect_jobs.collect_for_title("Data Analyst", {}, profile, plugin=plugin), 90)
        took = time.monotonic() - started
        out = capsys.readouterr().out
        assert first == [] and took < 60, took
        assert "🔎 Opening Board Test's results page" in out
        assert "Board Test didn't respond while opening the results page" in out and "moving on" in out
        assert prompts == []                                   # no AI request was spent on it
        stalled["on"] = False                                 # the next board answers
    finally:
        release.set()
    second = await asyncio.wait_for(collect_jobs.collect_for_title("Data Analyst", {}, profile, plugin=plugin), 240)
    assert [j["url"] for j in second][:1] == [f"{BOARD}/job/100001"]
    assert set(shared_config.read_jobs()) >= {j["url"] for j in second}
    assert "didn't respond" not in capsys.readouterr().out


# ── a results page that gives nothing: the log says what the reader saw ───────
from tests.backend.cli.test_collect_quick_live import PAGE_1  # noqa: E402

WALL = PAGE_1.replace("</body>", '<div id="consent-wall" style="position:fixed;inset:0;background:#fff;z-index:50">'
                                 "<h2>We value your privacy</h2><button>Accept all</button></div></body>")
MOVED_LINKS = PAGE_1.replace('href="/job/', 'href="/jobs/collections/recommended/?currentJobId=')


@pytest.mark.asyncio
@pytest.mark.parametrize("page,expected", [
    (WALL, ['Something covers the results: div#consent-wall "We value your privacy Accept all"',
            "14 matching this board's job-link pattern, some of them in view"]),        # 9 listings, 5 hidden traps
    (MOVED_LINKS, ["No job links found on Board Test's results page",
                   "0 matching this board's job-link pattern",
                   "look like: jobs.boardtest.example/jobs/collections/recommended/"]),
])
async def test_a_page_that_gives_no_listings_says_what_the_reader_saw(wire_site, data_dir, monkeypatch, capsys, page, expected):
    """Codex's note: one board said "something covers the results", another "no job links found",
    and nothing told which it really was (a sign-in or cookie wall, links out of view, or a page
    whose links changed shape). The log line now says."""
    plugin, prompts = _board(wire_site, data_dir, monkeypatch)
    wire_site[2]["responses"]["/search?q=Data+Analyst"] = (200, {}, page.encode())
    saved = await asyncio.wait_for(collect_jobs.collect_for_title(
        "Data Analyst", {}, {"job_search": {"mode": "quick"}}, plugin=plugin), 120)
    out = capsys.readouterr().out
    assert saved == [] and prompts == []
    for words in expected:
        assert words in out, out[-1500:]
    assert "Data+Analyst" not in out.split("What the reader saw", 1)[-1]          # no search terms in that line
