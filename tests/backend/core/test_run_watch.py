"""The run watcher's decisions without a browser (the real-browser tests drive it on local pages).

No page or step may hang (the user's rule, 2026-11): a page that stays blank or doesn't change
for 5 minutes is a failure. Time the app spends waiting on the AI, the user or an email doesn't
count against the page, and input during the agent's own actions isn't the user's.
"""
import asyncio
import time

import pytest

from core import run_watch
from core.run_watch import RunWatch, is_blank


@pytest.mark.parametrize("state, blank", [
    (None, True),
    ({"url": "chrome-error://chromewebdata/", "text": 900, "controls": 3}, True),
    ({"url": "about:blank", "text": 0, "controls": 0}, True),
    ({"url": "", "text": 500, "controls": 2}, True),
    ({"url": "https://jobs.example/1", "text": 12, "controls": 0}, True),      # an empty shell
    ({"url": "https://jobs.example/1", "text": 12, "controls": 1}, False),     # a form with one field
    ({"url": "https://jobs.example/1", "text": 400, "controls": 0}, False),
])
def test_what_counts_as_a_blank_page(state, blank):
    assert is_blank(state) is blank


def test_input_during_or_just_after_the_agents_action_is_the_agents():
    watch = RunWatch(browser_session=None)
    with watch.acting():
        during = time.time() * 1000
        assert watch._by_agent(during)
    just_after = time.time() * 1000 + run_watch.ACTION_GRACE_MS / 2
    later = time.time() * 1000 + run_watch.ACTION_GRACE_MS * 4
    assert watch._by_agent(just_after) and not watch._by_agent(later)


def test_the_user_counts_as_active_until_idle_or_resume():
    watch = RunWatch(browser_session=None, idle_seconds=0.2)
    assert not watch.user_active()
    watch._last_user_event = time.monotonic()
    assert watch.user_active() and watch.status()["user_active"] is True
    watch.resume_now()                      # Resume: the user is done, no idle wait
    assert not watch.user_active()
    watch._resume_now = False
    watch._last_user_event = time.monotonic() - 1
    assert not watch.user_active()          # idle longer than the window


async def _run_with(states, monkeypatch, stall_seconds=0.4, hold=False, limit=3.0):
    """Run the watcher against a page whose state comes from `states` (a function of time)."""
    monkeypatch.setattr(run_watch, "POLL_SECONDS", 0.02)
    monkeypatch.setattr(run_watch, "SIGNATURE_SECONDS", 0.05)
    began = time.monotonic()

    async def fake_state(browser, timeout=8.0):
        return states(time.monotonic() - began)
    monkeypatch.setattr(run_watch, "page_state", fake_state)
    watch = RunWatch(browser_session=None, stall_seconds=stall_seconds)
    task = asyncio.create_task(watch.run())
    try:
        if hold:
            with watch.hold():
                await asyncio.sleep(limit)
            return watch, None
        try:
            return watch, await asyncio.wait_for(watch.stalled(), limit)
        except asyncio.TimeoutError:
            return watch, None
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_a_page_that_never_changes_is_reported_stalled(monkeypatch):
    page = {"url": "https://jobs.example/1", "text": 800, "controls": 4, "sample": "Apply", "values": []}
    watch, reason = await _run_with(lambda t: page, monkeypatch)
    assert reason and reason.startswith("the page didn't change or respond")
    assert watch.stall_url == "https://jobs.example/1"


async def test_a_blank_page_says_so(monkeypatch):
    _, reason = await _run_with(lambda t: {"url": "about:blank", "text": 0, "controls": 0}, monkeypatch)
    assert reason and reason.startswith("the page stayed blank")


async def test_a_page_that_keeps_changing_is_never_stalled(monkeypatch):
    def moving(t):
        return {"url": "https://jobs.example/1", "text": 800, "controls": 4, "sample": f"step {int(t * 20)}", "values": []}
    _, reason = await _run_with(moving, monkeypatch, limit=1.2)
    assert reason is None


async def test_time_spent_waiting_on_the_ai_or_the_user_doesnt_count(monkeypatch):
    page = {"url": "https://jobs.example/1", "text": 800, "controls": 4, "sample": "same", "values": []}
    watch, _ = await _run_with(lambda t: page, monkeypatch, hold=True, limit=1.0)
    assert not watch._stall.is_set()        # 1 s on hold, 0.4 s allowed: still not stalled
