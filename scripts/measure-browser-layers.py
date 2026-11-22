"""Measure how each browser network layer affects real page loads. Read-only.

Opens each URL (a public job posting: one page load, no clicks, no forms, no sign-in) in a
fresh temporary browser profile, once per layer:

  plain  the browser as the app launched it before 2026-10-28: no proxy, no request gate
  proxy  the app's local SOCKS egress proxy only (Gmail and private addresses refused)
  gate   the application browser of v1.2.0: proxy + request gate (every request fetched by
         the backend) + "--host-resolver-rules=MAP * ~NOTFOUND"

and reports per URL and layer: seconds until the page had readable text, the amount of
visible text once the page stopped changing, loaded resources and whether the page stayed blank.

Usage:
  python scripts/measure-browser-layers.py <urls.txt> [--browser chromium|brave|chrome|msedge]
         [--layers plain,proxy,gate] [--out results.json] [--shots folder] [--headed]

The URL file is never part of the repo (keep it outside, or under tmp/, which is git-ignored).
The report names sites by host only.
"""
import argparse
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"

ROOT = Path(__file__).resolve().parent.parent
for entry in (str(ROOT), str(ROOT / "backend")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

READY_TEXT = 200          # the app's own "page has content" threshold (apply_jobs.PAGE_READY_TEXT)
NAVIGATE_SECONDS = 60
READY_SECONDS = 75
SETTLE_SECONDS = 40

_STATE_JS = """() => JSON.stringify({
  text: ((document.body && document.body.innerText) || '').trim().length,
  ready: document.readyState,
  title: (document.title || '').slice(0, 80),
  resources: performance.getEntriesByType('resource').length,
  host: location.hostname
})"""


def _executable(name: str) -> str:
    from core.browsers import find_browser, find_playwright_chromium
    path = find_playwright_chromium() if name == "chromium" else find_browser(name)
    if not path:
        raise SystemExit(f"Browser not found: {name}")
    return path


async def _open(layer: str, executable: str, headed: bool, url: str):
    """(browser session, gate or None, profile folder) for one layer."""
    from browser_use import BrowserProfile, BrowserSession
    from core import net_guard, request_gate
    profile = Path(tempfile.mkdtemp(prefix=f"langhire-measure-{layer}-"))
    owner = f"measure-{layer}:{uuid.uuid4().hex}"
    kwargs = dict(executable_path=executable, user_data_dir=str(profile), headless=not headed, permissions=[],
                  ignore_default_args=["--extensions-on-chrome-urls"],
                  browser_profile=BrowserProfile(block_ip_addresses=True))
    if layer in ("proxy", "gate"):
        net_guard.limit_webrtc(profile)
        kwargs["args"] = net_guard.browser_args(block_mail=True, allowed_hosts=[], owner=owner)
    if layer == "gate":
        kwargs["enable_default_extensions"] = False
        kwargs["args"] = [a for a in kwargs["args"] if not a.startswith("--host-resolver-rules=")]
        kwargs["args"] += ["--disable-extensions", "--host-resolver-rules=MAP * ~NOTFOUND", "--dns-prefetch-disable"]
    browser = BrowserSession(**kwargs)
    gate = None
    await browser.start()
    if layer == "gate":
        gate = request_gate.RequestGate(browser, "auto", {"url": url}, owner=owner,
                                        trusted_site=lambda page: True)
        await asyncio.wait_for(gate.start(), 30)
    return browser, gate, profile


async def measure(layer: str, executable: str, headed: bool, url: str, shots: Path | None, index: int) -> dict:
    from core import browser_policy
    row = {"layer": layer, "host": urlsplit(url).hostname or "", "index": index}
    browser = gate = profile = None
    started = time.monotonic()
    try:
        browser, gate, profile = await _open(layer, executable, headed, url)
        row["launch_s"] = round(time.monotonic() - started, 1)
        began = time.monotonic()
        try:
            await asyncio.wait_for(browser.navigate_to(url), NAVIGATE_SECONDS)
            row["navigate"] = "ok"
        except Exception as error:  # a refused or timed-out load is a result, not a crash
            row["navigate"] = f"{type(error).__name__}: {str(error)[:120]}"
        row["navigate_s"] = round(time.monotonic() - began, 1)
        state = {}
        while time.monotonic() - began < READY_SECONDS:
            try:
                state = json.loads(await asyncio.wait_for(browser_policy.evaluate(browser, _STATE_JS), 10) or "{}")
            except Exception as error:
                state = {"text": -1, "error": type(error).__name__}
            if state.get("text", 0) >= READY_TEXT:
                break
            if gate is not None and gate.failed:
                break
            await asyncio.sleep(1)
        row["ready_s"] = round(time.monotonic() - began, 1)
        # Single-page sites keep loading after their first text: read the page once it has stopped changing.
        steady, last = 0, state.get("text", 0)
        settle_until = time.monotonic() + SETTLE_SECONDS
        while steady < 4 and time.monotonic() < settle_until and not (gate is not None and gate.failed):
            await asyncio.sleep(1)
            try:
                state = json.loads(await asyncio.wait_for(browser_policy.evaluate(browser, _STATE_JS), 10) or "{}")
            except Exception:
                break
            steady = steady + 1 if state.get("text", 0) == last else 0
            last = state.get("text", 0)
        row["settled_s"] = round(time.monotonic() - began, 1)
        row.update(text=state.get("text", 0), resources=state.get("resources", 0), ready=state.get("ready", ""),
                   title=state.get("title", ""), final_host=state.get("host", ""))
        row["blank"] = row["text"] < READY_TEXT
        if gate is not None:
            row.update(gate_sent=gate.sent, gate_blocked=gate.blocked, gate_failed=bool(gate.failed),
                       gate_failure=getattr(gate, "failure", ""))
        if shots is not None:
            try:
                await asyncio.wait_for(browser.take_screenshot(path=str(shots / f"{index:02d}-{layer}.png")), 15)
            except Exception:
                pass
    except Exception as error:
        row["error"] = f"{type(error).__name__}: {str(error)[:160]}"
        row["blank"] = True
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
        if profile is not None:
            shutil.rmtree(profile, ignore_errors=True)
    row["total_s"] = round(time.monotonic() - started, 1)
    return row


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("urls")
    parser.add_argument("--browser", default="chromium")
    parser.add_argument("--layers", default="plain,proxy,gate")
    parser.add_argument("--out", default="")
    parser.add_argument("--shots", default="")
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()
    urls = [line.strip() for line in Path(args.urls).read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.startswith("#")]
    executable = _executable(args.browser)
    shots = Path(args.shots) if args.shots else None
    if shots is not None:
        shots.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, url in enumerate(urls, 1):
        for layer in args.layers.split(","):
            row = await measure(layer.strip(), executable, args.headed, url, shots, index)
            rows.append(row)
            print(f"{index:02d} {row['host'][:42]:42} {row['layer']:5} "
                  f"ready={row.get('ready_s', '-'):>5}s text={row.get('text', '-'):>6} "
                  f"res={row.get('resources', '-'):>4} blank={row.get('blank')} "
                  f"{row.get('navigate', '')[:40]} {row.get('error', '')[:60]} "
                  f"{'GATE FAILED: ' + row.get('gate_failure', '')[:60] if row.get('gate_failed') else ''}", flush=True)
            if args.out:
                Path(args.out).write_text(json.dumps(rows, indent=1), encoding="utf-8")
    print("\nSummary (pages with readable text / pages tried, median seconds to readable text):")
    for layer in args.layers.split(","):
        mine = [r for r in rows if r["layer"] == layer.strip()]
        good = sorted(r["ready_s"] for r in mine if not r.get("blank") and "ready_s" in r)
        median = good[len(good) // 2] if good else None
        print(f"  {layer:5} {len(good)}/{len(mine)}  median {median}s")


if __name__ == "__main__":
    asyncio.run(main())
