"""A job posting's title and employer as a browser shows the page.

Many application sites build their pages in the browser (Oracle's "Candidate Experience" sites,
some large organizations' own careers sites, Phenom, some iCIMS and Workday pages): the app's plain page fetch
(job_page.fetch) sees only a shell titled "Candidate Experience site" or "Careers", so a pasted
link kept a placeholder name until the agent opened it. For such links this opens the page once,
headless, in a fresh temporary browser profile (no cookies, no sign-in, deleted afterwards), with
the application browser's network rules (Gmail and private addresses unreachable, no bare IP
addresses), waits until the page has stopped changing, and parses what it shows with the same
job_page.parse. Nothing is clicked or typed.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import time
from pathlib import Path

try:
    from core import job_page
except ImportError:  # imported as backend.core.posting_reader
    from backend.core import job_page

READY_TEXT = 200            # visible characters that mean the page has content (as apply_jobs)
NAVIGATE_SECONDS = 40
READY_SECONDS = 35          # after navigating: until the first content
SETTLE_SECONDS = 15         # after the first content: until the page stops changing
STEADY_POLLS = 3
LAUNCH_SECONDS = 60
MAX_URLS = 50               # one batch (a big paste reads the rest by itself later, as the agent runs)
MAX_HTML = 3_000_000

# One reading browser at a time: two pastes in a row share the wait, not the CPU.
_lock = asyncio.Lock()

_STATE_JS = """() => JSON.stringify({
  text: ((document.body && document.body.innerText) || '').trim().length,
  ready: document.readyState
})"""

# The page and its same-site frames (iCIMS shows the posting in one), as the browser has them now.
_HTML_JS = """(limit) => {
  const frames = [];
  for (const frame of Array.from(document.querySelectorAll('iframe')).slice(0, 5)) {
    try {
      const doc = frame.contentDocument;
      if (doc && doc.documentElement) frames.push({url: doc.location.href, html: doc.documentElement.outerHTML.slice(0, limit)});
    } catch (e) { /* another site's frame: not readable, and not needed */ }
  }
  return JSON.stringify({url: location.href, html: document.documentElement.outerHTML.slice(0, limit), frames});
}"""


def _browser_kwargs(profile: Path, headless: bool) -> dict:
    try:
        from core import net_guard, shared_config
        from core.browsers import find_playwright_chromium
    except ImportError:
        from backend.core import net_guard, shared_config
        from backend.core.browsers import find_playwright_chromium
    from browser_use import BrowserProfile
    settings = shared_config.load_runtime_settings()
    executable, _profile = shared_config.selected_browser(settings)
    executable = executable or find_playwright_chromium()
    args = net_guard.direct_args(block_mail=True, allowed_hosts=[])
    if settings.get("stealth_enabled"):
        try:
            from core.stealth import STEALTH_ARGS
        except ImportError:
            from backend.core.stealth import STEALTH_ARGS
        args += list(STEALTH_ARGS)
    kwargs = {
        "user_data_dir": str(profile),
        "headless": headless,
        "permissions": [],
        "ignore_default_args": list(shared_config.IGNORED_CHROME_DEFAULT_ARGS),
        "browser_profile": BrowserProfile(block_ip_addresses=True),
        "enable_default_extensions": False,
        "args": args + ["--disable-extensions"],
    }
    if executable:
        kwargs["executable_path"] = executable
    return kwargs


async def _state(browser) -> dict:
    try:
        from core import browser_policy
    except ImportError:
        from backend.core import browser_policy
    try:
        return json.loads(await asyncio.wait_for(browser_policy.evaluate(browser, _STATE_JS), 10) or "{}")
    except Exception:
        return {}


async def _read_one(browser, url: str, currency: str):
    try:
        from core import browser_policy
    except ImportError:
        from backend.core import browser_policy
    try:
        await asyncio.wait_for(browser.navigate_to(url), NAVIGATE_SECONDS)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass        # a slow load can still have shown the posting: read what is there
    began = time.monotonic()
    state = await _state(browser)
    while state.get("text", 0) < READY_TEXT and time.monotonic() - began < READY_SECONDS:
        await asyncio.sleep(1)
        state = await _state(browser)
    # Single-page sites keep filling in after their first text: wait until it stops changing.
    steady, last, until = 0, state.get("text", 0), time.monotonic() + SETTLE_SECONDS
    while steady < STEADY_POLLS and time.monotonic() < until:
        await asyncio.sleep(1)
        now = (await _state(browser)).get("text", 0)
        steady = steady + 1 if now == last else 0
        last = now
    try:
        page = json.loads(await asyncio.wait_for(browser_policy.evaluate(browser, _HTML_JS, MAX_HTML), 20) or "{}")
    except Exception:
        return None
    if not page.get("html"):
        return None
    posting = job_page.parse(page.get("url") or url, 200, page["html"], original_url=url, currency=currency)
    for frame in page.get("frames") or []:
        if posting.title and not job_page.is_placeholder(posting.title) and posting.company:
            break
        inner = job_page.parse(frame.get("url") or url, 200, frame.get("html") or "", original_url=url, currency=currency)
        if (not posting.title or job_page.is_placeholder(posting.title)) and inner.title and not job_page.is_placeholder(inner.title):
            posting.title = inner.title
        if not posting.company and inner.company:
            posting.company, posting.company_stated = inner.company, inner.company_stated
        if not posting.locations and inner.locations:
            posting.locations = inner.locations
    return posting


async def read_rendered_many(urls: list[str], headless: bool = True, currency: str = "USD") -> dict:
    """{url: Posting or None} for up to MAX_URLS links, read in one temporary headless browser."""
    from browser_use import BrowserSession
    results: dict = {}
    urls = [u for u in dict.fromkeys(urls) if u][:MAX_URLS]
    if not urls:
        return results
    try:
        from core import shared_config
    except ImportError:
        from backend.core import shared_config
    async with _lock:
        profile = Path(tempfile.mkdtemp(prefix="langhire-posting-"))
        # Used in place: browser-use would otherwise run (and leave behind) a copy of it.
        key = shared_config._profile_key(profile)
        shared_config._OWN_PROFILES.add(key)
        browser = None
        try:
            browser = BrowserSession(**_browser_kwargs(profile, headless))
            await asyncio.wait_for(browser.start(), LAUNCH_SECONDS)
            for url in urls:
                try:
                    results[url] = await _read_one(browser, url, currency)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    results[url] = None
        except asyncio.CancelledError:
            raise
        except Exception as error:
            print(f"  ⚠️  Couldn't open a browser to read the job pages ({str(error)[:120]})")
        finally:
            if browser is not None:
                try:
                    await asyncio.wait_for(browser.kill(), 20)
                except Exception:
                    pass
            shutil.rmtree(profile, ignore_errors=True)
            shared_config._OWN_PROFILES.discard(key)
    return results


async def read_rendered(url: str, headless: bool = True, currency: str = "USD"):
    """One link's Posting as a browser shows it, or None when the page couldn't be read."""
    return (await read_rendered_many([url], headless=headless, currency=currency)).get(url)
