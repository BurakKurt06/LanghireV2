"""What the user's runs of 2026-11-20 showed, on local made-up pages in a real browser (no AI, no real site).

* A hiring site's Submit button lived in a web component (<button><slot></slot></button> in a shadow
  root, its "Submit" text in the page): the app read a button with no name, so the application it
  sent was never marked as sent, and a Trial run would have pressed it.
* A form's section "Submit" next to "Finish Later" (a longer application's section).
* Answered react-select lists (Greenhouse) and Workday's newer "pill-..." choices were reported
  unanswered, and Submit / Save and Continue were held back again and again.
"""
import os

import pytest

from core import browser_policy, form_check, submit_guard
from tests.backend.core.test_request_gate_live import browser_and_gate, bridge_to, wire_site  # noqa: F401
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

WEB_COMPONENT_FORM = """<!doctype html><title>Application</title>
<form id="application" onsubmit="return false"><label for="first">First name</label><input id="first" value="Ada">
<label for="last">Last name</label><input id="last" value="Test">
<label for="mail">Email</label><input id="mail" value="ada@example.com">
<spl-button id="send"><span>Submit</span></spl-button></form>
<script>
customElements.define('spl-button', class extends HTMLElement {
  constructor() { super(); this.attachShadow({mode: 'open'}).innerHTML = '<button type="button"><slot></slot></button>'; }
});
</script>"""


async def _describe(browser, find: str) -> submit_guard.Control:
    """What the submit guard reads for the element `find` (a script expression) selects."""
    raw = await browser_policy.evaluate(browser, "() => (" + submit_guard.NODE_JS + ").call(" + find + ")")
    return submit_guard.control_from_json(raw)


async def _open(wire_site, html):
    local, seen, site = wire_site
    site["html"] = html
    browser, gate = await browser_and_gate("auto", "https://careers.example/form", transport_factory=bridge_to(local))
    await browser.navigate_to("https://careers.example/form")
    return browser, gate


async def test_a_submit_button_inside_a_web_component_is_read_as_submit(wire_site):
    browser, gate = await _open(wire_site, WEB_COMPONENT_FORM)
    try:
        control = await _describe(browser, "document.querySelector('#send').shadowRoot.querySelector('button')")
        assert control.text == "Submit" and control.in_form is True
        assert SubmitGuardDecision.auto(control).submit                      # recorded as a submit before it is pressed
        trial = SubmitGuardDecision.trial(control)
        assert trial.submit and not trial.allowed                            # and Trial never presses it
    finally:
        await gate.close()


async def test_control_a_web_components_other_button_keeps_its_own_name(wire_site):
    page = WEB_COMPONENT_FORM.replace("<span>Submit</span>", "<span>Add another school</span>")
    browser, gate = await _open(wire_site, page)
    try:
        control = await _describe(browser, "document.querySelector('#send').shadowRoot.querySelector('button')")
        assert control.text == "Add another school" and not SubmitGuardDecision.auto(control).submit
    finally:
        await gate.close()


SECTION_FORM = """<!doctype html><title>Questions</title><h1>Job-specific questions</h1>
<form id="quesp_form"><label for="notice">Notice period</label><input id="notice" value="2 weeks">
<label for="relocate">Relocate?</label><input id="relocate" value="Yes"><label for="start">Start</label><input id="start" value="June">
<input type="submit" id="quesp_form_submit_i" value="Submit">
<input type="button" id="quesp_form_save_i" value="Finish Later"></form>"""


@pytest.mark.parametrize("later,tentative", [(True, True), (False, False)])
async def test_a_sections_submit_next_to_finish_later_is_only_possibly_final(wire_site, later, tentative):
    page = SECTION_FORM if later else SECTION_FORM.replace('value="Finish Later"', 'value="Clear the answers"')
    browser, gate = await _open(wire_site, page)
    try:
        control = await _describe(browser, "document.querySelector('#quesp_form_submit_i')")
        assert control.save_later is later
        decision = SubmitGuardDecision.auto(control)
        assert decision.submit and decision.tentative is tentative
    finally:
        await gate.close()


LISTS = """<!doctype html><title>Application</title><form>
<label id="auth-label" for="auth">Are you authorized to work in the US? *</label>
<div class="select__control"><div class="select__value-container">
  <div class="select__single-value">Yes</div>
  <input id="auth" role="combobox" aria-required="true" aria-labelledby="auth-label" value=""></div></div>
<label id="sponsor-label" for="sponsor">Will you need sponsorship? *</label>
<div class="select__control"><div class="select__value-container">
  <div class="select__placeholder">Select...</div>
  <input id="sponsor" role="combobox" aria-required="true" aria-labelledby="sponsor-label" value=""></div></div>
<div data-automation-id="formField-source"><label for="source">How Did You Hear About Us? *</label>
  <div><div id="pill-83238f9a14c9" role="option" aria-selected="false"
            aria-label="Corporate Website, press delete to clear value."></div></div>
  <input id="source" role="combobox" data-automation-id="searchBox" aria-required="true" value=""></div>
<div data-automation-id="formField-phoneCode"><label for="code">Country Phone Code *</label>
  <input id="code" role="combobox" data-automation-id="searchBox" aria-required="true" value=""></div>
</form>"""


async def test_answered_lists_are_read_as_answered(wire_site):
    browser, gate = await _open(wire_site, LISTS)
    try:
        data = await form_check.scan(browser)
        values = {f["label"]: f["value"] for f in data["fields"]}
        assert values["Are you authorized to work in the US?"] == "Yes"                      # react-select
        assert values["How Did You Hear About Us?"] == "Corporate Website"                   # Workday's pill
        # The ones with nothing chosen are still the ones missing (no placeholder counts as an answer).
        assert {f["label"] for f in form_check.missing_required(data)} == {"Will you need sponsorship?",
                                                                           "Country Phone Code"}
    finally:
        await gate.close()


ICIMS = """<!doctype html><title>Profile</title><form id="cp_form">
<label for="rcf3048">How did you hear about us?</label>
<a id="rcf3048_icimsDropdown" href="#" class="iCIMS_DropdownContainer"><span>Make a Selection</span></a>
<select id="rcf3048" class="iCIMS_Forms_IdDropDown" style="display:none"
        onchange="document.body.dataset.changed = this.value">
  <option value="">Make a Selection</option><option value="17">Company Website</option><option value="18">Job board</option>
</select><button id="other" type="button">Add a school</button></form>"""


async def _choose(browser, selector, option):
    import json
    from cli import apply_jobs
    raw = await browser_policy.evaluate(browser, "() => (" + apply_jobs.ICIMS_CHOICE_JS + ").call(document.querySelector("
                                        + json.dumps(selector) + "), " + json.dumps(option) + ")")
    return json.loads(raw)


async def test_a_make_a_selection_box_that_wont_open_is_chosen_in_the_list_behind_it(wire_site):
    """Seen 2026-11-20: iCIMS boxes never opened for the agent; two jobs ended 'Needs you'."""
    browser, gate = await _open(wire_site, ICIMS)
    try:
        assert await _choose(browser, "#rcf3048_icimsDropdown span", "company website") == {"chosen": "Company Website"}
        state = await browser_policy.evaluate(browser, "() => [document.querySelector('#rcf3048').value, "
                                              "document.body.dataset.changed, document.querySelector('#rcf3048_icimsDropdown').innerText]")
        assert state == ["17", "17", "Company Website"]               # the form's value, its own script told, the box shows it
        wrong = await _choose(browser, "#rcf3048_icimsDropdown", "LinkedIn")
        assert wrong["error"] == "no such option" and wrong["options"] == ["Make a Selection", "Company Website", "Job board"]
        assert "isn't an iCIMS" in (await _choose(browser, "#other", "Company Website"))["error"]
    finally:
        await gate.close()


class SubmitGuardDecision:
    @staticmethod
    def auto(control):
        return submit_guard.SubmitGuard(mode="auto").decide("click", control)

    @staticmethod
    def trial(control):
        return submit_guard.SubmitGuard(mode="trial", live_steps=True).decide("click", control)
