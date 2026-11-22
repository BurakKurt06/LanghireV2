"""The picture of a job's last page, the CAPTCHA solver's plain-word errors, English pages, log trimming.

All local: made-up pictures, a stand-in for the solver's API, throwaway profiles and databases.
"""
import base64
import json
import os
import time

import pytest

from core import captcha, job_screenshots, net_guard
from memory.metrics import MetricsStore

JPEG = b"\xff\xd8\xff\xe0" + b"made-up picture" * 30
URL = "https://jobs.example/apply/1"


# ── one picture per job ────────────────────────────────────────────────────────
def test_a_picture_is_kept_under_a_name_that_says_nothing_about_the_job(data_dir):
    assert job_screenshots.save(URL, JPEG) and job_screenshots.exists(URL)
    path = job_screenshots.path_for(URL)
    assert path.parent == data_dir / "job_screenshots" and path.read_bytes() == JPEG
    assert "jobs.example" not in path.name and path.suffix == ".jpg" and len(path.stem) == 24


def test_the_next_try_replaces_it_and_removal_deletes_it(data_dir):
    job_screenshots.save(URL, JPEG)
    job_screenshots.save(URL, JPEG + b"second try")
    assert len(list((data_dir / "job_screenshots").glob("*.jpg"))) == 1
    assert job_screenshots.path_for(URL).read_bytes().endswith(b"second try")
    job_screenshots.remove(URL)
    assert not job_screenshots.exists(URL)
    job_screenshots.remove(URL)                               # removing twice is fine


@pytest.mark.parametrize("picture", [None, b"", b"not a picture", b"\xff\xd8" + b"x" * (job_screenshots.MAX_BYTES + 1)],
                         ids=["none", "empty", "not-a-picture", "too-big"])
def test_what_is_not_a_reasonable_picture_is_not_kept(data_dir, picture):
    assert not job_screenshots.save(URL, picture) and not job_screenshots.exists(URL)


def test_only_the_newest_pictures_are_kept(data_dir, monkeypatch):
    monkeypatch.setattr(job_screenshots, "MAX_KEPT", 5)
    for number in range(8):
        url = f"https://jobs.example/apply/{number}"
        job_screenshots.save(url, JPEG)
        past = time.time() - (100 - number)                   # each newer than the one before
        os.utime(job_screenshots.path_for(url), (past, past))
    job_screenshots.save("https://jobs.example/apply/newest", JPEG)
    kept = {p.name for p in (data_dir / "job_screenshots").glob("*.jpg")}
    assert len(kept) == 5
    assert job_screenshots.path_for("https://jobs.example/apply/newest").name in kept
    assert job_screenshots.path_for("https://jobs.example/apply/0").name not in kept        # the oldest went first


def test_the_step_picture_browser_use_already_took_becomes_a_jpeg():
    import fitz
    png = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 40, 30), False)
    png.clear_with(200)
    as_text = base64.b64encode(png.tobytes("png")).decode()
    converted = job_screenshots.as_jpeg(as_text)
    assert converted and converted.startswith(b"\xff\xd8")
    assert job_screenshots.as_jpeg(JPEG) == JPEG
    assert job_screenshots.as_jpeg("not base64 at all!") is None and job_screenshots.as_jpeg(None) is None


async def test_a_page_that_does_not_answer_gives_no_picture_and_no_error():
    class Frozen:
        async def get_or_create_cdp_session(self):
            import asyncio
            await asyncio.sleep(30)

    started = time.monotonic()
    assert await job_screenshots.capture(Frozen(), seconds=0.2) is None
    assert time.monotonic() - started < 3


async def test_the_browser_takes_the_picture_itself():
    class Send:
        class Page:
            @staticmethod
            async def captureScreenshot(params, session_id):
                assert params == {"format": "jpeg", "quality": job_screenshots.JPEG_QUALITY}
                return {"data": base64.b64encode(JPEG).decode()}

    class Session:
        async def get_or_create_cdp_session(self):
            from types import SimpleNamespace
            return SimpleNamespace(cdp_client=SimpleNamespace(send=Send), session_id="s")

    assert await job_screenshots.capture(Session()) == JPEG


# ── the solver's own error words ───────────────────────────────────────────────
def _solver_answers(monkeypatch, answer):
    def solve(api_key, challenge):
        raise captcha.SolverRejected(captcha._solver_error_code(answer))
    monkeypatch.setattr(captcha, "_solve_sync", solve)


@pytest.mark.parametrize("answer,words,retry", [
    ("ERROR_ZERO_BALANCE", "your 2Captcha balance is empty", False),
    ("ERROR_WRONG_USER_KEY", "doesn't accept the saved API key", False),
    ("ERROR_CAPTCHA_UNSOLVABLE", "workers couldn't solve this one", True),
    ("ERROR_NO_SLOT_AVAILABLE", "too busy right now", True),
    ("ERROR_SOMETHING_NEW", "2Captcha refused it (ERROR_SOMETHING_NEW)", False),
])
async def test_a_solver_error_is_said_in_plain_words(monkeypatch, answer, words, retry):
    """It used to read "2Captcha error (ValueError)", which told the user nothing."""
    _solver_answers(monkeypatch, answer)
    with pytest.raises(captcha.CaptchaUnsolved) as refused:
        await captcha.solve(captcha.Challenge(type=captcha.HCAPTCHA, sitekey="made-up", page_url="https://jobs.example"),
                            "made-up-key")
    assert words in refused.value.reason and "ValueError" not in refused.value.reason
    assert refused.value.retry is retry


def test_the_error_code_is_only_ever_the_solvers_own_fixed_word():
    assert captcha._solver_error_code("ERROR_ZERO_BALANCE") == "ERROR_ZERO_BALANCE"
    assert captcha._solver_error_code("<html>ERROR key=abc123 something</html>") == "ERROR"      # nothing copied from it
    assert captcha._solver_error_code("IP_BANNED") == "IP_BANNED"


async def test_an_empty_balance_is_not_bought_twice(monkeypatch):
    calls = []

    async def detect(session):
        return captcha.Challenge(type=captcha.HCAPTCHA, sitekey="made-up", page_url="https://jobs.example/apply")

    async def no(session):
        return False

    def solve(api_key, challenge):
        calls.append(1)
        raise captcha.SolverRejected("ERROR_ZERO_BALANCE")

    monkeypatch.setattr(captcha, "detect", detect)
    monkeypatch.setattr(captcha, "answered", no)
    monkeypatch.setattr(captcha, "_solve_sync", solve)

    class Session:
        pass

    with pytest.raises(captcha.CaptchaUnsolved, match="balance is empty"):
        await captcha.handle(Session(), {"captcha_api_key": "made-up-key"})
    assert len(calls) == 1


def test_the_balance_is_read_with_one_free_request(monkeypatch):
    asked = []

    def res(self, **params):
        asked.append(params)
        return "3.417"

    monkeypatch.setattr(captcha._SolverAPI, "res", res)
    assert captcha.balance("made-up-key") == 3.417
    assert asked == [{"key": "made-up-key", "action": "getbalance"}]
    assert captcha.balance("") is None and len(asked) == 1


def test_a_refused_key_and_an_unreachable_solver_are_told_apart(monkeypatch):
    def refuse(self, **params):
        raise captcha.SolverRejected("ERROR_WRONG_USER_KEY")

    monkeypatch.setattr(captcha._SolverAPI, "res", refuse)
    with pytest.raises(captcha.SolverRejected):
        captcha.balance("made-up-key")

    def offline(self, **params):
        raise OSError("no network")

    monkeypatch.setattr(captcha._SolverAPI, "res", offline)
    assert captcha.balance("made-up-key") is None


def test_the_solver_is_told_this_browsers_user_agent_and_that_the_widget_is_invisible(monkeypatch):
    sent = {}

    class Solver:
        def __init__(self, key):
            pass

        def hcaptcha(self, **params):
            sent.update(params)
            return {"code": "made-up-token"}

    import twocaptcha
    monkeypatch.setattr(twocaptcha, "TwoCaptcha", Solver)
    challenge = captcha.Challenge(type=captcha.HCAPTCHA_INVISIBLE, sitekey="made-up", page_url="https://jobs.example/apply",
                                  user_agent="Mozilla/5.0 (made up)")
    assert captcha._solve_sync("made-up-key", challenge) == "made-up-token"
    assert sent == {"sitekey": "made-up", "url": "https://jobs.example/apply", "userAgent": "Mozilla/5.0 (made up)",
                    "invisible": 1}


def test_the_answer_is_handed_to_the_page_the_way_hcaptcha_would():
    waiting = captcha._inject_js(captcha.Challenge(type=captcha.HCAPTCHA, sitekey="k", open=True), "tok'en")
    armed = captcha._inject_js(captcha.Challenge(type=captcha.HCAPTCHA_INVISIBLE, sitekey="k"), "token")
    assert '[name="h-captcha-response"]' in waiting and "tok\\'en" in waiting
    assert "if (true) {" in waiting and "if (false) {" in armed          # now, or at the page's next execute()
    assert "window.__lhCaptcha" in armed and "hc.execute = function" in armed
    # reCAPTCHA and Turnstile answers are written as before, without the hCaptcha handover.
    assert "__lhCaptcha" not in captcha._inject_js(captcha.Challenge(type=captcha.RECAPTCHA_V2, sitekey="k"), "token")


def test_the_page_hook_only_records_and_passes_calls_through():
    assert "render.apply(this, arguments)" in captcha.HOOK_JS and "st.callbacks.push(fn)" in captcha.HOOK_JS
    assert "if (window.__lhCaptcha) return;" in captcha.HOOK_JS


# ── English pages ──────────────────────────────────────────────────────────────
def test_the_application_browser_asks_for_english(tmp_path):
    profile = tmp_path / "browser_profile"
    prefs = profile / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text(json.dumps({"intl": {"accept_languages": "tr-TR,tr"}, "kept": {"setting": 1}}), encoding="utf-8")
    assert net_guard.prefer_english(profile) is True
    saved = json.loads(prefs.read_text(encoding="utf-8"))
    assert saved["intl"] == {"accept_languages": "en-US,en", "selected_languages": "en-US,en"}
    assert saved["kept"] == {"setting": 1}                                 # nothing else in the profile changes
    assert net_guard.prefer_english(profile) is False                      # already English: not rewritten


def test_a_new_profile_gets_english_from_the_start(tmp_path):
    assert net_guard.prefer_english(tmp_path / "fresh") is True
    saved = json.loads((tmp_path / "fresh" / "Default" / "Preferences").read_text(encoding="utf-8"))
    assert saved["intl"]["accept_languages"] == "en-US,en"


def test_an_unreadable_profile_never_stops_a_launch(tmp_path):
    prefs = tmp_path / "broken" / "Default" / "Preferences"
    prefs.parent.mkdir(parents=True)
    prefs.write_text("{not json", encoding="utf-8")
    assert net_guard.prefer_english(tmp_path / "broken") is False


def test_english_is_asked_for_by_the_application_browser_only(monkeypatch, data_dir):
    from core import shared_config
    monkeypatch.setattr(shared_config, "load_runtime_settings", lambda: {})
    monkeypatch.setattr(shared_config, "selected_browser", lambda s=None: ("", data_dir / "browser_profile"))
    for purpose, english in (("application", True), ("agent", True), ("mail", False), ("inbox", False)):
        args = shared_config.build_browser_kwargs(purpose=purpose)["args"]
        assert ("--lang=en-US" in args and "--accept-lang=en-US,en" in args) is english, purpose


# ── run logs stay bounded ──────────────────────────────────────────────────────
def test_only_the_newest_log_lines_are_kept(tmp_path):
    store = MetricsStore(tmp_path / "m.db")
    conn = store._get_conn()
    store.log_batch([("run-1", "https://jobs.example/1", "INFO", f"line {n}") for n in range(120)])
    assert store.trim_logs(keep=50) == 70
    left = [row[0] for row in conn.execute("SELECT message FROM run_logs ORDER BY id")]
    assert left[0] == "line 70" and left[-1] == "line 119" and len(left) == 50
    assert store.trim_logs(keep=50) == 0                                   # nothing more to trim


def test_control_a_small_log_is_left_alone(tmp_path):
    store = MetricsStore(tmp_path / "m.db")
    conn = store._get_conn()
    store.log("run-1", "only line")
    assert store.trim_logs() == 0
    assert conn.execute("SELECT COUNT(*) FROM run_logs").fetchone()[0] == 1
