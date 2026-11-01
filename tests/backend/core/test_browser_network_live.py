"""Opt-in: the agent browser's network limits, checked in a real browser (no internet, no accounts).

Run with:  $env:LANGHIRE_REAL_BROWSER = "1"; uv run pytest tests/backend/core/test_browser_network_live.py
The browser is started with the app's own settings (shared_config.build_browser_kwargs), a
throwaway profile and local listeners only; the default run skips it.
"""
import asyncio
import http.server
import os
import socket
import threading
import json

import pytest

import conftest

from core import net_guard, shared_config

pytestmark = [
    pytest.mark.real_browser,
    pytest.mark.skipif(os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in: launches a real browser"),
]


def _run_in_agent_browser(tmp_path, monkeypatch, script, *args, wait=0.7, protect=True, settings=None, until=None):
    """Run `script` in an app-configured browser, then wait `wait` seconds, or until `until()` is true
    (at most `wait`): a request expected to arrive can take longer than 0.7 s on a slow runner."""
    from browser_use import BrowserSession
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: settings or {})
    # The app prepares its own (throwaway) profile folder, as it would before a real run.
    monkeypatch.setattr(shared_config, "selected_browser",
                        lambda settings=None: (conftest.live_browser(), tmp_path / "browser"))
    kwargs = (shared_config.build_browser_kwargs() if protect else {
        "executable_path": conftest.live_browser(),
        "user_data_dir": str(tmp_path / "browser"), "permissions": []})
    assert kwargs["user_data_dir"].startswith(str(tmp_path))
    kwargs.update(headless=True, enable_default_extensions=False)

    async def go():
        browser = BrowserSession(**kwargs)
        try:
            await browser.start()
            await browser.navigate_to("about:blank")
            await (await browser.get_current_page()).evaluate(script, *args)
            deadline = asyncio.get_running_loop().time() + wait
            while asyncio.get_running_loop().time() < deadline and not (until and until()):
                await asyncio.sleep(0.1)
        finally:
            await browser.kill()

    asyncio.run(go())


def test_a_page_cannot_request_a_loopback_service(tmp_path, monkeypatch):
    """Codex's check: an image on the page pointing at a service on this computer."""
    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _run_in_agent_browser(tmp_path, monkeypatch,
                              "(url) => { const i = document.createElement('img'); i.src = url; "
                              "document.body.appendChild(i); return 1; }",
                              f"http://127.0.0.1:{server.server_port}/probe")
    finally:
        server.shutdown()
        server.server_close()
    assert hits == []


def test_an_explicit_local_allowance_can_load_a_page_resource(tmp_path, monkeypatch):
    hits = []
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"dummy resource")
        def log_message(self, *args):
            pass
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        _run_in_agent_browser(tmp_path, monkeypatch,
            "(url) => { const i = document.createElement('img'); i.src = url; document.body.appendChild(i); return 1; }",
            f"http://127.0.0.1:{server.server_port}/allowed", settings={"browser_allowed_local_hosts": ["127.0.0.1"]},
            wait=10, until=lambda: hits)
        assert hits == ["/allowed"]
    finally:
        server.shutdown()
        server.server_close()


def test_webrtc_cannot_send_udp_to_this_computer(tmp_path, monkeypatch):
    """WebRTC doesn't use the proxy rules: a STUN server on this computer must get nothing."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(0.2)
    received, stop = [], threading.Event()

    def listen():
        while not stop.is_set():
            try:
                received.append(listener.recvfrom(2048)[1])
            except (socket.timeout, OSError):
                pass

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    try:
        _run_in_agent_browser(tmp_path, monkeypatch,
                              "(port) => { const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:' + port}]}); "
                              "pc.createDataChannel('probe'); pc.createOffer().then(o => pc.setLocalDescription(o)); "
                              "window.probe = pc; return 1; }",
                              listener.getsockname()[1], wait=3)
    finally:
        stop.set()
        thread.join()
        listener.close()
    assert received == []


def test_webrtc_tcp_cannot_bypass_the_proxy(tmp_path, monkeypatch):
    """A TCP TURN connection must also honor the production proxy restrictions."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    listener.settimeout(0.2)
    connections, stop = [], threading.Event()

    def listen():
        while not stop.is_set():
            try:
                connection, address = listener.accept()
                connections.append(address)
                connection.close()
            except (socket.timeout, OSError):
                pass

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()
    script = """(port) => {
      const pc = new RTCPeerConnection({iceServers: [{urls: 'turn:127.0.0.1:' + port + '?transport=tcp',
        username: 'dummy', credential: 'dummy'}]});
      pc.createDataChannel('probe'); pc.createOffer().then(o => pc.setLocalDescription(o));
      window.probe = pc; return 1;
    }"""
    try:
        _run_in_agent_browser(tmp_path / "control", monkeypatch, script, listener.getsockname()[1],
                              wait=3, protect=False)
        assert connections, "The unprotected control must actually reach the TCP listener"
        connections.clear()
        _run_in_agent_browser(tmp_path / "protected", monkeypatch, script, listener.getsockname()[1], wait=3)
        assert not connections
    finally:
        stop.set()
        thread.join()
        listener.close()


def test_running_profile_cannot_be_secured_by_rewriting_preferences(tmp_path):
    """A disk edit is not a live browser policy: refuse to claim it took effect."""
    from browser_use import BrowserSession
    # browser-use runs a copy of the profile when the executable path contains "chrome" (as the
    # bundled Chromium's does) unless the folder is named like its own temp folders. This test
    # needs the browser on this exact folder.
    profile = tmp_path / "browser-use-user-data-dir-live"
    prefs = profile / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"webrtc": {"ip_handling_policy": "default"}}))
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    listener.bind(("127.0.0.1", 0))
    listener.settimeout(0.2)
    received, stop = [], threading.Event()

    def listen():
        while not stop.is_set():
            try:
                received.append(listener.recvfrom(2048)[1])
            except (socket.timeout, OSError):
                pass

    thread = threading.Thread(target=listen, daemon=True)
    thread.start()

    async def go():
        browser = BrowserSession(executable_path=conftest.live_browser(),
                                 user_data_dir=str(profile), headless=True,
                                 permissions=[], enable_default_extensions=False)
        try:
            await browser.start()
            applied = net_guard.limit_webrtc(profile)
            await browser.navigate_to("about:blank")
            await (await browser.get_current_page()).evaluate(
                "(port) => { const pc = new RTCPeerConnection({iceServers: [{urls: 'stun:127.0.0.1:' + port}]}); "
                "pc.createDataChannel('probe'); pc.createOffer().then(o => pc.setLocalDescription(o)); "
                "window.probe = pc; return 1; }", listener.getsockname()[1])
            await asyncio.sleep(3)
            assert not applied, f"Reported secured while running browser sent {len(received)} UDP packets"
            assert received, "The unsafe control browser should demonstrate the live-policy mismatch"
        finally:
            await browser.kill()

    try:
        asyncio.run(go())
    finally:
        stop.set()
        thread.join()
        listener.close()


def test_one_workers_open_sockets_dont_block_another_workers_pages(tmp_path, monkeypatch):
    """Worker 1's page holds 120 WebSockets open. All workers used to share one proxy listener, which
    then had no free connections: worker 2's page failed with ERR_SOCKS_CONNECTION_FAILED."""
    import base64
    import hashlib
    from browser_use import BrowserSession
    held, pages = [], []

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self):
            if self.headers.get("Upgrade", "").lower() != "websocket":
                pages.append(self.path)
                body = b"second worker page"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            key = self.headers["Sec-WebSocket-Key"] + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
            self.send_response(101)
            self.send_header("Upgrade", "websocket")
            self.send_header("Connection", "Upgrade")
            self.send_header("Sec-WebSocket-Accept", base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            held.append(self.path)
            try:
                self.connection.settimeout(30)
                self.connection.recv(1)   # keep it open
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    # An allowed local host stands in for any public server that keeps sockets open.
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser_allowed_local_hosts": ["127.0.0.1"]})
    monkeypatch.setattr(shared_config, "selected_browser",
                        lambda settings=None: (conftest.live_browser(), tmp_path / "browser"))

    async def go():
        browsers = []
        for worker in (1, 2):
            kwargs = shared_config.build_browser_kwargs(worker=worker)
            assert kwargs["user_data_dir"].startswith(str(tmp_path))
            kwargs.update(headless=True, enable_default_extensions=False)
            browsers.append(BrowserSession(**kwargs))
            await browsers[-1].start()
        one, two = browsers
        try:
            await one.navigate_to("about:blank")
            await (await one.get_current_page()).evaluate(
                "(url) => { window.held = []; for (let i = 0; i < 120; i++) window.held.push(new WebSocket(url)); return 1; }",
                f"ws://127.0.0.1:{port}/ws")
            await asyncio.sleep(6)
            assert len(held) >= 60, "worker 1's page should be holding most of its proxy connections"
            await two.navigate_to("about:blank")
            await (await two.get_current_page()).evaluate(
                "(url) => { const i = document.createElement('img'); i.src = url; document.body.appendChild(i); return 1; }",
                f"http://127.0.0.1:{port}/second")
            await asyncio.sleep(2)
            assert pages == ["/second"]
        finally:
            for browser in browsers:
                await browser.kill()

    try:
        asyncio.run(go())
    finally:
        server.shutdown()
        server.server_close()


def test_what_the_agents_browser_stores_is_kept_for_the_next_run(tmp_path, monkeypatch):
    """The app's automation browser runs on its own profile, not browser-use's throwaway copy: a
    cookie a site set in one run is there in the next, and while it runs net_guard sees the profile
    as in use."""
    from browser_use import BrowserSession

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
    profile = tmp_path / "browser"
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"browser_allowed_local_hosts": ["localhost"]})
    monkeypatch.setattr(shared_config, "selected_browser", lambda settings=None: (conftest.live_browser(), profile))

    async def visit(path):
        kwargs = shared_config.build_browser_kwargs()
        kwargs.update(headless=True, enable_default_extensions=False)
        browser = BrowserSession(**kwargs)
        try:
            await browser.start()
            in_use = net_guard._profile_in_use(profile)
            await browser.navigate_to(f"http://localhost:{server.server_port}{path}")
            cookie = await (await browser.get_current_page()).evaluate("() => document.cookie")
        finally:
            await browser.kill()
        return in_use, cookie

    try:
        first = asyncio.run(visit("/set"))
        second = asyncio.run(visit("/read"))
    finally:
        server.shutdown()
        server.server_close()
    assert first == (True, "kept=yes")
    assert second == (True, "kept=yes"), "the cookie set in the first run was lost"
