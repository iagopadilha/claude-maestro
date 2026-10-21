"""Turns the timestamped sources of the error center into durable, turn-stamped events.

Sources: the guard blocks of `internal/guard.log` and the problem lines of `internal/monitor-relaunch.log`. Each line becomes one event
with a stable id (a hash of its own content plus its ordinal among identical lines), so reading the same line again never duplicates it.
The turn of an event is, in order: the `turn` the guard hook wrote in the line, the turn the launch log printed it in, else the turn
whose time window holds the line's timestamp (`TurnTimeline`). Unknown stays `None`: nothing is invented, and an event whose turn
was unknown is filled in by a later sync once the turn's file exists. Events go to `internal/events/errors.jsonl` (append only), so
they outlive a rotated or truncated source log. Texts are redacted and clipped BEFORE they are stored.
"""
import hashlib

from ..repositories.loop_read_formatting import clip_redacted_text
from .guard_event_service import BLOCK_DECISIONS
from .launch_log_parser import LAUNCH_PROBLEM_PATTERN
from .turn_timeline import parse_epoch

KIND_GUARD_EVENT, KIND_LAUNCH_EVENT = "guard", "launch"
PLANNING_PROFILE = "planning"  # the planning chat runs before any turn: its blocks belong to no turn
EVENT_DETAIL_LIMIT = 200
EVENT_ATTEMPT_LIMIT = 200
EVENT_LINE_LIMIT = 300
EVENT_FIELD_LIMIT = 60
EVENT_ID_LENGTH = 16


def make_event_id(prefix, *parts):
    digest = hashlib.sha1("\x1f".join(str(part) for part in parts).encode("utf-8", "replace")).hexdigest()
    return "%s-%s" % (prefix, digest[:EVENT_ID_LENGTH])


def as_turn_number(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class ErrorEventSyncService:
    def __init__(self, loop_file_repository, error_event_repository):
        self.loop_file_repository = loop_file_repository
        self.error_event_repository = error_event_repository

    def sync_events(self, work_dir, timeline, launches):
        """Stores what the sources hold now and returns every known event (oldest first), each with its best-known turn."""
        stored = self.error_event_repository.read_events(work_dir) if self.error_event_repository else []
        known = {event["id"]: event for event in stored}
        writes = []
        for event in self.derive_guard_events(work_dir) + self.derive_launch_events(launches):
            if event["id"] not in known:
                event["turn"] = event["turn"] if event["turn"] is not None else self.find_turn(event, timeline)
                known[event["id"]] = event
                writes.append(event)
        for event in known.values():  # history whose turn was unknown (the turn file did not exist yet)
            if event.get("turn") is None:
                turn = self.find_turn(event, timeline)
                if turn is not None:
                    event["turn"] = turn
                    writes.append({"id": event["id"], "turn": turn})
        if self.error_event_repository:
            self.error_event_repository.append_events(work_dir, writes)
        return list(known.values())

    @staticmethod
    def find_turn(event, timeline):
        if event.get("profile") == PLANNING_PROFILE:
            return None
        return timeline.turn_at(parse_epoch(event.get("t"))) if event.get("t") else None

    def derive_guard_events(self, work_dir):
        events, seen = [], {}
        for line in self.loop_file_repository.read_guard_lines(work_dir):
            if str(line.get("decision") or "").strip().lower() not in BLOCK_DECISIONS:
                continue
            fields = [str(line.get(name) or "") for name in ("t", "profile", "rule", "detail", "attempt")]
            ordinal = seen[tuple(fields)] = seen.get(tuple(fields), -1) + 1
            events.append({
                "id": make_event_id("g", *fields, ordinal), "kind": KIND_GUARD_EVENT, "t": clip_redacted_text(fields[0], EVENT_FIELD_LIMIT) or None,
                "turn": as_turn_number(line.get("turn")), "profile": clip_redacted_text(fields[1], EVENT_FIELD_LIMIT) or None,
                "rule": clip_redacted_text(fields[2], EVENT_FIELD_LIMIT) or "unknown",
                "detail": clip_redacted_text(fields[3], EVENT_DETAIL_LIMIT), "attempt": clip_redacted_text(fields[4], EVENT_ATTEMPT_LIMIT) or None})
        return events

    def derive_launch_events(self, launches):
        events = []
        for launch in launches:
            seen = {}
            for line in launch["lines"]:
                if not LAUNCH_PROBLEM_PATTERN.search(line["text"]):
                    continue
                ordinal = seen[line["text"]] = seen.get(line["text"], -1) + 1
                turn = line["turn"] if line["turn"] is not None else launch["first_turn"]
                events.append({"id": make_event_id("l", launch["marker"], line["text"], ordinal), "kind": KIND_LAUNCH_EVENT, "t": launch["time"],
                               "turn": turn, "launch": launch["marker"], "line": clip_redacted_text(line["text"], EVENT_LINE_LIMIT)})
        return events
