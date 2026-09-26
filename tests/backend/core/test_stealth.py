"""Tests for the anti-detection helpers and the settings switches that gate them.

Covers only the new behaviour: stealth launch flags, the injected patch script,
randomised human delays, and that every new switch defaults to preserving the
app's previous behaviour.
"""
import asyncio

import pytest

import core.shared_config as shared_config
from core.config import load_settings, save_settings
from core.stealth import (
    STEALTH_ARGS,
    human_delay,
    human_delay_seconds,
)


# ── Defaults: new switches must not change existing behaviour ───────────────
class TestSettingsDefaults:
    def test_job_collection_is_on_by_default(self, data_dir):
        assert load_settings()["job_collection_enabled"] is True

    def test_stealth_is_off_by_default(self, data_dir):
        assert load_settings()["stealth_enabled"] is False

    def test_human_delay_is_off_by_default(self, data_dir):
        assert load_settings()["human_delay_enabled"] is False

    def test_human_delay_window_defaults_to_5_15(self, data_dir):
        s = load_settings()
        assert s["human_delay_min"] == 5
        assert s["human_delay_max"] == 15

    def test_captcha_is_off_by_default_with_no_key(self, data_dir):
        s = load_settings()
        assert s["captcha_solver_enabled"] is False
        assert s["captcha_api_key"] == ""


class TestSettingsPersistence:
    def test_switches_round_trip(self, data_dir):
        save_settings({
            "job_collection_enabled": False,
            "stealth_enabled": True,
            "human_delay_enabled": True,
            "human_delay_min": 3,
            "human_delay_max": 9,
            "captcha_solver_enabled": True,
            "captcha_api_key": "k-123",
        })
        s = load_settings()
        assert s["job_collection_enabled"] is False
        assert s["stealth_enabled"] is True
        assert s["human_delay_enabled"] is True
        assert s["human_delay_min"] == 3
        assert s["human_delay_max"] == 9
        assert s["captcha_solver_enabled"] is True
        assert s["captcha_api_key"] == "k-123"

    def test_delay_bounds_are_clamped(self, data_dir):
        save_settings({"human_delay_min": -5, "human_delay_max": 9999})
        s = load_settings()
        assert s["human_delay_min"] == 0
        assert s["human_delay_max"] == 300

    def test_max_below_min_is_corrected(self, data_dir):
        """An inverted window would otherwise produce an empty range."""
        save_settings({"human_delay_min": 20, "human_delay_max": 5})
        s = load_settings()
        assert s["human_delay_max"] >= s["human_delay_min"]


# ── Launch flags ────────────────────────────────────────────────────────────
class TestStealthLaunchArgs:
    def test_no_extra_args_when_stealth_off(self, data_dir, monkeypatch):
        monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"stealth_enabled": False})
        # Only the always-on removal of the flag Chrome warns about.
        kwargs = shared_config.build_browser_kwargs()
        assert kwargs["ignore_default_args"] == ["--extensions-on-chrome-urls"]
        assert "args" not in kwargs

    def test_launch_args_have_no_chrome_warning_flag(self, data_dir, monkeypatch):
        """Chrome shows an 'unsupported command-line flag' bar for --extensions-on-chrome-urls."""
        from browser_use.browser.profile import BrowserProfile
        for stealth in (False, True):
            monkeypatch.setattr(shared_config, "load_runtime_settings", lambda s=stealth: {"stealth_enabled": s})
            args = BrowserProfile(**shared_config.build_browser_kwargs()).get_args()
            assert "--extensions-on-chrome-urls" not in args

    def test_args_added_when_stealth_on(self, data_dir, monkeypatch):
        monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"stealth_enabled": True})
        kwargs = shared_config.build_browser_kwargs()
        assert "args" in kwargs
        assert "--disable-blink-features=AutomationControlled" in kwargs["args"]

    def test_automation_controlled_flag_is_present(self):
        """The single most important flag: without it Chrome self-identifies."""
        joined = " ".join(STEALTH_ARGS)
        assert "AutomationControlled" in joined

    def test_args_list_is_copied_not_shared(self, data_dir, monkeypatch):
        """Callers must not be able to mutate the module-level list."""
        monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {"stealth_enabled": True})
        kwargs = shared_config.build_browser_kwargs()
        kwargs["args"].append("--poisoned")
        assert "--poisoned" not in STEALTH_ARGS


# ── Injected patch script ───────────────────────────────────────────────────
class TestStealthScript:
    @pytest.mark.parametrize("signal", [
        "webdriver",           # the most-checked automation flag
        "languages",
        "plugins",
        "chrome",
        "getParameter",        # WebGL vendor/renderer spoof
        "hardwareConcurrency",
    ])
    def test_script_patches_known_detection_signals(self, signal):
        from core.stealth import _STEALTH_JS
        assert signal in _STEALTH_JS

    def test_script_is_self_invoking(self):
        from core.stealth import _STEALTH_JS
        assert _STEALTH_JS.strip().startswith("(()")

    async def test_apply_stealth_never_raises(self):
        """A browser failure must not abort an application run."""
        from core.stealth import apply_stealth

        class Boom:
            async def _cdp_add_init_script(self, script):
                raise RuntimeError("no cdp")

        assert await apply_stealth(Boom()) is False

    async def test_apply_stealth_returns_true_on_success(self):
        from core.stealth import apply_stealth
        captured = {}

        class Fake:
            async def _cdp_add_init_script(self, script):
                captured["script"] = script
                return "id-1"

        assert await apply_stealth(Fake()) is True
        assert "webdriver" in captured["script"]


# ── Randomised delays ───────────────────────────────────────────────────────
class TestHumanDelay:
    def test_delay_falls_inside_the_window(self):
        for _ in range(50):
            assert 5.0 <= human_delay_seconds(5, 15) <= 15.0

    def test_delay_varies(self):
        """A constant delay would defeat the point."""
        values = {round(human_delay_seconds(5, 15), 4) for _ in range(30)}
        assert len(values) > 1

    def test_inverted_window_does_not_raise(self):
        assert human_delay_seconds(15, 5) == 15.0

    def test_negative_min_is_floored_at_zero(self):
        assert human_delay_seconds(-10, 0) == 0.0

    async def test_zero_window_does_not_sleep(self):
        delay = await human_delay(0, 0)
        assert delay == 0.0

    async def test_maybe_human_delay_is_a_noop_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            shared_config, "load_runtime_settings", lambda: {"human_delay_enabled": False}
        )
        assert await shared_config.maybe_human_delay() == 0.0

    async def test_maybe_human_delay_sleeps_when_enabled(self, monkeypatch):
        monkeypatch.setattr(
            shared_config,
            "load_runtime_settings",
            lambda: {"human_delay_enabled": True, "human_delay_min": 0.01, "human_delay_max": 0.02},
        )
        delay = await shared_config.maybe_human_delay("test")
        assert 0.01 <= delay <= 0.02


class TestWorkerStagger:
    """Worker launches are spaced out by the one delay system."""

    async def test_uses_the_fixed_pause_when_delays_are_off(self, monkeypatch):
        monkeypatch.setattr(
            shared_config, "load_runtime_settings", lambda: {"human_delay_enabled": False}
        )
        slept = {}

        async def fake_sleep(s):
            slept["for"] = s

        monkeypatch.setattr(shared_config.asyncio, "sleep", fake_sleep)
        out = await shared_config.worker_stagger(default_seconds=5)
        assert out == 5
        assert slept["for"] == 5

    async def test_uses_the_randomised_window_when_delays_are_on(self, monkeypatch):
        monkeypatch.setattr(
            shared_config,
            "load_runtime_settings",
            lambda: {"human_delay_enabled": True, "human_delay_min": 0.01, "human_delay_max": 0.02},
        )
        out = await shared_config.worker_stagger(default_seconds=5)
        assert 0.01 <= out <= 0.02


class TestStaggerDelayRemoved:
    """The dead `stagger_delay` setting is gone; one delay system remains."""

    def test_not_in_defaults(self, data_dir):
        assert "stagger_delay" not in load_settings()

    def test_human_delay_settings_are_the_replacement(self, data_dir):
        s = load_settings()
        assert "human_delay_enabled" in s
        assert "human_delay_min" in s
        assert "human_delay_max" in s


# ── Stealth gating ──────────────────────────────────────────────────────────
class TestMaybeApplyStealth:
    async def test_skipped_when_disabled(self, monkeypatch):
        monkeypatch.setattr(
            shared_config, "load_runtime_settings", lambda: {"stealth_enabled": False}
        )
        started = {"n": 0}

        class Fake:
            async def start(self):
                started["n"] += 1

        assert await shared_config.maybe_apply_stealth(Fake()) is False
        # Must not even start the browser when the switch is off.
        assert started["n"] == 0

    async def test_browser_failure_is_swallowed(self, monkeypatch):
        monkeypatch.setattr(
            shared_config, "load_runtime_settings", lambda: {"stealth_enabled": True}
        )

        class Boom:
            async def start(self):
                raise RuntimeError("launch failed")

        assert await shared_config.maybe_apply_stealth(Boom()) is False
