"""A collection never sits for hours on one board (Codex's note of 2026-11-17).

Seen on 1.3.2: a board's browser started, then nothing for four and a half hours while the
collection still reported "running"; the run only ended when the app was closed, and the request
gate was then still waiting for the browser to answer "Network.enable". Here a browser that never
answers is a stand-in (no browser, no network): every wait is bounded, the stuck board ends with
a message naming the phase, its browser is closed, and the next board is collected.
"""
import asyncio
import os
import time
from types import SimpleNamespace

import pytest

from cli import collect_jobs
from core import request_gate


def never():
    """A browser command the browser never answers."""
    return asyncio.get_running_loop().create_future()


async def answered(value=None):
    return value


class SilentCDP:
    """The gate's own browser connection, where the commands listed in `stuck` never get an answer."""

    def __init__(self, stuck=()):
        self.stuck, self.sent = set(stuck), []

        def domain(name):
            def command(method):
                def send(*args, **kwargs):
                    self.sent.append(f"{name}.{method}")
                    reply = {"getTargets": {"targetInfos": []}, "getCookies": {"cookies": []}}.get(method, {})
                    return never() if f"{name}.{method}" in self.stuck else answered(reply)
                return send
            return SimpleNamespace(**{m: command(m) for m in (
                "enable", "setBlockedURLs", "setBypassServiceWorker", "setCacheDisabled", "setAutoAttach",
                "closeTarget", "runIfWaitingForDebugger", "failRequest", "fulfillRequest", "getTargets", "getCookies")})

        self.send = SimpleNamespace(**{d: domain(d) for d in ("Network", "Fetch", "Target", "Runtime", "Storage")})

    async def stop(self):
        pass


def gate_with(cdp, monkeypatch, collection=True):
    monkeypatch.setattr(request_gate, "COMMAND_SECONDS", 0.2)
    killed = []

    async def kill():
        killed.append(True)

    browser = SimpleNamespace(kill=kill)
    gate = request_gate.RequestGate(browser, "trial", {"url": "https://jobs.example/search"},
                                    collection_policy=SimpleNamespace(sealed=False) if collection else None)
    gate.client = cdp
    return gate, killed


async def test_a_frame_whose_interception_is_never_confirmed_ends_the_browser_in_seconds(monkeypatch):
    gate, killed = gate_with(SilentCDP(stuck={"Network.enable"}), monkeypatch)
    started = time.monotonic()
    await asyncio.wait_for(gate._attached({"targetInfo": {"type": "iframe", "targetId": "f1"}, "sessionId": "s1"}), 5)
    assert time.monotonic() - started < 2
    assert gate.failed and killed == [True]          # closed, never left browsing without the gate
    assert "f1" not in gate.targets                   # and never resumed without interception


async def test_control_a_frame_that_answers_is_set_up_and_resumed(monkeypatch):
    cdp = SilentCDP()
    gate, killed = gate_with(cdp, monkeypatch)
    await gate._attached({"targetInfo": {"type": "iframe", "targetId": "f1"}, "sessionId": "s1"})
    assert not gate.failed and killed == [] and "f1" in gate.targets
    assert cdp.sent[-1] == "Runtime.runIfWaitingForDebugger" and "Fetch.enable" in cdp.sent


async def test_the_lock_is_free_again_for_the_next_frame_after_one_got_no_answer(monkeypatch):
    """The stuck command held the lock every later frame waits on."""
    gate, _ = gate_with(SilentCDP(stuck={"Fetch.enable"}), monkeypatch)
    with pytest.raises(asyncio.TimeoutError):
        await gate._configure("s1")
    assert not gate._configure_lock.locked()


async def test_a_worker_that_never_answers_stays_paused_and_the_page_goes_on(monkeypatch):
    gate, killed = gate_with(SilentCDP(stuck={"Network.enable"}), monkeypatch)
    await asyncio.wait_for(gate._attached({"targetInfo": {"type": "worker", "targetId": "w1"}, "sessionId": "ws"}), 5)
    assert "ws" in gate.held_workers and not gate.failed and killed == []
    assert "Runtime.runIfWaitingForDebugger" not in SilentCDP().sent      # a held worker is never resumed


async def test_collection_waits_seconds_not_twenty_six_minutes_and_puts_the_limits_back(monkeypatch):
    for event in request_gate._SLOW_EVENTS:
        monkeypatch.delenv("TIMEOUT_" + event, raising=False)

    class Client(SilentCDP):
        def __init__(self, url):
            super().__init__()
            self.register = SimpleNamespace(Target=SimpleNamespace(attachedToTarget=lambda f: None),
                                            Fetch=SimpleNamespace(requestPaused=lambda f: None))

        async def start(self):
            pass

    import cdp_use
    monkeypatch.setattr(cdp_use, "CDPClient", Client)
    browser = SimpleNamespace(cdp_url="ws://made-up", cdp_client=SimpleNamespace(_cdp_request_timeout_s=60),
                              kill=lambda: answered())
    for collecting, expected in ((True, (45, "75")), (False, (1560, "1560"))):
        policy = SimpleNamespace(sealed=False, snapshot_cookies=lambda c: None) if collecting else None
        gate = request_gate.RequestGate(browser, "trial", {"url": "https://jobs.example/s"}, collection_policy=policy)
        gate.healthy = lambda: True
        await gate.start()
        assert browser.cdp_client._cdp_request_timeout_s == expected[0]
        assert os.environ["TIMEOUT_NavigateToUrlEvent"] == expected[1]
        await gate.close()
        assert "TIMEOUT_NavigateToUrlEvent" not in os.environ                # put back as it was


# ── the collector: a board that stops answering ends, and the next one is read ─────────────────
class HangingBrowser:
    def __init__(self, hang_at):
        self.hang_at, self.killed = hang_at, False

    async def start(self):
        if self.hang_at == "start":
            await never()

    async def navigate_to(self, url):
        if self.hang_at == "open":
            await never()

    async def kill(self):
        self.killed = True


class Network:
    def __init__(self):
        self.closed = False
        self.collection_policy = SimpleNamespace(refused=0, read_hosts=set())

    async def start(self):
        pass

    async def close(self):
        self.closed = True

    def healthy(self):
        return True


@pytest.fixture
def board(monkeypatch):
    """quick_collect against a stand-in browser; returns the browser and network it was given."""
    made = {}

    def open_browser(plugin, search_url, title):
        made["browser"], made["network"] = HangingBrowser(made["hang_at"]), Network()
        return made["browser"], made["network"]

    async def stuck_read(*args, **kwargs):
        if made["hang_at"] == "read":
            await never()
        return [], {}

    async def no_stealth(browser):
        return False

    monkeypatch.setattr(collect_jobs, "_open_browser", open_browser)
    monkeypatch.setattr(collect_jobs, "_read_page", stuck_read)
    monkeypatch.setattr(collect_jobs.config, "maybe_apply_stealth", no_stealth)
    monkeypatch.setattr(collect_jobs.asyncio, "sleep", lambda s: answered())
    for name, seconds in (("BROWSER_START_SECONDS", 0.3), ("OPEN_PAGE_SECONDS", 0.3), ("READ_PAGE_SECONDS", 0.3),
                          ("CLOSE_SECONDS", 0.3)):
        monkeypatch.setattr(collect_jobs, name, seconds)
    return made


PLUGIN = SimpleNamespace(display_name="Example Board", name="example", is_builtin=True,
                         listing=SimpleNamespace(job_link="/jobs/", next_param=""))


@pytest.mark.parametrize("hang_at,phase", [("start", "starting the browser"), ("open", "opening the results page"),
                                           ("read", "reading results page 1")])
async def test_a_board_that_stops_answering_ends_with_its_phase_named_and_its_browser_closed(board, capsys, hang_at, phase):
    board["hang_at"] = hang_at
    started = time.monotonic()
    await collect_jobs._whole_board("Example Board", collect_jobs.quick_collect(
        "Intern", PLUGIN, {}, {"mode": "quick"}, SimpleNamespace(), "https://board.example/search"), 30)
    assert time.monotonic() - started < 5
    out = capsys.readouterr().out
    assert f"Example Board didn't respond while {phase}" in out and "moving on" in out
    assert board["browser"].killed and board["network"].closed


async def test_control_a_board_that_answers_says_where_it_is_and_finishes(board, capsys):
    board["hang_at"] = ""
    await collect_jobs._whole_board("Example Board", collect_jobs.quick_collect(
        "Intern", PLUGIN, {}, {"mode": "quick"}, SimpleNamespace(), "https://board.example/search"), 30)
    out = capsys.readouterr().out
    assert "🌐 Starting the browser for Example Board" in out and "🔎 Opening Example Board's results page" in out
    assert "📖 Reading results page 1" in out and "didn't respond" not in out


async def test_a_board_that_never_finishes_is_ended_by_its_overall_limit(capsys):
    await collect_jobs._whole_board("Slow Board", never(), 0.2)
    assert "Slow Board didn't respond while collecting" in capsys.readouterr().out


async def test_stop_ends_a_stuck_board_at_once_and_closes_its_browser(board, monkeypatch):
    board["hang_at"] = "read"
    monkeypatch.setattr(collect_jobs, "READ_PAGE_SECONDS", 3600)    # the board would wait an hour by itself
    task = asyncio.ensure_future(collect_jobs._whole_board("Example Board", collect_jobs.quick_collect(
        "Intern", PLUGIN, {}, {"mode": "quick"}, SimpleNamespace(), "https://board.example/search"), 7200))
    await asyncio.wait({task}, timeout=0.3)
    assert not task.done()                                     # stuck reading the page
    task.cancel()                                              # Stop
    started = time.monotonic()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert time.monotonic() - started < 3
    assert board["browser"].killed and board["network"].closed


async def test_a_stuck_board_returns_normally_so_the_run_goes_on_with_the_next_board(monkeypatch, capsys):
    """collect_for_title is what the run calls for each board: it must come back, not raise or hang."""
    async def stuck_quick(*args, **kwargs):
        await never()

    monkeypatch.setattr(collect_jobs, "quick_collect", stuck_quick)
    monkeypatch.setattr(collect_jobs, "QUICK_BOARD_SECONDS", 0.2)
    monkeypatch.setattr(collect_jobs, "refresh_credentials", lambda: None)
    monkeypatch.setattr(collect_jobs, "_agent_log_start", lambda *a: None)
    monkeypatch.setattr(collect_jobs, "localize", lambda plugin, url, place: (url, ""))
    monkeypatch.setattr(collect_jobs.job_search, "for_search", lambda profile, search: {"mode": "quick"})

    class Intake:
        saved = []

        def __init__(self, *args):
            pass

        def report(self):
            pass

    monkeypatch.setattr(collect_jobs, "Intake", Intake)
    plugin = SimpleNamespace(**vars(PLUGIN), build_search_url=lambda *a, **k: "https://board.example/search")
    started = time.monotonic()
    found = await collect_jobs.collect_for_title("Intern", {}, {}, plugin=plugin)
    assert found == [] and time.monotonic() - started < 3
    assert "Example Board didn't respond while collecting" in capsys.readouterr().out
