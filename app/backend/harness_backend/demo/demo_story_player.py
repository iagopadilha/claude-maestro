"""Applies the demo story one step at a time to the files the panel reads, the way `run_loop.py` and a real turn would:
ROADMAP.md statuses, live agent events (`turns/NNN.events.jsonl`), `guard.log`, then per finished turn `turns/NNN.json`,
`meta.json`, `LOOP.json`, a snapshot, `STATE.md` and the Claude Code transcripts.

Time is virtual: each action moves a story clock forward so turn durations and transcript timestamps look like a real loop
(minutes per turn) while the animator plays a whole turn in a few seconds. Nothing here calls `claude`.
"""
import datetime
import hashlib
import json
import os
import uuid

from ..repositories.claude_transcript_repository import summarize_tool_call
from ..repositories.loop_file_repository import graph_view, loop_state
from ..services.agent_history_service import get_inferred_role
from .demo_story_catalog import (FEATURES, FEATURES_BY_SLUG, GOAL, INTEGRATION_BRANCH, LOOP_BRANCH, PROJECT_NAME, STAGE_CLOSINGS, STAGES,
                                 STATE_HEADER, TURN_COSTS_USD, WAVE, spec_file_path)
from .demo_transcript_writer import DemoTranscriptWriter
from .demo_workspace import append_json_line, remove_file, write_json_atomically, write_text_atomically

ACTION_VIRTUAL_SECONDS = 11
STEP_VIRTUAL_SECONDS = 25
TOKENS_BASE = 1400
TOKENS_SPREAD = 5200
ORCHESTRATOR_TOKENS_BASE = 9000
USD_PER_TOKEN = 0.0000045
EVENT_RESULT_LIMIT = 300
SESSION_NAMESPACE = uuid.UUID("6f1f3e0a-5a2b-4c1e-9d3e-1d0a7c9b2e55")
ORCHESTRATOR_EVENT_AGENT = "orch"
LOCK_FILE = "LOOP.lock"
ANSWER_PENDING_MARK = "[pending]"
ANSWER_APPLIED_MARK = "[applied]"
STOPPED_STATUSES = ("awaiting_user", "done")
FIELD_ORDER = ("status", "scope", "kind", "paths", "deps", "tries", "commit", "note")


def stable_number(*parts):
    return int(hashlib.sha1(":".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:8], 16)


def format_clock(seconds):
    seconds = max(0, int(seconds))
    return "%02d:%02d:%02d" % (seconds // 3600, seconds % 3600 // 60, seconds % 60)


def to_local_iso(moment):
    return moment.astimezone().replace(tzinfo=None).isoformat(timespec="seconds")


def render_roadmap(features):
    lines = ["# Roadmap: %s" % PROJECT_NAME, "", "Goal: " + GOAL, ""]
    for stage in STAGES:
        lines += ["## Stage %d - %s" % (stage["n"], stage["title"]), "", stage["description"], ""]
        for feature in FEATURES:
            if feature["stage"] != stage["n"]:
                continue
            current = features[feature["slug"]]
            values = {"status": current["status"], "scope": feature["scope"], "kind": feature["kind"], "paths": feature["paths"],
                      "deps": feature["deps"], "tries": str(current["tries"]) if current["tries"] else "", "commit": current["commit"] or "",
                      "note": current["note"] or ""}
            fields = " ".join("%s=%s" % (key, values[key]) for key in FIELD_ORDER if values[key])
            lines.append("- [%s] %s" % (feature["slug"], fields))
        lines.append("")
    return "\n".join(lines)


def render_spec(slug):
    feature = FEATURES_BY_SLUG[slug]
    rows = ["| %s-%02d | %s | Must |" % (feature["prefix"], index, text) for index, text in enumerate(feature["requirements"], 1)]
    return "\n".join(["# %s" % feature["title"], "", "SCOPE: %s" % feature["scope"], "", "## Goal", "", feature["goal"], "",
                      "## Requirements", "", "| ID | Requirement | Priority |", "|---|---|---|"] + rows +
                     ["", "## Paths", "", "- `%s`" % feature["paths"], ""])


def render_stage_closing(number):
    sections = STAGE_CLOSINGS[number]
    lines = ["# Stage %d closing" % number, ""]
    for title, text in sections.items():
        lines += ["## %s" % title, "", text, ""]
    return "\n".join(lines)


def render_feature_file(slug):
    feature = FEATURES_BY_SLUG[slug]
    comment = "//" if feature["file"].endswith((".ts", ".tsx")) else "#"
    return "%s %s (%s): written by the demo loop.\n%s %s\n" % (comment, feature["title"], slug, comment, feature["goal"])


def answer_immediately(action):
    return action["answer"]


class DemoStoryPlayer:
    def __init__(self, workspace, git, turn_plans, story_start, generation=0, keeps_lock=False, answer_provider=answer_immediately):
        """`story_start` = virtual time of the first action (aware datetime); `keeps_lock` = write LOOP.lock (own PID) while a
        turn runs, so the panel shows `running`; `answer_provider(action)` returns the answer to the loop's question."""
        self.workspace = workspace
        self.work_dir = workspace.repo_dir
        self.git = git
        self.turn_plans = turn_plans
        self.generation = generation
        self.keeps_lock = keeps_lock
        self.answer_provider = answer_provider
        self.virtual_now = story_start
        self.transcript_writer = DemoTranscriptWriter(workspace.transcripts_dir, self.work_dir)
        self.features = {feature["slug"]: {"status": "pending", "tries": 0, "commit": None, "note": None} for feature in FEATURES}
        self.plan_index = 0
        self.step_index = 0
        self.turn = None
        self.total_cost = 0.0

    # ------------------------------------------------ position
    def is_finished(self):
        return self.plan_index >= len(self.turn_plans)

    def get_completed_turns(self):
        return self.plan_index

    def get_next_turn_number(self):
        return None if self.is_finished() else self.turn_plans[self.plan_index][0]

    def apply_next_step(self):
        """Applies one step of the current turn (opening the turn first when needed). Returns False when the story is over."""
        if self.is_finished():
            return False
        number, steps = self.turn_plans[self.plan_index]
        if self.turn is None and self.step_index == 0:
            self.begin_turn(number)
        for action in steps[self.step_index]:
            self.apply_action(action)
        self.virtual_now += datetime.timedelta(seconds=STEP_VIRTUAL_SECONDS)
        self.step_index += 1
        if self.step_index >= len(steps):
            self.plan_index += 1
            self.step_index = 0
        return True

    def play_until_turn(self, completed_turns):
        while not self.is_finished() and self.get_completed_turns() < completed_turns:
            self.apply_next_step()

    # ------------------------------------------------ paths
    def loop_file(self, name):
        return loop_state.loop_path(self.work_dir, name)

    def project_file(self, name):
        return os.path.join(self.work_dir, ".specs", "project", name)

    def write_roadmap(self):
        write_text_atomically(self.project_file("ROADMAP.md"), render_roadmap(self.features))

    def write_lock(self):
        write_text_atomically(self.loop_file(LOCK_FILE), str(os.getpid()))

    def release_lock(self):
        remove_file(self.loop_file(LOCK_FILE))

    # ------------------------------------------------ turn lifecycle
    def begin_turn(self, number):
        session_id = str(uuid.uuid5(SESSION_NAMESPACE, "%s-%d-%d" % (PROJECT_NAME, self.generation, number)))
        self.turn = {"number": number, "session_id": session_id, "started_at": self.virtual_now, "touched": [],
                     "orchestrator_actions": [], "agents": {}, "events_path": os.path.join(loop_state.turns_dir(self.work_dir),
                                                                                           "%03d.events.jsonl" % number)}
        write_text_atomically(self.turn["events_path"], "")
        if self.keeps_lock:
            self.write_lock()

    def tick(self):
        moment = self.virtual_now
        self.virtual_now += datetime.timedelta(seconds=ACTION_VIRTUAL_SECONDS)
        return moment

    def elapsed_clock(self, moment):
        return format_clock((moment - self.turn["started_at"]).total_seconds())

    def touch(self, slug):
        if slug not in self.turn["touched"]:
            self.turn["touched"].append(slug)

    def get_agent(self, slug):
        """(panel agent id, transcript record) of the agent the panel infers for the feature's current status."""
        feature = FEATURES_BY_SLUG[slug]
        current = self.features[slug]
        role = get_inferred_role({"id": slug, "status": current["status"], "kind": feature["kind"], "note": current["note"]})
        key = "%s:%s" % (slug, role)
        record = self.turn["agents"].get(key)
        if record is None:
            record = {"agent_id": "a" + hashlib.sha1(("%d:%d:%s" % (self.generation, self.turn["number"], key)).encode("utf-8")).hexdigest()[:16],
                      "role": role, "slug": slug, "started_at": self.virtual_now, "actions": [],
                      "prompt": "Role: %s\nSCOPE_FEATURE: %s\nPATHS: %s\nSpec: %s\n\nWork only inside the PATHS; end with STATUS or VERDICT "
                                "and a SUMMARY line." % (role, slug, feature["paths"], spec_file_path(slug))}
            self.turn["agents"][key] = record
        return key, record

    def record_event(self, agent_key, event_type, text, result, moment, tokens, extra=None):
        event = {"agent": agent_key, "type": event_type, "text": text, "result": (result or "")[:EVENT_RESULT_LIMIT],
                 "t": self.elapsed_clock(moment), "tokens": tokens, "cost_usd": round(tokens * USD_PER_TOKEN, 4),
                 "duration_s": ACTION_VIRTUAL_SECONDS}
        event.update(extra or {})
        append_json_line(self.turn["events_path"], event)

    def count_tokens(self, *parts):
        return TOKENS_BASE + stable_number(self.turn["number"], *parts) % TOKENS_SPREAD

    # ------------------------------------------------ actions
    def apply_action(self, action):
        handler = getattr(self, "apply_" + action["kind"])
        handler(action)

    def apply_orchestrator(self, action):
        moment, tokens = self.tick(), self.count_tokens("orch", action["text"])
        self.turn["orchestrator_actions"].append({"type": "ai", "text": action["text"], "at": moment, "tokens": tokens})
        self.record_event(ORCHESTRATOR_EVENT_AGENT, "ai", action["text"], "", moment, tokens)

    def apply_orchestrator_tool(self, action):
        moment, tokens = self.tick(), self.count_tokens("orch", action["tool"], str(action["input"]))
        self.turn["orchestrator_actions"].append({"type": "tool", "tool": action["tool"], "input": action["input"], "result": action["result"],
                                                  "at": moment, "tokens": tokens})
        self.record_event(ORCHESTRATOR_EVENT_AGENT, "tool", summarize_tool_call(action["tool"], action["input"]), action["result"], moment, tokens)

    def apply_status(self, action):
        current = self.features[action["slug"]]
        current["status"], current["note"] = action["status"], action.get("note")
        self.touch(action["slug"])
        self.write_roadmap()

    def apply_agent_tool(self, action):
        key, record = self.get_agent(action["slug"])
        moment, tokens = self.tick(), self.count_tokens(key, action["tool"], str(action["input"]))
        record["actions"].append({"type": "tool", "tool": action["tool"], "input": action["input"], "result": action["result"], "at": moment,
                                  "tokens": tokens})
        self.record_event(key, "tool", summarize_tool_call(action["tool"], action["input"]), action["result"], moment, tokens)

    def apply_agent_text(self, action):
        key, record = self.get_agent(action["slug"])
        moment, tokens = self.tick(), self.count_tokens(key, action["text"])
        record["actions"].append({"type": "ai", "text": action["text"], "at": moment, "tokens": tokens})
        self.record_event(key, "ai", action["text"], "", moment, tokens)

    def apply_agent_return(self, action):
        key, record = self.get_agent(action["slug"])
        moment, tokens = self.tick(), self.count_tokens(key, "return")
        record["actions"].append({"type": "return", "text": action["text"], "at": moment, "tokens": tokens})
        self.record_event(key, "ai", action["text"], "", moment, tokens)

    def apply_guard_block(self, action):
        """The executor's tool call the PreToolUse guard refused: live `guard` event, `guard.log` line, transcript tool error."""
        key, record = self.get_agent(action["slug"])
        moment, tokens = self.tick(), self.count_tokens(key, "guard", action["rule"])
        record["actions"].append({"type": "guard", "tool": action["tool"], "input": action["input"], "rule": action["rule"],
                                  "reason": action["reason"], "at": moment, "tokens": tokens})
        self.record_event(key, "guard", summarize_tool_call(action["tool"], action["input"]),
                          "exit 2 · [%s] %s" % (action["rule"], action["reason"]), moment, tokens,
                          {"rule": action["rule"], "reason": action["reason"]})
        append_json_line(self.loop_file("guard.log"), {"t": to_local_iso(moment), "profile": action["profile"], "rule": action["rule"],
                                                       "decision": "blocked", "detail": action["reason"][:200]})

    def apply_spec(self, action):
        write_text_atomically(os.path.join(self.work_dir, spec_file_path(action["slug"])), render_spec(action["slug"]))

    def apply_commit(self, action):
        slug = action["slug"]
        relative = FEATURES_BY_SLUG[slug]["file"]
        write_text_atomically(os.path.join(self.work_dir, relative), render_feature_file(slug))
        commit = self.git.commit_paths([relative], "feat(%s): %s" % (slug, FEATURES_BY_SLUG[slug]["title"].lower()))
        current = self.features[slug]
        current["status"], current["note"], current["commit"] = "verified", None, commit
        self.touch(slug)
        self.write_roadmap()

    def apply_stage_closing(self, action):
        path = os.path.join(self.work_dir, ".specs", "project", "phases", "stage-%02d.md" % action["stage"])
        write_text_atomically(path, render_stage_closing(action["stage"]))

    def apply_wait_for_answer(self, action):
        """The user answers the loop's question (the animator may hold here); the next turn reads it from ANSWERS.md."""
        answer = self.answer_provider(action)
        block = "\n## Answer %s %s\nQuestion: %s\nUser answer: %s\n" % (to_local_iso(self.virtual_now), ANSWER_PENDING_MARK,
                                                                      action.get("question") or "the loop's question", answer)
        path = loop_state.state_file(self.work_dir, "ANSWERS.md")
        write_text_atomically(path, loop_state.read(path) + block)
        if self.keeps_lock:  # the answer relaunches the loop: running again until the next turn ends
            self.write_lock()

    def apply_end_turn(self, action):
        number, finished_at = self.turn["number"], self.tick()
        duration_ms = int((finished_at - self.turn["started_at"]).total_seconds() * 1000)
        cost = TURN_COSTS_USD[(number - 1) % len(TURN_COSTS_USD)]
        self.total_cost = round(self.total_cost + cost, 4)
        orchestrator_tokens = ORCHESTRATOR_TOKENS_BASE + sum(item["tokens"] for item in self.turn["orchestrator_actions"])
        write_json_atomically(os.path.join(loop_state.turns_dir(self.work_dir), "%03d.json" % number), {
            "type": "result", "subtype": "success", "is_error": False, "total_cost_usd": cost, "duration_ms": duration_ms,
            "num_turns": 4 + len(self.turn["orchestrator_actions"]), "result": action["result"], "session_id": self.turn["session_id"],
            "usage": {"input_tokens": orchestrator_tokens // 10, "cache_read_input_tokens": orchestrator_tokens * 8 // 10,
                      "output_tokens": orchestrator_tokens // 10},
            "permission_denials": [], "exit_code": 0, "timeout": False, "turn": number})
        self.mark_answers_applied()
        loop_json = {"status": action["status"], "turn": number, "feature": ",".join(self.turn["touched"]), "reason": action["reason"],
                     "next": action["next"], "at": to_local_iso(finished_at)}
        if action["options"]:
            loop_json["options"] = action["options"]
        if action["to_check"]:
            loop_json["to_check"] = action["to_check"]
        write_json_atomically(self.loop_file("LOOP.json"), loop_json)
        meta = self.read_meta()
        meta.update({"turns": number, "cost_usd": self.total_cost})
        if action["status"] == "done":
            meta.update({"stopped": "done", "reason": action["reason"], "finished_at": to_local_iso(finished_at)})
        write_json_atomically(self.loop_file("meta.json"), meta)
        state_path = self.project_file("STATE.md")
        write_text_atomically(state_path, (loop_state.read(state_path) or STATE_HEADER) + "- turn %d: %s\n" % (number, action["result"]))
        loop_state.snapshot(self.work_dir, number, self.git.get_head())
        try:
            graph_view.write(self.work_dir)
        except (OSError, ValueError):
            pass
        self.write_turn_transcripts(action, finished_at)
        if action["status"] in STOPPED_STATUSES:
            self.release_lock()
        self.turn = None

    def mark_answers_applied(self):
        path = loop_state.state_file(self.work_dir, "ANSWERS.md")
        text = loop_state.read(path)
        if ANSWER_PENDING_MARK in text:
            write_text_atomically(path, text.replace(ANSWER_PENDING_MARK, ANSWER_APPLIED_MARK))

    def read_meta(self):
        try:
            with open(self.loop_file("meta.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_turn_transcripts(self, action, finished_at):
        orchestrator = {"prompt": "/orchestrator continue --headless --turn %d --wave %d --branch %s" % (self.turn["number"], WAVE, LOOP_BRANCH),
                        "started_at": self.turn["started_at"], "actions": self.turn["orchestrator_actions"], "result": action["result"],
                        "finished_at": finished_at}
        self.transcript_writer.write_turn(self.turn["session_id"], orchestrator, list(self.turn["agents"].values()))


def build_initial_meta(started_at):
    return {"turns": 0, "cost_usd": 0, "branch": LOOP_BRANCH, "base": INTEGRATION_BRANCH, "started_at": to_local_iso(started_at)}
