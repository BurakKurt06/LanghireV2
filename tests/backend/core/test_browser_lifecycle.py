"""Real output pipes and subprocess ownership, without a browser or network."""
import asyncio
import gc
import sys

import pytest

from core import browser_lifecycle as lifecycle

pytestmark = [pytest.mark.filterwarnings("error::ResourceWarning"),
    pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")]


async def spawn(owner, program, *, tracked=True):
    args = ("--remote-debugging-port=9999", "--user-data-dir=fake-test-profile") if tracked else ()
    token = lifecycle._owner.set(owner)
    try:
        return await lifecycle._AsyncioBinding().create_subprocess_exec(
            sys.executable, "-c", program, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    finally:
        lifecycle._owner.reset(token)


async def test_output_larger_than_the_pipe_limit_is_drained_and_bounded():
    owner = lifecycle.Owner()
    process = await spawn(owner, "import os; os.write(1,b'A'*2000000); os.write(2,b'B'*2000000)")
    await asyncio.wait_for(process.wait(), 10)
    await owner.close()
    child, = owner.children
    assert child.tails == [bytearray(b'A'*lifecycle._LIMIT), bytearray(b'B'*lifecycle._LIMIT)]
    assert all(task.done() and task.exception() is None for task in child.readers)
    assert process.stdout.at_eof() and process.stderr.at_eof()
    assert process._transport.is_closing()
    gc.collect()


async def test_concurrent_owners_close_only_their_own_processes():
    first, second = lifecycle.Owner(), lifecycle.Owner()
    p1 = await spawn(first, "import time;time.sleep(60)")
    p2 = await spawn(second, "import time;time.sleep(60)")
    try:
        await asyncio.gather(first.close(), first.close())
        assert p1.returncode is not None and p1._transport.is_closing()
        assert p2.returncode is None
    finally:
        await second.close()
    assert p2.returncode is not None and p2._transport.is_closing()


async def test_nonbrowser_subprocesses_and_global_asyncio_are_untouched():
    original = asyncio.create_subprocess_exec
    owner = lifecycle.Owner()
    process = await spawn(owner, "print('installer output')", tracked=False)
    output, errors = await process.communicate()
    assert output.strip() == b"installer output" and errors == b""
    assert owner.children == []
    assert asyncio.create_subprocess_exec is original


async def test_dead_parent_is_not_looked_up_again_by_a_reused_pid(monkeypatch):
    owner = lifecycle.Owner()
    process = await spawn(owner, "pass")
    await process.wait()
    monkeypatch.setattr(lifecycle.psutil, "Process", lambda pid: pytest.fail("Do not re-resolve an exited PID"))
    await owner.close()
    await owner.close()
    assert process._transport.is_closing()


async def test_repeated_cancellation_finishes_cleanup_before_propagating():
    owner = lifecycle.Owner()
    process = await spawn(owner, "import time;time.sleep(60)")
    entered, release = asyncio.Event(), asyncio.Event()
    close = owner.close

    async def held_close():
        entered.set()
        await release.wait()
        await close()

    owner.close = held_close
    task = asyncio.create_task(lifecycle._finish_cleanup(owner))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert process.returncode is not None and process._transport.is_closing()
    assert all(t.done() and not t.cancelled() for t in owner.children[0].readers)


async def _held_close(entered):
    entered.set()
    await asyncio.Future()  # cancelled here, as by a Stop while the browser is closing


@pytest.mark.parametrize("cancels_during_kill", [0, 2])
async def test_cancelled_clean_close_still_runs_the_full_kill_then_propagates(monkeypatch, cancels_during_kill):
    """Codex's case: cancelling the task during _close_cleanly skipped browser-use's kill (process,
    owned descendants, pipe readers, event bus). The kill runs to the end, even through further
    cancellations, and the cancellation still reaches the caller."""
    entered, in_kill, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    finished = []

    async def kill(session):
        in_kill.set()
        await release.wait()
        finished.append(session)
        return "killed"

    monkeypatch.setattr(lifecycle, "_close_cleanly", lambda session: _held_close(entered))
    task = asyncio.create_task(lifecycle._close_then_kill("session", kill))
    await entered.wait()
    task.cancel()
    await asyncio.wait_for(in_kill.wait(), 5)
    for _ in range(cancels_during_kill):
        task.cancel()
        await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished == ["session"]


async def test_an_unconnected_or_closed_browser_is_followed_by_exactly_one_kill(monkeypatch):
    class NotConnected:
        _local_browser_watchdog = type("Watchdog", (), {"_subprocess": object()})()

        @property
        def cdp_client(self):
            raise AssertionError("Root CDP client not initialized")

    calls = []

    async def kill(session):
        calls.append(session)
        return "killed"

    session = NotConnected()
    assert await lifecycle._close_then_kill(session, kill) == "killed"
    monkeypatch.setattr(lifecycle, "_close_cleanly", lambda session: asyncio.sleep(0))
    assert await lifecycle._close_then_kill("closed", kill) == "killed"
    assert calls == [session, "closed"]
