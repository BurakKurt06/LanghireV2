"""Own and reap browser-use's asyncio subprocesses and their output pipes.

browser-use 0.13 keeps only psutil.Process after create_subprocess_exec. Its
unread pipes can fill or survive until a Windows event loop closes. This shim
is confined to that module's asyncio binding and browser-launch context; the
global asyncio API, installer subprocesses and network guards are unchanged.
BrowserSession.kill first asks the browser to close, so it writes its profile.
"""
from __future__ import annotations

import asyncio
from contextvars import ContextVar
from dataclasses import dataclass, field
from functools import wraps

import psutil

_owner = ContextVar("langhire_browser_process_owner", default=None)
_STATE = "_langhire_async_process_owner"
_LIMIT = 16_384


@dataclass
class Child:
    process: asyncio.subprocess.Process
    tails: list[bytearray] = field(default_factory=lambda: [bytearray(), bytearray()])
    readers: list[asyncio.Task] = field(default_factory=list)
    descendants: list = field(default_factory=list)
    root: object = field(default=None, init=False)

    def __post_init__(self):
        try:
            self.root = psutil.Process(self.process.pid)
        except psutil.NoSuchProcess:
            pass

    def snapshot(self):
        if self.process.returncode is not None or self.root is None or not self.root.is_running():
            return
        try:
            known = {p.pid for p in self.descendants}
            for process in self.root.children(recursive=True):
                if process.pid not in known:
                    self.descendants.append(process)
        except psutil.NoSuchProcess:
            pass

    async def drain(self, stream, tail):
        while chunk := await stream.read(32_768):
            tail.extend(chunk)
            del tail[:-_LIMIT]

    def start_readers(self):
        for stream, tail in zip((self.process.stdout, self.process.stderr), self.tails):
            if stream is not None:
                self.readers.append(asyncio.create_task(self.drain(stream, tail)))

    async def close(self):
        self.snapshot()
        # These are descendants of this exact spawned process, not a scan of
        # browser names or user profiles. Process objects check PID reuse.
        for descendant in reversed(self.descendants):
            try:
                if descendant.is_running():
                    descendant.terminate()
            except psutil.NoSuchProcess:
                pass
        if self.process.returncode is None:
            try:
                self.process.terminate()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(self.process.wait(), 5)
        except TimeoutError:
            for descendant in self.descendants:
                try:
                    if descendant.is_running():
                        descendant.kill()
                except psutil.NoSuchProcess:
                    pass
            if self.process.returncode is None:
                self.process.kill()
            await asyncio.wait_for(self.process.wait(), 5)
        # TerminateProcess returning does not mean a descendant has exited yet.
        # Wait for the captured identities too, including children that closed
        # their output handles before exiting.
        if self.descendants:
            _, alive = await asyncio.to_thread(psutil.wait_procs, self.descendants, timeout=2)
            for descendant in alive:
                try:
                    descendant.kill()
                except psutil.NoSuchProcess:
                    pass
            _, alive = await asyncio.to_thread(psutil.wait_procs, alive, timeout=3)
            if alive:
                raise RuntimeError("Owned browser descendants did not exit")
        if self.readers:
            await asyncio.wait_for(asyncio.gather(*self.readers), 5)


class Owner:
    def __init__(self):
        self.children = []
        self.lock = asyncio.Lock()

    def snapshot(self):
        for child in self.children:
            child.snapshot()

    async def close(self):
        async with self.lock:
            for child in self.children:
                await child.close()


async def _finish_cleanup(owner):
    # A second Stop/cancellation must not strand pipe transports on an event
    # loop that is about to close. Finish the bounded reap, then propagate it.
    await _finish(owner.close())


async def _finish(work):
    """Run `work` to the end even if this task is cancelled meanwhile, then propagate the cancellation."""
    task = asyncio.ensure_future(work)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    await task
    if cancelled:
        raise asyncio.CancelledError


class _AsyncioBinding:
    """Module-local delegation; never patch asyncio.create_subprocess_exec globally."""
    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def create_subprocess_exec(self, *args, **kwargs):
        process = await asyncio.create_subprocess_exec(*args, **kwargs)
        owner = _owner.get()
        if (owner is not None and any(str(a).startswith("--remote-debugging-port=") for a in args)
                and any(str(a).startswith("--user-data-dir=") for a in args)):
            child = Child(process)
            owner.children.append(child)
            child.start_readers()
        return process


def install():
    from browser_use.browser.watchdogs import local_browser_watchdog as module
    cls = module.LocalBrowserWatchdog
    if getattr(cls._launch_browser, "_langhire_owned_pipes", False):
        return
    launch, kill = cls._launch_browser, cls.on_BrowserKillEvent

    @wraps(launch)
    async def owned_launch(self, *args, **kwargs):
        owner = self.__dict__.get(_STATE)
        if owner is None:
            owner = self.__dict__[_STATE] = Owner()
        token = _owner.set(owner)
        try:
            return await launch(self, *args, **kwargs)
        except BaseException:
            # Also runs if CDP startup is cancelled before the vendor records
            # the psutil handle. A failed start must not orphan that browser.
            await _finish_cleanup(owner)
            raise
        finally:
            _owner.reset(token)

    @wraps(kill)
    async def owned_kill(self, event):
        owner = self.__dict__.get(_STATE)
        if owner:
            owner.snapshot()
        try:
            return await kill(self, event)
        finally:
            if owner:
                await _finish_cleanup(owner)

    owned_launch._langhire_owned_pipes = True
    cls._launch_browser = owned_launch
    cls.on_BrowserKillEvent = owned_kill
    module.asyncio = _AsyncioBinding()

    from browser_use.browser.session import BrowserSession
    session_kill = BrowserSession.kill

    @wraps(session_kill)
    async def closing_kill(self):
        return await _close_then_kill(self, session_kill)

    BrowserSession.kill = closing_kill


async def _close_then_kill(session, kill):
    try:
        await _close_cleanly(session)
    except asyncio.CancelledError:
        # Stopped again while the browser was closing: browser-use's kill still has to end the
        # process, its descendants, the pipe readers and the event bus. Then propagate the stop.
        await _finish(kill(session))
        raise
    return await kill(session)


async def _close_cleanly(session, timeout=5.0):
    """Ask the browser to close before browser-use terminates it.

    Chrome writes cookies and site data to its profile on a clean shutdown;
    terminating it (TerminateProcess on Windows) loses what it hadn't written yet.
    Not connected, or not closing in time: the usual kill follows unchanged.
    """
    watchdog = getattr(session, "_local_browser_watchdog", None)
    process = getattr(watchdog, "_subprocess", None)
    if process is None:
        return
    # Record the browser's descendants while it still runs: after a clean close the kill's own
    # snapshot finds the browser gone and could not reap a child that outlived it.
    owner = watchdog.__dict__.get(_STATE)
    if owner:
        owner.snapshot()
    # As browser-use's kill() does first: the dropped connection is not a crash to reconnect from,
    # and (as its reset() does) the closing tab is not lost focus to recover with a new tab.
    session._intentional_stop = True
    session.agent_focus_target_id = None
    try:
        await asyncio.wait_for(session.cdp_client.send.Browser.close(), timeout)
        await asyncio.to_thread(process.wait, timeout)
    except Exception:
        pass


install()
