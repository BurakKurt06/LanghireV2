"""Make browser-use really click controls that sit under their own invisible overlay.

Before clicking, browser-use asks the page which element is on top of the
click point. If it isn't the target (or related to it), the target counts as
covered and browser-use falls back to a JavaScript ``element.click()``.

Workday covers its own controls with invisible siblings:
  - checkboxes and radio buttons: an opacity-0 ``<input>`` lies over the
    visible box ``<div>``, and the agent only sees the div;
  - buttons (Create Account, Sign In, ...): a transparent
    ``div[data-automation-id=click_filter]`` lies over the ``<button>``.
A JavaScript click on the div or the button does nothing there, so the Accept
box never got ticked, Create Account never submitted and Yes/No questions
never got answered. A real mouse click on the same spot works.

The patch: when what is on top sits inside the target's own, tight parent
(a sibling overlay), treat the target as clickable so browser-use performs a
real mouse click. A big parent (the page, a dialog) never counts, so a real
covering element such as a cookie banner still gets the old behaviour.
"""
from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

SIBLING_OVERLAY_JS = """
function(x, y) {
    const top = document.elementFromPoint(x, y);
    const parent = this.parentElement;
    if (!top || !parent || !parent.contains(top)) return false;
    const t = this.getBoundingClientRect(), p = parent.getBoundingClientRect();
    // Only a tight wrapper around the target, never a large container.
    return p.width * p.height <= 4 * t.width * t.height + 2500;
}
"""

_original_check = DefaultActionWatchdog._check_element_occlusion


async def is_sibling_overlay(cdp_session, backend_node_id: int, x: float, y: float) -> bool:
    """True when the element on top at (x, y) is an overlay inside the target's tight parent."""
    node = await cdp_session.cdp_client.send.DOM.resolveNode(
        params={"backendNodeId": backend_node_id}, session_id=cdp_session.session_id)
    result = await cdp_session.cdp_client.send.Runtime.callFunctionOn(
        params={
            "objectId": node["object"]["objectId"],
            "functionDeclaration": SIBLING_OVERLAY_JS,
            "arguments": [{"value": x}, {"value": y}],
            "returnByValue": True,
        },
        session_id=cdp_session.session_id,
    )
    return result.get("result", {}).get("value") is True


async def _check_element_occlusion(self, backend_node_id, x, y, cdp_session):
    if not await _original_check(self, backend_node_id, x, y, cdp_session):
        return False
    try:
        if await is_sibling_overlay(cdp_session, backend_node_id, x, y):
            self.logger.debug("Covered only by its own overlay: using a real mouse click")
            return False
    except Exception:
        pass
    return True


def apply() -> None:
    """Install the patch (idempotent)."""
    DefaultActionWatchdog._check_element_occlusion = _check_element_occlusion
