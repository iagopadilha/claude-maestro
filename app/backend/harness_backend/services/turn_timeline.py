"""Which loop turn was running at a given moment: maps a timestamped event (a guard line, a launch-log line) to a turn number.

A turn's window is [start, end]: the end is the mtime of `turns/NNN.json` (written when the turn finishes); the start is, in order of
trust, the `[loop] turn N | HH:MM:SS` line of the launch log, the end minus the `duration_ms` of the turn log, or the end of the previous
turn. A window without a known start is never used (unknown stays unknown). The turn in progress has an open end. Pure functions of
numbers: nothing is read here.
"""
import datetime

TURN_START_TOLERANCE_SECONDS = 2  # clock/second-resolution slack before a window opens
TURN_END_TOLERANCE_SECONDS = 1


def parse_epoch(text):
    """Epoch seconds of a local ISO time (`2026-10-19T22:55:19`, extra tail ignored); anything else = None."""
    try:
        return datetime.datetime.fromisoformat(str(text).strip()[:19]).timestamp()
    except (TypeError, ValueError):
        return None


class TurnTimeline:
    def __init__(self, windows):
        """`windows`: {turn: (start_epoch|None, end_epoch|None)}. Searched from the newest turn so overlaps go to the later one."""
        self.windows = sorted(windows.items(), reverse=True)

    @classmethod
    def build(cls, ends, starts=None, durations=None, running_turn=None):
        """`ends`: {turn: finished epoch}; `starts`: {turn: started epoch} (launch log); `durations`: {turn: seconds};
        `running_turn`: number of the turn in progress (open end) or None."""
        starts, durations = starts or {}, durations or {}
        windows = {}
        for number in sorted(set(ends) | ({running_turn} if running_turn else set())):
            end = ends.get(number)
            if number in starts:
                start = starts[number]
            elif end is not None and durations.get(number):
                start = end - durations[number]
            else:
                start = ends.get(number - 1)
            windows[number] = (start, end)
        return cls(windows)

    def turn_at(self, epoch):
        """Number of the turn whose window holds `epoch`, or None (before the first turn, between turns, unknown time)."""
        if epoch is None:
            return None
        for number, (start, end) in self.windows:
            if start is None or epoch < start - TURN_START_TOLERANCE_SECONDS:
                continue
            if end is None or epoch <= end + TURN_END_TOLERANCE_SECONDS:
                return number
        return None
