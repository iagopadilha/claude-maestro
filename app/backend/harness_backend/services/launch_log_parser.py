"""Reads `internal/monitor-relaunch.log`: one section per launch of the loop, each opened by `[loop-launch] <iso time> <action>`.

Inside a launch the engine prints `[loop] turn N | HH:MM:SS` when a turn starts. That gives two things the log would not say otherwise:
the start time of each turn (for the turn timeline) and the turn a problem line was printed in (the last turn line above it; a line
printed before any turn line is attributed to the first turn of that launch, the one the launch led to). Never invents: a launch
that never reached a turn leaves `turn` as None.
"""
import datetime
import re

from .turn_timeline import parse_epoch

LAUNCH_MARKER = "[loop-launch]"
LAUNCH_FATAL_PATTERN = re.compile(r"(?i)traceback|did not start")
LAUNCH_PROBLEM_PATTERN = re.compile(r"(?i)traceback|error|failed|exception|PROBLEM|did not start")
TURN_START_LINE = re.compile(r"^\[loop\] turn (\d+) \| (\d{2}):(\d{2}):(\d{2})")
SECONDS_PER_DAY = 86400
ROLLOVER_SLACK_SECONDS = 60  # a turn line earlier than the previous one by more than this crossed midnight


def seconds_of_day(hours, minutes, seconds):
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds)


def parse_launch(marker, body):
    """{"marker", "time": iso|None, "lines": [{"text", "turn"}], "turn_starts": {turn: epoch}, "first_turn"}."""
    token = marker.split()[0] if marker.split() else ""
    launch_epoch = parse_epoch(token)
    launch_time = token[:19] if launch_epoch is not None else None
    lines, turn_starts, current, first_turn = [], {}, None, None
    day_offset, previous = 0, None
    if launch_epoch is not None:
        started = datetime.datetime.fromtimestamp(launch_epoch)
        previous = seconds_of_day(started.hour, started.minute, started.second)
        base_date = started.date()
    for raw in body.splitlines():
        text = raw.strip()
        if not text:
            continue
        found = TURN_START_LINE.match(text)
        if found:
            current = int(found.group(1))
            first_turn = current if first_turn is None else first_turn
            if launch_epoch is not None:
                seconds = seconds_of_day(*found.groups()[1:])
                if previous is not None and seconds < previous - ROLLOVER_SLACK_SECONDS:
                    day_offset += 1
                previous = seconds
                moment = datetime.datetime.combine(base_date + datetime.timedelta(days=day_offset), datetime.time()) + datetime.timedelta(seconds=seconds)
                turn_starts[current] = moment.timestamp()
        lines.append({"text": text, "turn": current})
    return {"marker": marker.strip(), "time": launch_time, "lines": lines, "turn_starts": turn_starts, "first_turn": first_turn}


def parse_launch_log(text):
    """Launches of the log, oldest first. A log without any marker is one launch with an empty marker."""
    if not text:
        return []
    chunks = text.split(LAUNCH_MARKER)
    launches = [parse_launch("", chunks[0])] if chunks[0].strip() else []
    for chunk in chunks[1:]:
        marker, _, body = chunk.partition("\n")
        launches.append(parse_launch(marker, body))
    return launches


def collect_turn_starts(launches):
    """{turn: start epoch}: the latest launch that printed the turn wins (a rewound turn runs again)."""
    starts = {}
    for launch in launches:
        starts.update(launch["turn_starts"])
    return starts
