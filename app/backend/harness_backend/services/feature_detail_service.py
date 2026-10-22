"""Body of `GET /feature/{slug}`: one feature seen from the inside (header, step timeline, task graph with the agent spawned for
each task, and every agent call of the feature).

Everything is derived from what the loop already wrote (ROADMAP, tasks.md, task status file, Claude Code transcripts); what cannot be
derived is `null` (never invented). The live match of the executors of the running turn is best effort and never raises.
"""
from .claude_account_service import build_turn_auth, build_turn_claude
from .agent_history_service import ORCHESTRATOR_ROLE, VERIFIER_NOTE_MARK
from .loop_panel_service import compute_current_turn
from .pipeline_service import (DIR_DEPENDENCY_PREFIX, FAILED_RETURN_STATUSES, GATE_NOTE_PREFIX, STEP_COMMIT, STEP_EXEC, STEP_GATE, STEP_REVIEW,
                               STEP_SPEC, STEP_VERIFY, compute_waves, get_step_for_role)

STEP_SPEC_NAME, STEP_GATE_NAME, STEP_BUILD_NAME, STEP_VERIFY_NAME, STEP_COMMIT_NAME = "spec", "gate", "build", "verify", "commit"
STEP_ORDER = (STEP_SPEC_NAME, STEP_GATE_NAME, STEP_BUILD_NAME, STEP_VERIFY_NAME, STEP_COMMIT_NAME)
STEP_NAME_BY_PIPELINE_STEP = {STEP_SPEC: STEP_SPEC_NAME, STEP_REVIEW: STEP_GATE_NAME, STEP_GATE: STEP_GATE_NAME, STEP_EXEC: STEP_BUILD_NAME,
                              STEP_VERIFY: STEP_VERIFY_NAME, STEP_COMMIT: STEP_COMMIT_NAME}
STEP_DONE, STEP_RUNNING, STEP_PENDING, STEP_FAILED = "done", "running", "pending", "failed"
CURRENT_STEP_BY_STATUS = {"pending": STEP_SPEC_NAME, "speccing": STEP_SPEC_NAME, "specced": STEP_GATE_NAME, "approved": STEP_GATE_NAME,
                          "building": STEP_BUILD_NAME, "verified": STEP_COMMIT_NAME}
CURRENT_STEP_STATE_BY_STATUS = {"pending": STEP_PENDING, "speccing": STEP_RUNNING, "specced": STEP_RUNNING, "approved": STEP_DONE,
                                "building": STEP_RUNNING, "verified": STEP_DONE, "blocked": STEP_FAILED}
BLOCKED_STATUS = "blocked"
BUILDING_STATUS = "building"
AGENT_RUNNING_STATE = "running"
AGENT_FAILED_STATE = "failed"
AGENT_INTERRUPTED_STATE = "interrupted"  # cut off with its turn: no return while the task is still running
EXECUTOR_FIELDS = ("agent_id", "role", "turn", "state", "return_status", "duration_s", "tokens", "started_at", "finished_at",
                   "claude", "auth_method", "auth_email", "cc_account")  # the last four: command and sign-in inherited from the turn


def mark_interrupted_executor(task):
    """A task still `running` whose newest executor is not running and returned nothing was cut off with its turn (loop stopped,
    timeout, limit): that executor is `interrupted`, never `done`."""
    executors = task["executors"]
    if task.get("status") != "running" or not executors:
        return
    newest = executors[-1]
    if newest.get("state") != AGENT_RUNNING_STATE and not newest.get("return_status"):
        newest["state"] = AGENT_INTERRUPTED_STATE


class FeatureDetailService:
    def __init__(self, loop_status_service, loop_file_repository, roadmap_view_service, agent_history_service, task_status_repository, log):
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.roadmap_view_service = roadmap_view_service
        self.agent_history_service = agent_history_service
        self.task_status_repository = task_status_repository
        self.log = log

    def build_feature_detail(self, context, slug):
        """The contract of `GET /feature/{slug}`; None when the ROADMAP has no such feature."""
        work_dir = context.work_dir
        roadmap_text = self.loop_file_repository.read_roadmap(work_dir)
        _, feature_rows, _, _ = self.roadmap_view_service.build_roadmap_view(work_dir, roadmap_text)
        row = next((candidate for candidate in feature_rows if candidate["id"] == slug), None)
        if row is None:
            return None
        summary = self.loop_status_service.get_loop_summary(context)
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        calls = self.list_feature_calls(work_dir, slug, numbers, summary)
        current_step, steps = self.build_steps(row, calls)
        return {"id": row["id"], "name": row["name"], "status": row["status"], "stage": row["stage"],
                "wave": compute_waves(feature_rows).get(slug), "deps": [dep for dep in row["deps"] if not dep.startswith(DIR_DEPENDENCY_PREFIX)],
                "paths": row["paths"], "scope": (row.get("summary") or {}).get("size"), "kind": row["kind"], "current_step": current_step,
                "steps": steps, "claude": self.read_last_turn_claude(work_dir, numbers),
                "tasks": self.build_tasks_block(work_dir, slug, calls), "agents": calls}

    # ------------------------------------------------ agent calls
    def list_feature_calls(self, work_dir, slug, numbers, summary):
        """Every subagent call of the feature, oldest first: the finished turns plus, while the loop runs, the live session."""
        calls = []
        session_ids = set()
        claude_by_turn = {}  # every agent uses the command and sign-in of the turn that spawned it
        for number in numbers:
            record = self.loop_file_repository.read_turn(work_dir, number)
            claude_by_turn[number] = build_turn_claude(record)
            if isinstance(record.get("session_id"), str):
                session_ids.add(record["session_id"])
            try:
                agents, _ = self.agent_history_service.list_turn_agents(work_dir, number, record)
            except Exception as failure:  # one unreadable transcript does not take the detail down
                self.log.warn("feature_detail_turn_agents_failed", turn=number, type=type(failure).__name__)
                continue
            calls += [dict(self.build_call(agent, number), **claude_by_turn[number])
                      for agent in agents if agent["role"] != ORCHESTRATOR_ROLE and agent["feature"] == slug]
        if summary["running"]:
            live_number = compute_current_turn(summary["json"], summary["meta"] or {}, numbers, True)
            live_agents = self.agent_history_service.list_live_subagents(work_dir, live_number, session_ids)
            live_claude = build_turn_claude(summary["meta"])  # the live turn has no file yet: the account of the last turn is the best known
            calls += [dict(self.build_call(agent, live_number), **live_claude) for agent in live_agents if agent["feature"] == slug]
        return calls

    @staticmethod
    def build_call(agent, turn):
        """One call in the contract shape; a running agent has no finish time, duration or token total yet."""
        running = agent["state"] == AGENT_RUNNING_STATE
        return {"agent_id": agent["id"], "role": agent["role"], "turn": turn, "task": agent.get("task"), "state": agent["state"],
                "return_status": agent.get("return_status"), "duration_s": None if running else agent["duration_s"],
                "tokens": None if running else agent["tokens"], "started_at": agent["started_at"],
                "finished_at": None if running else agent["finished_at"], "summary": agent["summary"]}

    # ------------------------------------------------ tasks
    def build_tasks_block(self, work_dir, slug, calls):
        """The task block of `/state` plus, on every task, the executors spawned for it (all attempts, oldest first)."""
        block = self.task_status_repository.read_feature_tasks(work_dir, slug)
        for task in block["tasks"]:
            task["executors"] = [{field: call[field] for field in EXECUTOR_FIELDS if field in call}
                                 for call in calls if call["task"] == task["id"]]
            mark_interrupted_executor(task)
        return block

    # ------------------------------------------------ steps
    def build_steps(self, row, calls):
        """(current step, [{step, state}]) from the ROADMAP status and the agents found."""
        status = row["status"]
        last_call_step = self.find_last_call_step(calls)
        current = self.find_current_step(status, row.get("note"), last_call_step)
        state = CURRENT_STEP_STATE_BY_STATUS.get(status, STEP_PENDING)
        if status == BUILDING_STATUS and current == STEP_BUILD_NAME and self.is_last_build_call_failed(calls):
            state = STEP_FAILED
        if current is None:
            return None, [{"step": step, "state": STEP_PENDING} for step in STEP_ORDER]
        position = STEP_ORDER.index(current)
        steps = []
        for index, step in enumerate(STEP_ORDER):
            if index < position:
                steps.append({"step": step, "state": STEP_DONE})
            elif index == position:
                steps.append({"step": step, "state": state})
            else:
                steps.append({"step": step, "state": STEP_PENDING})
        return current, steps

    @staticmethod
    def find_current_step(status, note, last_call_step):
        """Step the feature is in. A blocked feature is in the step of its last agent, else the one its note shows, else unknown (None)."""
        if status == BUILDING_STATUS and VERIFIER_NOTE_MARK in str(note or "").lower():
            return STEP_VERIFY_NAME
        if status in CURRENT_STEP_BY_STATUS:
            return CURRENT_STEP_BY_STATUS[status]
        if status != BLOCKED_STATUS:
            return None
        if last_call_step:
            return last_call_step
        if str(note or "").startswith(GATE_NOTE_PREFIX):
            return STEP_GATE_NAME
        if VERIFIER_NOTE_MARK in str(note or "").lower():
            return STEP_VERIFY_NAME
        return None

    @staticmethod
    def find_last_call_step(calls):
        for call in reversed(calls):
            step = get_step_for_role(call["role"])
            if step is not None:
                return STEP_NAME_BY_PIPELINE_STEP[step]
        return None

    @staticmethod
    def is_last_build_call_failed(calls):
        """True when the newest executor call failed and none is running (the feature is stalled in the build step)."""
        executors = [call for call in calls if get_step_for_role(call["role"]) == STEP_EXEC]
        if not executors or any(call["state"] == AGENT_RUNNING_STATE for call in executors):
            return False
        last = executors[-1]
        return last["state"] == AGENT_FAILED_STATE or last["return_status"] in FAILED_RETURN_STATUSES

    # ------------------------------------------------ claude
    def read_last_turn_claude(self, work_dir, numbers):
        """`{command, account, auth_method, auth_email, auth_plan, cc_account}` of the last turn; None without a turn."""
        if not numbers:
            return None
        record = self.loop_file_repository.read_turn(work_dir, numbers[-1])
        return dict({"command": record.get("claude") or None, "account": record.get("claude_account") or None}, **build_turn_auth(record))
