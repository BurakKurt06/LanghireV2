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
from dataclasses import dataclass

MAX_SOLVE_ATTEMPTS = 2

# Supported challenge types.
RECAPTCHA_V2 = "recaptcha_v2"
RECAPTCHA_V3 = "recaptcha_v3"
HCAPTCHA = "hcaptcha"
TURNSTILE = "turnstile"
IMAGE_CAPTCHA = "image_captcha"
# Detected but never sent to the solver:
HCAPTCHA_INVISIBLE = "hcaptcha_invisible"  # runs by itself on submit; not a blocker
HUMAN_CHECK = "human_check"  # a site's own "are you human" box; needs the user

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
        self.reason = ("This site asks you to confirm you're a real person before it shows the application. "
                       "Run this job in Trial mode and tick the box when the agent asks for help.")
        super().__init__(self.reason)


@dataclass
class Challenge:
    """A CAPTCHA found on the page."""
    type: str
    sitekey: str = ""
    action: str = ""
    page_url: str = ""
    image_b64: str = ""

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

  // hCaptcha
  const hc = document.querySelector('.h-captcha[data-sitekey], [data-hcaptcha-sitekey]');
  if (hc) {
    return {
      type: 'hcaptcha',
      sitekey: hc.getAttribute('data-sitekey') || hc.getAttribute('data-hcaptcha-sitekey') || '',
      action: '',
    };
  }
  // hCaptcha iframes: the site key may be in any of them (the first one is
  // often the hidden "checkbox-invisible" frame, which has none). An
  // invisible hCaptcha runs by itself on submit and usually passes without a
  // puzzle; it only blocks once its challenge frame is actually showing.
  const hcFrames = Array.from(document.querySelectorAll('iframe[src*="hcaptcha.com"]'));
  if (hcFrames.length) {
    const key = hcFrames.map((f) => (f.src.match(/[?&#]sitekey=([^&#]+)/) || [])[1]).find(Boolean) || '';
    const checkbox = hcFrames.some((f) => /frame=checkbox(?!-invisible)/.test(f.src) && visible(f));
    const challenge = hcFrames.some((f) => /frame=challenge/.test(f.src) && visible(f) && !hiddenByAncestor(f));
    if (!checkbox && !challenge) return { type: 'hcaptcha_invisible', sitekey: key, action: '' };
    return { type: 'hcaptcha', sitekey: key, action: '' };
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

    page_url = ""
    try:
        page_url = await _evaluate(browser_session, "window.location.href") or ""
    except Exception:
        pass

    return Challenge(
        type=found.get("type", ""),
        sitekey=found.get("sitekey", "") or "",
        action=found.get("action", "") or "",
        page_url=page_url,
        image_b64=found.get("imgSrc", "") or "",
    )


# ── Token injection ─────────────────────────────────────────────────────────
def _inject_js(challenge: Challenge, token: str) -> str:
    """Build the JS that writes a solved token into the page."""
    safe = token.replace("\\", "\\\\").replace("'", "\\'")
    if challenge.type in (RECAPTCHA_V2, RECAPTCHA_V3):
        field = "g-recaptcha-response"
    elif challenge.type == HCAPTCHA:
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
  return wrote;
}})();
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
    if challenge.type == RECAPTCHA_V2:
        res = solver.recaptcha(sitekey=challenge.sitekey, url=challenge.page_url)
    elif challenge.type == RECAPTCHA_V3:
        res = solver.recaptcha(
            sitekey=challenge.sitekey,
            url=challenge.page_url,
            version="v3",
            action=challenge.action or "verify",
        )
    elif challenge.type == HCAPTCHA:
        res = solver.hcaptcha(sitekey=challenge.sitekey, url=challenge.page_url)
    elif challenge.type == TURNSTILE:
        res = solver.turnstile(sitekey=challenge.sitekey, url=challenge.page_url)
    elif challenge.type == IMAGE_CAPTCHA:
        # The detector hands back the <img> src, which is either a data: URI or
        # a normal URL. 2Captcha wants raw base64 for the former.
        src = challenge.image_b64
        if src.startswith("data:"):
            src = src.split(",", 1)[-1]
        res = solver.normal(src)
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
        token = await asyncio.to_thread(_solve_sync, api_key, challenge)
    except ImportError as e:
        raise CaptchaUnsolved(challenge.type, f"2captcha-python not installed: {e}") from e
    except Exception as e:
        # Deliberately does not include the key or the exception repr, which
        # some SDK errors embed the request URL (and key) into.
        raise CaptchaUnsolved(challenge.type, f"2Captcha error ({type(e).__name__})") from e
    if not token:
        raise CaptchaUnsolved(challenge.type, "2Captcha returned an empty token")
    return token


async def handle(browser_session, settings: dict) -> bool:
    """Detect and, if enabled, solve a CAPTCHA on the current page.

    Returns False when no CAPTCHA is present. Returns True when one was solved
    and its token injected. Raises CaptchaUnsolved when a CAPTCHA is present
    but cannot be solved — including when the solver switch is off.
    """
    challenge = await detect(browser_session)
    if challenge is None or not challenge.type:
        return False
    if challenge.type == HCAPTCHA_INVISIBLE:
        return False
    if challenge.type == HUMAN_CHECK:
        raise HumanCheckNeeded(challenge.page_url)

    if not settings.get("captcha_solver_enabled"):
        # Only a challenge that actually blocks submission ends the application.
        if challenge.type not in BLOCKING_TYPES:
            return False
        raise CaptchaUnsolved(challenge.type, "2Captcha solving is turned off in Settings")

    api_key = (settings.get("captcha_api_key") or "").strip()
    last_reason = "unknown error"
    for attempt in range(1, MAX_SOLVE_ATTEMPTS + 1):
        print(f"    🧩 CAPTCHA detected ({challenge.type}) — solve attempt {attempt}/{MAX_SOLVE_ATTEMPTS}")
        try:
            token = await solve(challenge, api_key)
        except CaptchaUnsolved as e:
            last_reason = e.reason
            # A missing key or an unsupported type will not improve on retry.
            if "API key" in e.reason or "site key" in e.reason or "not installed" in e.reason:
                break
            continue
        if await inject_token(browser_session, challenge, token):
            print("    ✅ CAPTCHA solved")
            return True
        last_reason = "token could not be injected into the page"

    # Solving failed. A non-blocking challenge still does not end the run.
    if challenge.type not in BLOCKING_TYPES:
        print(f"    ⚠️  Could not solve {challenge.type} ({last_reason}) — not blocking, continuing")
        return False
    raise CaptchaUnsolved(challenge.type, last_reason)
