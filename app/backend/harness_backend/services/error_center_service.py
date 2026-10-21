"""Error center of a project: every kind of problem the loop leaves behind, in one list for the panel (`GET /errors`).

Sources: the loop's own stop (LOOP.json / meta.json), the failed or noisy turns (`turns/NNN.json`), the quarantined features of the
ROADMAP, and the guard blocks and problem lines of the last launch, which are turned into durable, turn-stamped events in
`internal/events/errors.jsonl` (see ErrorEventSyncService). Each item says what happened in plain words, which turns it happened in
(`turns`, plus `turn_counts` for grouped items), what the user can do about it (`hint`, empty when there is nothing to do) and which
recovery buttons apply. Texts go through credential redaction and are clipped before they leave through the API. Besides reading the
sources, building the list appends the events it has not stored yet (the only write; a failure to write is ignored).
"""
import datetime
import re

from ..repositories.loop_file_repository import loop_state
from ..repositories.loop_read_formatting import clip_redacted_text, redact_credentials
from .error_event_sync_service import ErrorEventSyncService, KIND_GUARD_EVENT, KIND_LAUNCH_EVENT
from .launch_log_parser import LAUNCH_FATAL_PATTERN, collect_turn_starts, parse_launch_log
from .turn_timeline import TurnTimeline

SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO = "error", "warning", "info"
KIND_LOOP, KIND_TURN, KIND_GUARD, KIND_FEATURE, KIND_LAUNCH = "loop", "turn", "guard", "feature", "launch"
RECOVER_RETRY, RECOVER_COMMIT, RECOVER_DISCARD = "retry", "commit_leftovers", "discard_leftovers"
TURNS_SCANNED_LIMIT = 40
SNAPSHOTS_SCANNED_LIMIT = 40
TITLE_LIMIT = 160
DETAIL_LIMIT = 1200
HAPPENED_LIMIT = 700
LAUNCH_LOG_NAME = "monitor-relaunch.log"
LAUNCH_LINES_LIMIT = 6
GUARD_ATTEMPTS_LIMIT = 5
MILLISECONDS_PER_SECOND = 1000
BACKGROUND_TASKS_PATTERN = re.compile(r"Background tasks still running")
STOP_TURN_PATTERN = re.compile(r"(?:after|on|in) turn (\d+)")
STATUS_ERROR = "error"
BLOCKED_STATUS = "blocked"

# (pattern on the reason, short title, what happened, what the user can do, recovery buttons)
KNOWN_CAUSES = (
    (re.compile(r"dirty tree outside"), "Files left uncommitted in the worktree",
     "A turn finished with files edited but not committed (the executors never run git and the orchestrator ended the turn before committing). "
     "Files under the paths of the features in progress are now committed by the engine on its own; these ones are outside those paths.",
     "Look at the list: if the work is good, commit it and go on; if it is junk, discard it.", (RECOVER_COMMIT, RECOVER_DISCARD)),
    (re.compile(r"LOOP\.json missing, stale or invalid"), "The turn ended without recording its state",
     "The orchestrator finished the turn without writing LOOP.json (often it stopped to wait for a background agent). The engine already tried "
     "one correction turn.",
     "Try again usually works; if it repeats, rewind to the previous turn (`harness.py loop <repo> --rewind <N>`).", (RECOVER_RETRY,)),
    (re.compile(r"invalid state after turn"), "The turn left an invalid ROADMAP/STATE",
     "The ROADMAP or LOOP.json breaks a rule (illegal status change, verified without a commit...).",
     "Rewind to the turn before it (`harness.py loop <repo> --rewind <N>`) and try again.", ()),
    (re.compile(r"exceeded \d+ min"), "A turn took too long",
     "The turn went over the time limit and was terminated.",
     "Raise the turn timeout in New loop (or split the feature) and try again.", (RECOVER_RETRY,)),
    (re.compile(r"cost cap"), "Cost cap reached",
     "The sum of the turns passed the cost cap.", "Raise the cap in New loop (or accept the cost) and try again.", ()),
    (re.compile(r"usage limit"), "Account usage limit",
     "The Claude account hit its usage limit and the waits ran out.", "Try again after the limit resets.", (RECOVER_RETRY,)),
    (re.compile(r"permission denied"), "A command was denied",
     "The guard or the permission mode denied a command the turn needed.",
     "Read the command in the details; allow it in PERMISSIONS.json or change the feature.", ()),
    (re.compile(r"identical state|without ROADMAP progress"), "The loop is not advancing",
     "Turns are passing without the ROADMAP changing.",
     "Look at the last turn in History, answer what it needs, or send an instruction.", ()),
    (re.compile(r"pre-flight failed|did not start"), "The loop did not start",
     "The pre-flight check found a problem before spending a turn.", "Fix what the details say and start again.", ()),
)
TIMEOUT_CAUSE = KNOWN_CAUSES[3]
BACKGROUND_TASKS_HAPPENED = "The verifier (a background agent) was still running when the turn ended, so it was stopped."
BACKGROUND_TASKS_HINT = "The engine now waits for background agents, so this should not repeat; if the feature stays in building, try again."
GUARD_RULE_REASON = {
    "specs-only": "An agent tried to write outside .specs/ (planning and spec agents only write there).",
    "state-write": "An agent tried to write the loop state, which only the orchestrator and the engine own.",
    "approval-write": "Something tried to write the roadmap approval, which only your click on Approve roadmap can write.",
    "git-write": "An agent tried a git write command (the engine owns commits).",
    "git-push": "An agent tried git push (delivery is done by harness.py deliver, at the end).",
    "push-protected": "An agent tried to push a protected branch.",
    "pr-release": "An agent tried to open a pull request or a release.",
    "prod-write": "An agent tried to write in production (never allowed).",
    "secrets": "An agent tried to read a credential file.",
    "outbound-msg": "An agent tried to send a message outside the machine.",
    "panel-trigger": "An agent tried to operate the panel itself.",
    "repo-only": "An agent tried to act outside the project's worktrees.",
    "loop-run": "An agent tried to start the loop or the panel (only you do that).",
    "delivery": "An agent tried to deliver (only the orchestrator delivers).",
}
GUARD_NOTE = ("The guard stopped it: it worked and nothing was changed. This is not a problem; "
              "many blocks of the same rule only mean an agent kept retrying a forbidden command.")


def explain(reason):
    """(title, what happened, what the user can do, recovery) for a stop reason; an unknown reason is its own title and offers a retry."""
    for pattern, title, happened, fix, recover in KNOWN_CAUSES:
        if pattern.search(reason or ""):
            return title, happened, fix, list(recover)
    return (clip_redacted_text(reason, TITLE_LIMIT) or "The loop stopped with an error",
            "The loop stopped for a reason the panel does not recognize; the full text is in the details.", "", [RECOVER_RETRY])


def classify(reason):
    """(title, what the user can do, recovery) for a stop reason; unknown reasons get the reason itself as title and no hint."""
    title, _, fix, recover = explain(reason)
    return title, fix, recover


def to_iso(timestamp):
    return datetime.datetime.fromtimestamp(timestamp).isoformat(timespec="seconds") if timestamp else None


def make_item(item_id, kind, severity, title, detail="", hint="", when=None, turn=None, recover=None, count=1,
              happened="", turns=None, turn_counts=None, attempts=None, rule=None, profile=None):
    """One item of the list. `turns` = every turn it happened in (sorted, unique; empty only when unknown) and always holds `turn`;
    `turn` is the single turn the item points at (the latest of `turns` when not given). `turn_counts` = {"<turn>": events}."""
    numbers = sorted({number for number in (turns or []) if number is not None} | ({turn} if turn is not None else set()))
    return {"id": item_id, "kind": kind, "severity": severity, "title": clip_redacted_text(title, TITLE_LIMIT),
            "happened": redact_credentials(happened, HAPPENED_LIMIT).strip(),
            "detail": redact_credentials(detail, DETAIL_LIMIT).strip(), "hint": hint, "when": when,
            "turn": turn if turn is not None else (numbers[-1] if numbers else None), "turns": numbers,
            "turn_counts": {str(number): amount for number, amount in sorted((turn_counts or {}).items())},
            "recover": list(recover or []), "count": count, "attempts": list(attempts or []), "rule": rule, "profile": profile}


def count_turns(events):
    """{turn: how many of the events} for the events whose turn is known."""
    counts = {}
    for event in events:
        if event.get("turn") is not None:
            counts[event["turn"]] = counts.get(event["turn"], 0) + 1
    return counts


class ErrorCenterService:
    def __init__(self, loop_status_service, loop_file_repository, error_event_repository=None):
        """Without an `error_event_repository` the events are derived on every call and not stored (tests)."""
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.event_sync_service = ErrorEventSyncService(loop_file_repository, error_event_repository)
        self.duration_cache = {}

    def build_errors(self, context):
        work_dir = context.work_dir
        summary = self.loop_status_service.get_loop_summary(context)
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        launches = parse_launch_log(self.loop_file_repository.read_cached_text(loop_state.loop_path(work_dir, LAUNCH_LOG_NAME)))
        timeline = self.build_timeline(work_dir, numbers, launches, summary)
        events = self.event_sync_service.sync_events(work_dir, timeline, launches)
        items = []
        items += self.build_loop_items(work_dir, summary, numbers)
        items += self.build_turn_items(work_dir, numbers)
        items += self.build_feature_items(work_dir)
        items += self.build_launch_items(launches, events)
        items += self.build_guard_items(events)
        order = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}
        items.sort(key=lambda item: (order[item["severity"]], "" if item["kind"] == KIND_LOOP else "z",
                                     0 if item["kind"] == KIND_GUARD else -(item["turn"] or 0)))
        counts = {severity: sum(1 for item in items if item["severity"] == severity) for severity in (SEVERITY_ERROR, SEVERITY_WARNING, SEVERITY_INFO)}
        problems = counts[SEVERITY_ERROR] + counts[SEVERITY_WARNING]
        return {"items": items, "counts": counts, "problems": problems, "loop_status": summary["status"], "running": bool(summary["running"])}

    # ------------------------------------------------ when did each turn run
    def build_timeline(self, work_dir, numbers, launches, summary):
        ends, durations = {}, {}
        for number in numbers:
            path = self.loop_file_repository.get_turn_path(work_dir, number)
            modified = self.loop_file_repository.get_mtime(path)
            if modified is not None:
                ends[number] = modified
                durations[number] = self.read_duration_seconds(work_dir, number, path, modified)
        running_turn = (numbers[-1] if numbers else 0) + 1 if summary.get("running") else None
        return TurnTimeline.build(ends, collect_turn_starts(launches), durations, running_turn)

    def read_duration_seconds(self, work_dir, number, path, modified):
        """Wall time of the turn (`duration_ms` of the claude result), read once per file version; None when the log does not say."""
        cached = self.duration_cache.get(path)
        if cached and cached[0] == modified:
            return cached[1]
        milliseconds = self.loop_file_repository.read_turn(work_dir, number).get("duration_ms")
        seconds = milliseconds / MILLISECONDS_PER_SECOND if isinstance(milliseconds, (int, float)) and not isinstance(milliseconds, bool) and milliseconds > 0 else None
        self.duration_cache[path] = (modified, seconds)
        return seconds

    # ------------------------------------------------ items
    def build_loop_items(self, work_dir, summary, numbers):
        if summary["status"] != STATUS_ERROR or summary["running"]:
            return []
        reason = str(summary.get("reason") or "")
        title, happened, fix, recover = explain(reason)
        meta = self.loop_file_repository.read_meta(work_dir)
        when = meta.get("finished_at") or (summary.get("json") or {}).get("at")
        mentioned = STOP_TURN_PATTERN.search(reason)
        turn = int(mentioned.group(1)) if mentioned else ((summary.get("json") or {}).get("turn") or (numbers[-1] if numbers else None))
        return [make_item("loop-stop", KIND_LOOP, SEVERITY_ERROR, title, reason, fix, when, turn, recover, happened=happened)]

    def build_turn_items(self, work_dir, numbers):
        items = []
        for number in numbers[-TURNS_SCANNED_LIMIT:]:
            turn = self.loop_file_repository.read_turn(work_dir, number)
            when = to_iso(self.loop_file_repository.get_mtime(self.loop_file_repository.get_turn_path(work_dir, number)))
            stderr = str(turn.get("stderr") or "").strip()
            if turn.get("timeout"):
                items.append(make_item("turn-%d-timeout" % number, KIND_TURN, SEVERITY_ERROR, "Turn %d was terminated by the time limit" % number,
                                       stderr, TIMEOUT_CAUSE[3], when, number, happened=TIMEOUT_CAUSE[2]))
            elif turn.get("exit_code") not in (0, None) or turn.get("is_error"):
                detail = stderr or str(turn.get("result") or turn.get("sample") or turn.get("reason") or "")
                items.append(make_item("turn-%d-failed" % number, KIND_TURN, SEVERITY_ERROR, "Turn %d failed (exit code %s)" % (number, turn.get("exit_code")),
                                       detail, "", when, number,
                                       happened="Turn %d ended with a failure: the `claude -p` process exited with code %s." % (number, turn.get("exit_code"))))
            elif stderr:
                background = bool(BACKGROUND_TASKS_PATTERN.search(stderr))
                items.append(make_item("turn-%d-stderr" % number, KIND_TURN, SEVERITY_INFO,
                                       "Turn %d ended with its verifier still running" % number if background else "Turn %d wrote to stderr" % number,
                                       stderr, BACKGROUND_TASKS_HINT if background else "", when, number,
                                       happened=BACKGROUND_TASKS_HAPPENED if background else "Turn %d finished normally but wrote to stderr; it did not fail." % number))
        return items

    def build_feature_items(self, work_dir):
        features, _ = loop_state.parse_roadmap(self.loop_file_repository.read_roadmap(work_dir))
        return [make_item("feature-%s" % slug, KIND_FEATURE, SEVERITY_ERROR, "Feature %s is blocked (quarantine)" % slug,
                          "%s failures. %s" % (feature.get("tries", "?"), feature.get("note", "")),
                          "Answer in the panel: try again with guidance, or skip it.",
                          turns=self.find_turns_blocked(work_dir, slug),
                          happened="The feature failed %s times (gate, execution or verification) and was put in quarantine. "
                                   "The loop carries on with what does not depend on it." % feature.get("tries", "several"))
                for slug, feature in features.items() if feature.get("status") == BLOCKED_STATUS]

    def find_turns_blocked(self, work_dir, slug):
        """[turn the feature became blocked in]: walks the snapshots (state after each turn) back while the feature stays blocked. [] when no snapshot says."""
        turn = None
        for number in reversed(self.loop_file_repository.list_snapshot_numbers(work_dir)[-SNAPSHOTS_SCANNED_LIMIT:]):
            snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
            features, _ = loop_state.parse_roadmap(self.loop_file_repository.read_snapshot_roadmap(snapshot))
            if (features.get(slug) or {}).get("status") != BLOCKED_STATUS:
                break
            turn = number
        return [turn] if turn is not None else []

    def build_launch_items(self, launches, events):
        """Problem lines of the LAST launch (the log accumulates across launches; with the log gone, the last launch the events remember)."""
        launch_events = [event for event in events if event["kind"] == KIND_LAUNCH_EVENT]
        latest = launches[-1]["marker"] if launches else (launch_events[-1]["launch"] if launch_events else None)
        problems = [event for event in launch_events if event["launch"] == latest]
        if not problems:
            return []
        fatal = any(LAUNCH_FATAL_PATTERN.search(event["line"]) for event in problems)
        return [make_item("launch-last", KIND_LAUNCH, SEVERITY_WARNING if fatal else SEVERITY_INFO, "The last launch of the loop printed problems",
                          "\n".join(event["line"] for event in problems[-LAUNCH_LINES_LIMIT:]),
                          "If the loop is running fine this is only noise.", problems[-1].get("t"),
                          turns=[event["turn"] for event in problems], turn_counts=count_turns(problems),
                          happened="The last launch of the loop printed %d problem line(s) in .specs/project/loop/internal/monitor-relaunch.log." % len(problems))]

    def build_guard_items(self, events):
        """Every stored guard block, grouped by profile and rule (a block means the guard worked, so these are `info`, never a problem)."""
        grouped = {}
        for event in events:
            if event["kind"] == KIND_GUARD_EVENT:
                grouped.setdefault((event.get("profile") or "?", event["rule"]), []).append(event)
        items = []
        for (profile, rule), blocks in sorted(grouped.items(), key=lambda pair: -len(pair[1])):
            attempts = list(dict.fromkeys(block["attempt"] for block in blocks if block.get("attempt")))[:GUARD_ATTEMPTS_LIMIT]
            counts = count_turns(blocks)
            items.append(make_item("guard-%s-%s" % (profile, rule), KIND_GUARD, SEVERITY_INFO, "Guard blocked %d x %s (%s)" % (len(blocks), rule, profile),
                                   blocks[-1].get("detail") or "", "", blocks[-1].get("t"), turns=list(counts), turn_counts=counts, count=len(blocks),
                                   attempts=attempts, rule=rule, profile=profile,
                                   happened="%s %s" % (GUARD_RULE_REASON.get(rule, ""), GUARD_NOTE)))
        return items
