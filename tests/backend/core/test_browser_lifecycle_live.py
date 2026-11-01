"""Actual guarded Chromium launches, including failed/cancelled CDP startup."""
import asyncio
import gc
import os

import pytest

import conftest

from core import browser_lifecycle, net_guard

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in browser"),
    pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")]


@pytest.mark.parametrize("outcome", ["failure", "cancel", "kill"])
async def test_browser_pipes_are_closed_even_when_startup_does_not_complete(tmp_path, monkeypatch, outcome):
    from browser_use import BrowserSession
    from browser_use.browser.events import BrowserKillEvent
    from browser_use.browser.watchdogs.local_browser_watchdog import LocalBrowserWatchdog

    profile = tmp_path / "profile"
    net_guard.limit_webrtc(profile)
    # An invalid local-only proxy keeps this lifecycle-only test off the network.
    args = ["--proxy-server=http://127.0.0.1:1", "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic"]
    browser = BrowserSession(executable_path=conftest.live_browser(),
        user_data_dir=profile, headless=True, permissions=[], enable_default_extensions=False, args=args)
    watchdog = LocalBrowserWatchdog(browser_session=browser, event_bus=browser.event_bus)
    ready = asyncio.Event()
    original = LocalBrowserWatchdog._wait_for_cdp_url

    async def after_ready(*args, **kwargs):
        url = await original(*args, **kwargs)
        ready.set()
        if outcome == "failure":
            raise RuntimeError("test CDP startup failure")
        if outcome == "cancel":
            await asyncio.Future()
        return url

    monkeypatch.setattr(LocalBrowserWatchdog, "_wait_for_cdp_url", staticmethod(after_ready))
    launch = asyncio.create_task(watchdog._launch_browser(max_retries=1))
    try:
        await asyncio.wait_for(ready.wait(), 20)
        if outcome == "cancel":
            launch.cancel()
        if outcome == "kill":
            process, _ = await launch
            watchdog._subprocess = process
            await watchdog.on_BrowserKillEvent(BrowserKillEvent())
        else:
            with pytest.raises(asyncio.CancelledError if outcome == "cancel" else RuntimeError):
                await launch
        owner = watchdog.__dict__[browser_lifecycle._STATE]
        assert len(owner.children) == 1
        child = owner.children[0]
        assert child.process.returncode is not None
        assert child.process.stdout.at_eof() and child.process.stderr.at_eof()
        assert child.process._transport.is_closing()
        assert all(t.done() and not t.cancelled() and t.exception() is None for t in child.readers)
        assert not any(p.is_running() for p in child.descendants)
        await watchdog.on_BrowserKillEvent(BrowserKillEvent())
        gc.collect()
    finally:
        if not launch.done():
            launch.cancel()
            await asyncio.gather(launch, return_exceptions=True)
        await browser_lifecycle._finish_cleanup(watchdog.__dict__[browser_lifecycle._STATE])
        await browser.event_bus.stop(clear=True, timeout=5)


async def test_cancelling_the_clean_close_still_kills_the_browser_and_its_pipes(tmp_path, monkeypatch):
    """Codex's case: a Stop while the browser is closing cancelled BrowserSession.kill before
    browser-use's own kill ran, leaving the browser, its descendants, pipe readers and the profile
    behind. The full kill still runs and the cancellation still propagates."""
    from browser_use import BrowserSession

    profile = tmp_path / "profile"
    net_guard.limit_webrtc(profile)
    args = ["--proxy-server=http://127.0.0.1:1", "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic"]
    browser = BrowserSession(executable_path=conftest.live_browser(), user_data_dir=str(profile),
        headless=True, permissions=[], enable_default_extensions=False, args=args)
    await browser.start()
    watchdog = browser._local_browser_watchdog
    process, child = watchdog._subprocess, watchdog.__dict__[browser_lifecycle._STATE].children[0]
    entered = asyncio.Event()

    async def held_close(session):
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(browser_lifecycle, "_close_cleanly", held_close)
    kill = asyncio.create_task(browser.kill())
    try:
        await asyncio.wait_for(entered.wait(), 10)
        kill.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(kill, 60)
        alive = process.is_running()
        assert not alive, "the browser outlived a cancelled kill"
        assert child.process.returncode is not None and child.process._transport.is_closing()
        assert all(t.done() and not t.cancelled() and t.exception() is None for t in child.readers)
        assert not any(p.is_running() for p in child.descendants)
        assert not net_guard._profile_in_use(profile)
    finally:
        if process.is_running():  # leave nothing behind when the kill was skipped
            monkeypatch.undo()
            await browser.kill()
        gc.collect()


def _still_running(process):
    import psutil
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False


async def test_a_clean_close_still_reaps_the_browsers_descendants(tmp_path):
    """After Browser.close the kill's own snapshot found the browser gone and recorded no
    descendants, so none could be reaped. They are recorded before the close, and none outlive the kill.
    (A zombie, left by a container init that does not reap orphans, has exited.)"""
    from browser_use import BrowserSession

    profile = tmp_path / "profile"
    net_guard.limit_webrtc(profile)
    args = ["--proxy-server=http://127.0.0.1:1", "--proxy-bypass-list=<-loopback>",
            "--host-resolver-rules=MAP * ~NOTFOUND", "--disable-quic"]
    browser = BrowserSession(executable_path=conftest.live_browser(), user_data_dir=str(profile),
        headless=True, permissions=[], enable_default_extensions=False, args=args)
    await browser.start()
    watchdog = browser._local_browser_watchdog
    child = watchdog.__dict__[browser_lifecycle._STATE].children[0]
    before = watchdog._subprocess.children(recursive=True)
    try:
        await browser.kill()
        assert before and child.descendants, "the browser's descendants were not recorded"
        assert {p.pid for p in before} <= {p.pid for p in child.descendants}
        assert not any(_still_running(p) for p in before)
        assert not net_guard._profile_in_use(profile)
    finally:
        for process in before:
            if _still_running(process):
                process.kill()
        gc.collect()



def _stop_scenario(monkeypatch, hard_kill):
    """An application run on one of the app's own profiles, through the app's worker thread: a site
    sets a cookie, the run keeps working, the user presses Stop. Returns (seconds until the run ended
    or None, the cookie a fresh browser finds on the profile afterwards)."""
    import http.server
    import threading
    import time

    import psutil

    import main
    from browser_use import BrowserSession
    from core import shared_config

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            if self.path == "/set":
                self.send_header("Set-Cookie", "kept=yes; Max-Age=86400; Path=/")
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<title>local</title>")

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    profile = main.get_data_dir() / "browser_profile"   # one of the app's own profiles: Stop sweeps it
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser_allowed_local_hosts": ["localhost"]})
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (conftest.live_browser(), profile))
    if hard_kill:   # Windows: terminate() is TerminateProcess
        monkeypatch.setattr(psutil.Process, "terminate", psutil.Process.kill)

    def session():
        kwargs = shared_config.build_browser_kwargs()
        kwargs.update(headless=True, enable_default_extensions=False)
        return BrowserSession(**kwargs)

    cookie_set = threading.Event()

    async def run():   # as apply_jobs does: the browser is killed in a finally
        browser = session()
        try:
            await browser.start()
            await browser.navigate_to(f"http://localhost:{server.server_port}/set")
            cookie_set.set()
            await asyncio.sleep(120)
        finally:
            await browser.kill()

    async def read_cookie():
        browser = session()
        try:
            await browser.start()
            await browser.navigate_to(f"http://localhost:{server.server_port}/read")
            return await (await browser.get_current_page()).evaluate("() => document.cookie")
        finally:
            await browser.kill()

    status = {"running": False, "log": []}
    thread = main._run_async_in_thread(run, status)
    try:
        assert cookie_set.wait(60), "the run never reached the page"
        started = time.monotonic()
        main._force_stop(status)
        thread.join(30)
        ended = None if status["running"] else time.monotonic() - started
        main._kill_browser_processes()   # a hung run's browser must not hold the profile for the read
        return ended, asyncio.run(read_cookie())
    finally:
        server.shutdown()
        server.server_close()


def test_stop_ends_the_run_while_its_browser_is_open(monkeypatch):
    """Stop cancelled every task on the run's loop, browser-use's own event bus too: the run's
    browser.kill() then waited for an event nobody would handle, so the run never ended and stayed
    "running" (a new run was refused) until the app restarted."""
    ended, _ = _stop_scenario(monkeypatch, hard_kill=False)
    assert ended is not None, "the run was still running 30 s after Stop"


def test_stop_lets_the_browser_close_cleanly_so_a_cookie_just_set_is_kept(monkeypatch):
    """With Windows' hard kill, Stop's sweep lost what the site stored just before. Stop now waits
    briefly for the clean close the cancellation starts (user's decision 11-01)."""
    ended, cookie = _stop_scenario(monkeypatch, hard_kill=True)
    assert ended is not None, "the run was still running 30 s after Stop"
    assert cookie == "kept=yes", "the cookie set just before Stop was lost"
