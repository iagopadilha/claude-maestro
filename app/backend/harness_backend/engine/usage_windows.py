#!/usr/bin/env python3
"""The 5-hour and weekly usage windows of a Claude account, read from Claude Code's own output (standard library only).

Used by the engine (`run_loop`, imported as a script sibling, like `claude_binaries`) and by the app (the panel and the call log read
what the engine wrote), so every place names the windows the same way.

Source: `claude -p ... --output-format stream-json --verbose` prints a `rate_limit_event`; `cli_output.parse(...)["rate_limit"]` is its
normalised form (see the cli_output docstring):
  {"status", "limit_type", "overage_status", "reset_at", "windows": {"five_hour": {"pct": 60.0, "reset_at": 1792702800}, "seven_day": {...}}}
`pct` is 0-100 and `reset_at` is epoch seconds. Nothing here calls the network or reads a credential.

Blocking window: the window that stopped the account. A window with `pct >= 100` (and a reset still ahead) blocks; when several are
full the one that frees up LAST blocks (the account is usable only when all of them reset). A rejected status with no full window
blocks through its `limit_type`.
"""
import datetime

FIVE_HOUR = "five_hour"
SEVEN_DAY = "seven_day"
FULL_PERCENT = 100.0
WINDOW_NAMES = {FIVE_HOUR: "5-hour", SEVEN_DAY: "weekly"}
SHORT_NAMES = {FIVE_HOUR: "5h", SEVEN_DAY: "weekly"}
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")  # fixed English names: no dependence on the machine's locale


def windows_of(rate_limit):
    """{key: {"pct", "reset_at"}} of the parse result's `rate_limit` ({} when it has none)."""
    windows = (rate_limit or {}).get("windows")
    return {key: dict(window) for key, window in windows.items() if isinstance(window, dict)} if isinstance(windows, dict) else {}


def moment_of(epoch):
    """Local naive datetime of an epoch-seconds value; None when it is not a usable number."""
    try:
        return datetime.datetime.fromtimestamp(float(epoch)) if epoch is not None and not isinstance(epoch, bool) else None
    except (OverflowError, OSError, ValueError):
        return None


def with_local_reset(windows):
    """Copy of the windows with `reset_local` (local ISO text of `reset_at`, '' when unknown) added to each: what the turn log keeps."""
    result = {}
    for key, window in (windows or {}).items():
        moment = moment_of(window.get("reset_at"))
        result[key] = {"pct": window.get("pct"), "reset_at": window.get("reset_at"),
                       "reset_local": moment.isoformat(timespec="seconds") if moment else ""}
    return result


def is_rejected(status):
    """True when the rate limit status says the call was refused (`rejected`); `allowed` and `allowed_warning` are not."""
    return str(status or "").strip().lower().startswith("reject")


def blocking_window(rate_limit, now=None):
    """The window that stops the account right now: {"key", "pct", "reset_at"} (`reset_at` may be None) or None when nothing blocks.
    A full window whose reset already passed is stale and does not block."""
    now = now or datetime.datetime.now()
    windows = windows_of(rate_limit)
    full = []
    for key, window in windows.items():
        percent = window.get("pct")
        reset = moment_of(window.get("reset_at"))
        if isinstance(percent, (int, float)) and percent >= FULL_PERCENT and not (reset and reset <= now):
            full.append({"key": key, "pct": float(percent), "reset_at": window.get("reset_at")})
    if full:
        return max(full, key=lambda item: item["reset_at"] if item["reset_at"] is not None else 0)
    if is_rejected((rate_limit or {}).get("status")):
        key = str(rate_limit.get("limit_type") or "")
        window = windows.get(key) or {}
        reset_at = window.get("reset_at") if window.get("reset_at") is not None else rate_limit.get("reset_at")
        reset = moment_of(reset_at)
        if key and not (reset and reset <= now):
            percent = window.get("pct")
            return {"key": key, "pct": float(percent) if isinstance(percent, (int, float)) else FULL_PERCENT, "reset_at": reset_at}
    return None


def is_limited(rate_limit, now=None):
    """True when the output says the account is out of usage (a blocking window exists)."""
    return blocking_window(rate_limit, now) is not None


def reset_moment(blocking):
    """Local datetime when the blocking window frees up; None when unknown."""
    return moment_of((blocking or {}).get("reset_at"))


def window_name(key):
    """`five_hour` -> "5-hour", `seven_day` -> "weekly", another key -> its words ("seven_day_opus" -> "seven day opus")."""
    return WINDOW_NAMES.get(key) or str(key or "usage").replace("_", " ")


def format_moment(moment):
    """`Mon 26/10 14:00` (fixed English weekday)."""
    return "%s %s" % (WEEKDAYS[moment.weekday()], moment.strftime("%d/%m %H:%M"))


def phrase(blocking):
    """What a notice says about the blocking window: "5-hour window, back at 17:05", "weekly window, back on Mon 26/10 14:00";
    "weekly window" alone when the reset is unknown; '' when nothing blocks."""
    if not blocking:
        return ""
    moment = reset_moment(blocking)
    name = "%s window" % window_name(blocking.get("key"))
    if not moment:
        return name
    if blocking.get("key") == FIVE_HOUR:
        return "%s, back at %s" % (name, moment.strftime("%H:%M"))
    return "%s, back on %s" % (name, format_moment(moment))


def is_weekly_block(blocking):
    return bool(blocking) and blocking.get("key") == SEVEN_DAY


def describe_windows(windows):
    """Short text of known windows: "5h 60% (back 17:05), weekly 33% (back Mon 26/10 14:00)"; "usage unknown" when there are none."""
    parts = []
    for key in sorted(windows or {}, key=lambda item: (item != FIVE_HOUR, item != SEVEN_DAY, item)):
        window = windows[key]
        percent = window.get("pct")
        text = "%s %s%%" % (SHORT_NAMES.get(key) or window_name(key), ("%g" % round(percent)) if isinstance(percent, (int, float)) else "?")
        moment = moment_of(window.get("reset_at"))
        if moment:
            text += " (back %s)" % (moment.strftime("%H:%M") if key == FIVE_HOUR else format_moment(moment))
        parts.append(text)
    return ", ".join(parts) or "usage unknown"
