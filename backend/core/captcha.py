"""CAPTCHA detection and solving.

Implemented as code rather than prompt text. browser-use 0.13 drives Chrome
over CDP and exposes no Playwright ``Page``, so detection and token injection
both go through ``Runtime.evaluate`` on the live CDP session.

Flow, identical on first attempts and retries:

* solver switch ON  -> send the challenge to 2Captcha, at most
  ``MAX_SOLVE_ATTEMPTS`` times per application.
* solver switch OFF, or every attempt failed -> raise ``CaptchaUnsolved``.
  The caller abandons that application, marks it failed with a readable
  reason, and moves to the next job. Nothing waits for the user.

The 2Captcha API key is read from settings only at solve time. It is never put
into ``sensitive_data``, never interpolated into a prompt, and never printed.
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import re
import time
from dataclasses import dataclass

MAX_SOLVE_ATTEMPTS = 2
SOLVE_SECONDS = 120


class SolverRejected(ValueError):
    """2Captcha answered with one of its error codes (ERROR_ZERO_BALANCE, ERROR_CAPTCHA_UNSOLVABLE, ...)."""

    def __init__(self, code: str):
        self.code = code
        super().__init__("CAPTCHA provider rejected the request")


# 2Captcha's error codes, in the user's words. (The code is the provider's own fixed word, never
# the request or the key.)
_SOLVER_ERRORS = {
    "ERROR_ZERO_BALANCE": "your 2Captcha balance is empty: add funds at 2captcha.com",
    "ERROR_WRONG_USER_KEY": "2Captcha doesn't accept the saved API key: check it in Settings → Applications",
    "ERROR_KEY_DOES_NOT_EXIST": "2Captcha doesn't accept the saved API key: check it in Settings → Applications",
    "ERROR_CAPTCHA_UNSOLVABLE": "2Captcha's workers couldn't solve this one",
    "ERROR_NO_SLOT_AVAILABLE": "2Captcha is too busy right now",
    "ERROR_IP_NOT_ALLOWED": "2Captcha refuses requests from this computer's address (check your 2Captcha account)",
    "IP_BANNED": "2Captcha has blocked this computer's address for a while",
    "ERROR_METHOD_CALL": "2Captcha doesn't solve this kind of CAPTCHA",
    "ERROR_BAD_PARAMETERS": "2Captcha doesn't solve this kind of CAPTCHA",
    "ERROR_SITEKEY": "2Captcha doesn't accept this site's CAPTCHA key",
    "ERROR_PAGEURL": "2Captcha doesn't accept this page's address",
}
# Worth a second request: the same challenge can go through the next time.
_RETRYABLE_SOLVER_ERRORS = {"ERROR_CAPTCHA_UNSOLVABLE", "ERROR_NO_SLOT_AVAILABLE"}


def _solver_error_code(answer: str) -> str:
    found = re.search(r"\b(ERROR_[A-Z0-9_]{3,40}|IP_BANNED)\b", answer or "")
    return found.group(1) if found else "ERROR"


class _SolverAPI:
    """Bound every provider connection and the whole polling lifetime.

    The installed SDK's requests have no network timeout. Its poll timeout
    alone cannot stop a hung connection. Only these two fixed API addresses
    are contacted; browser/page destinations never become provider endpoints.
    """
    def __init__(self):
        self.deadline = time.monotonic() + SOLVE_SECONDS

    def _request(self, method, path, params, files=None):
        import httpx
        from .job_page import _PinnedTransport
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("CAPTCHA provider time limit")
        with httpx.Client(transport=_PinnedTransport(), trust_env=False,
                          follow_redirects=False, timeout=min(20, remaining)) as client:
            kwargs = {"params": params} if method == "GET" else {"data": params, "files": files or None}
            response = client.request(method, "https://2captcha.com/" + path, **kwargs)
            response.raise_for_status()
            answer = response.text
            if "ERROR" in answer or "IP_BANNED" in answer:
                raise SolverRejected(_solver_error_code(answer))
            return answer

    def in_(self, files=None, **kwargs):
        if files or "file" in kwargs:
            raise ValueError("CAPTCHA provider file paths are not accepted")
        return self._request("POST", "in.php", kwargs, files)

    def res(self, **kwargs):
        return self._request("GET", "res.php", kwargs)

# Supported challenge types.
RECAPTCHA_V2 = "recaptcha_v2"
RECAPTCHA_V3 = "recaptcha_v3"
HCAPTCHA = "hcaptcha"
TURNSTILE = "turnstile"
IMAGE_CAPTCHA = "image_captcha"
# Detected but never sent to the solver:
HCAPTCHA_INVISIBLE = "hcaptcha_invisible"  # runs by itself on submit; not a blocker
HUMAN_CHECK = "human_check"  # a site's own "are you human" box: ticked with a real click

# Challenges that actually stand between the agent and a submitted form.
#
# reCAPTCHA v3 / Enterprise is deliberately excluded: it is invisible and
# scores the session in the background rather than blocking submission, and it
# is present on a large share of ordinary career sites. Treating it as blocking
# would abandon many applications that would otherwise have gone through. It is
# still solved when the solver is switched on, it just never causes an abandon
# on its own.
BLOCKING_TYPES = frozenset({RECAPTCHA_V2, HCAPTCHA, TURNSTILE, IMAGE_CAPTCHA})


class CaptchaUnsolved(Exception):
    """A CAPTCHA was present and could not be solved.

    Raised so the caller abandons the application rather than hanging.
    """

    def __init__(self, challenge_type: str, reason: str):
        self.challenge_type = challenge_type
        self.reason = reason
        super().__init__(f"CAPTCHA ({challenge_type}) could not be solved: {reason}")


class HumanCheckNeeded(Exception):
    """The site shows its own "are you human" check, which only the user can pass."""

    challenge_type = HUMAN_CHECK

    def __init__(self, page_url: str = ""):
        self.page_url = page_url
        self.reason = ("This site asks you to confirm you're a real person, and ticking its box automatically "
                       "didn't get past it. This run stopped; complete this check on the employer site before retrying.")
        super().__init__(self.reason)


@dataclass
class Challenge:
    """A CAPTCHA found on the page."""
    type: str
    sitekey: str = ""
    action: str = ""
    page_url: str = ""
    image_b64: str = ""
    # Its puzzle is showing right now: the page asked for the check (Submit was pressed, or the
    # box was ticked) and waits for the answer.
    open: bool = False
    user_agent: str = ""

    @property
    def is_image(self) -> bool:
        return self.type == IMAGE_CAPTCHA


# ── Detection ───────────────────────────────────────────────────────────────
# Runs in the page. Ordered most specific first so an enterprise reCAPTCHA is
# not mistaken for a plain v2.
_DETECT_JS = r"""
(() => {
  const visible = (el) => {
    if (!el) return false;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  };
  const hiddenByAncestor = (el) => {
    for (let p = el.parentElement; p; p = p.parentElement) {
      const s = window.getComputedStyle(p);
      if (s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return true;
    }
    return false;
  };

  // Cloudflare Turnstile
  const ts = document.querySelector('.cf-turnstile[data-sitekey], [data-sitekey][class*="turnstile"]');
  if (ts) return { type: 'turnstile', sitekey: ts.getAttribute('data-sitekey') || '', action: '' };
  if (document.querySelector('input[name="cf-turnstile-response"]')) {
    const f = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
    if (f) {
      const m = f.src.match(/[?&]k=([^&]+)/);
      return { type: 'turnstile', sitekey: m ? m[1] : '', action: '' };
    }
  }

  // hCaptcha: the widget element and its iframes. The site key may be on the element or in any
  // frame (the first frame is often the hidden "checkbox-invisible" one, which has none). An
  // invisible hCaptcha runs by itself when the form is sent and usually passes without a puzzle;
  // it only blocks once its puzzle frame is actually showing ("open"). A widget element whose
  // frames show no tick box is an invisible one too (Lever renders it that way), whatever its
  // markup says.
  const hc = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]');
  const hcFrames = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"]'));
  if (hc || hcFrames.length) {
    const key = (hc && (hc.getAttribute('data-sitekey') || hc.getAttribute('data-hcaptcha-sitekey')))
      || hcFrames.map((f) => (f.src.match(/[?&#]sitekey=([^&#]+)/) || [])[1]).find(Boolean) || '';
    const checkbox = hcFrames.some((f) => /frame=checkbox(?!-invisible)/.test(f.src) && visible(f) && !hiddenByAncestor(f));
    const challenge = hcFrames.some((f) => /frame=challenge/.test(f.src) && visible(f) && !hiddenByAncestor(f));
    if (challenge) return { type: 'hcaptcha', sitekey: key, action: '', open: true };
    if (checkbox) return { type: 'hcaptcha', sitekey: key, action: '' };
    const saysInvisible = !!hc && (hc.getAttribute('data-size') || '').toLowerCase() === 'invisible';
    if (hcFrames.length || saysInvisible) return { type: 'hcaptcha_invisible', sitekey: key, action: '' };
    return { type: 'hcaptcha', sitekey: key, action: '' };   // the widget hasn't drawn yet: a visible one until it shows otherwise
  }

  // reCAPTCHA v3 / Enterprise — no visible checkbox, script carries render=<key>
  const v3 = Array.from(document.querySelectorAll('script[src*="recaptcha"]'))
    .map((s) => s.src)
    .find((src) => /[?&]render=/.test(src) && !/render=explicit/.test(src));
  if (v3) {
    const m = v3.match(/[?&]render=([^&]+)/);
    const box = document.querySelector('.g-recaptcha[data-sitekey]');
    if (!box || !visible(box)) {
      return { type: 'recaptcha_v3', sitekey: m ? m[1] : '', action: 'verify' };
    }
  }

  // reCAPTCHA v2
  const v2 = document.querySelector('.g-recaptcha[data-sitekey], [data-sitekey][class*="recaptcha"]');
  if (v2) return { type: 'recaptcha_v2', sitekey: v2.getAttribute('data-sitekey') || '', action: '' };
  const v2Frame = document.querySelector('iframe[src*="google.com/recaptcha"], iframe[src*="recaptcha/api2/anchor"]');
  if (v2Frame) {
    const m = v2Frame.src.match(/[?&]k=([^&]+)/);
    return { type: 'recaptcha_v2', sitekey: m ? m[1] : '', action: '' };
  }

  // Plain image CAPTCHA: a visible image whose attributes mention captcha,
  // sitting next to a text input.
  const img = Array.from(document.querySelectorAll('img')).find((i) => {
    const hay = ((i.src || '') + ' ' + (i.id || '') + ' ' + (i.className || '') + ' ' + (i.alt || '')).toLowerCase();
    return hay.includes('captcha') && visible(i);
  });
  if (img) return { type: 'image_captcha', sitekey: '', action: '', imgSrc: img.src };

  // A site's own "are you human" page (e.g. Oleeo Protect's "Quick Check
  // Needed"): a short page with a checkbox and no CAPTCHA service behind it.
  // No solver can answer it; a person has to.
  const body = (document.body && document.body.innerText) || '';
  const humanText = /quick check needed|confirm you(?:'|’)?re a real person|i(?:'|’)?m not a robot|verify (?:that )?you(?:'|’)?re (?:a )?human|are you a robot/i;
  if (body.length < 3000 && (humanText.test(document.title) || humanText.test(body))
      && document.querySelector('input[type=checkbox], [role=checkbox]')) {
    return { type: 'human_check', sitekey: '', action: '' };
  }

  return null;
})();
"""


async def _evaluate(browser_session, expression: str):
    """Run JS in the focused tab and return the plain value."""
    cdp = await browser_session.get_or_create_cdp_session()
    result = await cdp.cdp_client.send.Runtime.evaluate(
        params={"expression": expression, "returnByValue": True, "awaitPromise": True},
        session_id=cdp.session_id,
    )
    return (result or {}).get("result", {}).get("value")


# ── A site's own "I'm not a robot" box ──────────────────────────────────────
# Pages like Oleeo Protect's "Quick Check Needed" are a plain checkbox and a
# Continue button, with no CAPTCHA service behind them. They are ticked here
# with real mouse events (a JavaScript click is what such pages look for and
# ignore), at most HUMAN_CHECK_ATTEMPTS times per application.
HUMAN_CHECK_ATTEMPTS = 2
HUMAN_CHECK_WAIT_SECONDS = 10

_HUMAN_CHECK_TARGETS_JS = r"""
(() => {
  const shown = (el) => {
    if (!el) return false;
    const s = getComputedStyle(el), r = el.getBoundingClientRect();
    return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const center = (el) => {
    el.scrollIntoView({block: 'center', inline: 'center'});
    const r = el.getBoundingClientRect();
    return [r.left + r.width / 2, r.top + r.height / 2];
  };
  const boxes = Array.from(document.querySelectorAll('input[type=checkbox], [role=checkbox]'));
  const near = (el) => ((el.closest('label') || el.parentElement || el).innerText || '') + ' ' +
                       ((el.id && document.querySelector('label[for="' + CSS.escape(el.id) + '"]') || {}).innerText || '');
  const box = boxes.find((b) => /robot|human|real person/i.test(near(b))) || boxes[0];
  if (!box) return null;
  const checked = box.checked === true || box.getAttribute('aria-checked') === 'true';
  // An invisible input: click what the user sees (its label, else its wrapper).
  const label = box.id ? document.querySelector('label[for="' + CSS.escape(box.id) + '"]') : null;
  const target = shown(box) ? box : (shown(label) ? label : box.closest('label') || box.parentElement);
  const boxAt = target ? center(target) : null;
  const buttons = Array.from(document.querySelectorAll('button, input[type=submit], input[type=button], a, [role=button]'))
    .filter((b) => shown(b) && /^(continue|submit|verify|proceed|next|i'?m human|confirm)\b/i.test(((b.innerText || b.value || '') + '').trim()));
  return { checked, box: boxAt, button: buttons.length ? center(buttons[0]) : null };
})()
"""


async def _real_click(browser_session, x: float, y: float) -> None:
    """A real mouse click (move, press, release) at viewport coordinates."""
    import random
    cdp = await browser_session.get_or_create_cdp_session()
    send = cdp.cdp_client.send.Input.dispatchMouseEvent
    await send(params={"type": "mouseMoved", "x": x, "y": y}, session_id=cdp.session_id)
    await asyncio.sleep(random.uniform(0.08, 0.25))
    await send(params={"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id=cdp.session_id)
    await asyncio.sleep(random.uniform(0.05, 0.15))
    await send(params={"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1}, session_id=cdp.session_id)


async def solve_human_check(browser_session) -> bool:
    """Tick a site's own "I'm not a robot" box and press Continue. True once the check is gone.

    Never raises. Counts attempts on the browser session, so a check that
    won't pass is only tried HUMAN_CHECK_ATTEMPTS times per application.
    """
    import random
    tried = getattr(browser_session, "_langhire_human_check_tries", 0)
    if tried >= HUMAN_CHECK_ATTEMPTS:
        return False
    try:
        browser_session._langhire_human_check_tries = tried + 1
    except Exception:
        pass
    try:
        targets = await _evaluate(browser_session, _HUMAN_CHECK_TARGETS_JS)
        if not isinstance(targets, dict):
            return False
        await asyncio.sleep(random.uniform(0.6, 1.4))  # a person reads the page first
        if not targets.get("checked") and targets.get("box"):
            await _real_click(browser_session, *targets["box"])
            await asyncio.sleep(random.uniform(0.7, 1.5))
        if targets.get("button"):
            # Re-read: ticking the box can move or reveal the button.
            again = await _evaluate(browser_session, _HUMAN_CHECK_TARGETS_JS)
            button = (again or {}).get("button") if isinstance(again, dict) else None
            await _real_click(browser_session, *(button or targets["button"]))
        loop = asyncio.get_running_loop()
        deadline = loop.time() + HUMAN_CHECK_WAIT_SECONDS
        while loop.time() < deadline:
            await asyncio.sleep(1)
            found = await detect(browser_session)
            if found is None or found.type != HUMAN_CHECK:
                print("    🤖 Ticked the site's \"I'm not a robot\" check")
                return True
    except Exception:
        return False
    return False


async def detect(browser_session) -> Challenge | None:
    """Return the CAPTCHA on the current page, or None.

    Never raises — a detection failure must not break the run.
    """
    try:
        found = await _evaluate(browser_session, _DETECT_JS)
    except Exception:
        return None
    if not found or not isinstance(found, dict):
        return None

    page_url, user_agent = "", ""
    try:
        page_url = await _evaluate(browser_session, "window.location.href") or ""
    except Exception:
        pass
    if found.get("type") in (HCAPTCHA, HCAPTCHA_INVISIBLE):
        try:
            agent = await _evaluate(browser_session, "navigator.userAgent")
            user_agent = agent if isinstance(agent, str) else ""
        except Exception:
            pass

    return Challenge(
        type=found.get("type", ""),
        sitekey=found.get("sitekey", "") or "",
        action=found.get("action", "") or "",
        page_url=page_url,
        image_b64=found.get("imgSrc", "") or "",
        open=found.get("open") is True,
        user_agent=user_agent[:400],
    )


# Installed in every page before its own scripts run (cli/apply_jobs gives it to the run's
# watcher). hCaptcha hands its answer to a function the page passes to hcaptcha.render(); that
# function is the only way to complete a check the page is waiting on (an invisible hCaptcha
# after Submit), and the page keeps it to itself. This records it. Nothing else changes: the
# page's own calls go through untouched.
HOOK_JS = r"""(() => {
  if (window.__lhCaptcha) return;
  const st = { callbacks: [] };
  try { Object.defineProperty(window, '__lhCaptcha', { value: st }); } catch (e) { return; }
  const wrap = (hc) => {
    try {
      if (!hc || hc.__lhWrapped || typeof hc.render !== 'function') return hc;
      const render = hc.render;
      hc.render = function (container, params) {
        try {
          const given = params && params.callback;
          const fn = typeof given === 'string' ? window[given] : given;
          if (typeof fn === 'function' && !st.callbacks.includes(fn)) st.callbacks.push(fn);
        } catch (e) {}
        return render.apply(this, arguments);
      };
      Object.defineProperty(hc, '__lhWrapped', { value: true });
    } catch (e) {}
    return hc;
  };
  let current = wrap(window.hcaptcha);
  try {
    Object.defineProperty(window, 'hcaptcha', {
      configurable: true, enumerable: true,
      get() { return current; },
      set(value) { current = wrap(value); },
    });
  } catch (e) {}
})();"""


# Whether the page already holds an answer for its CAPTCHA: the response field the widget (or
# the solver's answer) fills in. A challenge with an answer isn't blocking anything, and its
# answer isn't bought again.
_ANSWERED_JS = r"""
(() => {
  const filled = (selector) => Array.from(document.querySelectorAll(selector)).some((el) => (el.value || '').length > 20);
  return filled('textarea[name="g-recaptcha-response"], textarea[id^="g-recaptcha-response"]')
    || filled('textarea[name="h-captcha-response"], [name="h-captcha-response"]')
    || filled('input[name="cf-turnstile-response"]');
})();
"""


async def answered(browser_session) -> bool:
    """The page's CAPTCHA response field holds an answer (the user's, the widget's or the solver's)."""
    try:
        return (await _evaluate(browser_session, _ANSWERED_JS)) is True
    except Exception:
        return False


def solver_enabled(settings: dict) -> bool:
    """2Captcha is used whenever a key is saved, unless the switch was turned off explicitly."""
    settings = settings or {}
    has_key = bool((settings.get("captcha_api_key") or "").strip())
    switch = settings.get("captcha_solver_enabled")
    return bool(switch) if switch is not None and not has_key else (has_key and switch is not False)


# ── Token injection ─────────────────────────────────────────────────────────
def _inject_js(challenge: Challenge, token: str) -> str:
    """Build the JS that writes a solved token into the page."""
    safe = token.replace("\\", "\\\\").replace("'", "\\'")
    if challenge.type in (RECAPTCHA_V2, RECAPTCHA_V3):
        field = "g-recaptcha-response"
    elif challenge.type in (HCAPTCHA, HCAPTCHA_INVISIBLE):
        field = "h-captcha-response"
    elif challenge.type == TURNSTILE:
        field = "cf-turnstile-response"
    else:
        field = ""

    if challenge.type == IMAGE_CAPTCHA:
        # An image CAPTCHA answer is typed into a text box, not written to a
        # hidden token element. Find the most likely input: one whose name/id
        # mentions captcha, else the text input nearest the captcha image.
        return f"""
(() => {{
  const answer = '{safe}';
  const visible = (el) => {{
    if (!el) return false;
    const s = window.getComputedStyle(el);
    if (s.display === 'none' || s.visibility === 'hidden') return false;
    const r = el.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  }};
  const inputs = Array.from(
    document.querySelectorAll('input[type="text"], input:not([type]), input[type="tel"]')
  ).filter(visible);
  if (!inputs.length) return false;

  let target = inputs.find((i) =>
    ((i.name || '') + ' ' + (i.id || '') + ' ' + (i.className || '') +
     ' ' + (i.placeholder || '') + ' ' + (i.getAttribute('aria-label') || ''))
      .toLowerCase().includes('captcha')
  );

  if (!target) {{
    // Fall back to the visible text input closest to the CAPTCHA image.
    const img = Array.from(document.querySelectorAll('img')).find((i) => {{
      const hay = ((i.src || '') + ' ' + (i.id || '') + ' ' + (i.className || '') +
                   ' ' + (i.alt || '')).toLowerCase();
      return hay.includes('captcha') && visible(i);
    }});
    if (img) {{
      const ir = img.getBoundingClientRect();
      let best = Infinity;
      for (const i of inputs) {{
        const r = i.getBoundingClientRect();
        const d = Math.hypot(r.left - ir.left, r.top - ir.top);
        if (d < best) {{ best = d; target = i; }}
      }}
    }}
  }}

  if (!target) return false;
  const setter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype, 'value'
  ).set;
  setter.call(target, answer);            // works with React-controlled inputs
  target.dispatchEvent(new Event('input', {{ bubbles: true }}));
  target.dispatchEvent(new Event('change', {{ bubbles: true }}));
  return true;
}})();
"""

    if not field:
        return ""

    hcaptcha = _HCAPTCHA_HANDOVER_JS.replace("__DELIVER_NOW__", "true" if challenge.open else "false") \
        if field == "h-captcha-response" else ""
    return f"""
(() => {{
  const token = '{safe}';
  let wrote = false;
  document.querySelectorAll('[name="{field}"], #{field}').forEach((el) => {{
    el.value = token;
    el.innerHTML = token;
    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
    wrote = true;
  }});
  if (!wrote) {{
    const ta = document.createElement('textarea');
    ta.name = '{field}';
    ta.id = '{field}';
    ta.style.display = 'none';
    ta.value = token;
    (document.forms[0] || document.body).appendChild(ta);
    wrote = true;
  }}
  // Fire the site's own completion callback when one is registered.
  try {{
    const c = window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients;
    if (c) {{
      Object.values(c).forEach((client) => {{
        Object.values(client || {{}}).forEach((v) => {{
          if (v && typeof v === 'object') {{
            Object.values(v).forEach((inner) => {{
              if (inner && typeof inner.callback === 'function') inner.callback(token);
            }});
          }}
        }});
      }});
    }}
  }} catch (e) {{}}
  {hcaptcha}
  return wrote;
}})();
"""


# hCaptcha gives its answer to the page through the function the page handed to
# hcaptcha.render() (recorded by HOOK_JS) or named in data-callback. Writing the answer into the
# response field is enough for a page that reads the field when the form is sent (Lever does);
# a page that waits for that function gets it called:
# - its puzzle is showing (the page asked for the check and waits): now;
# - otherwise: the next time the page asks for the check (hcaptcha.execute), once.
_HCAPTCHA_HANDOVER_JS = r"""
  try {
    const st = window.__lhCaptcha;
    const named = Array.from(document.querySelectorAll('.h-captcha[data-callback], [data-hcaptcha-sitekey][data-callback]'))
      .map((el) => window[el.getAttribute('data-callback')]).filter((fn) => typeof fn === 'function');
    const callbacks = Array.from(new Set([...(st ? st.callbacks : []), ...named]));
    const hand = () => callbacks.forEach((fn) => { try { fn(token); } catch (e) {} });
    const hc = window.hcaptcha;
    if (hc) { try { hc.getResponse = function () { return token; }; } catch (e) {} }
    if (__DELIVER_NOW__) {
      hand();
      try { if (hc && typeof hc.close === 'function') hc.close(); } catch (e) {}
    } else if (hc && typeof hc.execute === 'function' && callbacks.length) {
      const execute = hc.execute;
      hc.execute = function () {
        hc.execute = execute;
        hand();
        const last = arguments.length ? arguments[arguments.length - 1] : null;
        if (last && typeof last === 'object' && last.async) return Promise.resolve({ response: token, key: '' });
      };
    }
  } catch (e) {}
"""


async def inject_token(browser_session, challenge: Challenge, token: str) -> bool:
    """Write a solved token into the page. Returns True when written."""
    script = _inject_js(challenge, token)
    if not script:
        return False
    try:
        return bool(await _evaluate(browser_session, script))
    except Exception:
        return False


# ── Solving ─────────────────────────────────────────────────────────────────
def _solve_sync(api_key: str, challenge: Challenge) -> str:
    """Call 2Captcha. Runs off the event loop; returns the token."""
    from twocaptcha import TwoCaptcha

    solver = TwoCaptcha(api_key)
    solver.default_timeout = SOLVE_SECONDS
    solver.recaptcha_timeout = SOLVE_SECONDS
    solver.api_client = _SolverAPI()
    if challenge.type == RECAPTCHA_V2:
        res = solver.recaptcha(sitekey=challenge.sitekey, url=challenge.page_url)
    elif challenge.type == RECAPTCHA_V3:
        res = solver.recaptcha(
            sitekey=challenge.sitekey,
            url=challenge.page_url,
            version="v3",
            action=challenge.action or "verify",
        )
    elif challenge.type in (HCAPTCHA, HCAPTCHA_INVISIBLE):
        # The answer is checked against the browser that hands it in: 2Captcha's worker is told
        # this browser's user agent, and that the widget is an invisible one when it is.
        extra = {}
        if challenge.user_agent:
            extra["userAgent"] = challenge.user_agent
        if challenge.type == HCAPTCHA_INVISIBLE:
            extra["invisible"] = 1
        res = solver.hcaptcha(sitekey=challenge.sitekey, url=challenge.page_url, **extra)
    elif challenge.type == TURNSTILE:
        res = solver.turnstile(sitekey=challenge.sitekey, url=challenge.page_url)
    elif challenge.type == IMAGE_CAPTCHA:
        # The detector hands back the <img> src, which is either a data: URI or
        # a normal URL. 2Captcha wants raw base64 for the former.
        # Only an image already in the page: the solver library would download
        # a URL itself (any address) or upload a local file named by the page.
        src = challenge.image_b64
        match = re.fullmatch(r"data:image/[a-zA-Z0-9.+-]+;base64,([A-Za-z0-9+/=\r\n]+)", src)
        if not match or len(src) > 1_500_000:
            raise ValueError("the CAPTCHA image isn't embedded in the page")
        body = match[1].replace("\r", "").replace("\n", "")
        try:
            decoded = base64.b64decode(body, validate=True)
        except (ValueError, binascii.Error) as error:
            raise ValueError("the CAPTCHA image is not valid base64") from error
        if not decoded:
            raise ValueError("the CAPTCHA image is empty")
        # normal(string) guesses URL/file/base64 from the string, including
        # opening a local path or fetching a URL hidden after 'data:image/...'.
        # This explicit SDK operation cannot select either of those branches.
        res = solver.solve(method="base64", body=body)
    else:
        raise ValueError(f"unsupported challenge type: {challenge.type}")
    return (res or {}).get("code", "")


async def solve(challenge: Challenge, api_key: str) -> str:
    """Solve one challenge via 2Captcha, off the event loop."""
    if not api_key:
        raise CaptchaUnsolved(challenge.type, "no 2Captcha API key configured")
    if challenge.type != IMAGE_CAPTCHA and not challenge.sitekey:
        raise CaptchaUnsolved(challenge.type, "could not read the site key")
    try:
        token = await asyncio.wait_for(asyncio.to_thread(_solve_sync, api_key, challenge), SOLVE_SECONDS + 25)
    except ImportError as e:
        raise CaptchaUnsolved(challenge.type, f"2captcha-python not installed: {e}") from e
    except Exception as e:
        # Deliberately does not include the key or the exception repr, which
        # some SDK errors embed the request URL (and key) into.
        if isinstance(e, TimeoutError) or type(e).__name__ in {"TimeoutException", "ReadTimeout", "ConnectTimeout"}:
            raise CaptchaUnsolved(challenge.type, "2Captcha gave no answer within its time limit") from e
        if isinstance(e, SolverRejected):
            # Its own fixed error word, in plain words (it used to read "2Captcha error (ValueError)").
            unsolved = CaptchaUnsolved(challenge.type, _SOLVER_ERRORS.get(e.code, f"2Captcha refused it ({e.code})"))
            unsolved.retry = e.code in _RETRYABLE_SOLVER_ERRORS
            raise unsolved from e
        raise CaptchaUnsolved(challenge.type, f"2Captcha error ({type(e).__name__})") from e
    if not token:
        raise CaptchaUnsolved(challenge.type, "2Captcha returned an empty token")
    return token


def balance(api_key: str, seconds: float = 8) -> float | None:
    """The 2Captcha account's balance (a free request to its API), or None when it can't be read
    within `seconds`.

    Raises SolverRejected when 2Captcha refuses the key itself.
    """
    if not (api_key or "").strip():
        return None
    try:
        api = _SolverAPI()
        api.deadline = time.monotonic() + seconds
        answer = api.res(key=api_key.strip(), action="getbalance")
    except SolverRejected:
        raise
    except Exception:
        return None
    try:
        return float(str(answer).strip())
    except ValueError:
        return None


async def handle(browser_session, settings: dict, include_invisible: bool = False) -> bool:
    """Detect and, if enabled, solve a CAPTCHA on the current page.

    Returns False when no CAPTCHA is present. Returns True when 2Captcha's answer
    was written into the page (whether the site accepts it shows on the page). Raises CaptchaUnsolved when a CAPTCHA is present
    but cannot be solved — including when the solver switch is off.

    An invisible hCaptcha runs by itself when the form is sent and usually passes; it is left to
    do so unless `include_invisible` (it didn't get the form through the first time), and then
    never ends the application by itself.
    """
    challenge = await detect(browser_session)
    if challenge is None or not challenge.type:
        return False
    if challenge.type == HCAPTCHA_INVISIBLE and (not include_invisible or not solver_enabled(settings)):
        return False
    if challenge.type == HUMAN_CHECK:
        if await solve_human_check(browser_session):
            return True
        raise HumanCheckNeeded(challenge.page_url)

    if challenge.type in (RECAPTCHA_V2, HCAPTCHA, TURNSTILE) and not challenge.open and await answered(browser_session):
        return False    # already answered: nothing blocks, and no second answer is bought

    # A step callback runs repeatedly. Successful token injection is not proof
    # the site accepted it, and must not reset the per-application paid budget.
    state = getattr(browser_session, "_langhire_captcha_state", None)
    if not isinstance(state, dict):
        state = {"attempts": 0, "nonblocking": set()}
        browser_session._langhire_captcha_state = state
    key = (challenge.type, challenge.sitekey, challenge.page_url)
    if challenge.type not in BLOCKING_TYPES and key in state["nonblocking"]:
        return False

    if not solver_enabled(settings):
        # Only a challenge that actually blocks submission ends the application.
        if challenge.type not in BLOCKING_TYPES:
            return False
        raise CaptchaUnsolved(challenge.type, "2Captcha solving is turned off in Settings"
                              if settings.get("captcha_solver_enabled") is False
                              or (settings.get("captcha_api_key") or "").strip()
                              else "no 2Captcha key is saved in Settings → Applications")

    api_key = (settings.get("captcha_api_key") or "").strip()
    last_reason = "unknown error"
    while state["attempts"] < MAX_SOLVE_ATTEMPTS:
        state["attempts"] += 1
        attempt = state["attempts"]
        print(f"    🧩 CAPTCHA detected ({challenge.type}) — solve attempt {attempt}/{MAX_SOLVE_ATTEMPTS}")
        try:
            token = await solve(challenge, api_key)
        except CaptchaUnsolved as e:
            last_reason = e.reason
            # A missing key, an empty balance or an unsupported type will not improve on retry.
            if getattr(e, "retry", True) is False or any(
                    reason in e.reason for reason in ("API key", "site key", "not installed", "time limit")):
                break
            continue
        if await inject_token(browser_session, challenge, token):
            # Written into the page, not yet known to be accepted: the agent sees whether the
            # site lets it through (and a blocking one that stays is reported as unsolved).
            print("    🧩 CAPTCHA answer from 2Captcha entered into the page")
            if challenge.type not in BLOCKING_TYPES:
                state["nonblocking"].add(key)
            return True
        last_reason = "token could not be injected into the page"

    # Solving failed. A non-blocking challenge still does not end the run.
    if challenge.type not in BLOCKING_TYPES:
        print(f"    ⚠️  Could not solve {challenge.type} ({last_reason}) — not blocking, continuing")
        return False
    raise CaptchaUnsolved(challenge.type, last_reason if last_reason != "unknown error" else
                         "the site still blocks progress after the application's two solver attempts")
