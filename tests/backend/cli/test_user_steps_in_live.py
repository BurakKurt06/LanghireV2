"""The user steps in during a run: the agent keeps its hands off. Real browser, local pages, scripted AI.

The user's rule (2026-11): any click, typing or page change in the agent's browser that the
agent didn't make itself is the user's, at any moment of a run (not only during a pause).
The agent stops acting at once, waits until the user has been idle (or pressed Resume),
re-reads the page, never touches a field the user changed, and goes on from the page the
user is on.
"""
import asyncio
import os
import time

import pytest

from core import run_watch
from tests.backend import e2e_harness as lab
from tests.backend.test_main_api import _isolate_state  # noqa: F401

pytestmark = [pytest.mark.real_browser, pytest.mark.skipif(
    os.environ.get("LANGHIRE_REAL_BROWSER") != "1", reason="opt-in local browser")]

FORM = f"""<!doctype html><title>Test Analyst at Example Works</title><h1>Application</h1><p>{lab.FILLER}</p>
<form>
  <label for="first">First name</label><input id="first" name="first">
  <label for="city">Preferred work city</label><input id="city" name="city">
  <label for="terms"><input type="checkbox" id="terms" name="terms"> I agree to the terms</label>
  <a id="next" href="/page2">Continue to page 2</a>
</form>"""
PAGE2 = f"""<!doctype html><title>Page 2</title><h1>Page 2 of the application</h1><p>{lab.FILLER}</p>
<form><label for="school">School name</label><input id="school" name="school"></form>"""


def _page2(method, data):
    time.sleep(0.25)        # a site takes a moment to answer; a page on this computer otherwise takes none
    return PAGE2


@pytest.fixture
def site():
    site = lab.Site({"/job": FORM, "/page2": _page2})
    yield site
    site.close()


async def _until(predicate, seconds=40):
    deadline = asyncio.get_running_loop().time() + seconds
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.1)
    return False


async def test_a_user_edit_made_while_the_agent_is_running_is_never_overwritten(site, data_dir, monkeypatch, tmp_path):
    from memory.store import MemoryStore
    monkeypatch.setattr(run_watch, "USER_IDLE_SECONDS", 2.0)
    thinking, user_done = asyncio.Event(), asyncio.Event()
    state = {"agent": None, "values": {}, "errors": []}

    async def decide(text, step):
        page = lab.browser_state(text)
        if step == 1:
            thinking.set()
            await user_done.wait()          # the user works in the browser while the AI "thinks"
            return {"input": {"index": lab.index_of(text, "id=city") or lab.index_of(text, "name=city"), "text": "AgentTown"}}
        if "Not done" in text.split("Current url:")[0][-3000:] or step <= 4:
            state["errors"].append(text[-6000:] if step == 2 else "")
        if step == 2:   # the plan made before the user stepped in was dropped: the agent tries the field again
            return {"input": {"index": lab.index_of(text, "id=city") or lab.index_of(text, "name=city"), "text": "AgentTown"}}
        if step == 3:   # ... and other fields are still the agent's to fill
            return {"input": {"index": lab.index_of(text, "id=first") or lab.index_of(text, "name=first"), "text": "Ada"}}
        state["values"] = {"city": await lab.field_value(state["agent"], "#city"),
                           "first": await lab.field_value(state["agent"], "#first")}
        state["note"] = state["agent"]._langhire_watch.note
        state["locked"] = dict(state["agent"]._langhire_watch.locked)
        state["system"] = state["agent"]._message_manager.state.history.system_message.text
        return {"done": {"text": "NEEDS YOU: the local test is over", "success": False}}

    script = lab.Script(decide)
    apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
    store = MemoryStore(tmp_path / "answers.db")
    monkeypatch.setattr(apply_jobs, "get_memory_store", lambda: store)
    monkeypatch.setattr(shared_config, "get_memory_store", lambda: store)
    job = lab.add_job(shared_config, site.url("/job"))
    run = asyncio.ensure_future(lab.run_job(apply_jobs, job))
    try:
        state["agent"] = await lab.running_agent(apply_jobs, job["url"])
        await asyncio.wait_for(thinking.wait(), 60)
        await asyncio.sleep(1.5)            # the watcher is installed in the page by now
        await lab.as_the_user(state["agent"], "click #city\ntype UserTown")
        assert await _until(lambda: state["agent"]._langhire_watch.user_stepped_in), "the user's typing wasn't noticed"
        user_done.set()
        status = await asyncio.wait_for(run, 120)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert status == "needs_you"
    assert state["values"] == {"city": "UserTown", "first": "Ada"}       # never typed over; the rest still filled
    assert any(value == "UserTown" for value in state["locked"].values())
    assert "keeping your 1 edit" in state["note"]
    assert '"Preferred work city" = "UserTown"' in state["system"]      # the agent is told, for the rest of the run
    assert store.qa_user_answers().get("Preferred work city") == "UserTown"   # remembered as the user's answer


async def test_a_page_the_user_moved_to_is_continued_from_not_undone(site, data_dir, monkeypatch):
    monkeypatch.setattr(run_watch, "USER_IDLE_SECONDS", 2.0)
    thinking, user_done = asyncio.Event(), asyncio.Event()
    state = {"agent": None, "urls": [], "back": ""}

    async def decide(text, step):
        state["urls"].append(await state["agent"].browser_session.get_current_page_url() if state["agent"] else "")
        if step == 1:
            thinking.set()
            await user_done.wait()
            return {"input": {"index": lab.index_of(text, "id=first") or lab.index_of(text, "name=first") or 1, "text": "Ada"}}
        if step == 2:
            return {"go_back": {}}          # the agent would like to redo page 1
        if step == 3:
            state["back"] = text
            return {"input": {"index": lab.index_of(text, "id=school") or lab.index_of(text, "name=school"), "text": "State University"}}
        state["school"] = await lab.field_value(state["agent"], "#school")
        state["note"] = state["agent"]._langhire_watch.note
        state["system"] = state["agent"]._message_manager.state.history.system_message.text
        return {"done": {"text": "NEEDS YOU: the local test is over", "success": False}}

    script = lab.Script(decide)
    apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
    job = lab.add_job(shared_config, site.url("/job"))
    run = asyncio.ensure_future(lab.run_job(apply_jobs, job))
    try:
        state["agent"] = await lab.running_agent(apply_jobs, job["url"])
        await asyncio.wait_for(thinking.wait(), 60)
        await asyncio.sleep(1.5)
        await lab.as_the_user(state["agent"], "click #next")          # the user goes on to page 2 themselves
        assert await _until(lambda: state["agent"]._langhire_watch.user_stepped_in), "the user's click wasn't noticed"
        user_done.set()
        status = await asyncio.wait_for(run, 120)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert status == "needs_you"
    assert state["urls"][1].endswith("/page2") and state["urls"][2].endswith("/page2") and state["urls"][3].endswith("/page2")
    assert "never go back to an earlier page" in state["back"]            # the go_back was refused
    assert state["school"] == "State University"                           # work went on, on the user's page
    assert "Continuing from your page" in state["note"]
    assert "THE USER MOVED THIS APPLICATION ON THEMSELVES" in state["system"]
    assert [path for method, path, _ in site.requests if path in ("/job", "/page2")] == ["/job", "/page2"]   # page 1 never reloaded


async def test_the_agents_own_typing_is_not_mistaken_for_the_user(site, data_dir, monkeypatch):
    monkeypatch.setattr(run_watch, "USER_IDLE_SECONDS", 2.0)
    state = {"agent": None}

    async def decide(text, step):
        if step == 1:
            return [{"input": {"index": lab.index_of(text, "id=first") or lab.index_of(text, "name=first"), "text": "Ada"}},
                    {"input": {"index": lab.index_of(text, "id=city") or lab.index_of(text, "name=city"), "text": "Springfield"}}]
        if step == 2:
            await asyncio.sleep(2.5)            # several watcher polls pass
            return {"click": {"index": lab.index_of(text, "id=terms") or lab.index_of(text, "name=terms")}}
        state["stepped_in"] = state["agent"]._langhire_watch.user_stepped_in
        state["locked"] = dict(state["agent"]._langhire_watch.locked)
        state["terms"] = await lab.field_value(state["agent"], "#terms")
        return {"done": {"text": "NEEDS YOU: the local test is over", "success": False}}

    script = lab.Script(decide)
    apply_jobs, shared_config = lab.prepare(monkeypatch, data_dir, site, script)
    job = lab.add_job(shared_config, site.url("/job"))
    run = asyncio.ensure_future(lab.run_job(apply_jobs, job))
    try:
        state["agent"] = await lab.running_agent(apply_jobs, job["url"])
        await asyncio.wait_for(run, 120)
    finally:
        if not run.done():
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)
    assert state["stepped_in"] is False and state["locked"] == {} and state["terms"] == "true"
    assert script.calls == 3                    # no step was lost to a false "the user stepped in"
