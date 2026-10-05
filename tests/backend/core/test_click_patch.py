"""The Workday click fix: a target covered only by its own sibling overlay gets a real click."""
import asyncio

import pytest
from browser_use.browser.watchdogs.default_action_watchdog import DefaultActionWatchdog

from core import click_patch


class FakeSend:
    def __init__(self, overlay_answer):
        self.overlay_answer = overlay_answer
        self.calls = []

        class DOM:
            @staticmethod
            async def resolveNode(params, session_id=None):
                self.calls.append(("resolveNode", params["backendNodeId"]))
                return {"object": {"objectId": "obj-1"}}

        class Runtime:
            @staticmethod
            async def callFunctionOn(params, session_id=None):
                self.calls.append(("callFunctionOn", params["arguments"]))
                if isinstance(self.overlay_answer, Exception):
                    raise self.overlay_answer
                return {"result": {"value": self.overlay_answer}}

        self.DOM, self.Runtime = DOM, Runtime


class FakeCdpSession:
    def __init__(self, overlay_answer):
        self.session_id = "s-1"
        self.send = FakeSend(overlay_answer)
        self.cdp_client = type("Client", (), {"send": self.send})()


class FakeWatchdog:
    logger = type("L", (), {"debug": staticmethod(lambda *a, **k: None)})()


def check(monkeypatch, original_says_covered, overlay_answer):
    async def original(self, backend_node_id, x, y, cdp_session):
        return original_says_covered

    monkeypatch.setattr(click_patch, "_original_check", original)
    session = FakeCdpSession(overlay_answer)
    covered = asyncio.run(click_patch._check_element_occlusion(FakeWatchdog(), 7, 10.0, 20.0, session))
    return covered, session.send.calls


def test_sibling_overlay_means_clickable(monkeypatch):
    """Workday's invisible input over a checkbox box, or click_filter over a button: click for real."""
    covered, calls = check(monkeypatch, True, True)
    assert covered is False
    assert calls == [("resolveNode", 7), ("callFunctionOn", [{"value": 10.0}, {"value": 20.0}])]


def test_a_real_cover_still_counts(monkeypatch):
    """Something else on top (a cookie banner, a dialog): keep browser-use's old behaviour."""
    covered, _ = check(monkeypatch, True, False)
    assert covered is True


def test_uncovered_target_skips_the_extra_check(monkeypatch):
    covered, calls = check(monkeypatch, False, True)
    assert covered is False and calls == []


def test_a_failed_check_keeps_the_old_answer(monkeypatch):
    covered, _ = check(monkeypatch, True, RuntimeError("node gone"))
    assert covered is True


def test_apply_installs_the_patch():
    click_patch.apply()
    assert DefaultActionWatchdog._check_element_occlusion is click_patch._check_element_occlusion


def test_overlay_rule_only_trusts_a_tight_parent():
    js = click_patch.SIBLING_OVERLAY_JS
    assert "parent.contains(top)" in js
    assert "4 * t.width * t.height" in js


def test_apply_jobs_installs_it_on_import():
    from cli import apply_jobs  # noqa: F401
    assert DefaultActionWatchdog._check_element_occlusion is click_patch._check_element_occlusion


@pytest.mark.parametrize("name", ["_check_element_occlusion"])
def test_patched_method_keeps_the_signature(name):
    import inspect
    assert list(inspect.signature(getattr(click_patch, name)).parameters) == \
        ["self", "backend_node_id", "x", "y", "cdp_session"]
