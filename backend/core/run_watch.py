"""What happens in the agent's browser between its own actions: the user, or nothing at all.

Two things are watched while an application runs, by a task that polls the browser:

THE USER STEPPING IN. Any click, key press or field change in the agent's browser window
that the agent didn't make itself is the user's, at any moment of a run (not only during a
pause). The agent's own actions are known: each runs inside `acting()`, and input the page
receives in that window is the agent's. Everything else is the user's. While the user is
active the agent does nothing; it goes on once the user has been idle for USER_IDLE_SECONDS
(or pressed Resume), re-reads the page, and never again touches a field the user changed.

A PAGE THAT DOESN'T RESPOND. If the page stays exactly as it is for STALL_SECONDS while
the agent is working on it (time spent waiting for the AI, for the user, for an email or for
a CAPTCHA solver doesn't count), the application is reported stalled and its caller retries
the job. No page or step can hold a run for hours.

The page script runs in an isolated world: page scripts can't see or change it. It records
only trusted input events (a script calling el.click() or dispatchEvent isn't the user).
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import time
from contextlib import contextmanager

from . import browser_policy, form_snapshot

USER_IDLE_SECONDS = 25        # the user's last click or key press was this long ago: carry on
STALL_SECONDS = 300           # the page hasn't changed for this long: a failure
POLL_SECONDS = 0.7
SIGNATURE_SECONDS = 10
ACTION_GRACE_MS = 600         # events arriving just after an agent action still belong to it
# A link the agent clicked: the browser announces the navigation when the site starts it, which
# can be seconds after the click on a slow page (and the announcement carries no click time).
# Only a click or key press of the agent can own such a late navigation, and only one each: a
# link the user clicks right after the agent typed, or after the agent's click already moved the
# page, is the user's.
NAVIGATION_GRACE_MS = 3000
NAVIGATING_ACTIONS = ("click", "send_keys")
# A page that calls window.print() opens the browser's print window, and the tab stops answering
# until somebody closes it (seen 2026-11: an application's "printable version" tab froze five
# runs). Printing is never part of applying: in the agent's browser the call does nothing.
NO_PRINT_JS = "(() => { try { window.print = function () {}; } catch (e) {} })();"
WAITING_NOTE = "You stepped in, so the agent is waiting."
NOTE_SECONDS = 30             # how long "Continuing from your page ..." stays in the run panel
WORLD = "langhire-watch"

_WATCH_JS = r"""(() => {
  if (window.__lhWatch) return;
  const w = { events: [], presence: 0 };
  Object.defineProperty(window, '__lhWatch', { value: w });
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  const labelOf = (el) => {
    if (el.labels && el.labels.length) return text(el.labels[0]);
    const ids = el.getAttribute && el.getAttribute('aria-labelledby');
    if (ids) {
      const t = ids.split(/\s+/).map((id) => text(document.getElementById(id))).join(' ').trim();
      if (t) return t;
    }
    if (el.getAttribute && el.getAttribute('aria-label')) return el.getAttribute('aria-label');
    const group = el.closest && el.closest('fieldset, [role="radiogroup"], [role="group"]');
    if (group) {
      const legend = group.querySelector('legend, label');
      if (legend) return text(legend);
    }
    return (el.getAttribute && el.getAttribute('placeholder')) || el.name || el.id || '';
  };
  const field = (target) => {
    const el = target && target.closest && target.closest(
      'input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"], [contenteditable="true"]');
    if (!el) return null;
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (['hidden', 'submit', 'button', 'image', 'reset', 'search'].includes(type)) return null;
    let label = labelOf(el), value;
    if (type === 'password') value = '';
    else if (type === 'checkbox') value = el.checked ? 'checked' : 'unchecked';
    else if (type === 'radio') {
      const group = el.closest('fieldset, [role="radiogroup"], [role="group"]');
      const legend = group && group.querySelector('legend');
      const checked = el.name ? document.querySelector('input[type="radio"][name="' + CSS.escape(el.name) + '"]:checked') : (el.checked ? el : null);
      value = checked ? labelOf(checked) : '';
      if (legend) label = text(legend);
    } else if (type === 'file') value = Array.from(el.files || []).map((f) => f.name).join(', ');
    else if (el.tagName === 'SELECT') value = el.selectedOptions.length ? text(el.selectedOptions[0]) : '';
    else if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA') value = el.value || '';
    else value = text(el);
    const key = el.id || el.name || label;
    if (!key) return null;
    return { key: String(key).slice(0, 200), label: String(label).slice(0, 200), value: String(value).slice(0, 500),
             secret: type === 'password' };
  };
  const top = window.top === window;
  const send = (record) => {
    if (top) {
      w.events.push(record);
      if (w.events.length > 400) w.events.splice(0, 200);
    } else {
      try { window.top.postMessage({ __lhWatch: record }, '*'); } catch (e) {}
    }
  };
  ['pointerdown', 'mousedown', 'keydown', 'input', 'change', 'paste', 'drop'].forEach((type) => {
    window.addEventListener(type, (e) => {
      if (!e.isTrusted) return;
      const record = { t: Date.now(), type };
      if (type === 'input' || type === 'change') { try { record.field = field(e.target); } catch (err) {} }
      send(record);
    }, { capture: true, passive: true });
  });
  let lastPresence = 0;
  ['mousemove', 'wheel'].forEach((type) => {
    window.addEventListener(type, (e) => {
      const now = Date.now();
      if (!e.isTrusted || now - lastPresence < 300) return;
      lastPresence = now;
      if (top) w.presence = now; else send({ t: now, type: 'presence' });
    }, { capture: true, passive: true });
  });
  if (top) {
    window.addEventListener('message', (e) => {
      const r = e.data && e.data.__lhWatch;
      if (!r || typeof r.t !== 'number') return;
      if (r.type === 'presence') { w.presence = r.t; return; }
      w.events.push({ t: r.t, type: String(r.type).slice(0, 20), field: r.field || null });
    });
  }
})();"""

_READ_JS = ("(() => { const w = window.__lhWatch; if (!w) return null; "
            "return JSON.stringify({ events: w.events.splice(0), presence: w.presence, url: location.href }); })()")

# The page as the agent meets it: how much it shows, whether anything can be used, and a
# fingerprint of its text and form values (to tell a page that changes from one that doesn't).
_STATE_JS = r"""() => {
  const body = document.body;
  const text = ((body && body.innerText) || '').trim();
  let controls = 0;
  for (const el of document.querySelectorAll('input, select, textarea, button, a[href], [role="button"], iframe')) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'hidden') continue;
    const r = el.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) { controls++; if (controls >= 50) break; }
  }
  const values = [];
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.getAttribute('type') || '').toLowerCase();
    if (type === 'password' || type === 'hidden') continue;
    values.push(type === 'checkbox' || type === 'radio' ? (el.checked ? '1' : '0') : String(el.value || '').slice(0, 80));
    if (values.length >= 300) break;
  }
  return JSON.stringify({ text: text.length, controls, url: location.href, ready: document.readyState,
                          sample: text.slice(0, 4000) + '|' + text.slice(-1000), values: values.join('\u0001') });
}"""

# The field an action would change, as the form snapshot names it (run on the action's element).
FIELD_OF_NODE_JS = r"""function() {
  const text = (el) => ((el && (el.innerText || el.textContent)) || '').replace(/\s+/g, ' ').trim();
  let el = this.closest ? this.closest('input, select, textarea, [role="combobox"], button[aria-haspopup="listbox"], [contenteditable="true"]') : null;
  if (!el && this.closest) {
    const label = this.closest('label');
    if (label) el = label.control || label.querySelector('input, select, textarea');
  }
  if (!el) return '';
  const type = (el.getAttribute('type') || '').toLowerCase();
  let label = '';
  if (el.labels && el.labels.length) label = text(el.labels[0]);
  if (!label) {
    const ids = el.getAttribute('aria-labelledby');
    if (ids) label = ids.split(/\s+/).map((id) => text(document.getElementById(id))).join(' ').trim();
  }
  if (!label) label = el.getAttribute('aria-label') || '';
  const group = el.closest('fieldset, [role="radiogroup"], [role="group"]');
  if (group && (type === 'radio' || !label)) {
    const legend = group.querySelector(type === 'radio' ? 'legend' : 'legend, label');
    if (legend) label = text(legend);
  }
  if (!label) label = el.getAttribute('placeholder') || el.name || el.id || '';
  return JSON.stringify({ key: String(el.id || el.name || label).slice(0, 200), label: String(label).slice(0, 200) });
}"""


class PageStalled(Exception):
    """The page stopped responding (or never loaded): this attempt at the job is over."""

    def __init__(self, reason: str, url: str = "", final: bool = False):
        super().__init__(reason)
        self.reason, self.url, self.final = reason, url, final


def is_blank(state: dict | None) -> bool:
    """Nothing to read and nothing to use: a page that didn't load (or a browser error page)."""
    if not state:
        return True
    url = str(state.get("url") or "")
    if url.startswith(("chrome-error://", "about:blank")) or url in ("", "about:srcdoc"):
        return True
    return int(state.get("text") or 0) < 40 and int(state.get("controls") or 0) == 0


async def page_state(browser_session, timeout: float = 8.0) -> dict | None:
    """The current page's state (see _STATE_JS), or None when the page doesn't answer in time."""
    try:
        raw = await asyncio.wait_for(browser_policy.evaluate(browser_session, _STATE_JS), timeout)
        return json.loads(raw) if raw else None
    except Exception:
        return None


def _signature(state: dict | None) -> str:
    if not state:
        return ""
    return hashlib.sha1(json.dumps([state.get("url"), state.get("sample"), state.get("values")]).encode()).hexdigest()


class RunWatch:
    """One application's watcher (see the module docstring). Start with run(); stop by cancelling it."""

    def __init__(self, browser_session, *, stall_seconds: float | None = None,
                 idle_seconds: float | None = None, init_scripts: tuple[str, ...] = (),
                 page_scripts: tuple[str, ...] = ()):
        self.browser = browser_session
        self.stall_seconds = STALL_SECONDS if stall_seconds is None else stall_seconds
        self.idle_seconds = USER_IDLE_SECONDS if idle_seconds is None else idle_seconds
        self.init_scripts = tuple(init_scripts)     # also installed in every new tab (stealth)
        # The app's own page helpers, in every tab including the first (NO_PRINT_JS, the CAPTCHA hook).
        self.page_scripts = tuple(page_scripts)
        self._page_scripted: set[str] = set()
        self._windows: list[tuple[float, float]] = []   # (start ms, end ms) of the agent's own actions
        self._open: list[float] = []                    # start ms of the agent actions running now
        self._may_navigate: list[float] = []            # end ms of agent clicks that haven't moved the page yet
        self._holds = 0
        self._installed: set[str] = set()
        self._scripted: set[str] = set()                # tabs that already have init_scripts
        self._contexts: dict[str, int] = {}             # tab -> its watcher world
        self._listening = False
        self._seen: list[tuple] = []                    # (time, type) of events already counted
        self._last_user_event = 0.0     # monotonic seconds
        self._resume_now = False
        self.user_stepped_in = False    # since the agent last looked (cleared by take_user_changes)
        self._user_events: list[dict] = []
        self._baseline: dict[str, tuple[str, str]] = {}
        self._baseline_url = ""
        self.locked: dict[tuple[str, str], str] = {}    # (key, label) -> the value the user left there
        self.user_moved = False         # the user took the application to another page at least once
        self.note = ""                  # the last "Continuing ..." line (shown for NOTE_SECONDS)
        self._note_at = 0.0
        self.edits_kept = 0
        self._stalled_for = 0.0
        self._last_signature = None
        self._stall = asyncio.Event()
        self.stall_reason = ""
        self.stall_url = ""
        self.last_state: dict | None = None
        self._moved_in_action = False

    # ── the agent's own actions, and the app's long waits ───────────────────
    @contextmanager
    def acting(self, action: str = ""):
        """Input arriving inside this block (and just after it) is the agent's, not the user's."""
        start = time.time() * 1000
        self._open.append(start)
        self._moved_in_action = False
        try:
            yield
        finally:
            self._open.remove(start)
            end = time.time() * 1000
            self._windows.append((start, end))
            del self._windows[:-80]
            if action in NAVIGATING_ACTIONS and not self._moved_in_action:
                self._may_navigate.append(end)
                del self._may_navigate[:-5]

    @contextmanager
    def hold(self):
        """The app is waiting on something that isn't the page (the AI, the user, an email, a solver)."""
        self._holds += 1
        try:
            yield
        finally:
            self._holds -= 1

    def _by_agent(self, at_ms: float, grace: float = ACTION_GRACE_MS) -> bool:
        if any(start - 50 <= at_ms for start in self._open):
            return True         # an agent action that began before this event is still running
        return any(start - 50 <= at_ms <= end + grace for start, end in self._windows)

    # ── the user ────────────────────────────────────────────────────────────
    def user_active(self) -> bool:
        if self._resume_now or not self._last_user_event:
            return False
        return time.monotonic() - self._last_user_event < self.idle_seconds

    def resume_now(self) -> None:
        """Resume pressed: the user is done, don't wait out the idle time."""
        self._resume_now = True

    async def wait_for_user(self) -> bool:
        """Blocks while the user is active in the browser. True when the user stepped in since the
        agent last took their changes (take_user_changes)."""
        waited = False
        while self.user_active():
            if not waited:
                waited = True
                print("  ✋ You stepped in: the agent is waiting until you've finished "
                      f"({int(self.idle_seconds)} seconds without a click or key press, or press Resume).")
            with self.hold():
                await asyncio.sleep(0.3)
        self._resume_now = False
        return self.user_stepped_in

    async def take_user_changes(self) -> dict:
        """What the user did while the agent wasn't acting: {"fields": [(label, old, new, certain)],
        "moved_to": url or ""}. The changed fields are locked for the rest of the application."""
        if not self.user_stepped_in:
            return {"fields": [], "moved_to": ""}
        events, self._user_events = self._user_events, []
        self.user_stepped_in = False
        after = await form_snapshot.snapshot(self.browser)
        try:
            url = await self.browser.get_current_page_url()
        except Exception:
            url = ""
        fields: dict[str, tuple[str, str, str, bool]] = {}
        for event in events:
            field = event.get("field")
            if not field or field.get("secret") or not field.get("key"):
                continue
            key = field["key"]
            current = after.get(key, (field.get("label") or key, field.get("value") or ""))
            old = self._baseline.get(key, ("", ""))[1]
            if current[1].strip() != old.strip() or key not in self._baseline:
                fields[key] = (current[0] or field.get("label") or key, old, current[1], True)
        same_page = _page_key(url) == _page_key(self._baseline_url)
        if same_page:
            for key, (label, value) in after.items():
                if key in fields or key not in self._baseline:
                    continue
                old = self._baseline[key][1]
                if value.strip() != old.strip():
                    fields[key] = (label, old, value, False)
        # The page is another one than at the agent's last look, and the user was at work meanwhile.
        moved = bool(not same_page and self._baseline_url and events)
        for key, (label, _old, new, _certain) in fields.items():
            self.locked[(key, label)] = new
        self._baseline, self._baseline_url = after, url
        self.edits_kept = len(self.locked)
        if moved:
            self.user_moved = True
        if moved and self.locked:
            self.note = f"Continuing from your page, keeping your {_count(self.edits_kept, 'edit')}."
        elif moved:
            self.note = "Continuing from your page."
        elif fields:
            self.note = f"Continuing, keeping your {_count(self.edits_kept, 'edit')}."
        else:
            self.note = "Continuing where you left it."
        self._note_at = time.monotonic()
        print(f"  ▶️ {self.note}")
        return {"fields": list(fields.values()), "moved_to": url if moved else ""}

    def locked_value(self, key: str, label: str) -> str | None:
        """The value the user left in this field (None when the user never touched it)."""
        if (key, label) in self.locked:
            return self.locked[(key, label)]
        for (k, l), value in self.locked.items():
            if (key and k == key and (not label or not l or l == label)) or (not key and label and l == label):
                return value
        return None

    async def field_of(self, node) -> tuple[str, str]:
        """(key, label) of the form field an action on `node` would change; ("", "") when it isn't one."""
        try:
            raw = await asyncio.wait_for(browser_policy.call_on_node(self.browser, node, FIELD_OF_NODE_JS), 5)
            data = json.loads(raw) if raw else {}
            return str(data.get("key") or ""), str(data.get("label") or "")
        except Exception:
            return "", ""

    async def remember_page(self) -> None:
        """Called after each agent action and before each step: the form as the agent left it."""
        if self.user_active() or self.user_stepped_in:
            return          # the user's changes are being collected against the earlier state
        try:
            self._baseline = await asyncio.wait_for(form_snapshot.snapshot(self.browser), 6)
            self._baseline_url = await self.browser.get_current_page_url()
        except Exception:
            pass

    def status(self) -> dict:
        """For the run panel. The waiting note exists only while the agent waits for the user (it
        used to stay after Resume, or when reading the user's changes failed); what the agent did
        with the user's work is shown for a short while, then nothing."""
        active = self.user_active()
        recent = bool(self.note) and time.monotonic() - self._note_at < NOTE_SECONDS
        return {"user_active": active, "note": WAITING_NOTE if active else (self.note if recent else ""),
                "edits_kept": self.edits_kept}

    # ── the watcher task ────────────────────────────────────────────────────
    async def stalled(self) -> str:
        """Returns once the page has stalled (the reason); cancelled when the application ends first."""
        await self._stall.wait()
        return self.stall_reason

    async def run(self) -> None:
        last_signature_at = 0.0
        previous = time.monotonic()
        while True:
            await asyncio.sleep(POLL_SECONDS)
            now = time.monotonic()
            elapsed, previous = now - previous, now
            try:
                await self._poll_user()
            except asyncio.CancelledError:
                raise
            except Exception:
                pass
            if self._holds or self.user_active():
                continue            # waiting on the AI, the user or the app's own work: not the page's fault
            self._stalled_for += elapsed
            if now - last_signature_at >= SIGNATURE_SECONDS:
                last_signature_at = now
                state = await page_state(self.browser, timeout=6)
                self.last_state = state
                signature = _signature(state)
                if signature != self._last_signature and signature:
                    self._last_signature = signature
                    self._stalled_for = 0.0
            if self._stalled_for >= self.stall_seconds and not self._stall.is_set():
                blank = is_blank(self.last_state)
                self.stall_url = str((self.last_state or {}).get("url") or "")
                self.stall_reason = ("the page stayed blank" if blank else "the page didn't change or respond") + \
                    f" for {int(self.stall_seconds // 60) or 1} minutes"
                self._stall.set()

    def progress(self) -> None:
        """The application moved on for a reason the page fingerprint can't show (called by the app)."""
        self._stalled_for = 0.0

    async def _targets(self) -> list[str]:
        try:
            pages = await self.browser._cdp_get_all_pages()
        except Exception:
            return []
        return [p["targetId"] for p in pages if p.get("targetId")]

    def has_init_scripts(self, target_id: str | None) -> None:
        """This tab already got init_scripts (the first tab, before its first page)."""
        if target_id:
            self._scripted.add(target_id)

    async def prime(self) -> None:
        """Install the watcher and the page helpers in the tabs that exist now (the first tab, before
        the job's page opens in it), instead of at the first poll."""
        try:
            await asyncio.wait_for(self._poll_user(), 10)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _read(self, cdp, target_id: str):
        """The watcher's record for one tab (installing the watcher in the page when it isn't there)."""
        send, sid = cdp.cdp_client.send, cdp.session_id
        expression = "(" + _WATCH_JS.rstrip().rstrip(";") + ", " + _READ_JS + ")"
        for attempt in (0, 1):
            context = self._contexts.get(target_id)
            if context is None:
                tree = await asyncio.wait_for(send.Page.getFrameTree(session_id=sid), 5)
                world = await asyncio.wait_for(send.Page.createIsolatedWorld(
                    params={"frameId": tree["frameTree"]["frame"]["id"], "worldName": WORLD}, session_id=sid), 5)
                context = self._contexts[target_id] = world["executionContextId"]
            try:
                result = await asyncio.wait_for(send.Runtime.evaluate(params={
                    "expression": expression, "contextId": context, "returnByValue": True}, session_id=sid), 5)
            except asyncio.CancelledError:
                raise
            except Exception:
                self._contexts.pop(target_id, None)     # the page navigated: its world is a new one
                if attempt:
                    raise
                continue
            if result.get("exceptionDetails"):
                self._contexts.pop(target_id, None)
                if attempt:
                    return None
                continue
            return (result.get("result") or {}).get("value")
        return None

    def _listen(self, client) -> None:
        """A link the user clicks takes them to another page, and the page's own record of that
        click goes with the page (the browser doesn't let a page be read while it is being left;
        measured). The browser does announce how a navigation began, before it happens: a link
        click made while the agent isn't acting is the user's.

        Only link clicks are taken from this announcement. A form being sent or a script moving
        the page can be the site's own doing (sign-in pages post a form by themselves), so those
        count only together with input the page recorded.
        """
        def leaving(event=None, session_id=None):
            try:
                if (event or {}).get("reason") == "anchorClick":
                    self._count({"t": time.time() * 1000, "type": "navigation", "url": (event or {}).get("url", "")})
            except Exception:
                pass

        try:
            client.register.Page.frameRequestedNavigation(leaving)
        except Exception:
            pass

    def _count(self, event: dict) -> None:
        """One input event from the page: the user's, unless the agent was acting then."""
        if not isinstance(event, dict) or event.get("type") == "presence":
            return
        key = (event.get("t"), event.get("type"))
        if event.get("type") == "navigation":
            key = (round(float(event.get("t") or 0) / 500), "navigation")    # announced once per listening session
        if key in self._seen:
            return          # reported as it happened and read again on the next look
        self._seen.append(key)
        del self._seen[:-600]
        at = float(event.get("t") or 0)
        if self._by_agent(at):
            if event.get("type") == "navigation":
                self._moved_in_action = True        # this action's own navigation: no later one belongs to it
                self._may_navigate.clear()
            return
        if event.get("type") == "navigation":
            pending = [end for end in self._may_navigate if end <= at <= end + NAVIGATION_GRACE_MS]
            self._may_navigate.clear()
            if pending:
                return                              # the agent's click, announced late by a slow site
        self._last_user_event = time.monotonic()
        self._resume_now = False
        self.user_stepped_in = True
        self._user_events.append(event)
        del self._user_events[:-300]

    async def _poll_user(self) -> None:
        for target_id in await self._targets():
            try:
                cdp = await self.browser.get_or_create_cdp_session(target_id, focus=False)
                send, sid = cdp.cdp_client.send, cdp.session_id
                if not self._listening:
                    self._listening = True
                    self._listen(cdp.cdp_client)
                if target_id not in self._installed:
                    self._installed.add(target_id)
                    await asyncio.wait_for(send.Page.addScriptToEvaluateOnNewDocument(
                        params={"source": _WATCH_JS, "worldName": WORLD}, session_id=sid), 5)
                if target_id not in self._scripted:
                    self._scripted.add(target_id)
                    for script in self.init_scripts:
                        await asyncio.wait_for(send.Page.addScriptToEvaluateOnNewDocument(
                            params={"source": script, "runImmediately": True}, session_id=sid), 5)
                if target_id not in self._page_scripted:
                    self._page_scripted.add(target_id)
                    for script in self.page_scripts:
                        await asyncio.wait_for(send.Page.addScriptToEvaluateOnNewDocument(
                            params={"source": script, "runImmediately": True}, session_id=sid), 5)
                raw = await self._read(cdp, target_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            if not raw:
                continue
            data = json.loads(raw)
            for event in data.get("events") or []:
                self._count(event)
        for gone in set(self._contexts) - set(await self._targets()):
            self._contexts.pop(gone, None)


def _page_key(url: str) -> str:
    return (url or "").split("#")[0].split("?")[0].rstrip("/")


def _count(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")
