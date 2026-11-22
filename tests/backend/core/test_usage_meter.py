"""The ChatGPT plan's usage meters: read from each reply, used to stop a run before the limit.

No test here makes a request to ChatGPT: the headers are made up, with the names the Codex CLI reads.
"""
import asyncio
import json
import time

import httpx
import pytest

from core import chatgpt_auth, chatgpt_llm, usage_meter
from tests.backend.core.test_chatgpt import _messages, _sse, _tokens


def headers(primary=None, secondary=None, primary_reset=None, secondary_reset=None, **more):
    now = time.time()
    found = {}
    if primary is not None:
        found.update({"x-codex-primary-used-percent": str(primary), "x-codex-primary-window-minutes": "300",
                      "x-codex-primary-reset-at": str(int(now + (3600 if primary_reset is None else primary_reset)))})
    if secondary is not None:
        found.update({"x-codex-secondary-used-percent": str(secondary), "x-codex-secondary-window-minutes": "10080",
                      "x-codex-secondary-reset-at": str(int(now + (86400 if secondary_reset is None else secondary_reset)))})
    found.update(more)
    return found


def replies(values, window="primary", session="job-1"):
    for value in values:
        usage_meter.note_reply(headers(**{window: value}), session)


def test_the_meters_are_read_from_a_replys_headers():
    read = usage_meter.read_headers(headers(primary=42.5, secondary=7))
    assert read["primary"]["used"] == 42.5 and read["secondary"]["used"] == 7
    assert 3500 < read["primary"]["resets_at"] - time.time() <= 3600
    assert read["primary"]["window_minutes"] == 300


def test_the_older_reset_after_seconds_header_is_read_too():
    read = usage_meter.read_headers({"x-codex-primary-used-percent": "10", "x-codex-primary-reset-after-seconds": "600"})
    assert 590 < read["primary"]["resets_at"] - time.time() <= 600


@pytest.mark.parametrize("bad", [{}, None, {"x-codex-primary-used-percent": "soon"},
                                 {"x-codex-primary-used-percent": "-3"}, {"x-codex-primary-used-percent": "nan"}])
def test_missing_or_unreadable_headers_are_no_meter(bad):
    assert usage_meter.read_headers(bad) == {}
    usage_meter.note_reply(bad)
    assert usage_meter.stop_reason() is None          # nothing stops a run without a meter


def test_plenty_of_room_lets_the_next_job_start():
    usage_meter.note_reply(headers(primary=30, secondary=10))
    assert usage_meter.stop_reason(running_jobs=3) is None


def test_no_job_starts_when_the_next_one_would_not_fit():
    """Before anything is measured a job is taken to need about 5% of the 5-hour limit."""
    usage_meter.note_reply(headers(primary=96, secondary=10, primary_reset=1800))
    stop = usage_meter.stop_reason()
    assert stop is not None and stop["window"] == "primary"
    assert "96% of the 5-hour limit is used" in stop["message"] and "resets in about 30 minutes" in stop["message"]


def test_control_just_under_the_default_cost_still_starts():
    usage_meter.note_reply(headers(primary=94, secondary=10))
    assert usage_meter.stop_reason() is None


def test_jobs_already_running_need_room_for_the_rest_of_theirs():
    usage_meter.note_reply(headers(primary=88, secondary=10))
    assert usage_meter.stop_reason(running_jobs=0) is None
    assert usage_meter.stop_reason(running_jobs=3) is not None      # 5% + 3 half jobs = 12.5% > 12% left


def test_the_weekly_limit_stops_a_run_too():
    usage_meter.note_reply(headers(primary=20, secondary=99.5, secondary_reset=7200))
    stop = usage_meter.stop_reason()
    assert stop["window"] == "secondary" and "weekly limit" in stop["message"] and "2 hours" in stop["message"]


def test_a_job_costs_what_its_requests_moved_the_meter():
    """60 replies that move the meter by 6 points in all: one request costs 0.1%, a 50-request job 5%."""
    usage_meter.note_reply(headers(primary=10), "job-1")
    replies([10 + (i // 10) for i in range(1, 61)])                 # whole percents: most replies show no movement
    for _ in range(49):
        usage_meter.note_reply({}, "job-2")
    usage_meter.note_reply(headers(primary=16), "job-2")
    usage_meter.job_finished("job-2")                                # 50 requests in that job
    assert usage_meter.job_percent("primary") == pytest.approx(0.1 * 50, rel=0.15)


def test_a_jump_from_the_users_own_chatgpt_use_is_not_this_apps_cost():
    usage_meter.note_reply(headers(primary=10))
    steady = [10 + (i // 10) for i in range(1, 61)]
    replies(steady)
    without_jump = usage_meter.job_percent("primary")
    replies([steady[-1] + 25])                                        # somebody used the plan elsewhere
    replies([steady[-1] + 25 + (i // 10) for i in range(1, 21)])
    assert usage_meter.job_percent("primary") == pytest.approx(without_jump, rel=0.1)


def test_control_a_jump_counted_as_cost_would_triple_it():
    """The control for the test above: the same readings without the outside-use rule."""
    usage_meter.OUTSIDE_USE_FACTOR, kept = 10_000.0, usage_meter.OUTSIDE_USE_FACTOR
    try:
        usage_meter.note_reply(headers(primary=10))
        steady = [10 + (i // 10) for i in range(1, 61)]
        replies(steady)
        without_jump = usage_meter.job_percent("primary")
        replies([steady[-1] + 25])
        assert usage_meter.job_percent("primary") > 2.5 * without_jump
    finally:
        usage_meter.OUTSIDE_USE_FACTOR = kept


def test_a_window_that_reset_since_the_last_reply_stops_nothing():
    usage_meter.note_reply(headers(primary=99, primary_reset=-5))
    assert usage_meter.stop_reason() is None


def test_limit_reached_blocks_until_it_resets_even_without_headers():
    usage_meter.limit_reached(1800)
    stop = usage_meter.stop_reason()
    assert usage_meter.blocked() and stop["message"] == "ChatGPT limit reached, resets in about 30 minutes"
    assert 1790 < stop["resets_in"] <= 1800


def test_a_reply_that_gets_through_again_lifts_the_block():
    usage_meter.limit_reached(1800)
    usage_meter.note_reply(headers(primary=3, secondary=40))
    assert not usage_meter.blocked() and usage_meter.stop_reason() is None


def test_what_was_measured_is_kept_for_the_next_run(data_dir):
    usage_meter._state["loaded"] = False
    usage_meter.note_reply(headers(primary=10), "job-1")
    replies([10 + (i // 5) for i in range(1, 61)])                   # 0.2% a request
    for _ in range(40):
        usage_meter.note_reply({}, "job-9")
    usage_meter.job_finished("job-9")
    measured = usage_meter.job_percent("primary")
    saved = json.loads((data_dir / "usage_meter.json").read_text(encoding="utf-8"))
    assert saved["requests_per_job"] == 40 and saved["per_request"]["primary"] == pytest.approx(0.2, rel=0.1)
    usage_meter.reset()                                               # a new run of the app
    assert usage_meter.job_percent("primary") == pytest.approx(measured, rel=0.05)


@pytest.mark.parametrize("text,expected", [
    ("Your ChatGPT plan's usage limit is reached. It resets in about 220 minutes. (The usage limit has been reached)", True),
    ("usage_limit_reached", True), ("The site took too long to respond.", False), ("", False)])
def test_limit_errors_are_recognised_by_their_text(text, expected):
    assert usage_meter.is_limit_error(text) is expected


# ── the ChatGPT model object feeds the meter ───────────────────────────────────
@pytest.fixture
def codex(data_dir, monkeypatch):
    chatgpt_auth._save(_tokens())
    captured = {"reply": None}
    # Loaded before httpx.AsyncClient is swapped: these modules subclass it when first imported.
    import browser_use.llm.exceptions  # noqa: F401
    import browser_use.llm.openai.responses_serializer  # noqa: F401
    import browser_use.llm.views  # noqa: F401
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request):
        status, content, reply_headers = captured["reply"]
        return httpx.Response(status, content=content, headers=reply_headers)

    monkeypatch.setattr(chatgpt_llm.httpx, "AsyncClient",
                        lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    return captured


def test_every_reply_updates_the_meter_without_another_request(codex):
    codex["reply"] = (200, _sse({"type": "response.output_text.delta", "delta": "ok"},
                                {"type": "response.completed", "response": {}}), headers(primary=61, secondary=12))
    llm = chatgpt_llm.ChatChatGPT()
    asyncio.run(llm.ainvoke(_messages()))
    seen = usage_meter.snapshot()
    assert seen["meters"]["primary"]["used"] == 61 and seen["meters"]["secondary"]["name"] == "weekly"
    assert seen["requests"] == 1 and usage_meter._state["by_session"][llm.session_id] == 1


def test_a_refused_request_blocks_the_run_until_the_reset_it_names(codex):
    from browser_use.llm.exceptions import ModelRateLimitError
    codex["reply"] = (429, json.dumps({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached",
                                                 "resets_in_seconds": 13200}}).encode(), {})
    with pytest.raises(ModelRateLimitError):
        asyncio.run(chatgpt_llm.ChatChatGPT().ainvoke(_messages()))
    stop = usage_meter.stop_reason()
    assert usage_meter.blocked() and "resets in about 4 hours" in stop["message"]
