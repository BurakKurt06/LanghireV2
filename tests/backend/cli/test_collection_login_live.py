"""Local fake password/SSO login persists into a guarded collection browser.

No production sign-ins, accounts, model calls, or outside network connections.
"""
import asyncio
import http.server
import os
import threading
from urllib.parse import parse_qs

import httpx
import pytest

import conftest

import main
from core import browser_policy, request_gate, shared_config
from core.collection_policy import CollectionPolicy
from tests.backend.core.test_request_gate_live import wait_for
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local login browser")]


@pytest.fixture
def login_site():
    seen = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def send(self, body=b"", status=200, headers=()):
            self.send_response(status)
            self.send_header("Content-Type", "text/html")
            for key, value in headers:
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.append(("GET", self.path, self.headers.get("Cookie", "")))
            if self.path == "/login":
                self.send(("<form action='/session' method='post'><input name='email'>"
                    "<input name='password' type='password'><button>Sign in</button></form>"
                    f"<a id='sso' href='http://identity.example:{self.server.server_port}/identity'>Sign in with SSO</a>").encode())
            elif self.path == "/identity":
                self.send(b"<form action='/identity/session' method='post'><input name='email'>"
                    b"<input name='password' type='password'><button>Sign in</button></form>")
            elif self.path.startswith("/callback?"):
                self.send(b"Signed in", headers=[("Set-Cookie", "board_session=fake-persistent-session; Max-Age=3600; Path=/; HttpOnly; SameSite=Lax")])
            elif self.path == "/search":
                if "board_session=fake-persistent-session" not in self.headers.get("Cookie", ""):
                    self.send(b"No saved sign-in", 401)
                    return
                self.send(b"<a href='/job/100001'>BI Analyst</a><script src='/runtime.js'></script>")
            elif self.path == "/session-check":
                valid = "board_session=fake-persistent-session" in self.headers.get("Cookie", "")
                self.send(b"Session survived" if valid else b"No session", 200 if valid else 401)
            elif self.path == "/runtime.js":
                self.send(b"document.body.dataset.loaded='yes';fetch('/apply',{method:'POST',body:'candidate'}).catch(()=>{});"
                    b"fetch('https://mail.google.com/mail/u/0/').catch(()=>{});"
                    b"fetch('https://outside.example/stolen').catch(()=>{});")
            else:
                self.send(b"", 404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append(("POST", self.path, body))
            if self.path not in ("/session", "/identity/session") or parse_qs(body.decode()) != {
                "email": ["fake@example.test"], "password": ["fake-local-password"]}:
                self.send(b"Unexpected write", 400)
                return
            self.send(status=303, headers=[("Location",
                f"http://board.example:{self.server.server_port}/callback?code=fake-local-code")])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server.server_port, seen
    server.shutdown()
    server.server_close()


@pytest.mark.parametrize("method", ["password", "sso"])
@pytest.mark.parametrize("selected", ["automatic", "custom"])
async def test_normal_login_survives_restart_and_collection_keeps_its_guards(
        login_site, data_dir, monkeypatch, method, selected):
    from browser_use import BrowserSession
    from playwright.async_api import async_playwright

    port, seen = login_site
    executable = (main._find_playwright_chromium() if selected == "automatic" else
        conftest.live_browser())
    assert executable, "A Chromium browser is required"
    settings = {} if selected == "automatic" else {"browser": "custom", "browser_path": executable}
    monkeypatch.setattr(main, "load_settings", lambda: settings)
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: settings)
    monkeypatch.setattr(shared_config, "BROWSER_PROFILE_DIR", data_dir / "browser_profile")
    profile = main._get_browser_profile_dir()
    assert str(shared_config.selected_browser()[1]) == profile

    # User-operated login phase: normal redirects, native form submission, and
    # fake SSO are allowed. Browser DNS is restricted to our two local test names.
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(profile, executable_path=executable,
            headless=True, args=["--no-proxy-server", "--disable-background-networking",
                "--host-resolver-rules=MAP board.example 127.0.0.1, MAP identity.example 127.0.0.1, MAP * ~NOTFOUND"])
        try:
            page = await context.new_page()
            await page.goto(f"http://board.example:{port}/login")
            if method == "sso":
                await page.click("#sso")
            await page.fill("input[name=email]", "fake@example.test")
            await page.fill("input[name=password]", "fake-local-password")
            await page.click("button")
            await page.wait_for_url("**/callback?code=fake-local-code")
            assert await page.text_content("body") == "Signed in"
        finally:
            await context.close()
    login_posts = [r for r in seen if r[0] == "POST"]
    assert len(login_posts) == 1
    assert login_posts[0][1] == ("/session" if method == "password" else "/identity/session")

    # A fresh collection process reuses the app-selected profile, not an injected
    # cookie jar. Its backend transport reaches only our local HTTP server.
    def bridge(req):
        assert req.url.host == "board.example"
        with httpx.Client(trust_env=False) as client:
            response = client.request(req.method, f"http://127.0.0.1:{port}" + req.url.raw_path.decode(),
                headers=dict(req.headers), content=req.content)
        return httpx.Response(response.status_code, headers=response.headers, content=response.content)

    kwargs = shared_config.build_browser_kwargs(purpose="collection")
    kwargs.update(headless=True)
    browser = BrowserSession(**kwargs)
    url = "https://board.example/search"
    gate = request_gate.RequestGate(browser, "trial", {"url": url}, owner="collection:1",
        transport_factory=lambda: httpx.MockTransport(bridge),
        collection_policy=CollectionPolicy(url, read_hosts={"board.example"}))
    try:
        await browser.start()
        await gate.start()
        await browser.navigate_to(url)
        assert await wait_for(lambda: any(r[:2] == ("GET", "/runtime.js") for r in seen), gate)
        assert await browser_policy.evaluate(browser, "() => document.body.innerText") == "BI Analyst"
        assert await browser_policy.evaluate(browser, "() => document.body.dataset.loaded") == "yes"
        assert any(r[:2] == ("GET", "/search") and "board_session=fake-persistent-session" in r[2] for r in seen)
        await asyncio.sleep(.4)
        await gate.wait_idle()
        assert [r for r in seen if r[0] == "POST"] == login_posts
        assert gate.blocked >= 3 and not request_gate.pending() and gate.healthy()
        assert not await gate.approve_credentials(url)
    finally:
        await gate.close()
    # Reopening the normal browser after automation must also keep the session.
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(profile, executable_path=executable,
            headless=True, args=["--no-proxy-server", "--disable-background-networking",
                "--host-resolver-rules=MAP board.example 127.0.0.1, MAP * ~NOTFOUND"])
        try:
            page = await context.new_page()
            response = await page.goto(f"http://board.example:{port}/session-check")
            assert response.status == 200 and await page.text_content("body") == "Session survived"
        finally:
            await context.close()
