"""CI preflight: can each browser the browser tests use start and answer on its debugging port?

Usage: uv run --locked python scripts/browser-preflight.py [--seconds 90]
For the bundled Playwright Chromium and any installed Chrome/Brave, headless with a throwaway
profile and about:blank only:
  1. a plain launch: how long the DevTools endpoint took to answer, or the browser's output;
  2. a browser-use BrowserSession start configured like the browser tests (outside pytest):
     how long it took, or the error, launch arguments and the browser's captured output.
No sites, accounts or AI calls. Exits 1 when no BrowserSession starts, so a broken browser
setup fails here in about a minute instead of in every browser test.
"""
import argparse, asyncio, json, socket, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))
from core import browser_lifecycle  # noqa: E402  (the app's pipe ownership, as in the tests)
from core.browsers import find_browser, find_playwright_chromium  # noqa: E402

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--seconds", type=float, default=90)
args = parser.parse_args()
for stream in (sys.stdout, sys.stderr):  # browser-use messages contain symbols a Windows console can't encode
    stream.reconfigure(errors="replace")


DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # localhost, never via a proxy


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def probe(name, exe):
    port, profile = free_port(), tempfile.mkdtemp(prefix="langhire-preflight-")
    log = Path(profile).with_suffix(".log")
    with open(log, "wb") as out:
        proc = subprocess.Popen([exe, "--headless=new", f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
                                 "--no-first-run", "--no-default-browser-check", "about:blank"],
                                stdout=out, stderr=subprocess.STDOUT)
        start, answer = time.monotonic(), None
        while time.monotonic() - start < args.seconds and proc.poll() is None:
            try:
                with DIRECT.open(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                    answer = json.load(r)
                    break
            except OSError:
                time.sleep(0.5)
        took, exited = time.monotonic() - start, proc.poll()
        proc.kill()
        proc.wait(10)
    tail = log.read_text(errors="replace")[-3000:].strip()
    if answer:
        print(f"OK   {name}: DevTools answered after {took:.1f}s ({answer.get('Browser')}) {exe}", flush=True)
    else:
        state = "never answered" if exited is None else f"exited with code {exited}"
        print(f"FAIL {name}: {state} after {took:.1f}s {exe}\n--- browser output ---\n{tail or '(none)'}\n---", flush=True)
    return bool(answer)


# The lifecycle keeps the last output of each browser it owns; keep it for the report.
captured = []
_close = browser_lifecycle.Child.close


async def _keep_output(self):
    try:
        await _close(self)
    finally:
        captured.append(bytes(self.tails[1] + self.tails[0]).decode(errors="replace"))

browser_lifecycle.Child.close = _keep_output


async def session(name, exe, extensions):
    from browser_use import BrowserSession
    from browser_use.browser.profile import BrowserProfile
    kwargs = dict(executable_path=exe, user_data_dir=tempfile.mkdtemp(prefix="langhire-preflight-"), headless=True,
                  permissions=[], enable_default_extensions=extensions)
    label = f"{name}, browser-use session, default extensions {'on' if extensions else 'off'}"
    browser, start, ok = BrowserSession(**kwargs), time.monotonic(), False
    captured.clear()
    try:
        await asyncio.wait_for(browser.start(), 60)
        print(f"OK   {label}: started in {time.monotonic() - start:.1f}s", flush=True)
        ok = True
    except Exception as e:
        print(f"FAIL {label}: {type(e).__name__} after {time.monotonic() - start:.1f}s: {str(e)[:300]}", flush=True)
        print("--- launch arguments ---\n" + " ".join(BrowserProfile(**kwargs).get_args()), flush=True)
    finally:
        await browser.kill()
    for output in [] if ok else captured:
        print(f"--- browser output ({name}) ---\n{output[-3000:].strip() or '(none)'}\n---", flush=True)
    return ok


async def sessions(found):
    return [await session(name, exe, extensions) for name, exe in found.items() for extensions in (False, True)]


candidates = {"bundled Chromium": find_playwright_chromium(), "Chrome": find_browser("chrome"), "Brave": find_browser("brave")}
print({name: exe for name, exe in candidates.items()}, flush=True)
found = {name: exe for name, exe in candidates.items() if exe}
for name, exe in found.items():
    probe(name, exe)
sys.exit(0 if any(asyncio.run(sessions(found))) else 1)
