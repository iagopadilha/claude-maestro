"""Loop agents: history per turn (Claude Code transcripts), full log of one agent and the agents of `/state`
(real ones from the last turn when there is a transcript and the loop is stopped; otherwise inferred from the feature statuses).

Never invents a number: tokens/cost per agent only with a transcript or `*.events.jsonl`; the rest is None.
"""
import re

from ..repositories.loop_read_formatting import format_minutes_clock, ms_to_seconds, redact_credentials, sum_usage_tokens, to_number
from .claude_account_service import build_turn_claude
from .turn_service import get_turn_status

HISTORY_ID_PATTERN = re.compile(r"^v(\d{1,6}):([A-Za-z0-9_-]{1,80})$")
HISTORY_ID_FORMAT = "v%03d:%s"
ORCHESTRATOR_ID = "orch"
ORCHESTRATOR_ROLE = "orchestrator"
CLOSED_STATUSES = ("verified", "blocked")
ROLE_BY_STATUS = {"pending": "spec-writer", "speccing": "spec-writer", "specced": "spec-reviewer"}
EXECUTOR_BY_KIND = {"back": "executor-back", "front": "executor-front"}
DEFAULT_EXECUTOR = "executor-generic"
VERIFIER_ROLE = "verifier"
VERIFIER_NOTE_MARK = "verif"
WORKING_STATUSES = ("speccing", "building")
LOOP_DONE_STATUS = "done"
LOG_TYPES = ("sys", "tool", "ai", "user", "guard")
SUMMARY_LIMIT = 200
ORCHESTRATOR_STATE_SUMMARY_LIMIT = 140
ACTION_LIMIT = 160
LOG_TEXT_LIMIT = 2000
ORCHESTRATOR_RESULT_LIMIT = 4000
LOG_ITEMS_IN_STATE = 8
GUARD_LOG_TYPE = "guard"
GUARD_RULE_LIMIT = 60


def build_event_log_item(event):
    item = {"type": event.get("type") if event.get("type") in LOG_TYPES else "sys",
            "text": redact_credentials(event.get("text"), LOG_TEXT_LIMIT), "result": redact_credentials(event.get("result"), LOG_TEXT_LIMIT),
            "t": str(event.get("t") or "")}
    if item["type"] == GUARD_LOG_TYPE:  # a live block by the PreToolUse guard keeps its rule and reason
        item["rule"] = redact_credentials(event.get("rule"), GUARD_RULE_LIMIT)
        item["reason"] = redact_credentials(event.get("reason"), LOG_TEXT_LIMIT)
    return item


def get_event_agent(event):
    return event.get("agent") or event.get("id")


def is_turn_failed(turn_record):
    return bool(turn_record.get("is_error") or turn_record.get("timeout") or turn_record.get("exit_code") not in (None, 0))


def get_orchestrator_last_action(turn_record, transcript):
    """From the transcript; without it, the turn result as model speech; else None."""
    if transcript and transcript.get("last_action"):
        return transcript["last_action"]
    text = " ".join(redact_credentials(turn_record.get("result")).split())
    return {"type": "ai", "text": text[:ACTION_LIMIT], "t": None} if text else None


def get_orchestrator_decision(turn_record, transcript):
    """1st line with text of the turn `result`; else the one derived from the transcript."""
    lines = [line.strip() for line in str(turn_record.get("result") or "").splitlines() if line.strip()]
    return redact_credentials(lines[0].lstrip("-*# ").strip(), SUMMARY_LIMIT) if lines else (transcript or {}).get("decision")


def get_inferred_role(feature):
    status = feature["status"]
    if status == "building" and VERIFIER_NOTE_MARK in str(feature.get("note") or "").lower():
        return VERIFIER_ROLE
    if status in ROLE_BY_STATUS:
        return ROLE_BY_STATUS[status]
    return EXECUTOR_BY_KIND.get(feature["kind"], DEFAULT_EXECUTOR)


class AgentHistoryService:
    def __init__(self, loop_file_repository, claude_transcript_repository, log):
        self.loop_file_repository = loop_file_repository
        self.claude_transcript_repository = claude_transcript_repository
        self.log = log

    # ------------------------------------------------ history (per turn)
    def build_orchestrator_agent(self, work_dir, number, turn_record, transcript):
        """Cost/duration/result come from turns/NNN.json; tokens and times from the transcript when there is one."""
        snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
        tokens = transcript["tokens"] if transcript and transcript["tokens"] is not None else sum_usage_tokens(turn_record.get("usage"))
        duration = ms_to_seconds(turn_record.get("duration_ms"))
        transcript_data = transcript or {}
        return {"id": HISTORY_ID_FORMAT % (number, ORCHESTRATOR_ID), "role": ORCHESTRATOR_ROLE, "feature": None,
                "state": "failed" if is_turn_failed(turn_record) else "done",
                "summary": redact_credentials(turn_record.get("result"), SUMMARY_LIMIT).replace("\n", " ") or transcript_data.get("summary"),
                "tokens": tokens, "duration_s": duration if duration is not None else transcript_data.get("duration_s"),
                "started_at": transcript_data.get("started_at"), "finished_at": transcript_data.get("finished_at"),
                "last_action": get_orchestrator_last_action(turn_record, transcript),
                "return_status": get_turn_status(turn_record, self.loop_file_repository.read_snapshot_loop_json(snapshot))
                or transcript_data.get("return_status"),
                "decision": get_orchestrator_decision(turn_record, transcript)}

    def list_turn_agents(self, work_dir, number, turn_record):
        """(agents, transcript_found): orchestrator + one agent per subagents/agent-*.jsonl of the session."""
        main_path, subagents = self.claude_transcript_repository.locate_session_files(work_dir, turn_record.get("session_id"))
        main = self.claude_transcript_repository.read_transcript(main_path) if main_path else None
        agents = [self.build_orchestrator_agent(work_dir, number, turn_record, main)]
        rows = self.build_subagent_rows(number, subagents)
        return agents + rows, main is not None or bool(rows)

    def build_subagent_rows(self, number, subagents, live=False):
        """One row per readable subagent transcript, oldest first. `live` = the session is still running: an unfinished, unfailed
        transcript is `running` (in a finished turn it stays `done`)."""
        rows = []
        for agent_id, path, meta_path in subagents:
            transcript = self.claude_transcript_repository.read_transcript(path)
            if transcript is None:
                continue
            if transcript["failed"]:
                state = "failed"
            elif live and not transcript["final"]:
                state = "running"
            else:
                state = "done"
            rows.append({"id": HISTORY_ID_FORMAT % (number, agent_id), "role": self.claude_transcript_repository.read_agent_type(meta_path),
                         "feature": transcript["feature"], "task": transcript["task"], "state": state, "summary": transcript["summary"],
                         "tokens": transcript["tokens"], "duration_s": transcript["duration_s"], "started_at": transcript["started_at"],
                         "finished_at": transcript["finished_at"], "last_action": transcript["last_action"],
                         "return_status": transcript["return_status"], "decision": transcript["decision"]})
        rows.sort(key=lambda row: (row["started_at"] is None, row["started_at"] or "", row["id"]))
        return rows

    def find_live_session_id(self, work_dir):
        """Session of the turn in progress (a running turn has no turns/NNN.json yet): the most recent session of the project modified after
        the turn started (lock file or last turn file, the later one). Best effort: None when it cannot be told, never raises."""
        try:
            repository = self.loop_file_repository
            paths = [repository.get_lock_path(work_dir)]
            paths += [repository.get_turn_path(work_dir, number) for number in repository.list_turn_numbers(work_dir)[-1:]]
            moments = [moment for moment in (repository.get_mtime(path) for path in paths) if moment is not None]
            if not moments:
                return None
            return self.claude_transcript_repository.find_live_session_id(work_dir, max(moments))
        except Exception as failure:
            self.log.warn("loop_read_live_session_failed", type=type(failure).__name__)
            return None

    def list_live_subagents(self, work_dir, number, excluded_session_ids=()):
        """Subagent rows (id `vNNN:...` of the turn in progress, state `running` while unfinished) of the live session; [] when not found
        or when the session is one that a finished turn already owns (`excluded_session_ids`)."""
        session_id = self.find_live_session_id(work_dir)
        if session_id is None or session_id in excluded_session_ids:
            return []
        try:
            _, subagents = self.claude_transcript_repository.locate_session_files(work_dir, session_id)
            return self.build_subagent_rows(number, subagents, live=True)
        except Exception as failure:
            self.log.warn("loop_read_live_subagents_failed", turn=number, type=type(failure).__name__)
            return []

    def build_history(self, context):
        work_dir = context.work_dir
        turns = []
        for number in self.loop_file_repository.list_turn_numbers(work_dir):
            record = self.loop_file_repository.read_turn(work_dir, number)
            snapshot = self.loop_file_repository.load_snapshot(work_dir, number)
            snapshot_loop = self.loop_file_repository.read_snapshot_loop_json(snapshot)
            try:
                agents, found = self.list_turn_agents(work_dir, number, record)
            except Exception as failure:
                self.log.warn("loop_read_turn_agents_failed", turn=number, type=type(failure).__name__)
                agents, found = [self.build_orchestrator_agent(work_dir, number, record, None)], False
            turns.append({"n": number, "session_id": record.get("session_id") if isinstance(record.get("session_id"), str) else None,
                          "cost_usd": to_number(record.get("total_cost_usd")), "duration_s": ms_to_seconds(record.get("duration_ms")),
                          "status": get_turn_status(record, snapshot_loop), "transcript": found,
                          **build_turn_claude(record), "agents": [dict(agent, **build_turn_claude(record)) for agent in agents]})
        turns.sort(key=lambda turn: turn["n"], reverse=True)
        return {"turns": turns}

    def get_history_log(self, work_dir, agent_id):
        """(list, truncated) of a `vNNN:...` id; id outside the pattern = None; missing/corrupted = ([], False)."""
        found = HISTORY_ID_PATTERN.match(agent_id)
        if not found:
            return None
        number, name = int(found.group(1)), found.group(2)
        record = self.loop_file_repository.read_turn(work_dir, number)
        session_id = record.get("session_id")
        if not record and name != ORCHESTRATOR_ID:  # the turn in progress has no record yet: its executors are in the live session
            session_id = self.find_live_session_id(work_dir)
        main_path, subagents = self.claude_transcript_repository.locate_session_files(work_dir, session_id)
        path = main_path if name == ORCHESTRATOR_ID else next((item[1] for item in subagents if item[0] == name), None)
        transcript = self.claude_transcript_repository.read_transcript(path) if path else None
        if transcript is not None:
            return list(transcript["log"]), transcript["truncated"]
        if name == ORCHESTRATOR_ID and record.get("result"):
            return [{"type": "ai", "text": redact_credentials(record.get("result"), LOG_TEXT_LIMIT), "result": "",
                     "t": format_minutes_clock(0)}], False
        return [], False

    def get_agent_log(self, context, agent_id):
        """Body of `/agent/<id>/log`: a history id (with `truncated`) or a `/state` id (events of the last turn)."""
        work_dir = context.work_dir
        history = self.get_history_log(work_dir, agent_id)
        if history is not None:
            return {"id": agent_id, "log": history[0], "truncated": history[1]}
        return {"id": agent_id, "log": self.list_event_log(work_dir, agent_id)}

    def list_event_log(self, work_dir, agent_id):
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        events_turn = self.loop_file_repository.get_live_events_turn(work_dir, numbers)
        if events_turn is None:
            return []
        events = [build_event_log_item(event) for event in self.loop_file_repository.read_turn_events(work_dir, events_turn)
                  if get_event_agent(event) == agent_id]
        if not numbers:
            return events
        if agent_id == ORCHESTRATOR_ID:
            return self.build_orchestrator_log_head(self.loop_file_repository.read_turn(work_dir, numbers[-1])) + events
        return events

    @staticmethod
    def build_orchestrator_log_head(last_turn):
        if not last_turn.get("result"):
            return []
        duration = to_number(last_turn.get("duration_ms"))
        return [{"type": "ai", "text": redact_credentials(last_turn.get("result"), ORCHESTRATOR_RESULT_LIMIT), "result": "",
                 "t": format_minutes_clock(duration / 1000 if duration else 0)}]

    # ------------------------------------------------ agents of /state
    def list_real_agents(self, work_dir, numbers, running):
        """Real agents of the last turn (transcript) in the `/state` format. None = do not use (loop running or no transcript)."""
        if running or not numbers:
            return None
        record = self.loop_file_repository.read_turn(work_dir, numbers[-1])
        try:
            agents, found = self.list_turn_agents(work_dir, numbers[-1], record)
        except Exception as failure:
            self.log.warn("loop_read_real_agents_failed", turn=numbers[-1], type=type(failure).__name__)
            return None
        if not found:
            return None
        converted = []
        for agent in agents:
            is_orchestrator = agent["role"] == ORCHESTRATOR_ROLE
            agent_log = (self.get_history_log(work_dir, agent["id"]) or ([], False))[0]
            converted.append({"id": ORCHESTRATOR_ID if is_orchestrator else agent["id"], "role": agent["role"], "feature": agent["feature"], "task": agent.get("task"),
                              "state": "idle" if is_orchestrator else agent["state"], "summary": agent["summary"], "tokens": agent["tokens"],
                              "cost_usd": to_number(record.get("total_cost_usd")) if is_orchestrator else None,
                              "duration_s": agent["duration_s"], "log": agent_log[-LOG_ITEMS_IN_STATE:],
                              "last_action": agent.get("last_action"), "return_status": agent.get("return_status"),
                              "decision": agent.get("decision")})
        return converted

    def build_state_agents(self, work_dir, loop_summary, features, numbers):
        running = loop_summary["running"]
        last_turn = self.loop_file_repository.read_turn(work_dir, numbers[-1]) if numbers else {}
        real = self.list_real_agents(work_dir, numbers, running)
        events_by_agent = {}
        events_turn = self.loop_file_repository.get_live_events_turn(work_dir, numbers)
        for event in (self.loop_file_repository.read_turn_events(work_dir, events_turn) if events_turn is not None else []):
            events_by_agent.setdefault(get_event_agent(event), []).append(event)

        def sum_totals(agent_id, default):
            own = events_by_agent.get(agent_id) or []
            if not own:
                return {"tokens": default[0], "cost_usd": default[1], "duration_s": default[2]}

            def sum_field(key):
                if not any(to_number(event.get(key)) is not None for event in own):
                    return None
                return sum(to_number(event.get(key)) or 0 for event in own)
            return {"tokens": sum_field("tokens"), "cost_usd": sum_field("cost_usd"), "duration_s": sum_field("duration_s")}

        duration_ms = to_number(last_turn.get("duration_ms"))
        orchestrator_log = self.build_orchestrator_log_head(last_turn)
        orchestrator_log += [build_event_log_item(event) for event in events_by_agent.get(ORCHESTRATOR_ID, [])]
        orchestrator_state = "running" if running else ("done" if loop_summary["status"] == LOOP_DONE_STATUS else "idle")
        orchestrator = {"id": ORCHESTRATOR_ID, "role": ORCHESTRATOR_ROLE, "feature": None, "state": orchestrator_state,
                        "summary": redact_credentials(last_turn.get("result"), ORCHESTRATOR_STATE_SUMMARY_LIMIT).replace("\n", " ") or None,
                        "last_action": get_orchestrator_last_action(last_turn, None), "return_status": None if running else loop_summary["status"],
                        "decision": get_orchestrator_decision(last_turn, None)}
        orchestrator.update(sum_totals(ORCHESTRATOR_ID, (sum_usage_tokens(last_turn.get("usage")), to_number(last_turn.get("total_cost_usd")),
                                                         ms_to_seconds(duration_ms))))
        orchestrator["log"] = orchestrator_log[-LOG_ITEMS_IN_STATE:] if orchestrator_log else []
        agents = [orchestrator]
        if real is not None:
            real_orchestrator = next((agent for agent in real if agent["id"] == ORCHESTRATOR_ID), None)
            if real_orchestrator and real_orchestrator["tokens"] is not None:
                orchestrator["tokens"] = real_orchestrator["tokens"]
            if real_orchestrator and real_orchestrator.get("last_action"):
                orchestrator["last_action"] = real_orchestrator["last_action"]
            return agents + [agent for agent in real if agent["id"] != ORCHESTRATOR_ID]
        for feature in features:
            if feature["status"] in CLOSED_STATUSES:
                continue
            role = get_inferred_role(feature)
            agent_id = "%s:%s" % (feature["id"], role)
            if not running:
                agent_state = "idle"
            else:
                agent_state = "running" if feature["status"] in WORKING_STATUSES else "queued"
            own_log = [build_event_log_item(event) for event in events_by_agent.get(agent_id, [])]
            agent = {"id": agent_id, "role": role, "feature": feature["id"], "state": agent_state, "summary": feature.get("note"),
                     "last_action": None, "return_status": None, "decision": None}
            agent.update(sum_totals(agent_id, (None, None, None)))
            agent["log"] = own_log[-LOG_ITEMS_IN_STATE:]
            agents.append(agent)
        return agents
