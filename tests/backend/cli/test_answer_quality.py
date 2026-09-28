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
    assert form_snapshot.changed_fields(before, after) == [("City", "", "Boston"), ("New", "", "x")]


def test_snapshot_without_a_page_is_empty():
    class NoPage:
        async def get_current_page(self):
            return None

    assert asyncio.run(form_snapshot.snapshot(NoPage())) == {}
