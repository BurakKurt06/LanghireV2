"""The ChatGPT plan's usage meters, read from the replies the app already gets (no extra requests).

Every reply from the Codex backend carries how much of the plan's two limits is used: the short
window (about 5 hours) and the weekly one, each as a percentage with its reset time (the header
names are the Codex CLI's: x-codex-primary-used-percent, x-codex-primary-reset-at, and the same
with "secondary"). A run reads them to stop BEFORE the limit:

* no new job starts when the next one wouldn't fit in what is left;
* jobs already running finish;
* a job the limit cut off goes back to ready (cli/apply_jobs), never to Failed.

The meter counts everything on the plan: the user's own ChatGPT and Codex use too. So what one
request of this app costs is measured from how the meter moves between this app's replies, and a
jump far above the usual one (somebody else using the plan meanwhile) is left out of that
measure. The meter moves in whole percents, so most replies show no movement at all: the cost of
one request is the average movement, not the middle value (which would be zero).

Without the headers (another provider, or a backend that stops sending them) nothing here stops
a run early; the first "limit reached" reply then stops it, and the jobs not started stay ready.
"""
from __future__ import annotations

import json
import threading
import time

from .config import get_data_dir

WINDOWS = ("primary", "secondary")
# Until this app has measured its own jobs: one job is taken to cost this much of a window.
DEFAULT_JOB_PERCENT = {"primary": 5.0, "secondary": 1.0}
DEFAULT_REQUESTS_PER_JOB = 60
MIN_REQUESTS_MEASURED = 30          # replies seen before the measured cost replaces the default
MAX_SAMPLES = 400
# A single reply moving the meter by more than this many times the usual movement (and by more than
# OUTSIDE_USE_MIN_POINTS) is somebody else using the plan: not this app's cost.
OUTSIDE_USE_FACTOR = 8.0
OUTSIDE_USE_MIN_POINTS = 2.0
# A running job is, on average, half done: it still needs about half a job's worth.
RUNNING_JOB_SHARE = 0.5
# When the limit resets this soon, a run waits for it instead of stopping.
WAIT_FOR_RESET_SECONDS = 15 * 60

_lock = threading.Lock()
_state: dict = {}


def reset() -> None:
    """Forget everything measured in this process (tests, and a new sign-in)."""
    with _lock:
        _state.clear()
        _state.update({"meters": {}, "moves": {w: [] for w in WINDOWS}, "requests": 0, "by_session": {},
                       "job_requests": [], "blocked_until": 0.0, "blocked_reason": "", "loaded": False})


reset()


def _path():
    return get_data_dir() / "usage_meter.json"


def _load_saved() -> None:
    """What earlier runs measured (cost of one request per window, requests per job)."""
    if _state["loaded"]:
        return
    _state["loaded"] = True
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(saved, dict):
        return
    for window in WINDOWS:
        value = (saved.get("per_request") or {}).get(window)
        if isinstance(value, (int, float)) and 0 < value < 50:
            _state.setdefault("saved_per_request", {})[window] = float(value)
    jobs = saved.get("requests_per_job")
    if isinstance(jobs, (int, float)) and 1 <= jobs <= 2000:
        _state["saved_requests_per_job"] = float(jobs)
    # The last readings and a limit that was reached, while they still hold: closing and opening
    # the app doesn't make it forget that the plan is used up.
    now = time.time()
    until = saved.get("blocked_until")
    if isinstance(until, (int, float)) and now < until < now + 8 * 86400 and not _state["blocked_until"]:
        _state["blocked_until"], _state["blocked_reason"] = float(until), "limit"
    for window, meter in (saved.get("meters") or {}).items() if isinstance(saved.get("meters"), dict) else ():
        if (window in WINDOWS and window not in _state["meters"] and isinstance(meter, dict)
                and isinstance(meter.get("used"), (int, float)) and isinstance(meter.get("resets_at"), (int, float))
                and meter["resets_at"] > now):
            _state["meters"][window] = {"used": float(meter["used"]), "resets_at": float(meter["resets_at"]),
                                        "window_minutes": meter.get("window_minutes"), "seen_at": meter.get("seen_at")}


def _save() -> None:
    try:
        data = {"per_request": {w: round(v, 5) for w in WINDOWS if (v := _per_request(w)) is not None},
                "requests_per_job": round(_requests_per_job(), 1), "updated": int(time.time()),
                "blocked_until": _state["blocked_until"] or None,
                "meters": {w: {k: m.get(k) for k in ("used", "resets_at", "window_minutes", "seen_at")}
                           for w, m in _state["meters"].items()}}
        path = _path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def _number(value) -> float | None:
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number and abs(number) != float("inf") else None


def read_headers(headers) -> dict:
    """{"primary": {"used", "resets_at", "window_minutes"}, "secondary": {...}} from a reply's headers
    (only the windows the reply reports). `resets_at` is a Unix time."""
    found: dict = {}
    if headers is None:
        return found
    now = time.time()
    for window in WINDOWS:
        used = _number(headers.get(f"x-codex-{window}-used-percent"))
        if used is None or not 0 <= used <= 1000:
            continue
        meter = {"used": min(used, 100.0), "resets_at": None, "window_minutes": None}
        at = _number(headers.get(f"x-codex-{window}-reset-at"))
        after = _number(headers.get(f"x-codex-{window}-reset-after-seconds"))
        if at is not None and at > 0:
            meter["resets_at"] = at
        elif after is not None and after >= 0:
            meter["resets_at"] = now + after
        minutes = _number(headers.get(f"x-codex-{window}-window-minutes"))
        if minutes is not None and minutes > 0:
            meter["window_minutes"] = minutes
        found[window] = meter
    return found


def note_reply(headers, session_id: str = "") -> None:
    """One reply arrived: remember its meters and how far they moved since the reply before."""
    meters = read_headers(headers)
    lifted = False
    with _lock:
        _load_saved()
        _state["requests"] += 1
        if session_id:
            _state["by_session"][session_id] = _state["by_session"].get(session_id, 0) + 1
            if len(_state["by_session"]) > 200:
                for key in list(_state["by_session"])[:100]:
                    _state["by_session"].pop(key, None)
        if not meters:
            return
        for window, meter in meters.items():
            before = _state["meters"].get(window)
            if before is not None and meter["used"] >= before["used"] and (
                    not before.get("resets_at") or not meter.get("resets_at")
                    or abs(meter["resets_at"] - before["resets_at"]) < 120):
                moves = _state["moves"][window]
                moves.append(meter["used"] - before["used"])
                del moves[:-MAX_SAMPLES]
            meter["seen_at"] = time.time()
            _state["meters"][window] = meter
        if _state["blocked_until"] and all(m["used"] < 99.5 for m in meters.values()):
            _state["blocked_until"], _state["blocked_reason"] = 0.0, ""     # a reply got through: not blocked
            lifted = True
    if lifted:
        _save()


def limit_reached(resets_in_seconds=None) -> None:
    """The backend refused a request because the plan's limit is used up."""
    seconds = _number(resets_in_seconds)
    with _lock:
        if seconds is None or seconds < 0:
            resets = [m["resets_at"] for m in _state["meters"].values()
                      if m.get("resets_at") and m["used"] >= 99.5]
            until = min(resets) if resets else time.time() + 3600
        else:
            until = time.time() + seconds
        _state["blocked_until"] = until
        _state["blocked_reason"] = "limit"
    _save()


def blocked() -> bool:
    """A "limit reached" reply came, and the limit hasn't reset since."""
    with _lock:
        return _state["blocked_until"] > time.time()


def _per_request(window: str) -> float | None:
    """What one request of this app moves `window`'s meter by (percent), or None when not measured yet."""
    moves = _state["moves"][window]
    if len(moves) < MIN_REQUESTS_MEASURED:
        return (_state.get("saved_per_request") or {}).get(window)
    usual = sum(moves) / len(moves)
    own = [m for m in moves if not (m > OUTSIDE_USE_MIN_POINTS and m > OUTSIDE_USE_FACTOR * max(usual, 0.05))]
    return sum(own) / len(own) if own else None


def _requests_per_job() -> float:
    counts = sorted(_state["job_requests"][-60:])
    if counts:
        return float(counts[len(counts) // 2])          # the middle job: one runaway job doesn't count
    return float(_state.get("saved_requests_per_job") or DEFAULT_REQUESTS_PER_JOB)


def job_percent(window: str) -> float:
    """What one whole job is expected to use of `window` (percent)."""
    with _lock:
        _load_saved()
        each = _per_request(window)
        if each is None:
            return DEFAULT_JOB_PERCENT[window]
        return max(0.2, each * _requests_per_job())


def job_finished(session_id: str) -> None:
    """A job ended: how many requests it took goes into the measure of what a job costs."""
    with _lock:
        count = _state["by_session"].pop(session_id, 0)
        if count >= 5:                      # a job that ended at once says nothing about a whole one
            _state["job_requests"].append(count)
            del _state["job_requests"][:-200]
    _save()


def _name(window: str, meter: dict) -> str:
    minutes = meter.get("window_minutes")
    if minutes:
        if minutes >= 6 * 24 * 60:
            return "weekly"
        if minutes >= 24 * 60:
            return f"{round(minutes / 1440)}-day"
        return f"{max(1, round(minutes / 60))}-hour"
    return "5-hour" if window == "primary" else "weekly"


def resets_in_seconds() -> float | None:
    """Seconds until the limit that stops the run resets (None when unknown)."""
    stop = stop_reason()
    return stop["resets_in"] if stop else None


def stop_reason(running_jobs: int = 0) -> dict | None:
    """Why no further job should start now: {"message", "resets_in", "window"}; None to go on.

    `running_jobs`: jobs in progress right now, which still need room for what is left of them.
    """
    now = time.time()
    with _lock:
        _load_saved()
        if _state["blocked_until"] > now:
            seconds = _state["blocked_until"] - now
            return {"message": f"ChatGPT limit reached, resets in about {_minutes(seconds)}", "resets_in": seconds,
                    "window": "limit"}
        tight = []
        for window in WINDOWS:
            meter = _state["meters"].get(window)
            if meter is None:
                continue
            if meter.get("resets_at") and meter["resets_at"] <= now:
                continue                    # that window has reset since this reading
            each = _per_request(window)
            job = DEFAULT_JOB_PERCENT[window] if each is None else max(0.2, each * _requests_per_job())
            needed = job * (1 + RUNNING_JOB_SHARE * max(0, running_jobs))
            left = 100.0 - meter["used"]
            if left < needed:
                seconds = max(0.0, meter["resets_at"] - now) if meter.get("resets_at") else None
                tight.append((window, meter, seconds, left, job))
        if not tight:
            return None
        # The one that resets last decides how long the run has to wait.
        window, meter, seconds, left, job = max(tight, key=lambda t: t[2] if t[2] is not None else float("inf"))
        when = f", resets in about {_minutes(seconds)}" if seconds is not None else ""
        return {"message": (f"ChatGPT limit: {meter['used']:.0f}% of the {_name(window, meter)} limit is used and "
                            f"the next job needs about {job:.0f}%{when}"),
                "resets_in": seconds, "window": window}


def resets_soon(stop: dict | None) -> bool:
    """The limit behind `stop` (a stop_reason) resets within minutes: a run waits for it."""
    return bool(stop) and stop.get("resets_in") is not None and stop["resets_in"] <= WAIT_FOR_RESET_SECONDS


def _minutes(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    if minutes >= 120:
        return f"{round(minutes / 60)} hours"
    return f"{minutes} minute" + ("" if minutes == 1 else "s")


def snapshot() -> dict:
    """For the run panel and the start check: the meters as last read."""
    now = time.time()
    with _lock:
        meters = {}
        for window, meter in _state["meters"].items():
            fresh = not meter.get("resets_at") or meter["resets_at"] > now
            meters[window] = {"used": meter["used"] if fresh else 0.0, "name": _name(window, meter),
                              "resets_in": max(0.0, meter["resets_at"] - now) if meter.get("resets_at") and fresh else None,
                              "seen_at": meter.get("seen_at")}
        return {"meters": meters, "blocked": _state["blocked_until"] > now, "requests": _state["requests"]}


def is_limit_error(text: str) -> bool:
    """The error text of a request the plan's limit refused."""
    low = (text or "").lower()
    return "usage limit is reached" in low or "usage_limit_reached" in low or "usage limit has been reached" in low
