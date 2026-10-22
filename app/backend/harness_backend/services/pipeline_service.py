"""Pipeline of the loop for the panel (`GET /pipeline`): the six fixed steps of every feature, the features with their wave,
and, per turn, the events that turn produced (who ran which step and how it ended).

Everything is derived from what the loop already wrote: the agents of the turn (Claude Code transcripts, through the agent
history), the feature status changes between two consecutive snapshots and the commits of the turn. Never invents anything:
what cannot be derived is `null` or an empty list. The body is cached per project by the turn files, the ROADMAP and the loop
state, so repeated reads while nothing changed cost nothing.
"""
import os
import re
import threading

from ..repositories.loop_file_repository import loop_state
from ..repositories.loop_read_formatting import clip_redacted_text, ms_to_seconds, redact_credentials, to_number
from .agent_history_service import ORCHESTRATOR_ROLE, VERIFIER_NOTE_MARK, WORKING_STATUSES, get_inferred_role
from .loop_panel_service import compute_current_turn
from .turn_service import TURN_STATUS_RUNNING, get_turn_status

STEPS = (
    {"key": "spec", "agent": "spec-writer", "role": "writes the spec (PATHS + criteria)"},
    {"key": "review", "agent": "spec-reviewer", "role": "reviews the spec"},
    {"key": "gate", "agent": "gate", "role": "approves for execution"},
    {"key": "exec", "agent": "executor", "role": "implements"},
    {"key": "verify", "agent": "verifier", "role": "build · types · lint · tests"},
    {"key": "commit", "agent": "commit", "role": "git checkpoint"},
)
STEP_SPEC, STEP_REVIEW, STEP_GATE, STEP_EXEC, STEP_VERIFY, STEP_COMMIT = range(len(STEPS))
STEP_BY_ROLE = {"spec-writer": STEP_SPEC, "spec-reviewer": STEP_REVIEW, "verifier": STEP_VERIFY, "verifier-flow": STEP_VERIFY}
EXECUTOR_ROLE_PREFIX = "executor"
STEP_BY_STATUS_IN_PROGRESS = {"pending": STEP_SPEC, "speccing": STEP_SPEC, "specced": STEP_REVIEW, "approved": STEP_EXEC, "building": STEP_EXEC}
GATE_NOTE_PREFIX = "gate"
STEP_LABELS = {STEP_SPEC: "spec writing", STEP_REVIEW: "spec review", STEP_GATE: "gate", STEP_EXEC: "execution", STEP_VERIFY: "verification",
               STEP_COMMIT: "commit"}
LABEL_STEP_ORDER = (STEP_SPEC, STEP_REVIEW, STEP_EXEC, STEP_VERIFY)
LABEL_MAX_PARTS = 3
LABEL_MIXED = "mixed steps"
LABEL_ORCHESTRATION = "orchestration"
LABEL_IN_PROGRESS = "in progress"
LABEL_SEPARATOR = " + "
LABEL_WAVE_FORMAT = " · wave %d"
EVENT_DONE, EVENT_RUNNING, EVENT_QUEUED, EVENT_FAIL, EVENT_BLOCKED = "DONE", "RUNNING", "QUEUED", "FAIL", "BLOCKED"
FAILED_RETURN_STATUSES = ("FAIL", "BLOCKED")
BLOCKED_RETURN_STATUS = "BLOCKED"
AGENT_STATE_RUNNING = "running"
AGENT_STATE_FAILED = "failed"
GATE_AGENT = "gate"
COMMIT_AGENT = "commit"
COMMIT_OUT_FORMAT = "commit %s"
COMMIT_IN_STATE_PATTERN = re.compile(r"commit\s+([0-9a-f]{7,40})")
SHORT_COMMIT_LENGTH = 7
EVENT_OUT_LIMIT = 300
DECISION_LIMIT = 6000  # the whole orchestrator return of a turn (the panel renders it as Markdown); clipped only when huge
FILES_PER_TURN_LIMIT = 200
DIR_DEPENDENCY_PREFIX = loop_state.DIR_PREFIX
STATUS_AWAITING_USER = "awaiting_user"
BLOCKED_STATUS = "blocked"
ROADMAP_NAME = "ROADMAP.md"


def compute_waves(features):
    """{slug: wave}: 1 + the highest wave of its slug dependencies (none = 1). `dir:` dependencies and unknown slugs are ignored;
    a cycle (the engine's validation reports it) does not recurse forever."""
    dependencies = {feature["id"]: [dep for dep in feature["deps"] if not dep.startswith(DIR_DEPENDENCY_PREFIX)] for feature in features}
    waves = {}

    def resolve(slug, visiting):
        if slug in waves:
            return waves[slug]
        if slug in visiting:
            return 0
        visiting.add(slug)
        value = 1 + max((resolve(dep, visiting) for dep in dependencies.get(slug, []) if dep in dependencies), default=0)
        visiting.discard(slug)
        waves[slug] = value
        return value

    for slug in dependencies:
        resolve(slug, set())
    return waves


def get_step_for_role(role):
    """Pipeline step of an agent role; None for roles outside the six steps (the orchestrator, mappers...)."""
    if str(role or "").startswith(EXECUTOR_ROLE_PREFIX):
        return STEP_EXEC
    return STEP_BY_ROLE.get(role)


def get_event_status_for_agent(agent, step):
    """RUNNING while it runs; FAIL when it failed or returned FAIL/BLOCKED (BLOCKED for the reviewer, which blocks a spec); else DONE."""
    if agent.get("state") == AGENT_STATE_RUNNING:
        return EVENT_RUNNING
    return_status = agent.get("return_status")
    if step in (STEP_REVIEW, STEP_GATE) and return_status == BLOCKED_RETURN_STATUS:
        return EVENT_BLOCKED
    if agent.get("state") == AGENT_STATE_FAILED or return_status in FAILED_RETURN_STATUSES:
        return EVENT_FAIL
    return EVENT_DONE


def make_event(feature, step, status, out, agent_id, agent):
    return {"feature": feature, "step": step, "status": status, "out": clip_redacted_text(out, EVENT_OUT_LIMIT) or None,
            "agent_id": agent_id, "agent": agent}


def build_turn_label(events, waves):
    """Short deterministic text from the steps the turn's agents ran ("spec review", "execution + verification"), plus the wave when all
    its features share one."""
    steps = {event["step"] for event in events if event["agent_id"]}  # agent runs only: gate/commit marks are derived
    steps = steps or {event["step"] for event in events}
    ordered = [step for step in LABEL_STEP_ORDER if step in steps] + [step for step in (STEP_GATE, STEP_COMMIT) if step in steps]
    if not ordered:
        label = LABEL_ORCHESTRATION
    elif len(ordered) > LABEL_MAX_PARTS:
        label = LABEL_MIXED
    else:
        label = LABEL_SEPARATOR.join(STEP_LABELS[step] for step in ordered)
    wave_numbers = {waves.get(event["feature"]) for event in events}
    if len(wave_numbers) == 1 and None not in wave_numbers:
        label += LABEL_WAVE_FORMAT % wave_numbers.pop()
    return label


class PipelineService:
    def __init__(self, loop_status_service, loop_file_repository, roadmap_view_service, agent_history_service, log):
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.roadmap_view_service = roadmap_view_service
        self.agent_history_service = agent_history_service
        self.log = log
        self.cache = {}  # work dir -> (key, body)
        self.cache_lock = threading.Lock()

    # ------------------------------------------------ public
    def build_pipeline(self, context):
        work_dir = context.work_dir
        summary = self.loop_status_service.get_loop_summary(context)
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        key = self.build_cache_key(work_dir, summary, numbers)
        with self.cache_lock:
            stored = self.cache.get(work_dir)
        if stored and stored[0] == key:
            return stored[1]
        body = self.compute_pipeline(work_dir, summary, numbers)
        with self.cache_lock:
            self.cache[work_dir] = (key, body)
        return body

    def list_feature_events(self, context, slug):
        """[{"turn": n, ...event}] of one feature, oldest first (the quarantine detail reads its attempts from here)."""
        rows = []
        for turn in self.build_pipeline(context)["turns"]:
            rows += [dict(event, turn=turn["n"]) for event in turn["events"] if event["feature"] == slug]
        return rows

    # ------------------------------------------------ cache
    def build_cache_key(self, work_dir, summary, numbers):
        repository = self.loop_file_repository
        turn_stamps = tuple((number, repository.get_mtime(repository.get_turn_path(work_dir, number))) for number in numbers)
        roadmap_stamp = repository.get_mtime(os.path.join(work_dir, ".specs", "project", ROADMAP_NAME))
        loop_stamp = repository.get_mtime(loop_state.state_file(work_dir, "LOOP.json"))
        return (turn_stamps, roadmap_stamp, loop_stamp, summary["status"], bool(summary["running"]), (summary["meta"] or {}).get("turns"))

    # ------------------------------------------------ body
    def compute_pipeline(self, work_dir, summary, numbers):
        roadmap_text = self.loop_file_repository.read_roadmap(work_dir)
        stage_rows, feature_rows, _, _ = self.roadmap_view_service.build_roadmap_view(work_dir, roadmap_text)
        waves = compute_waves(feature_rows)
        features = [{"id": row["id"], "name": row["name"], "stage": row["stage"], "wave": waves.get(row["id"], 1),
                     "deps": [dep for dep in row["deps"] if not dep.startswith(DIR_DEPENDENCY_PREFIX)], "kind": row["kind"],
                     "status": row["status"]} for row in feature_rows]
        turns = [self.build_turn(work_dir, number, waves) for number in numbers]
        running = bool(summary["running"])
        live_number = compute_current_turn(summary["json"], summary["meta"] or {}, numbers, True) if running else None
        if running and live_number not in numbers:
            turns.append(self.build_live_turn(live_number, feature_rows, waves))
        paused = summary["status"] == STATUS_AWAITING_USER and not running
        return {"steps": [dict(step) for step in STEPS], "features": features,
                "stages": [{"n": stage["n"], "title": stage["title"]} for stage in stage_rows], "turns": turns,
                "live_turn": live_number if running else (numbers[-1] if numbers else None), "paused": paused,
                "quarantine": [row["id"] for row in feature_rows if row["status"] == BLOCKED_STATUS]}

    def build_turn(self, work_dir, number, waves):
        repository = self.loop_file_repository
        record = repository.read_turn(work_dir, number)
        snapshot = repository.load_snapshot(work_dir, number)
        previous = repository.load_snapshot(work_dir, number - 1) if number > 0 else None
        events = self.list_agent_events(work_dir, number, record)
        events += self.list_transition_events(snapshot, previous, events)
        decision = redact_credentials(record.get("result"), DECISION_LIMIT).strip()
        return {"n": number, "label": build_turn_label(events, waves),
                "status": get_turn_status(record, repository.read_snapshot_loop_json(snapshot)),
                "cost_usd": to_number(record.get("total_cost_usd")), "duration_s": ms_to_seconds(record.get("duration_ms")),
                "decision": decision or None, "events": events, "files": self.list_turn_files(work_dir, snapshot, previous),
                "claude": str(record.get("claude") or ""), "claude_account": str(record.get("claude_account") or "")}

    def build_live_turn(self, number, feature_rows, waves):
        """The turn in progress has no file yet: its RUNNING events are the features the loop is working on (agent inferred from the status,
        like `/state` does)."""
        events = []
        for row in feature_rows:
            if row["status"] not in WORKING_STATUSES:
                continue
            role = get_inferred_role(row)
            step = get_step_for_role(role)
            if step is not None:
                events.append(make_event(row["id"], step, EVENT_RUNNING, row.get("note"), None, role))
        return {"n": number, "label": build_turn_label(events, waves) if events else LABEL_IN_PROGRESS, "status": TURN_STATUS_RUNNING,
                "cost_usd": None, "duration_s": None, "decision": None, "events": events, "files": []}

    # ------------------------------------------------ events
    def list_agent_events(self, work_dir, number, record):
        try:
            agents, _ = self.agent_history_service.list_turn_agents(work_dir, number, record)
        except Exception as failure:  # one unreadable transcript does not take the pipeline down
            self.log.warn("pipeline_turn_agents_failed", turn=number, type=type(failure).__name__)
            return []
        events = []
        for agent in agents:
            step = get_step_for_role(agent["role"]) if agent["role"] != ORCHESTRATOR_ROLE else None
            if step is None or not agent.get("feature"):
                continue
            events.append(make_event(agent["feature"], step, get_event_status_for_agent(agent, step),
                                     agent.get("decision") or agent.get("summary"), agent["id"], agent["role"]))
        return events

    def list_transition_events(self, snapshot, previous, agent_events):
        """Marks only the status changes between two snapshots can show: approved (gate), verified with a commit (commit), blocked (the step
        that was in progress). Without both snapshots there is nothing to compare."""
        if not snapshot or not previous:
            return []
        now = self.read_snapshot_features(snapshot)
        before = self.read_snapshot_features(previous)
        events = []
        for slug, fields in now.items():
            old_status, new_status = (before.get(slug) or {}).get("status"), fields.get("status")
            if old_status is None or old_status == new_status:
                continue
            own = [event for event in agent_events if event["feature"] == slug]
            if new_status == "approved":
                reviewer = next((event for event in reversed(own) if event["step"] == STEP_REVIEW), None)
                events.append(make_event(slug, STEP_GATE, EVENT_DONE, reviewer["out"] if reviewer else None, None, GATE_AGENT))
            elif new_status == "verified":
                commit = self.read_snapshot_commit(snapshot, slug, fields)
                if commit:
                    events.append(make_event(slug, STEP_COMMIT, EVENT_DONE, COMMIT_OUT_FORMAT % commit, None, COMMIT_AGENT))
            elif new_status == BLOCKED_STATUS:
                step = self.find_step_in_progress(old_status, fields, own)
                out = (own[-1]["out"] if own and own[-1]["step"] == step else None) or fields.get("note")
                events.append(make_event(slug, step, EVENT_BLOCKED, out, None, STEPS[step]["agent"]))
        return events

    @staticmethod
    def find_step_in_progress(old_status, fields, own_events):
        """The step the feature was in when it got blocked: the last agent that ran for it in the turn, else the gate when the note says
        so, else the one the status before shows."""
        if own_events:
            return own_events[-1]["step"]
        if str(fields.get("note") or "").startswith(GATE_NOTE_PREFIX):
            return STEP_GATE
        if old_status == "building" and VERIFIER_NOTE_MARK in str(fields.get("note") or "").lower():
            return STEP_VERIFY
        return STEP_BY_STATUS_IN_PROGRESS.get(old_status, STEP_EXEC)

    def read_snapshot_features(self, snapshot):
        features, _ = loop_state.parse_roadmap(self.loop_file_repository.read_snapshot_roadmap(snapshot))
        return features

    def read_snapshot_commit(self, snapshot, slug, fields):
        """Short commit of a feature that became verified: the ROADMAP `commit=` field, else `<slug> ... commit <sha>` in the snapshot's STATE.md."""
        if fields.get("commit"):
            return fields["commit"][:SHORT_COMMIT_LENGTH]
        state_text = self.loop_file_repository.read_cached_text(os.path.join(snapshot["dir"], "STATE.md"))
        for line in state_text.splitlines():
            found = COMMIT_IN_STATE_PATTERN.search(line)
            if found and slug in line:
                return found.group(1)[:SHORT_COMMIT_LENGTH]
        return None

    # ------------------------------------------------ files
    def list_turn_files(self, work_dir, snapshot, previous):
        """Per-file numstat between the two snapshots' HEADs; [] when either HEAD is unknown or git cannot tell."""
        if not snapshot or not previous or not snapshot.get("head") or not previous.get("head"):
            return []
        rows = self.loop_file_repository.read_numstat_between(work_dir, previous["head"], snapshot["head"])
        return list(rows[:FILES_PER_TURN_LIMIT]) if rows else []
