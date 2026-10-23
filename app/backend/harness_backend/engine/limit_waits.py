#!/usr/bin/env python3
"""Pause request and waiting: the PAUSE file the panel writes, the wait until a usage window resets, and the sleep that a pause cuts short.

Moved out of `run_loop.py` as it was.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import datetime
import time

import loop_state  # noqa: E402  (typed state: loop folder paths)
from loop_io import load_json, save_json  # noqa: E402


PAUSE_REASON = "paused by the user in the panel; click Resume in the panel or run the command again"


PAUSE_NEXT = "resume when the user wants"


def pause_requested(work, turn):
    """PAUSE file (created by the panel): deletes it and returns the stop text; empty if there is no request. Spends no turn."""
    path = loop_state.loop_path(work, "PAUSE")
    if not os.path.exists(path):
        return ""
    try:
        os.remove(path)
    except OSError:
        pass
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    state.update(status="awaiting_user", turn=turn, reason=PAUSE_REASON, next=PAUSE_NEXT,
                 at=datetime.datetime.now().isoformat(timespec="seconds"))
    state.pop("options", None)
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    return PAUSE_REASON


MAX_LIMIT_WAIT_SECONDS = 8 * 24 * 3600  # the weekly window is the longest one


RESET_MARGIN_SECONDS = 120  # wake up a little after the reset, not on it


def limit_wait_seconds(opts):
    """(seconds, datetime of the reset or None): until the first known reset among the limited commands plus a margin, else --limit-wait."""
    now = datetime.datetime.now()
    future = sorted(moment for moment in getattr(opts, "limit_resets", None) or [] if moment > now)
    if not future:
        return opts.limit_wait * 60, None
    seconds = int((future[0] - now).total_seconds()) + RESET_MARGIN_SECONDS
    return min(max(seconds, 60), MAX_LIMIT_WAIT_SECONDS), future[0]


def main_reset_moment(opts, reset):
    """When the loop tries the main command again while a fallback keeps the turns: the reset of the main command's blocking window
    (the weekly one when that is what ran out, never an earlier moment) plus a margin; an unknown or already past reset waits
    --limit-wait minutes instead, so the loop never bounces between the commands."""
    now = datetime.datetime.now()
    known = reset if reset and reset > now else now + datetime.timedelta(minutes=opts.limit_wait)
    return known + datetime.timedelta(seconds=RESET_MARGIN_SECONDS)


def wait_text(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    days, hours = divmod(hours, 24)
    return " ".join(part for part in ("%dd" % days if days else "", "%dh" % hours if hours else "", "%dmin" % minutes if minutes or not (days or hours) else "") if part)


LIMIT_WAIT_STEP_SECONDS = 60


def sleep_unless_paused(work, turn, seconds):
    """Sleeps `seconds` in steps, stopping early when the panel asks for a pause; returns the pause text ('' = slept the whole time)."""
    deadline = time.time() + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            return ""
        time.sleep(min(LIMIT_WAIT_STEP_SECONDS, left))
        paused = pause_requested(work, turn)
        if paused:
            return paused
