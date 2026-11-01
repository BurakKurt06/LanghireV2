"""Anti-detection helpers for the browsing agent.

Two things live here:

1. ``STEALTH_ARGS`` / ``apply_stealth`` — make the automated Chrome look like an
   ordinary one. The usual Python packages for this (``playwright-stealth``,
   and the Node-only ``puppeteer-extra-plugin-stealth``) cannot be used:
   browser-use 0.13 drives Chrome over CDP directly and never exposes a
   Playwright ``Page`` for those libraries to patch. Instead the same patches
   are injected through CDP ``Page.addScriptToEvaluateOnNewDocument``, which
   runs before any page script on every document and every frame.

2. ``human_delay`` — a randomised pause, so actions are not evenly spaced.

Everything here is opt-in from settings and defaults to today's behaviour.
"""
from __future__ import annotations

import asyncio
import random

# Launch flags. The important one is AutomationControlled: without it Chrome
# advertises itself as automated regardless of any JS patching.
STEALTH_ARGS: list[str] = [
    "--disable-blink-features=AutomationControlled",
    # Only AutomationControlled: turning off site isolation (IsolateOrigins, site-per-process)
    # would let one site's page read another's data in memory, and nothing here needs it.
    "--disable-features=AutomationControlled",
    "--exclude-switches=enable-automation",
    "--no-default-browser-check",
    "--no-first-run",
    "--disable-infobars",
]

# Injected before any page script runs. Each patch addresses a signal that
# common bot-detection scripts read.
_STEALTH_JS = r"""
(() => {
  const patch = (obj, prop, value) => {
    try {
      Object.defineProperty(obj, prop, { get: () => value, configurable: true });
    } catch (e) { /* some properties are locked down; ignore */ }
  };

  // navigator.webdriver is the single most-checked automation signal.
  patch(Navigator.prototype, 'webdriver', undefined);
  try { delete Object.getPrototypeOf(navigator).webdriver; } catch (e) {}

  // Headless Chrome exposes no plugins and no languages.
  patch(navigator, 'languages', ['en-US', 'en']);
  if (!navigator.plugins || navigator.plugins.length === 0) {
    patch(navigator, 'plugins', [1, 2, 3, 4, 5]);
  }

  // window.chrome is absent in automated builds.
  if (!window.chrome) {
    window.chrome = { runtime: {}, loadTimes: () => {}, csi: () => {}, app: {} };
  }

  // Notification.permission normally never reports 'denied' while prompting.
  try {
    const orig = window.navigator.permissions && window.navigator.permissions.query;
    if (orig) {
      window.navigator.permissions.query = (params) =>
        params && params.name === 'notifications'
          ? Promise.resolve({ state: Notification.permission })
          : orig(params);
    }
  } catch (e) {}

  // WebGL vendor/renderer betray SwiftShader in headless builds.
  try {
    const getParameter = WebGLRenderingContext.prototype.getParameter;
    WebGLRenderingContext.prototype.getParameter = function (p) {
      if (p === 37445) return 'Intel Inc.';            // UNMASKED_VENDOR_WEBGL
      if (p === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
      return getParameter.apply(this, [p]);
    };
  } catch (e) {}

  // Automated builds report 0 here.
  if (navigator.hardwareConcurrency === 0) patch(navigator, 'hardwareConcurrency', 8);
  if (!navigator.deviceMemory) patch(navigator, 'deviceMemory', 8);
})();
"""


async def apply_stealth(browser_session) -> bool:
    """Inject the stealth patches into a live BrowserSession.

    Returns True when the script was registered. Never raises: anti-detection
    is best-effort and must not abort an application run.
    """
    try:
        await browser_session._cdp_add_init_script(_STEALTH_JS)
        return True
    except Exception as e:  # pragma: no cover - depends on a live browser
        print(f"    ⚠️  Stealth injection skipped ({type(e).__name__}: {e})")
        return False


def human_delay_seconds(min_seconds: float, max_seconds: float) -> float:
    """Pick a randomised pause length within the configured window."""
    lo, hi = float(min_seconds), float(max_seconds)
    if lo < 0:
        lo = 0.0
    if hi < lo:
        hi = lo
    return random.uniform(lo, hi)


async def human_delay(min_seconds: float, max_seconds: float) -> float:
    """Sleep for a randomised interval. Returns the delay actually used."""
    delay = human_delay_seconds(min_seconds, max_seconds)
    if delay > 0:
        await asyncio.sleep(delay)
    return delay
