"""Tests for how the agent's answers are kept truthful.

Covers the questions it can't answer (reported, not guessed), the trial run
(wait for the user's approval before submitting), keeping the user's own
edits, and splitting the resume into job and non-job sections.
"""
import asyncio
from types import SimpleNamespace

import pytest
from browser_use.llm.messages import SystemMessage

from cli import apply_jobs
from core import form_snapshot

RESUME = """Ada Lovelace
SUMMARY
Student.
WORK EXPERIENCE
ACME SOFTWARE
Software Developer Intern
ADDITIONAL EXPERIENCE
CORNER MARKET | Cashier
PROJECTS
WEATHER APP
ACTIVITIES
Chess Club
"""


class FakeStore:
    def __init__(self):
        self.answers = []

    def qa_set_answer(self, question, answer, source="user", question_type="text", source_domain="", options=None):
        self.answers.append((question, answer, source, source_domain))
        self.options = options


@pytest.fixture
def store(monkeypatch):
    fake = FakeStore()
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: fake)
    return fake


def _action(tools, name):
    return tools.registry.registry.actions[name].function


JOB = {"url": "https://acme.wd5.myworkdayjobs.com/job/1", "title": "Intern", "company": "Acme"}


# ── resume sections ─────────────────────────────────────────────────────────

def test_resume_sections_split_jobs_from_projects():
    sections = apply_jobs.resume_sections(RESUME)
    assert sections["work"] == ["WORK EXPERIENCE", "ADDITIONAL EXPERIENCE"]
    assert sections["not_job"] == ["PROJECTS", "ACTIVITIES"]


def test_company_names_are_not_mistaken_for_headings():
    sections = apply_jobs.resume_sections(RESUME)
    assert "ACME SOFTWARE" not in sections["work"] + sections["not_job"]


def test_resume_instructions_name_the_sections():
    text = apply_jobs.resume_instructions(RESUME)
    assert "job sections are: WORK EXPERIENCE, ADDITIONAL EXPERIENCE" in text
    assert "Entries under PROJECTS, ACTIVITIES are NOT jobs" in text
    assert "never make up a company or job title" in text
    assert "DELETE entries that are projects" in text


def test_resume_without_known_headings_has_no_section_rule():
    assert "job sections are" not in apply_jobs.resume_instructions("Just some text\nno headings")


# ── questions nothing answers: guessed, logged, and the run goes on ────────

def test_guesses_are_logged_for_the_user_and_the_run_continues(store):
    tools = apply_jobs.build_tools(JOB, "acme.wd5.myworkdayjobs.com", False, {}, 1)
    params = apply_jobs.GuessedAnswers(guesses=[
        apply_jobs.GuessedAnswer(question="Were you referred by an Acme employee?", answer="No",
                                 options=["Yes", "No"], field_type="radio"),
        apply_jobs.GuessedAnswer(question="  ", answer="x"),
    ])
    result = asyncio.run(_action(tools, "record_guesses")(params=params))
    assert not result.is_done
    assert "Logged 1 guessed answer" in result.extracted_content
    assert store.answers == [("Were you referred by an Acme employee?", "No", "guess", "acme.wd5.myworkdayjobs.com")]
    assert store.options == ["Yes", "No"]


def test_review_action_only_in_trial_runs():
    assert "request_submit_review" not in apply_jobs.build_tools(JOB, "", False, {}, 1).registry.registry.actions
    assert "request_submit_review" in apply_jobs.build_tools(JOB, "", True, {}, 1).registry.registry.actions


# ── trial run: waiting for approval ─────────────────────────────────────────

def _review(approve, monkeypatch, before=None, after=None, agent=None):
    snapshots = iter([before or {}, after or {}])

    async def fake_snapshot(session):
        return next(snapshots)

    monkeypatch.setattr(apply_jobs.form_snapshot, "snapshot", fake_snapshot)
    agent_ref = {"agent": agent}
    tools = apply_jobs.build_tools(JOB, "acme", True, agent_ref, 1)

    async def go():
        task = asyncio.create_task(_action(tools, "request_submit_review")(
            params=apply_jobs.SubmitReview(summary="Filled 3 pages."), browser_session=object()))
        while not apply_jobs.pending_reviews():
            await asyncio.sleep(0.01)
        assert apply_jobs.pending_reviews() == [{"url": JOB["url"], "title": "Intern", "company": "Acme",
                                                 "summary": "Filled 3 pages."}]
        assert apply_jobs.decide_review(JOB["url"], approve)
        return await task

    result = asyncio.run(go())
    assert apply_jobs.pending_reviews() == []
    return result, agent_ref


def test_approved_review_tells_the_agent_to_submit(monkeypatch):
    result, agent_ref = _review(True, monkeypatch)
    assert not result.is_done
    assert "approved" in result.extracted_content and "click the final Submit" in result.extracted_content
    assert agent_ref["approved"] is True


def test_skipped_review_ends_the_run_unsent(monkeypatch):
    result, _ = _review(False, monkeypatch)
    assert result.is_done and result.success is False
    assert "Not submitted" in result.extracted_content


def test_edits_made_while_reviewing_are_kept(monkeypatch, store):
    agent = _FakeAgent()
    result, _ = _review(True, monkeypatch, agent=agent,
                        before={"q1": ("Are you willing to relocate?", "No")},
                        after={"q1": ("Are you willing to relocate?", "Yes")})
    assert '"Are you willing to relocate?" = "Yes"' in result.extracted_content
    assert '"Are you willing to relocate?" = "Yes"' in agent.system_text()
    assert store.answers == [("Are you willing to relocate?", "Yes", "user", "")]


def test_unknown_review_url_is_a_no_op():
    assert apply_jobs.decide_review("https://nothing.example", True) is False


def test_trial_run_flag():
    apply_jobs.set_trial_run(True)
    assert apply_jobs.is_trial_run()
    apply_jobs.set_trial_run(False)
    assert not apply_jobs.is_trial_run()


# ── keeping the user's edits ────────────────────────────────────────────────

class _FakeAgent:
    """Just what keep_user_edits touches: the message manager and a few attributes."""

    def __init__(self):
        self.resumed = False
        history = SimpleNamespace(system_message=SystemMessage(content="BASE PROMPT"))
        self._message_manager = SimpleNamespace(state=SimpleNamespace(history=history))
        self._message_manager._set_message_with_type = lambda msg, kind: setattr(history, "system_message", msg)
        self.browser_session = object()

    def system_text(self):
        return self._message_manager.state.history.system_message.text

    def resume(self):
        self.resumed = True


def test_user_edits_are_pinned_and_saved(store):
    agent = _FakeAgent()
    agent._langhire_domain = "acme.com"
    apply_jobs.keep_user_edits(agent, [("How did you hear about us?", "LinkedIn", "Company Website")])
    apply_jobs.keep_user_edits(agent, [("Street address line 1", "", "12 Example St")])
    text = agent.system_text()
    assert text.startswith("BASE PROMPT")
    assert text.count("FIELDS THE USER SET BY HAND") == 1          # replaced, not stacked
    assert '"How did you hear about us?" = "Company Website"' in text
    assert '"Street address line 1" = "12 Example St"' in text
    assert ("How did you hear about us?", "Company Website", "user", "acme.com") in store.answers


def test_ids_and_essays_are_not_saved_as_answers(store):
    agent = _FakeAgent()
    apply_jobs.keep_user_edits(agent, [("input-12", "", "x"), ("Why do you want this job?", "", "y" * 400)])
    assert store.answers == []
    assert '"input-12" = "x"' in agent.system_text()                 # still kept for this run


def test_resume_after_pause_keeps_what_the_user_changed(monkeypatch, store):
    agent = _FakeAgent()
    agent._langhire_before_edits = {"gpa": ("GPA", "3.2")}

    async def after(session):
        return {"gpa": ("GPA", "3.6")}

    monkeypatch.setattr(apply_jobs.form_snapshot, "snapshot", after)
    asyncio.run(apply_jobs._resume_keeping_user_edits(agent))
    assert agent.resumed
    assert '"GPA" = "3.6"' in agent.system_text()


# ── form snapshot diffing ───────────────────────────────────────────────────

def test_changed_fields():
    before = {"a": ("Name", "Ada"), "b": ("City", ""), "c": ("GPA", "3.2")}
    after = {"a": ("Name", "Ada"), "b": ("City", "Boston"), "c": ("GPA", ""), "d": ("New", "x")}
    # "New" only appeared during the wait: the page could have added it, so it isn't the user's edit.
    assert form_snapshot.changed_fields(before, after) == [("City", "", "Boston")]


def test_snapshot_without_a_page_is_empty():
    class NoPage:
        async def get_current_page(self):
            return None

    assert asyncio.run(form_snapshot.snapshot(NoPage())) == {}


# ── trial runs ask for help instead of giving up ────────────────────────────

def _wait_help(monkeypatch, call, reply, agent=None, before=None, after=None, notes=None):
    """Run a trial-run action that waits for help, answer it, return its result."""
    snapshots = iter([before or {}, after or {}])

    async def fake_snapshot(session):
        return next(snapshots)

    monkeypatch.setattr(apply_jobs.form_snapshot, "snapshot", fake_snapshot)
    monkeypatch.setattr(apply_jobs, "update_job", lambda url, **kw: (notes if notes is not None else []).append(kw))
    tools = apply_jobs.build_tools(JOB, "acme", True, {"agent": agent}, 1)

    async def go():
        task = asyncio.create_task(call(tools))
        while not apply_jobs.pending_help():
            if task.done():  # the action failed before asking: surface its error
                return task.result()
            await asyncio.sleep(0.01)
        assert apply_jobs.pending_help()[0]["url"] == JOB["url"]
        assert apply_jobs.answer_help(JOB["url"], *reply)
        return await task

    result = asyncio.run(go())
    assert apply_jobs.pending_help() == []
    return result


def _ask(problem):
    return lambda tools: _action(tools, "ask_user_for_help")(
        params=apply_jobs.HelpRequest(problem=problem), browser_session=object())


def test_help_with_a_message_continues_and_pins_the_message(monkeypatch, store):
    agent, notes = _FakeAgent(), []
    result = _wait_help(monkeypatch, _ask("Save and Continue is disabled."),
                        ("Tick the terms box first", False), agent=agent, notes=notes)
    assert not result.is_done
    assert 'Their message: "Tick the terms box first"' in result.extracted_content
    assert "MESSAGES FROM THE USER DURING THIS RUN" in agent.system_text()
    assert "- Tick the terms box first" in agent.system_text()
    assert notes == [{"user_note": "Tick the terms box first"}]          # kept for retries


def test_help_give_up_ends_the_run(monkeypatch):
    result = _wait_help(monkeypatch, _ask("Stuck on the dropdown."), ("", True))
    assert result.is_done and result.success is False
    assert "Stuck on the dropdown. (you chose Give up)" in result.extracted_content


def test_trial_run_done_with_failure_asks_for_help_instead(monkeypatch, tmp_path):
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.tools.views import DoneAction

    def finish_failed(tools):
        return _action(tools, "done")(params=DoneAction(text="Could not advance.", success=False),
                                      file_system=FileSystem(tmp_path), browser_session=object())

    result = _wait_help(monkeypatch, finish_failed, ("I fixed it, go on", False))
    assert not result.is_done and "I fixed it, go on" in result.extracted_content


def test_trial_run_done_with_success_still_finishes(tmp_path):
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.tools.views import DoneAction
    tools = apply_jobs.build_tools(JOB, "acme", True, {}, 1)
    result = asyncio.run(_action(tools, "done")(
        params=DoneAction(text="Submitted.", success=True), file_system=FileSystem(tmp_path), browser_session=object()))
    assert result.is_done and result.success is True


def test_full_auto_keeps_the_normal_done_and_has_no_help_action():
    actions = apply_jobs.build_tools(JOB, "acme", False, {}, 1).registry.registry.actions
    assert "ask_user_for_help" not in actions
    assert "success=false does not end" not in actions["done"].description


def test_user_note_instructions():
    assert apply_jobs.user_note_instructions("") == ""
    assert "NOTE FROM THE USER ABOUT THIS JOB" in apply_jobs.user_note_instructions("Tick the terms box")


# ── giving up too early (Full auto / Training) ──────────────────────────────

def _done(tools, tmp_path, text, success=False):
    from browser_use.filesystem.file_system import FileSystem
    from browser_use.tools.views import DoneAction
    return asyncio.run(_action(tools, "done")(params=DoneAction(text=text, success=success), file_system=FileSystem(tmp_path)))


def _agent_at_step(n):
    return SimpleNamespace(state=SimpleNamespace(n_steps=n))


def test_an_early_give_up_is_sent_back_once(tmp_path):
    agent_ref = {"agent": _agent_at_step(10), "max_steps": 70}
    tools = apply_jobs.build_tools(JOB, "acme", False, agent_ref, 1)
    first = _done(tools, tmp_path, "The Create Account page did not advance.")
    assert not first.is_done
    assert "used only 10 of 70 steps" in first.extracted_content and "Sign In" in first.extracted_content
    second = _done(tools, tmp_path, "Still stuck on Create Account.")
    assert second.is_done and second.success is False


@pytest.mark.parametrize("text", [
    "POSTING CLOSED: the page says this job is no longer available.",
    "Gmail sign-in required to read the verification code: sign in to Gmail in Settings.",
    "The site shows a CAPTCHA I can't get past.",
])
def test_hard_stops_end_the_run_even_early(tmp_path, text):
    tools = apply_jobs.build_tools(JOB, "acme", False, {"agent": _agent_at_step(3), "max_steps": 70}, 1)
    result = _done(tools, tmp_path, text)
    assert result.is_done and result.success is False


def test_a_late_give_up_ends_the_run(tmp_path):
    tools = apply_jobs.build_tools(JOB, "acme", False, {"agent": _agent_at_step(40), "max_steps": 70}, 1)
    assert _done(tools, tmp_path, "Stuck.").is_done


def test_success_is_never_sent_back(tmp_path):
    tools = apply_jobs.build_tools(JOB, "acme", False, {"agent": _agent_at_step(2), "max_steps": 70}, 1)
    result = _done(tools, tmp_path, "Submitted.", success=True)
    assert result.is_done and result.success is True


def test_resume_rules_cover_every_job_and_education():
    text = apply_jobs.resume_instructions("WORK EXPERIENCE\nACME | Intern\nEDUCATION\nState University")
    assert "Add EVERY entry under the job sections" in text
    assert "never delete the education section" in text and "record_guesses" in text


def test_markers_are_stripped_from_what_the_user_sees():
    text = ('I could not continue. @@LEARNING: {"domain":"x","category":"navigation","insight":"Apply opens a modal."}'
            '\n@@QUESTION: {"question": "Q", "answer": "A", "type": "text"}')
    assert apply_jobs.strip_markers(text) == "I could not continue."


def test_page_wait_returns_once_the_page_has_content():
    lengths = iter([0, 12, 500])

    class Page:
        async def evaluate(self, js):
            return next(lengths)

    class Session:
        async def get_current_page(self):
            return Page()

    asyncio.run(apply_jobs.wait_for_page_content(SimpleNamespace(browser_session=Session())))
    assert next(lengths, "used up") == "used up"


def test_page_wait_never_breaks_a_step():
    class Session:
        async def get_current_page(self):
            raise RuntimeError("no page")

    asyncio.run(apply_jobs.wait_for_page_content(SimpleNamespace(browser_session=Session())))
