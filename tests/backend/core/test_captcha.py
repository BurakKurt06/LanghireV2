"""Tests for CAPTCHA detection, solving, injection and the abandon fallback.

No network and no browser: the CDP evaluate call and the 2Captcha SDK are both
stubbed. Covers each supported challenge type, the 2-attempt cap, the
switch-off short-circuit, the non-blocking reCAPTCHA v3 carve-out, and that
the API key never leaks into an error message.
"""
import asyncio

import pytest

import core.captcha as captcha
from core.captcha import (
    BLOCKING_TYPES,
    HCAPTCHA,
    IMAGE_CAPTCHA,
    MAX_SOLVE_ATTEMPTS,
    RECAPTCHA_V2,
    RECAPTCHA_V3,
    TURNSTILE,
    CaptchaUnsolved,
    Challenge,
)

API_KEY = "super-secret-2captcha-key"


_real_sleep = asyncio.sleep


async def _no_sleep(seconds):
    await _real_sleep(0)


class FakeSession:
    """Stands in for a BrowserSession: records evaluated JS, returns canned values."""

    def __init__(self, detect_result=None, inject_result=True, url="https://jobs.example/apply"):
        self._detect = detect_result
        self._inject = inject_result
        self._url = url
        self.evaluated: list[str] = []

    async def get_or_create_cdp_session(self):
        raise AssertionError("tests stub _evaluate instead")


def stub_evaluate(monkeypatch, detect_result, url="https://jobs.example/apply", inject_result=True):
    calls = {"inject": []}

    async def fake_eval(session, expression):
        if expression is captcha._DETECT_JS or "cf-turnstile" in expression and "querySelector" in expression and "return null" in expression:
            return detect_result
        if expression.strip().startswith("window.location.href"):
            return url
        if "const token =" in expression:
            calls["inject"].append(expression)
            return inject_result
        return detect_result

    monkeypatch.setattr(captcha, "_evaluate", fake_eval)
    return calls


# ── Detection ───────────────────────────────────────────────────────────────
class TestDetect:
    @pytest.mark.parametrize("kind,sitekey", [
        (RECAPTCHA_V2, "k-v2"),
        (RECAPTCHA_V3, "k-v3"),
        (HCAPTCHA, "k-hc"),
        (TURNSTILE, "k-ts"),
    ])
    async def test_detects_each_token_based_type(self, monkeypatch, kind, sitekey):
        stub_evaluate(monkeypatch, {"type": kind, "sitekey": sitekey, "action": ""})
        found = await captcha.detect(FakeSession())
        assert found is not None
        assert found.type == kind
        assert found.sitekey == sitekey

    async def test_detects_image_captcha(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": IMAGE_CAPTCHA, "imgSrc": "data:image/png;base64,AAA"})
        found = await captcha.detect(FakeSession())
        assert found.type == IMAGE_CAPTCHA
        assert found.is_image is True

    async def test_returns_none_when_no_captcha(self, monkeypatch):
        stub_evaluate(monkeypatch, None)
        assert await captcha.detect(FakeSession()) is None

    async def test_detection_failure_is_swallowed(self, monkeypatch):
        async def boom(session, expression):
            raise RuntimeError("cdp gone")
        monkeypatch.setattr(captcha, "_evaluate", boom)
        assert await captcha.detect(FakeSession()) is None

    async def test_captures_page_url_for_the_solver(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V2, "sitekey": "k"}, url="https://acme.test/jobs/1")
        found = await captcha.detect(FakeSession())
        assert found.page_url == "https://acme.test/jobs/1"

    @pytest.mark.parametrize("marker", [
        "cf-turnstile", "hcaptcha", "g-recaptcha", "render=", "captcha",
    ])
    def test_detector_script_covers_each_vendor(self, marker):
        assert marker in captcha._DETECT_JS

    def test_detector_script_ignores_hidden_elements(self):
        """Hidden elements are honeypots, not real challenges."""
        assert "display" in captcha._DETECT_JS and "visibility" in captcha._DETECT_JS


# ── Token injection ─────────────────────────────────────────────────────────
class TestInjection:
    @pytest.mark.parametrize("kind,field", [
        (RECAPTCHA_V2, "g-recaptcha-response"),
        (RECAPTCHA_V3, "g-recaptcha-response"),
        (HCAPTCHA, "h-captcha-response"),
        (TURNSTILE, "cf-turnstile-response"),
    ])
    def test_targets_the_right_response_field(self, kind, field):
        js = captcha._inject_js(Challenge(type=kind, sitekey="k"), "tok")
        assert field in js

    def test_image_captcha_types_the_answer_into_an_input(self):
        """An image answer is typed into a text box, not written to a token field."""
        js = captcha._inject_js(Challenge(type=IMAGE_CAPTCHA), "AB12")
        assert js != ""
        assert "AB12" in js
        assert "input" in js
        # Must use the native setter so React-controlled inputs register it.
        assert "HTMLInputElement.prototype" in js

    def test_image_injection_prefers_a_captcha_named_input(self):
        js = captcha._inject_js(Challenge(type=IMAGE_CAPTCHA), "AB12")
        assert "captcha" in js.lower()

    def test_image_injection_skips_hidden_inputs(self):
        js = captcha._inject_js(Challenge(type=IMAGE_CAPTCHA), "AB12")
        assert "display" in js and "visibility" in js

    def test_unknown_type_has_no_injection_script(self):
        assert captcha._inject_js(Challenge(type="something_else"), "tok") == ""

    def test_token_is_escaped(self):
        """A token containing a quote must not break out of the JS string."""
        js = captcha._inject_js(Challenge(type=RECAPTCHA_V2), "ab'cd")
        assert "ab\\'cd" in js

    async def test_inject_returns_false_for_unsupported_type(self, monkeypatch):
        stub_evaluate(monkeypatch, None)
        ok = await captcha.inject_token(FakeSession(), Challenge(type="something_else"), "t")
        assert ok is False


class TestImageCaptchaSource:
    """2Captcha wants raw base64 for a data: URI, and a URL otherwise."""

    def test_data_uri_prefix_is_stripped(self, monkeypatch):
        captured = {}

        class FakeSolver:
            def __init__(self, key): pass
            def normal(self, src):
                captured["src"] = src
                return {"code": "AB12"}

        import sys, types
        mod = types.ModuleType("twocaptcha")
        mod.TwoCaptcha = FakeSolver
        monkeypatch.setitem(sys.modules, "twocaptcha", mod)

        ch = Challenge(type=IMAGE_CAPTCHA, image_b64="data:image/png;base64,IMGDATA")
        assert captcha._solve_sync(API_KEY, ch) == "AB12"
        assert captured["src"] == "IMGDATA"

    def test_a_plain_url_is_refused(self, monkeypatch):
        captured = {}

        class FakeSolver:
            def __init__(self, key): pass
            def normal(self, src):
                captured["src"] = src
                return {"code": "CD34"}

        import sys, types
        mod = types.ModuleType("twocaptcha")
        mod.TwoCaptcha = FakeSolver
        monkeypatch.setitem(sys.modules, "twocaptcha", mod)

        # The solver library would fetch any address (or upload a local file) named by the page.
        for src in ("https://x.test/c.png", "http://192.168.1.1/c.png", "C:/Users/ada/resume.pdf"):
            with pytest.raises(ValueError):
                captcha._solve_sync(API_KEY, Challenge(type=IMAGE_CAPTCHA, image_b64=src))
        assert captured == {}


# ── Solving ─────────────────────────────────────────────────────────────────
class TestSolve:
    async def test_missing_key_raises(self):
        with pytest.raises(CaptchaUnsolved) as e:
            await captcha.solve(Challenge(type=RECAPTCHA_V2, sitekey="k"), "")
        assert "API key" in e.value.reason

    async def test_missing_sitekey_raises(self):
        with pytest.raises(CaptchaUnsolved) as e:
            await captcha.solve(Challenge(type=RECAPTCHA_V2, sitekey=""), API_KEY)
        assert "site key" in e.value.reason

    async def test_empty_token_raises(self, monkeypatch):
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: "")
        with pytest.raises(CaptchaUnsolved):
            await captcha.solve(Challenge(type=RECAPTCHA_V2, sitekey="k"), API_KEY)

    async def test_returns_token_on_success(self, monkeypatch):
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: "TOKEN-123")
        assert await captcha.solve(Challenge(type=RECAPTCHA_V2, sitekey="k"), API_KEY) == "TOKEN-123"

    async def test_api_key_never_appears_in_the_error(self, monkeypatch):
        """SDK errors sometimes embed the request URL, which carries the key."""
        def boom(key, ch):
            raise RuntimeError(f"https://2captcha.com/in.php?key={API_KEY}&method=userrecaptcha")
        monkeypatch.setattr(captcha, "_solve_sync", boom)
        with pytest.raises(CaptchaUnsolved) as e:
            await captcha.solve(Challenge(type=RECAPTCHA_V2, sitekey="k"), API_KEY)
        assert API_KEY not in str(e.value)
        assert API_KEY not in e.value.reason


# ── handle(): the end-to-end policy ─────────────────────────────────────────
class TestHandle:
    async def test_invisible_hcaptcha_is_not_a_blocker(self, monkeypatch):
        """Oracle's invisible hCaptcha runs by itself on submit: no solving, no abandoning."""
        stub_evaluate(monkeypatch, {"type": captcha.HCAPTCHA_INVISIBLE, "sitekey": "k"})
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: pytest.fail("must not solve"))
        for enabled in (True, False):
            assert await captcha.handle(FakeSession(), {"captcha_solver_enabled": enabled, "captcha_api_key": API_KEY}) is False

    async def test_a_simple_human_check_is_ticked_with_real_clicks(self, monkeypatch):
        """Tick the box, press Continue, and carry on once the check page is gone."""
        page = {"passed": False}
        clicks = []

        async def fake_eval(session, expression):
            if expression is captcha._HUMAN_CHECK_TARGETS_JS:
                return {"checked": False, "box": [50, 120], "button": [60, 160]}
            if expression.strip().startswith("window.location.href"):
                return "https://soylent.example/check"
            return None if page["passed"] else {"type": captcha.HUMAN_CHECK, "sitekey": ""}

        async def fake_click(session, x, y):
            clicks.append((x, y))
            if (x, y) == (60, 160):
                page["passed"] = True

        monkeypatch.setattr(captcha, "_evaluate", fake_eval)
        monkeypatch.setattr(captcha, "_real_click", fake_click)
        monkeypatch.setattr(captcha.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: pytest.fail("no 2Captcha for a plain checkbox"))
        assert await captcha.handle(FakeSession(), {"captcha_solver_enabled": False}) is True
        assert clicks == [(50, 120), (60, 160)]

    async def test_a_human_check_that_wont_pass_needs_the_user(self, monkeypatch):
        clicks = []

        async def fake_eval(session, expression):
            if expression is captcha._HUMAN_CHECK_TARGETS_JS:
                return {"checked": False, "box": [50, 120], "button": [60, 160]}
            return {"type": captcha.HUMAN_CHECK, "sitekey": ""}

        async def fake_click(session, x, y):
            clicks.append((x, y))

        monkeypatch.setattr(captcha, "_evaluate", fake_eval)
        monkeypatch.setattr(captcha, "_real_click", fake_click)
        monkeypatch.setattr(captcha.asyncio, "sleep", _no_sleep)
        monkeypatch.setattr(captcha, "HUMAN_CHECK_WAIT_SECONDS", 0)
        session = FakeSession()
        for _ in range(captcha.HUMAN_CHECK_ATTEMPTS + 1):
            with pytest.raises(captcha.HumanCheckNeeded) as e:
                await captcha.handle(session, {"captcha_solver_enabled": True, "captcha_api_key": API_KEY})
        assert "Trial mode" in e.value.reason
        # Only HUMAN_CHECK_ATTEMPTS tries per application, then it stops clicking.
        assert len(clicks) == 2 * captcha.HUMAN_CHECK_ATTEMPTS

    def test_detection_reads_the_site_key_from_any_hcaptcha_frame(self):
        """The first frame is often the hidden checkbox-invisible one, without a key."""
        js = captcha._DETECT_JS
        assert "querySelectorAll('iframe[src*=\"hcaptcha.com\"]')" in js
        assert "[?&#]sitekey=" in js
        assert "hcaptcha_invisible" in js and "human_check" in js

    async def test_no_captcha_is_a_noop(self, monkeypatch):
        stub_evaluate(monkeypatch, None)
        assert await captcha.handle(FakeSession(), {"captcha_solver_enabled": True}) is False

    async def test_switch_off_abandons_on_a_blocking_challenge(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V2, "sitekey": "k"})
        with pytest.raises(CaptchaUnsolved) as e:
            await captcha.handle(FakeSession(), {"captcha_solver_enabled": False})
        assert "turned off" in e.value.reason

    async def test_switch_off_ignores_invisible_recaptcha_v3(self, monkeypatch):
        """v3 scores in the background and does not block submission."""
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V3, "sitekey": "k"})
        assert await captcha.handle(FakeSession(), {"captcha_solver_enabled": False}) is False

    async def test_solves_and_injects_when_enabled(self, monkeypatch):
        calls = stub_evaluate(monkeypatch, {"type": HCAPTCHA, "sitekey": "k"})
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: "TOK")
        ok = await captcha.handle(
            FakeSession(), {"captcha_solver_enabled": True, "captcha_api_key": API_KEY}
        )
        assert ok is True
        assert len(calls["inject"]) == 1

    async def test_gives_up_after_two_attempts(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": TURNSTILE, "sitekey": "k"})
        attempts = {"n": 0}

        def flaky(key, ch):
            attempts["n"] += 1
            raise RuntimeError("service busy")

        monkeypatch.setattr(captcha, "_solve_sync", flaky)
        with pytest.raises(CaptchaUnsolved):
            await captcha.handle(
                FakeSession(), {"captcha_solver_enabled": True, "captcha_api_key": API_KEY}
            )
        assert attempts["n"] == MAX_SOLVE_ATTEMPTS

    async def test_missing_key_does_not_burn_retries(self, monkeypatch):
        """A missing key will not fix itself on a second attempt."""
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V2, "sitekey": "k"})
        attempts = {"n": 0}

        def counter(key, ch):
            attempts["n"] += 1
            return "TOK"

        monkeypatch.setattr(captcha, "_solve_sync", counter)
        with pytest.raises(CaptchaUnsolved):
            await captcha.handle(
                FakeSession(), {"captcha_solver_enabled": True, "captcha_api_key": ""}
            )
        assert attempts["n"] == 0

    async def test_failed_injection_is_a_failure(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V2, "sitekey": "k"}, inject_result=False)
        monkeypatch.setattr(captcha, "_solve_sync", lambda key, ch: "TOK")
        with pytest.raises(CaptchaUnsolved) as e:
            await captcha.handle(
                FakeSession(), {"captcha_solver_enabled": True, "captcha_api_key": API_KEY}
            )
        assert "inject" in e.value.reason

    async def test_unsolvable_v3_still_does_not_abandon(self, monkeypatch):
        stub_evaluate(monkeypatch, {"type": RECAPTCHA_V3, "sitekey": "k"})

        def boom(key, ch):
            raise RuntimeError("busy")

        monkeypatch.setattr(captcha, "_solve_sync", boom)
        ok = await captcha.handle(
            FakeSession(), {"captcha_solver_enabled": True, "captcha_api_key": API_KEY}
        )
        assert ok is False


class TestPolicyConstants:
    def test_two_attempts_per_application(self):
        assert MAX_SOLVE_ATTEMPTS == 2

    @pytest.mark.parametrize("kind", [RECAPTCHA_V2, HCAPTCHA, TURNSTILE, IMAGE_CAPTCHA])
    def test_visible_challenges_are_blocking(self, kind):
        assert kind in BLOCKING_TYPES

    def test_invisible_v3_is_not_blocking(self):
        assert RECAPTCHA_V3 not in BLOCKING_TYPES


def test_human_check_js_has_no_stray_control_characters():
    """A \b written through a shell once became a backspace and the Continue button was never found."""
    js = captcha._HUMAN_CHECK_TARGETS_JS
    assert not any(ord(c) < 32 and c not in "\n\t" for c in js)
    assert "confirm)" + chr(92) + "b/i" in js
