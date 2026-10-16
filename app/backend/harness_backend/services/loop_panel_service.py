"""Builds a project's panel `/state` and `/graph` (Mermaid + text from the engine)."""
import os
import re
import time

from ..repositories.loop_file_repository import graph_view, loop_state
from ..repositories.loop_read_formatting import now_iso, to_number, to_text_list

DEFAULT_LIMITS = {"max_cost": 30.0, "max_stall": 10, "timeout": 60}
LIMIT_FLAGS = {"--max-cost": ("max_cost", float), "--max-stall": ("max_stall", int), "--timeout": ("timeout", int)}
QUARANTINE_OPTIONS = ["try again", "skip and continue"]
KNOWN_ACTIONS = ("deliver", "summary", "push", "clean_worktrees")
RECENT_ACTIONS_LIMIT = 10
LOOP_WORKTREE_SUFFIX = "-worktree-loop"
SECONDS_PER_MINUTE = 60
PERCENT_DECIMALS = 1
COST_UNIT = "USD"
MINUTES_UNIT = "min"
ANSWER_HEADER_PATTERN = re.compile(r"^## Answer (\S+)(?:\s+\[([^\]]*)\])?\s*$")
QUESTION_PREFIX_PATTERN = re.compile(r"^\s*Question:\s*")
USER_ANSWER_LABEL = "User answer:"
AUTOMATIC_ANSWER_MARK = "automatic answer"
ANSWER_STATE_PENDING = "pending"
ANSWER_STATE_APPLIED = "applied"
ANSWER_STATE_AUTOMATIC = "automatic"
ANSWER_TEXT_LIMIT = 600
ANSWERS_LIMIT = 50
VALIDATE_LINE_PREFIX = "- "


def read_command_limits(loop_command):
    """Caps that run_loop received (LOOP-CMD.json); missing flag = run_loop default."""
    args = loop_command.get("argv")
    limits = dict(DEFAULT_LIMITS)
    if not isinstance(args, list):
        return limits
    for index, item in enumerate(args):
        name, _, joined_value = str(item).partition("=")
        if name not in LIMIT_FLAGS:
            continue
        key, converter = LIMIT_FLAGS[name]
        raw = joined_value if joined_value else (str(args[index + 1]) if index + 1 < len(args) else "")
        try:
            limits[key] = converter(raw)
        except ValueError:
            pass
    return limits


def build_limit(key, label, value, maximum, unit):
    percent = round(value / maximum * 100, PERCENT_DECIMALS) if maximum else None
    return {"key": key, "label": label, "value": value, "max": maximum or None, "unit": unit, "pct": percent}


def compute_current_turn(loop_json, meta, numbers, running):
    """Running = the next one after the last one done; stopped = the one in LOOP.json, else the last one done (or None)."""
    loop_turn = to_number(loop_json.get("turn"))
    turns_done = max([to_number(meta.get("turns")) or 0, loop_turn or 0] + numbers[-1:])
    if running:
        return turns_done + 1
    if loop_turn is not None:
        return loop_turn
    return turns_done or None


def get_project_name(meta, context):
    """Name of the repo folder (from meta.json when the engine wrote it), without the worktree suffix."""
    project_source = meta.get("repo") or context.repo_path
    return re.sub(re.escape(LOOP_WORKTREE_SUFFIX) + "$", "", os.path.basename(os.path.normpath(project_source)))


def parse_answers(text):
    """`## Answer <date> [marker]` blocks of ANSWERS.md (the last ANSWERS_LIMIT)."""
    blocks, current = [], None
    for line in text.splitlines():
        header = ANSWER_HEADER_PATTERN.match(line.strip())
        if header:
            current = {"date": header.group(1), "marker": (header.group(2) or "").strip().lower(), "lines": []}
            blocks.append(current)
        elif current is not None:
            current["lines"].append(line)
    answers = []
    for block in blocks:
        body = "\n".join(block["lines"]).strip()
        question, _, answer = body.partition(USER_ANSWER_LABEL)
        question = QUESTION_PREFIX_PATTERN.sub("", question).strip()
        if AUTOMATIC_ANSWER_MARK in (block["marker"] + " " + answer).lower():
            state = ANSWER_STATE_AUTOMATIC
        else:
            state = block["marker"] if block["marker"] in (ANSWER_STATE_PENDING, ANSWER_STATE_APPLIED) else ANSWER_STATE_PENDING
        answers.append({"date": block["date"], "question": question[:ANSWER_TEXT_LIMIT], "answer": answer.strip()[:ANSWER_TEXT_LIMIT],
                        "state": state})
    return answers[-ANSWERS_LIMIT:]


class LoopPanelService:
    def __init__(self, loop_status_service, loop_file_repository, roadmap_view_service, turn_service, agent_history_service, activity_monitor):
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.roadmap_view_service = roadmap_view_service
        self.turn_service = turn_service
        self.agent_history_service = agent_history_service
        self.activity_monitor = activity_monitor

    def build_state(self, context, project_url):
        work_dir = context.work_dir
        summary = self.loop_status_service.get_loop_summary(context)
        meta = summary["meta"]
        running = summary["running"]
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        roadmap_text = self.loop_file_repository.read_roadmap(work_dir)
        stages, features, counts, graph = self.roadmap_view_service.build_roadmap_view(work_dir, roadmap_text)
        turn = compute_current_turn(summary["json"], meta, numbers, running)

        return {
            "now": now_iso(), "project": get_project_name(meta, context), "repo": context.repo_path, "work": work_dir,
            "branch": meta.get("branch"),
            "project_ref": {"id": context.project.id, "name": context.project.name, "repo_path": context.project.repo_path},
            "loop": self.build_loop_block(work_dir, summary, turn),
            "meta": {"turns": meta.get("turns"), "cost_usd": meta.get("cost_usd"), "branch": meta.get("branch"), "pr": meta.get("pr"),
                     "pr_status": meta.get("pr_status")},
            "limits": self.build_limits(work_dir, meta, running, numbers), "stages": stages, "features": features, "counts": counts,
            "graph": graph, "agents": self.agent_history_service.build_state_agents(work_dir, summary, features, numbers),
            "validate": [line.strip() for line in self.loop_file_repository.read_state_file(work_dir, "VALIDATE.md").splitlines()
                         if line.strip().startswith(VALIDATE_LINE_PREFIX)],
            "recent_actions": self.list_recent_actions(work_dir),
            "roadmap_approval": self.loop_file_repository.read_roadmap_approval(work_dir),
            "answers": parse_answers(self.loop_file_repository.read_state_file(work_dir, "ANSWERS.md")),
            "turns": self.turn_service.list_turns(work_dir, turn if running else None),
            "quarantine": self.build_quarantine(features),
            "monitor": {"url": project_url, "idle_in_s": None, "shutdown_in_s": self.activity_monitor.seconds_until_shutdown()},
        }

    def build_loop_block(self, work_dir, summary, turn):
        loop_json, running = summary["json"], summary["running"]
        options = loop_json.get("options")
        to_check = loop_json.get("to_check")
        return {"status": summary["status"], "running": running, "pid": summary["pid"], "turn": turn,
                "feature": to_text_list(loop_json.get("feature")),
                "reason": None if running else (summary["reason"] or None),
                "next": None if running else (loop_json.get("next") or None),
                "options": [] if running else (to_text_list(options) if isinstance(options, list) else []),
                "to_check": to_text_list(to_check) if isinstance(to_check, list) else [],
                "at": loop_json.get("at"),
                "pause_requested": self.loop_file_repository.is_pause_requested(work_dir)}

    def build_limits(self, work_dir, meta, running, numbers):
        limits = read_command_limits(self.loop_file_repository.read_loop_command(work_dir))
        cost = to_number(meta.get("cost_usd")) or 0
        result = [build_limit("cost", "cost", cost, limits["max_cost"], COST_UNIT),
                  build_limit("turns_without_progress", "without progress", self.turn_service.count_turns_without_progress(work_dir),
                              limits["max_stall"], "")]
        if running:
            paths = [self.loop_file_repository.get_lock_path(work_dir)]
            paths += [self.loop_file_repository.get_turn_path(work_dir, number) for number in numbers[-1:]]
            moments = [moment for moment in (self.loop_file_repository.get_mtime(path) for path in paths) if moment is not None]
            start = max(moments or [time.time()])
            result.append(build_limit("turn_minutes", "current turn", round((time.time() - start) / SECONDS_PER_MINUTE, 1), limits["timeout"],
                                      MINUTES_UNIT))
        return result

    @staticmethod
    def build_quarantine(features):
        return [{"id": feature["id"],
                 "reason": "%s (%d failures)" % (feature["note"], feature["tries"]) if feature["note"] else "%d failures" % feature["tries"],
                 "stuck": [other["id"] for other in features if feature["id"] in other["deps"] and other["status"] != "verified"],
                 "options": list(QUARANTINE_OPTIONS)}
                for feature in features if feature["status"] == "blocked"]

    def list_recent_actions(self, work_dir):
        """Latest actions of the Act mode (`internal/actions.log`), newest first."""
        rows = [{"action": item["action"], "t": item.get("t"), "ok": bool(item.get("ok")), "summary": item.get("summary")}
                for item in self.loop_file_repository.read_action_lines(work_dir) if item.get("action") in KNOWN_ACTIONS]
        return rows[-RECENT_ACTIONS_LIMIT:][::-1]

    def build_graph(self, context):
        """Dependency graph (Mermaid), state graph (Mermaid) and ASCII text, from the engine (`graph_view` and `loop_state.graph`)."""
        roadmap_text = self.loop_file_repository.read_roadmap(context.work_dir)
        features, _ = loop_state.parse_roadmap(roadmap_text)
        return {"dependencies_mermaid": graph_view.mermaid(features), "states_mermaid": loop_state.graph(roadmap_text),
                "text": graph_view.ascii_graph(features)}
