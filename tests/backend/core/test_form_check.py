"""The page checklist and the Next guard (fields read from the page, in page order)."""
import asyncio
from types import SimpleNamespace

import pytest

from core import form_check

# As read from Northwind's Workday "My Information" page.
PAGE = {"fields": [
    {"label": "How Did You Hear About Us?", "kind": "dropdown", "required": True, "value": "", "error": "", "options": []},
    {"label": "Have You Worked For Northwind Before?", "kind": "choice", "required": True, "value": "", "error": "",
     "options": ["Yes", "No"]},
    {"label": "Country", "kind": "dropdown", "required": True, "value": "United States of America", "error": "", "options": []},
    {"label": "Address Line 1", "kind": "text", "required": False, "value": "", "error": "", "options": []},
    {"label": "Email", "kind": "text", "required": True, "value": "ada@example.com", "error": "", "options": []},
], "errorBanner": ""}


def test_missing_required_keeps_page_order():
    assert [f["label"] for f in form_check.missing_required(PAGE)] == [
        "How Did You Hear About Us?", "Have You Worked For Northwind Before?"]


def test_checklist_lists_missing_questions_with_options():
    text = form_check.checklist_text(PAGE)
    assert '1. "How Did You Hear About Us?"' in text
    assert '2. "Have You Worked For Northwind Before?" (Yes / No)' in text
    assert "Address Line 1" not in text and "Email" not in text


def test_checklist_reports_errors_and_banner():
    page = {"fields": [dict(f, value="x") for f in PAGE["fields"]], "errorBanner": "Errors Found"}
    page["fields"][4]["error"] = "Enter a valid email"
    text = form_check.checklist_text(page)
    assert '"Email": Enter a valid email' in text and "Page error message: Errors Found" in text


def test_complete_page_says_so():
    page = {"fields": [dict(f, value="x") for f in PAGE["fields"]], "errorBanner": ""}
    assert form_check.checklist_text(page).startswith("FORM CHECK: every required question")


def test_no_checklist_off_forms():
    assert form_check.checklist_text({"fields": PAGE["fields"][:2]}) == ""
    assert form_check.checklist_text({}) == ""


@pytest.mark.parametrize("label,advance", [
    ("Next", True), ("Save and Continue", True), ("Continue", True), ("Submit", True), ("Submit Application", True),
    ("Review", True), ("Back", False), ("Add Another", False), ("Select One", False), ("", False),
])
def test_advance_buttons(label, advance):
    assert form_check.is_advance_button(label) is advance


def test_scan_js_walks_shadow_roots_and_reads_hidden_stars():
    js = form_check._SCAN_JS
    assert "shadowRoot" in js and "aria-required" in js and "textContent" in js
    assert not any(ord(c) < 32 and c not in "\n\t" for c in js)


# ── the Next guard ──────────────────────────────────────────────────────────

class FakeSession:
    def __init__(self, label):
        self.label = label

    async def get_element_by_index(self, index):
        return SimpleNamespace(get_all_children_text=lambda: self.label, attributes={})


def _guarded(monkeypatch, page):
    from browser_use import Tools
    from cli import apply_jobs
    clicks = []

    async def fake_click(params, browser_session):
        clicks.append(params.index)
        return "clicked"

    async def fake_scan(session):
        return page

    tools = Tools()
    tools._click_by_index = fake_click
    monkeypatch.setattr(form_check, "scan", fake_scan)
    apply_jobs.guard_next_clicks(tools, {}, 1)
    return tools._click_by_index, clicks


def test_next_is_held_back_once_while_required_questions_are_empty(monkeypatch):
    click, clicks = _guarded(monkeypatch, PAGE)
    first = asyncio.run(click(SimpleNamespace(index=7), FakeSession("Next")))
    assert clicks == [] and "Have You Worked For Northwind Before?" in first.error
    # Same questions still missing: never block twice (a misread field can't stall the run).
    assert asyncio.run(click(SimpleNamespace(index=7), FakeSession("Next"))) == "clicked"


def test_other_buttons_and_complete_pages_click_normally(monkeypatch):
    click, clicks = _guarded(monkeypatch, PAGE)
    assert asyncio.run(click(SimpleNamespace(index=3), FakeSession("Add Another"))) == "clicked"
    done = {"fields": [dict(f, value="x") for f in PAGE["fields"]]}
    click, clicks = _guarded(monkeypatch, done)
    assert asyncio.run(click(SimpleNamespace(index=7), FakeSession("Next"))) == "clicked"


def test_form_check_note_is_added_after_the_page(monkeypatch):
    from cli import apply_jobs
    added = []

    async def budget(step_info=None):
        added.append("budget")

    async def fake_scan(session):
        return PAGE

    monkeypatch.setattr(form_check, "scan", fake_scan)
    agent = SimpleNamespace(_inject_budget_warning=budget, browser_session=object(),
                            _message_manager=SimpleNamespace(_add_context_message=lambda m: added.append(m.content)))
    apply_jobs.add_form_check_each_step(agent)
    asyncio.run(agent._inject_budget_warning(None))
    assert added[0] == "budget" and added[1].startswith("FORM CHECK (read from the page")



PROFILE = {"name": "Ada Lovelace", "phone": "5551234567", "email": "ada@example.com", "address": {"zip": "10001"}}


def test_wrong_degree_level_is_flagged_without_rejecting_equivalent_spelling():
    profile={"education":{"degree":"B.S."}}
    page={'fields':[{'label':'Degree','value':'Doctorate','kind':'dropdown'}]}
    assert form_check.profile_mismatches(page,profile)==[('Degree','Doctorate','B.S.')]
    page['fields'][0]['value']='Bachelor of Science'
    assert form_check.profile_mismatches(page,profile)==[]


def _field(label, value, kind="text"):
    return {"label": label, "kind": kind, "required": True, "value": value, "error": "", "options": []}


def test_phone_typo_is_flagged():
    data = {"fields": [_field("Phone Number", "5551324567"), _field("Email", "ada@example.com"), _field("Postal Code", "10001")]}
    assert form_check.profile_mismatches(data, PROFILE) == [("Phone Number", "5551324567", "5551234567")]
    assert '"Phone Number" shows "5551324567", profile: "5551234567"' in form_check.checklist_text(data, PROFILE)


@pytest.mark.parametrize("shown", ["+1 (555) 123-4567", "555-123-4567", "15551234567"])
def test_formatted_phone_numbers_match(shown):
    assert form_check.profile_mismatches({"fields": [_field("Phone Number", shown)]}, PROFILE) == []


def test_other_fields_and_dropdowns_are_not_compared():
    data = {"fields": [_field("Phone Device Type", "Mobile", "dropdown"), _field("Country Phone Code", "+1"),
                       _field("First Name", "Ada"), _field("Last Name", "Lovelace")]}
    assert form_check.profile_mismatches(data, PROFILE) == []
    assert form_check.profile_mismatches({"fields": [_field("First Name", "Ada Lovelace")]}, PROFILE) == [
        ("First Name", "Ada Lovelace", "Ada")]


def test_the_account_email_on_an_account_page_is_not_a_mismatch():
    data = {"fields": [{"label": "Email Address", "kind": "text", "value": "ada.accounts@example.com"}]}
    profile = {"email": "ada@example.com", "account_email": "ada.accounts@example.com"}
    assert form_check.profile_mismatches(data, profile) == []
    data["fields"][0]["value"] = "ada.acounts@example.com"
    assert form_check.profile_mismatches(data, profile) == [("Email Address", "ada.acounts@example.com", "ada@example.com")]
