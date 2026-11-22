"""Which things real hiring sites rely on work in the agent's browser? Local pages only.

Starts a local test site and checks, in a real browser, the features application sites use
beyond a plain page load:

  page          a normal page loads
  new_tab       a link that opens the application form in a new tab (target="_blank")
  worker        a Web Worker that makes its own request (CAPTCHA widgets, upload parsers)
  websocket     a WebSocket connection (live form saves, chat-style application flows)
  service_wkr   a service worker that takes over requests (Workday, many single-page sites)
  slow_reply    a request the server answers after 35 seconds (long uploads, slow saves)

Layers:
  gate   the application browser of v1.2.0: SOCKS proxy + request gate + resolver flag
  app    the application browser as the app launches it now (core.shared_config.build_browser_kwargs)
  plain  a bare browser-use session (no proxy, no gate), for reference

Usage:  python scripts/probe-site-features.py [--layers gate,app,plain] [--browser chromium|brave|chrome|msedge]
Nothing here leaves this computer: the only site opened is the local test site.
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
import uuid
from pathlib import Path

# The app's data folder for this probe is a throwaway one (never the real app data).
_HOME = tempfile.mkdtemp(prefix="langhire-probe-home-")
os.environ["HOME"] = os.environ["USERPROFILE"] = _HOME
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

FEATURES = ("page", "new_tab", "worker", "websocket", "service_wkr", "slow_reply")
SLOW_SECONDS = 35

_INDEX = """<!doctype html><title>Feature probe</title>
<h1>Feature probe: a job posting stand-in with enough text to count as loaded.</h1>
<p>{filler}</p>
<a id="open" href="/form" target="_blank" style="display:block;width:300px;height:40px;background:#ddd">Apply Now</a>
<script>
const report = (what) => fetch('/report?what=' + what, {{cache: 'no-store'}}).catch(() => {{}});
report('page');
try {{ new Worker('/worker.js'); }} catch (e) {{}}
try {{
  const socket = new WebSocket('{ws_url}');
  socket.onopen = () => socket.send('hello');
  socket.onmessage = (m) => {{ if (m.data === 'echo:hello') report('websocket'); }};
}} catch (e) {{}}
if (navigator.serviceWorker) navigator.serviceWorker.register('/sw.js').catch(() => {{}});
fetch('/slow', {{cache: 'no-store'}}).then((r) => r.text()).then((t) => {{ if (t === 'slow-done') report('slow_reply'); }}).catch(() => {{}});
</script>"""
_FORM = """<!doctype html><title>Application form</title><h1>Application form</h1>
<script>fetch('/report?what=new_tab', {cache: 'no-store'});</script>"""
_WORKER = "fetch('/report?what=worker', {cache: 'no-store'});"
_SW = ("self.addEventListener('install', () => self.skipWaiting());"
       "self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()"
       ".then(() => fetch('/report?what=service_wkr', {cache: 'no-store'}))));")


def start_site(ws_url: str):
    seen: set[str] = set()

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, kind: str = "text/html"):
            self.send_response(200)
            self.send_header("Content-Type", kind)
            self.send_header("Content-Length", str(len(body)))
            if self.path.startswith("/sw.js"):
                self.send_header("Service-Worker-Allowed", "/")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?")[0]
            if path == "/report":
                seen.add(self.path.split("what=")[-1])
                return self._send(b"ok", "text/plain")
            if path == "/form":
                return self._send(_FORM.encode())
            if path == "/worker.js":
                return self._send(_WORKER.encode(), "text/javascript")
            if path == "/sw.js":
                return self._send(_SW.encode(), "text/javascript")
            if path == "/slow":
                time.sleep(SLOW_SECONDS)
                return self._send(b"slow-done", "text/plain")
            return self._send(_INDEX.format(filler="Readable text. " * 30, ws_url=ws_url).encode())

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, seen


async def start_websocket():
    import websockets

    async def echo(socket):
        async for message in socket:
            await socket.send("echo:" + message)

    server = await websockets.serve(echo, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]


def _executable(name: str) -> str:
    from core.browsers import find_browser, find_playwright_chromium
    path = find_playwright_chromium() if name == "chromium" else find_browser(name)
    if not path:
        raise SystemExit(f"Browser not found: {name}")
    return path


async def open_layer(layer: str, executable: str, site_port: int):
    """(browser, gate or None, the URL of the test site as this layer reaches it, profile folder or None)."""
    import httpx
    from browser_use import BrowserSession
    local = f"http://localhost:{site_port}/"
    if layer == "plain":
        profile = Path(tempfile.mkdtemp(prefix="langhire-probe-plain-"))
        browser = BrowserSession(executable_path=executable, user_data_dir=str(profile), headless=True, permissions=[])
        await browser.start()
        return browser, None, local, profile
    if layer == "app":
        import core.shared_config as config
        config.selected_browser = lambda settings=None: (executable, Path(_HOME) / "probe-app-profile")
        kwargs = config.build_browser_kwargs(worker=1, purpose="application")
        browser = BrowserSession(headless=True, chromium_sandbox=(sys.platform != "linux"), **kwargs)
        await browser.start()
        return browser, None, local, None
    if layer == "gate":
        from core import net_guard, request_gate
        profile = Path(tempfile.mkdtemp(prefix="langhire-probe-gate-"))
        owner = "probe-gate:" + uuid.uuid4().hex
        net_guard.limit_webrtc(profile)
        args = [a for a in net_guard.browser_args(block_mail=True, owner=owner)
                if not a.startswith("--host-resolver-rules=")]
        args += ["--disable-extensions", "--host-resolver-rules=MAP * ~NOTFOUND", "--dns-prefetch-disable"]
        browser = BrowserSession(executable_path=executable, user_data_dir=str(profile), headless=True,
                                 permissions=[], enable_default_extensions=False, args=args)

        def bridge(request):
            # The gate's backend fetch, pointed at the local test site (as the gate's own tests do).
            with httpx.Client(trust_env=False, timeout=60) as client:
                reply = client.request(request.method, f"http://127.0.0.1:{site_port}" + request.url.raw_path.decode("ascii"),
                                       headers=dict(request.headers), content=request.content)
            return httpx.Response(reply.status_code, headers=reply.headers, content=reply.content)

        virtual = "https://careers.probe.test/"
        gate = request_gate.RequestGate(browser, "auto", {"url": virtual}, owner=owner,
                                        transport_factory=lambda: httpx.MockTransport(bridge),
                                        trusted_site=lambda page: True)
        await browser.start()
        await asyncio.wait_for(gate.start(), 30)
        return browser, gate, virtual, profile
    raise SystemExit(f"Unknown layer: {layer}")


async def _click_apply(browser) -> None:
    """A real mouse click on the "Apply Now" link (a scripted click can't open a tab)."""
    from core import browser_policy
    box = json.loads(await browser_policy.evaluate(
        browser, "() => { const r = document.querySelector('#open').getBoundingClientRect();"
                 " return JSON.stringify({x: r.left + r.width / 2, y: r.top + r.height / 2}); }"))
    cdp = await browser.get_or_create_cdp_session()
    for kind in ("mousePressed", "mouseReleased"):
        await cdp.cdp_client.send.Input.dispatchMouseEvent(
            params={"type": kind, "x": box["x"], "y": box["y"], "button": "left", "clickCount": 1},
            session_id=cdp.session_id)


async def probe(layer: str, executable: str) -> dict:
    ws_server, ws_port = await start_websocket()
    # The gate's virtual https site can't name a local ws:// address it is allowed to reach;
    # it refuses every WebSocket anyway, which is what this row shows.
    server, seen = start_site(f"ws://localhost:{ws_port}/")
    browser = gate = profile = None
    result = {"layer": layer}
    try:
        browser, gate, url, profile = await open_layer(layer, executable, server.server_port)
        try:
            await asyncio.wait_for(browser.navigate_to(url), 30)
        except Exception as error:
            result["navigate"] = f"{type(error).__name__}: {str(error)[:100]}"
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(_click_apply(browser), 10)
        except Exception as error:
            result["click"] = f"{type(error).__name__}: {str(error)[:100]}"
        deadline = time.monotonic() + SLOW_SECONDS + 15
        while time.monotonic() < deadline and not set(FEATURES) <= seen:
            if gate is not None and gate.failed:
                result["gate_failure"] = getattr(gate, "failure", "") or "the gate stopped"
                break
            await asyncio.sleep(0.5)
        for feature in FEATURES:
            result[feature] = feature in seen
    except Exception as error:
        result["error"] = f"{type(error).__name__}: {str(error)[:160]}"
        for feature in FEATURES:
            result.setdefault(feature, feature in seen)
    finally:
        if gate is not None:
            try:
                await asyncio.wait_for(gate.close(), 20)
            except Exception:
                pass
        if browser is not None:
            try:
                await asyncio.wait_for(browser.kill(), 20)
            except Exception:
                pass
        server.shutdown()
        server.server_close()
        ws_server.close()
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
    return result


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--layers", default="gate,app,plain")
    parser.add_argument("--browser", default="chromium")
    parser.add_argument("--out", default="")
    args = parser.parse_args()
    executable = _executable(args.browser)
    rows = []
    for layer in args.layers.split(","):
        rows.append(await probe(layer.strip(), executable))
    print("\n" + f"{'layer':7}" + "".join(f"{f:>13}" for f in FEATURES))
    for row in rows:
        print(f"{row['layer']:7}" + "".join(f"{'works' if row.get(f) else 'BROKEN':>13}" for f in FEATURES)
              + ("   " + (row.get("error") or row.get("gate_failure") or "") if row.get("error") or row.get("gate_failure") else ""))
    if args.out:
        Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    shutil.rmtree(_HOME, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
