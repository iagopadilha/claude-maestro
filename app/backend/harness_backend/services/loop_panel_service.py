"""Builds a project's panel `/state` and `/graph` (Mermaid + text from the engine)."""
import os
import re
import time

from ..repositories.loop_file_repository import graph_view, loop_state
from ..repositories.loop_read_formatting import now_iso, to_number, to_text_list
from ..domain.roadmap_settings import parse_loop_settings
from ..domain.project_errors import InvalidPathError, SpecFileNotFoundError
from ..repositories.specs_file_repository import compute_bytes_version, run_hints  # noqa: F401  (also puts the engine folder on sys.path)
import subprojects as subprojects_engine  # noqa: E402
from .claude_account_service import build_turn_claude, build_usage_totals, build_usage_view  # noqa: E402
from .decisions_service import DecisionsService  # noqa: E402

DEFAULT_LIMITS = {"max_cost": 50.0, "max_stall": 10, "timeout": 60}
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
CODEBASE_MAP_FOLDER = ("docs", "codebase")
CODEBASE_MAP_EXTENSIONS = (".md", ".json")
ROOT_GROUP_FOLDER = "."
ROOT_GROUP_LABEL = "Project"
BUILDING_STATUS = "building"


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


def effective_cost_cap(meta, command_limits):
    """The cost cap in force: `meta.max_cost_usd` (raised in the panel) wins, else the loop command's --max-cost, else the default."""
    raised = to_number(meta.get("max_cost_usd"))
    return raised if raised else command_limits["max_cost"]


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
    def __init__(self, loop_status_service, loop_file_repository, roadmap_view_service, turn_service, agent_history_service, activity_monitor,
                 guard_event_service, planning_requirement_service, decisions_service=None, task_status_repository=None, claude_account_service=None,
                 claude_call_log_service=None):
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.roadmap_view_service = roadmap_view_service
        self.turn_service = turn_service
        self.agent_history_service = agent_history_service
        self.activity_monitor = activity_monitor
        self.guard_event_service = guard_event_service
        self.planning_requirement_service = planning_requirement_service
        self.decisions_service = decisions_service or DecisionsService()
        self.task_status_repository = task_status_repository  # None = no `tasks` block in the features
        self.claude_account_service = claude_account_service  # None = `meta.claude_account_info` only repeats what meta.json has
        self.claude_call_log_service = claude_call_log_service  # None = the totals count only the loop turns

    def build_state(self, context, project_url):
        work_dir = context.work_dir
        summary = self.loop_status_service.get_loop_summary(context)
        meta = summary["meta"]
        running = summary["running"]
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        roadmap_text = self.loop_file_repository.read_roadmap(work_dir)
        stages, features, counts, graph = self.roadmap_view_service.build_roadmap_view(work_dir, roadmap_text)
        turn = compute_current_turn(summary["json"], meta, numbers, running)
        guard = self.guard_event_service.build_guard_summary(work_dir)
        self.attach_tasks_to_features(work_dir, features)
        turn_rows = self.turn_service.list_turns(work_dir, turn if running else None)
        calls = self.list_panel_calls(context)
        agents = self.agent_history_service.build_state_agents(work_dir, summary, features, numbers)
        loop_settings = parse_loop_settings(roadmap_text)
        agent_claude = build_turn_claude(meta)  # every agent runs inside the turn's process: same command and sign-in
        agents = [dict(agent, **agent_claude) for agent in agents]

        return {
            "now": now_iso(), "project": get_project_name(meta, context), "repo": context.repo_path, "work": work_dir,
            "branch": meta.get("branch"),
            "project_ref": {"id": context.project.id, "name": context.project.name, "repo_path": context.project.repo_path},
            "loop": self.build_loop_block(work_dir, summary, turn),
            "meta": {"turns": meta.get("turns"), "cost_usd": meta.get("cost_usd"),
                     "max_cost_usd": effective_cost_cap(meta, read_command_limits(self.loop_file_repository.read_loop_command(work_dir))), "branch": meta.get("branch"), "pr": meta.get("pr"),
                     "pr_status": meta.get("pr_status"), "claude": meta.get("claude"), "claude_account": meta.get("claude_account"),
                     "claude_switches": meta.get("claude_switches"), "claude_account_info": self.build_claude_account(meta),
                     "claude_totals": self.build_claude_totals(context, turn_rows, calls),
                     "claude_usage_view": self.build_claude_usage_view(meta, turn_rows, calls), **build_turn_claude(meta),
                     "max_parallel_tasks": loop_settings["settings"]["max_parallel_tasks"],
                     "max_parallel_tasks_in_header": "max_parallel_tasks" not in loop_settings["missing"]},
            "limits": self.build_limits(work_dir, meta, running, numbers), "stages": stages, "features": features, "counts": counts,
            "graph": graph, "agents": agents,
            "validate": [line.strip() for line in self.loop_file_repository.read_state_file(work_dir, "VALIDATE.md").splitlines()
                         if line.strip().startswith(VALIDATE_LINE_PREFIX)],
            "decisions_summary": self.decisions_service.open_counts(context),
            "recent_actions": self.list_recent_actions(work_dir),
            "roadmap_approval": self.loop_file_repository.read_roadmap_approval(work_dir, context.repo_path),
            "planning_required": self.planning_requirement_service.is_planning_required(context),
            "answers": parse_answers(self.loop_file_repository.read_state_file(work_dir, "ANSWERS.md")),
            "turns": turn_rows,
            "quarantine": self.build_quarantine(features),
            "guard_events": guard["events"], "guard_counts": guard["counts"],
            "monitor": {"url": project_url, "idle_in_s": None, "shutdown_in_s": self.activity_monitor.seconds_until_shutdown()},
        }

    def list_panel_calls(self, context):
        """Every kept panel call (chat, map, roadmap...), newest first; [] when there is no call log service."""
        if self.claude_call_log_service is None:
            return []
        return self.claude_call_log_service.list_all_calls([context.work_dir, context.repo_path])

    def build_claude_totals(self, context, turn_rows, calls=None):
        """Turns and panel calls (chat, map, roadmap...) with their cost, per sign-in method and per command; see build_usage_totals."""
        return build_usage_totals(turn_rows, self.list_panel_calls(context) if calls is None else calls)

    def build_claude_usage_view(self, meta, turn_rows, calls):
        """5-hour and weekly usage per command/account from meta, turn logs and the call log; never waits and never queries Claude."""
        if self.claude_account_service is None:
            return build_usage_view(meta, (), turn_rows, calls)
        return self.claude_account_service.build_usage_view(meta, turn_rows, calls)

    def build_claude_account(self, meta):
        """Structured account in use ({command, role, method, email, org, plan, cc_account, switches, label}); never waits for `auth status`."""
        if self.claude_account_service is None:
            return None
        return self.claude_account_service.build_current_account(meta)

    def attach_tasks_to_features(self, work_dir, features):
        """Adds `tasks` ({source, max_parallel, tasks:[{id,title,status,wave,deps,files,parallel,note}], waves}) to each feature that has a
        tasks.md and to each feature being built (empty list without tasks.md); others get no key."""
        if self.task_status_repository is None:
            return
        for feature in features:
            block = self.task_status_repository.read_feature_tasks(work_dir, feature["id"])
            if block["tasks"] or feature.get("status") == BUILDING_STATUS:
                feature["tasks"] = block

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
        result = [build_limit("cost", "cost", cost, effective_cost_cap(meta, limits), COST_UNIT),
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

    # ------------------------------------------------ codebase maps (read only)
    @staticmethod
    def list_map_files(work_dir, folder):
        """Files of one `docs/codebase` folder (relative to the work folder, "/" separators), sorted; [] when the folder is missing."""
        parts = [] if folder == ROOT_GROUP_FOLDER else folder.split("/")
        map_folder = os.path.join(work_dir, *parts, *CODEBASE_MAP_FOLDER)
        files = []
        for current, folders, names in os.walk(map_folder):
            folders.sort()
            for name in sorted(names):
                if name.lower().endswith(CODEBASE_MAP_EXTENSIONS):
                    relative = os.path.relpath(os.path.join(current, name), work_dir).replace(os.sep, "/")
                    files.append({"path": relative, "name": name})
        return files

    def build_codebase_tree(self, context):
        """`GET codebase/tree`: `{groups:[{folder, label, files:[{path, name}]}]}` for the root map and the map of each non-skipped subproject
        of the stored list (monorepo). Groups without files are left out; nothing is written."""
        work_dir = context.work_dir
        entry = subprojects_engine.load(work_dir)["entry"]
        candidates = [(ROOT_GROUP_FOLDER, ROOT_GROUP_LABEL)]
        candidates += [(project["path"].replace("\\", "/").strip("/"), project.get("name") or project["path"])
                       for project in subprojects_engine.entry_projects(entry) if not project.get("skip")]
        groups = []
        for folder, label in candidates:
            files = self.list_map_files(work_dir, folder)
            if files:
                groups.append({"folder": folder, "label": label, "files": files})
        return {"groups": groups}

    def read_codebase_file(self, context, path):
        """`GET codebase/file?path=`: read-only content of ONE file listed by `build_codebase_tree` (same shape as `specs/file`, never editable).
        A path with `..`, a drive or a leading slash = InvalidPathError (400); a path the tree does not list = SpecFileNotFoundError (404)."""
        text = str(path or "").replace("\\", "/")
        if not text.strip() or text.startswith("/") or re.match(r"^[A-Za-z]:", text) or ".." in text.split("/"):
            raise InvalidPathError("invalid path")
        allowed = {item["path"] for group in self.build_codebase_tree(context)["groups"] for item in group["files"]}
        if text not in allowed:
            raise SpecFileNotFoundError("file not found in the codebase maps: %s" % text)
        try:
            with open(os.path.join(context.work_dir, *text.split("/")), "rb") as handle:
                content = handle.read()
        except OSError:
            raise SpecFileNotFoundError("file not found in the codebase maps: %s" % text)
        return {"path": text, "text": content.decode("utf-8", "replace"), "version": compute_bytes_version(content), "editable": False,
                "not_editable_reason": "read-only view"}
