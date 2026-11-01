"""Local wire evidence for the application request boundary (no AI/accounts)."""
import asyncio
import http.server
import os
import tempfile
import threading
import httpx
from pathlib import Path
import uuid

import pytest

import conftest

from core import request_gate, egress_proxy, net_guard

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in browser")]


@pytest.fixture
def wire_site():
    seen = []
    page = {"html": '<!doctype html><button id="send" onclick="fetch(\'/sent\', {method:\'POST\',body:\'candidate\'})">Continue</button>'}
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(("GET", self.path))
            page.setdefault("request_headers", []).append(dict(self.headers))
            code, headers, body = page.get("responses", {}).get(self.path, (200, {}, page["html"].encode()))
            self.send_response(code)
            self.send_header("Content-Type", "text/html")
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
            seen.append(("POST", self.path, body))
            page.setdefault("request_headers", []).append(dict(self.headers))
            self.send_response(page.get("post_status", 200))
            self.end_headers()
            self.wfile.write(b"ok")
        def log_message(self, *args):
            pass
    class Server(http.server.ThreadingHTTPServer):
        def get_request(self):
            sock, addr = super().get_request()
            page["connections"] = page.get("connections", 0) + 1
            return sock, addr
    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}/", seen, page
    server.shutdown()
    server.server_close()


async def wait_pending(kind="request", suffix=None, gate=None):
    for _ in range(120):
        if gate is not None and gate.failed:
            raise AssertionError(getattr(gate, "failure", "Gate failed"))
        matches = [r for r in request_gate.pending() if r["kind"] == kind and (suffix is None or r["url"].endswith(suffix))]
        if matches:
            return matches
        await asyncio.sleep(.05)
    raise AssertionError(f"No {kind} approval: {request_gate.pending()}")


async def browser_and_gate(mode, url, transport_factory=None, before_gate=None, trusted_site=None, *, strict_trial_protocols=True):
    # Historical ATS-contract regressions explicitly opt into protocol mode.
    # Normal action-based Trial tests pass False and exercise the app's default.
    from browser_use import BrowserSession
    profile = Path(tempfile.mkdtemp(prefix="request-gate-live-"))
    owner = "request-gate-test:" + uuid.uuid4().hex
    net_guard.limit_webrtc(profile)
    args = net_guard.browser_args(block_mail=True, owner=owner)
    args = [a for a in args if not a.startswith("--host-resolver-rules=")] + ["--host-resolver-rules=MAP * ~NOTFOUND", "--dns-prefetch-disable"]
    browser = BrowserSession(executable_path=conftest.live_browser(),
        user_data_dir=profile, headless=True, permissions=[], enable_default_extensions=False, args=args)
    gate = request_gate.RequestGate(browser, mode, {"url": url}, owner=owner,
                                   transport_factory=transport_factory or httpx.HTTPTransport, trusted_site=trusted_site,
                                   strict_trial_protocols=strict_trial_protocols)
    try:
        await browser.start()
        if before_gate is not None:
            before_gate(browser)
        await gate.start()
    except BaseException:
        await gate.close()
        raise
    return browser, gate


async def wait_for(predicate, gate=None, tries=100):
    for _ in range(tries):
        if gate is not None and gate.failed:
            raise AssertionError(getattr(gate, "failure", "Gate failed"))
        if predicate():
            return True
        await asyncio.sleep(.05)
    return False


async def settled(read, quiet=1.0, limit=10.0):
    """read()'s value once it has stayed the same for `quiet` seconds (waiting at most `limit` seconds)."""
    value, still, waited = read(), 0.0, 0.0
    while still < quiet and waited < limit:
        await asyncio.sleep(.05)
        still, waited = still + .05, waited + .05
        if (current := read()) != value:
            value, still = current, 0.0
    return value


def bridge_to(url):
    """A virtual https site served by the local wire server (the backend fetches it as the browser asks)."""
    def bridge(request):
        with httpx.Client(trust_env=False) as client:
            reply = client.request(request.method, url + request.url.raw_path.decode("ascii").lstrip("/"), headers=dict(request.headers),
                                   content=request.content)
        return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)
    return lambda: httpx.MockTransport(bridge)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trial", "auto"])
async def test_pages_and_script_saves_go_through_without_questions(wire_site, mode):
    """Moving between pages is automatic in every mode (the final submit is stopped at the action, by the
    submit guard). Everything still goes through the backend: the browser itself can't connect."""
    url, seen, _ = wire_site
    browser, gate = await browser_and_gate(mode, url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        page = await browser.get_current_page()
        await page.evaluate("() => document.querySelector('#send').click()")
        await page.evaluate("() => { window.pixel = new Image(); window.pixel.src = '/pixel'; }")
        if mode == 'trial':
            # Runtime.evaluate can return before Chromium schedules the image
            # request. Draining already active requests does not await future
            # initiation; prove the read actually reaches the wire first.
            assert await wait_for(lambda: ('GET', '/pixel') in seen, gate)
            await gate.wait_idle()
            assert ('GET','/') in seen and ('GET','/pixel') in seen and not request_gate.pending()
            assert all(r[0] == 'GET' and r[1] in ('/', '/pixel', '/favicon.ico') for r in seen), seen
            return
        assert await wait_for(lambda: ("POST", "/sent", b"candidate") in seen and ("GET", "/pixel") in seen, gate)
        assert request_gate.pending() == [] and gate.sent >= 3
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_native_file_upload_keeps_the_complete_bytes(wire_site, tmp_path):
    url, seen, page_source = wire_site
    page_source["html"] = ('<form action="/sent" method="post" enctype="multipart/form-data">'
                           '<input id="file" type="file" name="resume"><button>Submit</button></form>')
    document = tmp_path / "resume.txt"
    document.write_bytes(b"LOCAL TEST RESUME 123456")
    browser, gate = await browser_and_gate("auto", url)
    gate.allowed_files = [str(document)]
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        cdp = await browser.get_or_create_cdp_session()
        doc = await cdp.cdp_client.send.DOM.getDocument(session_id=cdp.session_id)
        node = await cdp.cdp_client.send.DOM.querySelector(params={"nodeId": doc["root"]["nodeId"], "selector": "#file"},
                                                            session_id=cdp.session_id)
        await cdp.cdp_client.send.DOM.setFileInputFiles(params={"nodeId": node["nodeId"], "files": [str(document)]},
                                                         session_id=cdp.session_id)
        await (await browser.get_current_page()).evaluate("() => {document.querySelector('form').submit();}")
        assert await wait_for(lambda: any(r[0] == "POST" for r in seen), gate)
        assert any(r[0] == "POST" and b"LOCAL TEST RESUME 123456" in r[2] for r in seen)
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_a_redirect_is_followed_as_a_new_request(wire_site):
    url, seen, source = wire_site
    source["responses"] = {"/": (302, {"Location": "/redirected"}, b"")}
    browser, gate = await browser_and_gate("auto", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        assert seen[:2] == [("GET", "/"), ("GET", "/redirected")]
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_cookie_login_state_survives_the_http_broker(wire_site):
    url, seen, source = wire_site
    source["responses"] = {"/": (200, {"Set-Cookie": "test_session=dummy-session; HttpOnly; Path=/; SameSite=Lax"},
                                     source["html"].encode())}
    browser, gate = await browser_and_gate("auto", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        await (await browser.get_current_page()).evaluate(
            "() => {fetch('/sent', {method:'POST',body:'candidate'}).catch(()=>{});}")
        assert await wait_for(lambda: any(r[0] == "POST" for r in seen), gate)
        assert source["request_headers"][-1].get("Cookie") == "test_session=dummy-session"
    finally:
        await gate.close()


@pytest.mark.asyncio
async def test_no_websocket_turn_or_worker_connection_escapes(wire_site):
    url, seen, source = wire_site
    browser, gate = await browser_and_gate("auto", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        page = await browser.get_current_page()
        # The page's own loading (the browser's favicon request) can still connect after navigate_to
        # returns on a slow machine (Windows CI run 27): count from the point it has gone quiet.
        before = await settled(lambda: source.get("connections", 0))
        port = url.split(":")[-1].strip("/")
        await page.evaluate("() => {window.ws = new WebSocket('" + url.replace("http:", "ws:") + "socket');}")
        await page.evaluate("() => {window.pc = new RTCPeerConnection({iceServers:[{urls:'turn:127.0.0.1:" + port
                            + "?transport=tcp',username:'local',credential:'dummy'}]});pc.createDataChannel('test');"
                            "pc.createOffer().then(o=>pc.setLocalDescription(o));}")
        await page.evaluate("() => {window.worker = new Worker(URL.createObjectURL(new Blob([\"fetch('/sent',"
                            "{method:'POST',body:'worker'})\"],{type:'text/javascript'})));}")
        await asyncio.sleep(1.5)
        assert source.get("connections", 0) == before
        assert not any(r[0] == "POST" for r in seen)
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("target", ["popup", "iframe"])
async def test_new_documents_go_through_the_gate_from_their_first_request(wire_site, target):
    """A popup or another site's frame can't reach the network on its own: what it loads and sends arrives
    only because the gate fetched it."""
    url, seen, source = wire_site
    source["responses"] = {"/child": (200, {}, b"<script>fetch('/sent',{method:'POST',body:'child document'})</script>")}
    browser, gate = await browser_and_gate("auto", url)
    try:
        await asyncio.wait_for(browser.navigate_to(url), 15)
        page = await browser.get_current_page()
        before = gate.sent
        if target == "popup":
            await page.evaluate("() => {window.child = window.open('/child');}")
        else:
            child_url = url.replace("127.0.0.1", "localhost") + "child"
            await page.evaluate("() => {let f=document.createElement('iframe');f.src='" + child_url
                                + "';document.body.append(f);}")
        assert await wait_for(lambda: ("POST", "/sent", b"child document") in seen, gate)
        assert gate.sent >= before + 2
    finally:
        await gate.close()


PASSWORD_FORM = ('<form action="/sent" method="post"><label>Password<input id="pw" name="password" type="password">'
                 '</label><button>Submit</button></form>')


async def password_index(browser):
    state = await browser.get_browser_state_summary()
    return next(i for i, node in state.dom_state.selector_map.items() if node.attributes.get("id") == "pw")


@pytest.mark.asyncio
@pytest.mark.parametrize("allow_site", [False, True])
async def test_trial_never_requests_credential_approval_or_sends_a_password(wire_site, data_dir, allow_site):
    from cli import apply_jobs
    from core import secret_store
    url, seen, source = wire_site
    virtual = "https://acme.test/"
    source['html'] = PASSWORD_FORM
    browser, gate = await browser_and_gate('trial', virtual, transport_factory=bridge_to(url))
    try:
        await browser.navigate_to(virtual)
        index = await password_index(browser)
        ref={'request_gate':gate,'secret_masks':{},'secrets':{'account_email':'test@example.com'}}
        tools=apply_jobs.build_tools({'url':virtual,'title':'Intern','company':'Local'},'',True,ref,1)
        result=await tools.registry.execute_action('input',{'index':index,'text':'<secret>password</secret>'},browser_session=browser)
        assert result.error and request_gate.pending()==[]
        assert not request_gate.decide(['a'*32],allow_site)
        assert not secret_store.site_password('https://acme.test',create=False)
        assert ('GET','/') in seen and all(r[0] == 'GET' and r[1] in ('/', '/favicon.ico') for r in seen), seen
    finally:
        await gate.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["trial", "auto"])
async def test_modes_type_password_on_recognised_employer_without_asking(wire_site, data_dir, mode):
    from cli import apply_jobs
    from core import secret_store
    url, seen, source = wire_site
    virtual = "https://acme.test/"
    source["html"] = PASSWORD_FORM
    browser, gate = await browser_and_gate(mode, virtual, transport_factory=bridge_to(url),
                                           trusted_site=lambda page: page.startswith(virtual))
    try:
        await asyncio.wait_for(browser.navigate_to(virtual), 15)
        page = await browser.get_current_page()
        index = await password_index(browser)
        ref = {"request_gate": gate, "secret_masks": {}, "secrets": {"account_email": "test@example.com"}}
        apply_jobs.set_run_mode(mode)
        try:
            tools = apply_jobs.build_tools({"url": virtual, "title": "Intern", "company": "Acme"}, "", mode == "trial", ref, 1)
        finally:
            apply_jobs.set_run_mode("auto")
        result = await asyncio.wait_for(tools.registry.execute_action(
            "input", {"index": index, "text": "<secret>password</secret>"}, browser_session=browser), 10)
        password = secret_store.site_password("https://acme.test", create=False)
        assert result.error is None and request_gate.pending() == []
        assert password and await page.evaluate("() => document.querySelector('#pw').value") == password
    finally:
        await gate.close()
