"""The submit policy is enforced in code: forbidden submits never reach the browser.

Each test sends a real action through the agent's own tool pipeline
(build_tools -> registry.execute_action) and checks whether the underlying
browser action ran, not what the prompt says.
"""
import asyncio
import json

import pytest

from cli import apply_jobs
from core import shared_config, submit_guard

JOB = {"url": "https://jobs.example/apply/1", "title": "Intern", "company": "Acme"}
REVIEW_PAGE = "https://jobs.example/apply/1/review"


class Node:
    def __init__(self, text, tag="button", **attrs):
        self.text, self.tag_name, self.attributes = text, tag, attrs

    def get_all_children_text(self):
        return self.text


class Page:
    def __init__(self, focused=None, at_point=None):
        self.focused, self.at_point = focused, at_point

    async def evaluate(self, js, *args):
        if js == submit_guard.FOCUSED_JS:
            return json.dumps(self.focused)
        if js == submit_guard.AT_POINT_JS:
            return json.dumps(self.at_point)
        return ""


class Session:
    cdp_client = None  # browser-use passes it to actions; the spies don't use it

    def __init__(self, nodes=None, focused=None, at_point=None, url=REVIEW_PAGE):
        self.nodes, self.page, self.url = nodes or {}, Page(focused, at_point), url

    async def get_element_by_index(self, index):
        if self.nodes.get(index) == "boom":
            raise RuntimeError("stale index")
        return self.nodes.get(index)

    async def get_current_page(self):
        return self.page

    async def get_current_page_url(self):
        return self.url


@pytest.fixture
def fields(monkeypatch):
    """The form's visible answers, as form_check.scan reads them (editable per test)."""
    current = [{"label": "Email", "kind": "text", "value": "ada@example.com"}]

    async def scan(session):
        return {"fields": [dict(f) for f in current]}

    monkeypatch.setattr(apply_jobs.form_check, "scan", scan)
    return current


def make_tools(mode, monkeypatch, data_dir=None):
    """The agent's tools in `mode`, with spies in place of the real browser actions.

    The job is in a throwaway job list, so a submit attempt can be recorded (it must be,
    before any submit runs).
    """
    import tempfile
    from pathlib import Path
    if data_dir is None:
        folder = Path(tempfile.mkdtemp(prefix="guard-jobs-"))
        monkeypatch.setattr(shared_config, "JOBS_FILE", folder / "jobs.json")
        monkeypatch.setattr(shared_config, "JOBS_LOCK", folder / "jobs.json.lock")
        shared_config.write_jobs({JOB["url"]: {"title": JOB["title"], "status": "in_progress"}})
    ran = []
    apply_jobs.set_run_mode(mode)
    try:
        agent_ref = {}
        tools = apply_jobs.build_tools(JOB, "jobs.example", mode == "trial", agent_ref, 1)
    finally:
        apply_jobs.set_run_mode("auto")

    async def clicked(params, browser_session):
        ran.append(("click", params.index if params.index is not None else (params.coordinate_x, params.coordinate_y)))
        return apply_jobs.ActionResult(extracted_content="clicked")

    tools._click_by_index = clicked
    tools._click_by_coordinate = clicked
    for name in ("send_keys", "input", "evaluate"):
        action = tools.registry.registry.actions[name]

        async def spy(*a, _name=name, **k):
            ran.append((_name, k.get("params") or (a[0] if a else None)))
            return apply_jobs.ActionResult(extracted_content="ran")

        action.function = spy
    return tools, agent_ref, ran


def run(tools, action, params, session):
    return asyncio.run(tools.registry.execute_action(action, params, browser_session=session))


# 8: a script-driven Next (type=button): can't submit a form. 11: a Next that is its form's submit button.
SUBMIT = {7: Node("Submit application"), 8: Node("Next", type="button"), 9: Node("", type="submit"), 10: "boom",
          11: Node("Next")}


# ── training: never submits ─────────────────────────────────────────────────

def test_training_blocks_a_submit_click_before_it_reaches_the_browser(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    result = run(tools, "click", {"index": 7}, Session(SUBMIT))
    assert "Trial run" in result.error and ran == []
    assert run(tools, "click", {"index": 8}, Session(SUBMIT)).error is None  # a script Next still works
    assert ran == [("click", 8)]


@pytest.mark.parametrize("node", [Node("Apply Now", type="submit"), Node("Next"),
                                  Node("Submit application verification", type="submit")])
def test_training_blocks_any_form_submit_button(monkeypatch, fields, node):
    """Codex's cases: a form's own submit button sends it, whatever the label says."""
    tools, _, ran = make_tools("trial", monkeypatch)
    result = run(tools, "click", {"index": 7}, Session({7: node}))
    assert result.error and "Trial run" in result.error and ran == []


def test_trial_form_submit_needs_approval_even_when_it_says_apply_now(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    result = run(tools, "click", {"index": 7}, Session({7: Node("Apply Now", type="submit")}))
    assert "submission is disabled" in result.error and ran == []
    agent_ref["submit_guard"].approve(REVIEW_PAGE, submit_guard.fingerprint(fields))
    assert run(tools, "click", {"index": 7}, Session({7: Node("Apply Now", type="submit")})).error
    assert ran == []


@pytest.mark.parametrize("keys", ["Space", " ", "Enter"])
def test_space_or_enter_on_a_submit_button_is_a_submit(monkeypatch, fields, keys):
    tools, _, ran = make_tools("trial", monkeypatch)
    focused = {"text": "Submit application", "tag": "button", "type": "submit", "in_form": True}
    assert "Trial run" in run(tools, "send_keys", {"keys": keys}, Session(focused=focused)).error and ran == []
    focused_next = {"text": "Next", "tag": "button", "type": "submit", "in_form": True}
    assert "Trial run" in run(tools, "send_keys", {"keys": keys}, Session(focused=focused_next)).error


def test_space_in_a_text_field_is_just_a_space(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    field = {"text": "", "tag": "input", "type": "text", "in_form": True}
    assert run(tools, "send_keys", {"keys": "Space"}, Session(focused=field)).error is None


def test_tab_then_enter_must_be_separate_steps(monkeypatch, fields):
    """The focused element is read before the keys run; after a Tab, Enter lands somewhere else."""
    tools, _, ran = make_tools("trial", monkeypatch)
    field = {"text": "", "tag": "input", "type": "text", "in_form": False}
    assert "separate steps" in run(tools, "send_keys", {"keys": "Tab Enter"}, Session(focused=field)).error
    assert ran == []


def test_training_blocks_an_icon_only_submit_button(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    assert "Trial run" in run(tools, "click", {"index": 9}, Session(SUBMIT)).error and ran == []


def test_training_blocks_a_coordinate_click_on_submit(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    tools.registry.registry.actions  # coordinate clicks go through the same guard
    tools.set_coordinate_clicking(True)
    tools._click_by_coordinate = lambda *a: ran.append("coordinate click")
    session = Session(at_point={"text": "Submit", "tag": "button", "type": "submit"})
    result = run(tools, "click", {"coordinate_x": 400, "coordinate_y": 600}, session)
    assert "Trial run" in result.error and ran == []


def test_training_blocks_enter_that_would_submit_the_form(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    in_form = {"text": "", "tag": "input", "type": "text", "in_form": True}
    assert "Trial run" in run(tools, "send_keys", {"keys": "Enter"}, Session(focused=in_form)).error
    assert ran == []
    # A combobox role cannot exempt a native form field: Enter may still submit.
    combo = {"text": "", "tag": "input", "type": "text", "in_form": True, "combo": True}
    assert "Trial run" in run(tools, "send_keys", {"keys": "Enter"}, Session(focused=combo)).error
    assert ran == []
    combo["in_form"] = False  # a choice outside any form still works
    assert run(tools, "send_keys", {"keys": "Enter"}, Session(focused=combo)).error is None
    assert ran[-1][0] == "send_keys"


def test_training_blocks_page_scripts_and_line_breaks(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    script = "document.querySelector('form').submit()"
    assert "page scripts" in run(tools, "evaluate", {"code": script}, Session()).error
    nodes = {3: Node("", tag="input", type="text")}
    assert "line breaks" in run(tools, "input", {"index": 3, "text": "Ada\n"}, Session(nodes)).error
    assert ran == []


def test_training_fails_closed_when_the_element_cant_be_read(monkeypatch, fields):
    tools, _, ran = make_tools("trial", monkeypatch)
    assert "Couldn't check this action" in run(tools, "click", {"index": 10}, Session(SUBMIT)).error
    assert ran == []


# ── trial: only with the user's approval, once, for this exact state ─────────

def test_trial_submit_needs_approval(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    assert "approval" in run(tools, "click", {"index": 7}, Session(SUBMIT)).error and ran == []


def test_old_trial_approval_never_allows_a_submit(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    agent_ref['submit_guard'].approve(REVIEW_PAGE,submit_guard.fingerprint(fields))
    assert run(tools,"click",{"index":7},Session(SUBMIT)).error
    assert run(tools,"click",{"index":7},Session(SUBMIT)).error and ran == []


def test_changed_answers_or_page_cannot_make_trial_send(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    agent_ref['submit_guard'].approve(REVIEW_PAGE,submit_guard.fingerprint(fields))
    fields[0]['value']='other@example.test'
    assert run(tools,"click",{"index":7},Session(SUBMIT)).error
    assert run(tools,"click",{"index":7},Session(SUBMIT,url='https://jobs.example/other')).error
    assert ran == []


def test_trial_blocks_scripts_even_after_approval(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    agent_ref["submit_guard"].approve(REVIEW_PAGE, submit_guard.fingerprint(fields))
    assert "page scripts" in run(tools, "evaluate", {"code": "form.submit()"}, Session()).error
    assert ran == []


def test_finishing_trial_never_approves_a_submit(monkeypatch, fields):
    tools, agent_ref, ran = make_tools("trial", monkeypatch)
    review = tools.registry.registry.actions["request_submit_review"].function
    async def go():
        result = await asyncio.wait_for(review(params=apply_jobs.SubmitReview(summary="Done."), browser_session=Session()),1)
        assert result.is_done and result.success and apply_jobs.pending_reviews()==[]
        first = await tools.registry.execute_action("click", {"index":7}, browser_session=Session(SUBMIT))
        return first
    assert asyncio.run(go()).error and ran == []
    assert agent_ref['submit_guard']._approval is None


# ── auto: allowed, and recorded before the click ─────────────────────────────

def test_auto_submit_is_recorded_on_the_job_before_the_click(monkeypatch, fields, data_dir):
    monkeypatch.setattr(shared_config, "JOBS_FILE", data_dir / "jobs.json")
    monkeypatch.setattr(shared_config, "JOBS_LOCK", data_dir / "jobs.json.lock")
    shared_config.write_jobs({JOB["url"]: {"title": "Intern", "status": "in_progress"}})
    tools, agent_ref, ran = make_tools("auto", monkeypatch)
    seen_at_click = {}

    async def clicked(params, browser_session):
        seen_at_click.update(shared_config.read_jobs()[JOB["url"]])
        ran.append(("click", params.index))
        return apply_jobs.ActionResult(extracted_content="clicked")

    tools._click_by_index = clicked
    assert run(tools, "click", {"index": 7}, Session(SUBMIT)).error is None
    assert seen_at_click.get("submit_attempted_at")  # persisted first: a crash now is "outcome unknown"
    assert agent_ref["submit_guard"].attempts == 1
    assert run(tools, "click", {"index": 8}, Session(SUBMIT)).error is None  # a script Next: not a submit
    assert agent_ref["submit_guard"].attempts == 1
    assert run(tools, "click", {"index": 11}, Session(SUBMIT)).error is None  # a form-submit Next: recorded
    assert agent_ref["submit_guard"].attempts == 2


@pytest.mark.parametrize("label, submit", [
    ("Submit", True), ("Submit Application", True), ("Send application", True), ("Finish", True),
    ("Confirm and submit", True), ("Submit application verification", True), ("Next", False),
    ("Save and Continue", False), ("Review", False), ("Apply Now", False), ("Send code", False),
    ("Submit verification code", False), ("Search", False),
])
def test_labels_that_say_submit(label, submit):
    """The label rule alone (it catches script-driven buttons); structure decides the rest."""
    assert submit_guard.is_submit_label(label) is submit
    assert submit_guard.Control(text=label, tag="button", type="button").is_final_submit is submit


@pytest.mark.parametrize("control, submit", [
    # A form's submit button is a possible submit whatever its label says...
    (dict(text="Apply Now", tag="button", type="submit", in_form=True), True),
    (dict(text="Next", tag="button", in_form=True), True),                          # no type: defaults to submit
    (dict(text="Continue", tag="input", type="submit", in_form=True), True),
    (dict(text="", tag="input", type="image", in_form=True), True),
    (dict(text="Next", tag="button"), True),                                        # form unknown: assumed
    # ...except steps that can't send an application.
    (dict(text="Sign in", tag="button", type="submit", in_form=True), True),
    (dict(text="Create account", tag="button", in_form=True), True),
    (dict(text="Verify", tag="button", type="submit", in_form=True), True),
    (dict(text="Send code", tag="button", type="submit", in_form=True), True),
    (dict(text="Sign in and apply", tag="button", type="submit", in_form=True), True),
    # Not a form submit: outside any form, or a script button.
    (dict(text="Next", tag="button", in_form=False), False),
    (dict(text="Apply Now", tag="button", type="button", in_form=True), False),
    (dict(text="Apply Now", tag="a", in_form=False), False),
])
def test_form_submit_buttons_count_whatever_they_say(control, submit):
    assert submit_guard.Control(**control).is_final_submit is submit


@pytest.mark.parametrize("mode", ["auto", "trial", "training"])
@pytest.mark.parametrize("address", ["javascript:document.forms[0].submit()", "JavaScript:void(0)",
                                     "file:///C:/Users/ada/Documents/cv.pdf", "data:text/html,<form>"])
def test_only_web_pages_can_be_opened(monkeypatch, fields, mode, address):
    """javascript: would run page script round the evaluate rule; file: reads this computer's files."""
    tools, _, ran = make_tools(mode, monkeypatch)
    opened = []

    async def navigate(*a, **k):
        opened.append(k.get("params") or a)
        return apply_jobs.ActionResult(extracted_content="opened")

    tools.registry.registry.actions["navigate"].function = navigate
    result = run(tools, "navigate", {"url": address}, Session(SUBMIT))
    assert result.error and "Only web pages" in result.error and opened == []


def test_web_pages_still_open(monkeypatch, fields):
    tools, _, _ = make_tools("trial", monkeypatch)
    opened = []

    async def navigate(*a, **k):
        opened.append(1)
        return apply_jobs.ActionResult(extracted_content="opened")

    tools.registry.registry.actions["navigate"].function = navigate
    assert run(tools, "navigate", {"url": "https://boards.greenhouse.io/acme/jobs/1"}, Session(SUBMIT)).error is None
    assert run(tools, "navigate", {"url": "about:blank"}, Session(SUBMIT)).error is None
    assert opened == [1, 1]



def test_a_submit_that_cant_be_recorded_never_runs(monkeypatch, fields):
    """Codex's case: the attempt mark couldn't be saved, so the click must not happen (auto mode too)."""
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(apply_jobs, "update_job", broken)
    tools, agent_ref, ran = make_tools("auto", monkeypatch)
    result = run(tools, "click", {"index": 7}, Session({7: Node("Submit application", type="submit")}))
    assert "couldn't record" in result.error and ran == [] and agent_ref["submit_guard"].attempts == 0


def test_a_submit_for_a_job_missing_from_the_list_never_runs(monkeypatch, fields):
    tools, _, ran = make_tools("auto", monkeypatch)
    shared_config.write_jobs({})  # deleted meanwhile: nowhere to record the attempt
    result = run(tools, "click", {"index": 7}, Session({7: Node("Submit application", type="submit")}))
    assert "couldn't record" in result.error and ran == []
