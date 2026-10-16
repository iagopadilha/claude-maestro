"""Guard events of a project for the panel: the latest blocks of the plugin's PreToolUse hook and how many per rule.

Source: `<work>/.specs/project/loop/internal/guard.log`, one JSON line per decision (`{t, profile, rule, decision, detail}`)
written by `plugin/skills/orchestrator/hooks/guard.py`. Only `blocked` decisions count (info and allowed-by-user lines are
left out). The raw file never leaves through the API: each field is clipped and the detail goes through credential redaction.
"""
from ..repositories.loop_read_formatting import clip_redacted_text

BLOCK_DECISIONS = ("blocked", "block")
GUARD_EVENTS_LIMIT = 20
GUARD_DETAIL_LIMIT = 200
GUARD_FIELD_LIMIT = 60
UNKNOWN_RULE = "unknown"


def build_guard_event(line):
    rule = clip_redacted_text(line.get("rule"), GUARD_FIELD_LIMIT) or UNKNOWN_RULE
    return {"t": clip_redacted_text(line.get("t"), GUARD_FIELD_LIMIT) or None,
            "profile": clip_redacted_text(line.get("profile"), GUARD_FIELD_LIMIT) or None,
            "rule": rule, "detail": clip_redacted_text(line.get("detail"), GUARD_DETAIL_LIMIT)}


class GuardEventService:
    def __init__(self, loop_file_repository):
        self.loop_file_repository = loop_file_repository

    def build_guard_summary(self, work_dir):
        """{"events": last GUARD_EVENTS_LIMIT blocks, newest first, "counts": {rule: blocks}} (counts cover the whole log)."""
        blocks = [build_guard_event(line) for line in self.loop_file_repository.read_guard_lines(work_dir)
                  if str(line.get("decision") or "").strip().lower() in BLOCK_DECISIONS]
        counts = {}
        for event in blocks:
            counts[event["rule"]] = counts.get(event["rule"], 0) + 1
        return {"events": blocks[-GUARD_EVENTS_LIMIT:][::-1], "counts": counts}
