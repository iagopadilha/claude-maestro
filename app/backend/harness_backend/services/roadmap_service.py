"""Project roadmap: structured read, approval (only by the user's click) and the headless `/orchestrator` jobs that
generate and refine the roadmap and map the codebase.

Approval requires `confirm:true`, refuses with the loop running or with a planning gate not ok (422 `planning_gates`,
see planning_gates.py) and writes through the engine function
(`loop_state.approve_roadmap`), the same signature `run_loop.py --check` verifies.
The app never approves on its own: a job only generates; the user's POST approves. In planning mode (never approved, never
looped) the one-shot generate/refine jobs are refused (`planning_required`): the planning chat replaces them.
"""
import datetime
import os
import re
from typing import Protocol

from ..domain.project_errors import (PLANNING_GATES_CODE, InvalidRequestError, InvalidRoadmapError, JobInProgressError,
                                     LoopRunningError, LoopWorktreeExistsError, PlanningGatesError, RoadmapSessionMissingError)
from ..domain.roadmap_settings import parse_roadmap_e2e
from ..repositories.specs_file_repository import loop_state, run_hints
from .claude_job_service import JOB_TYPE_MAP, JOB_TYPE_ROADMAP, JOB_TYPE_ROADMAP_REFINE, ROADMAP_JOB_TYPES, ClaudeJobCommand

GOAL_FILE_NAME = "roadmap-goal.md"
GOAL_RELATIVE_PATH = "/".join((".specs", "project", loop_state.LOOP_FOLDER_NAME, loop_state.INTERNAL_FOLDER_NAME, GOAL_FILE_NAME))
GENERATE_ROADMAP_PROMPT = "/orchestrator roadmap --headless --goal-file " + GOAL_RELATIVE_PATH
MAP_FIRST_FLAG = "--map-first"
MAP_CODEBASE_PROMPT = "/orchestrator map"
MAP_ALLOWED_FOLDERS = (".specs", run_hints.MAP_FOLDER)  # the map job may only change files here; anything else is undone after the job
MAP_GUARD_IGNORED_PATHS = (".git", "node_modules", ".specs/project/loop/internal", ".specs/project/loop/json/MONITOR.json",
                        ".specs/.trash")
MAP_NOTHING_WRITTEN = ("The AI finished but wrote no document in docs/codebase, so nothing was mapped. The usual cause: the /orchestrator "
                       "skill is not loaded in that claude session (update the plugin: claude plugin update "
                       "loop-engineering-harness@loop-engineering-harness) and the AI answered as a plain chat.")
MAP_GUARD_NOTE = "\n\n(The app undid changes outside .specs/ and docs/codebase/: %s)"
MAP_GUARD_FILES_SHOWN = 8
REFINE_ROADMAP_PREFIX = ("Continue in this session's `/orchestrator roadmap --headless` mode: adjust .specs/project/ROADMAP.md "
                         "and .specs/project/FLOWS.md (one flow per feature), and PROJECT.md if needed, following the request below, in the grammar of references/state.md. Do not ask "
                         "(doubts go to the `## Open questions` section of the ROADMAP), never approve the roadmap, and finish.\n\n"
                         "User request:\n")
JOB_PERMISSION_MODE = "auto"  # the same default as run_loop (--permission-mode auto)
DEFAULT_JOB_MODEL = ""  # empty = the account's default model, as in run_loop
DEFAULT_BUDGET_USD = 5.0
MAX_BUDGET_USD = 50.0
GOAL_TEXT_LIMIT = 20000
STAGE_TITLE_PATTERN = re.compile(r"^#{1,6}\s*Stage\s+(\S+)\s*(?:[—–:-]+\s*)?(.*)$", re.I)
SECTION_TITLE_PATTERN = re.compile(r"^#{1,6}\s+(.*)$")
OPEN_QUESTIONS_TITLE = "open questions"
NO_QUESTION_ANSWERS = ("none", "none.", "-")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.S)


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def list_roadmap_stages(text, features):
    """[{"n","title","features":[slug]}] in file order (the number comes from the engine; the title, from the rest of the line)."""
    titles = {}
    for line in (text or "").splitlines():
        found = STAGE_TITLE_PATTERN.match(line.strip())
        if found:
            titles.setdefault(found.group(1), found.group(2).strip())
    stages = {}
    for slug, fields in features.items():
        number = fields.get("stage", "")
        stages.setdefault(number, {"n": number, "title": titles.get(number, ""), "features": []})["features"].append(slug)
    return list(stages.values())


def extract_open_questions(text):
    """Items of the `## Open questions` section (the headless /orchestrator mode puts there what it cannot ask)."""
    questions, inside = [], False
    for line in HTML_COMMENT_PATTERN.sub("", text or "").splitlines():  # a (multi-line) comment is never a question
        title = SECTION_TITLE_PATTERN.match(line.strip())
        if title:
            inside = title.group(1).strip().lower().startswith(OPEN_QUESTIONS_TITLE)
            continue
        item = line.strip().lstrip("-*").strip()
        if inside and item and item.lower() not in NO_QUESTION_ANSWERS:
            questions.append(item)
    return questions


class IRoadmapService(Protocol):
    def get_roadmap(self, context) -> dict: ...
    def approve_roadmap(self, context, confirm) -> dict: ...
    def generate_roadmap(self, context, goal, map_first=False, budget_usd=None) -> dict: ...
    def refine_roadmap(self, context, text, budget_usd=None) -> dict: ...
    def map_codebase(self, context, budget_usd=None) -> dict: ...


class RoadmapService:
    def __init__(self, specs_file_repository, project_progress_service, claude_job_service, claude_client, planning_requirement_service,
                 workspace_file_guard, log):
        self.specs_file_repository = specs_file_repository
        self.project_progress_service = project_progress_service
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.planning_requirement_service = planning_requirement_service
        self.workspace_file_guard = workspace_file_guard
        self.log = log

    # ------------------------------------------------ read
    def get_roadmap(self, context):
        text = self.specs_file_repository.read_text(os.path.join(context.specs_dir, "project", "ROADMAP.md")) or ""
        features, errors = loop_state.parse_roadmap(text)
        errors = errors + loop_state.check_fields(features)
        approval = loop_state.roadmap_approval(context.work_dir)
        return {"text": text, "approved": approval["approved"], "not_approved_reason": approval["reason"],
                "approved_at": approval["at"], "stages": list_roadmap_stages(text, features),
                "features": [dict(fields, slug=slug) for slug, fields in features.items()], "errors": errors,
                "open_questions": extract_open_questions(text), "e2e": bool(parse_roadmap_e2e(text))}

    # ------------------------------------------------ approval
    def approve_roadmap(self, context, confirm):
        if confirm is not True:
            raise InvalidRequestError("confirm must be true")
        if self.project_progress_service.is_loop_running(context):
            raise LoopRunningError("loop running: the roadmap was already approved")
        self.ensure_planning_gates_ok(context)
        try:
            approval = loop_state.approve_roadmap(context.work_dir, now_iso())
        except ValueError as failure:
            raise InvalidRoadmapError(str(failure))
        self.planning_requirement_service.clear_reopened(context)
        self.log.info("roadmap_approved", project=context.project.id, features=approval["features"])
        return approval

    def ensure_planning_gates_ok(self, context):
        gates = self.project_progress_service.build_planning_gates(context)
        failing = [gate for gate in gates if not gate["ok"] and not gate["optional"]]
        if failing:
            self.log.info("roadmap_approval_refused", project=context.project.id, gates=[gate["id"] for gate in failing])
            raise PlanningGatesError("the roadmap cannot be approved yet: %s" % "; ".join(
                "%s (%s)" % (gate["label"], gate["detail"]) if gate["detail"] else gate["label"] for gate in failing),
                gates, PLANNING_GATES_CODE)

    # ------------------------------------------------ jobs
    def generate_roadmap(self, context, goal, map_first=False, budget_usd=None):
        self.planning_requirement_service.ensure_planning_done(context)
        goal_text = self.validate_text(goal, "goal")
        budget = self.validate_budget(budget_usd)
        self.ensure_repo_free_for_job(context)
        self.write_goal(context.repo_path, goal_text, bool(map_first))
        prompt = GENERATE_ROADMAP_PROMPT + (" " + MAP_FIRST_FLAG if map_first else "")
        argv = self.claude_client.build_command(DEFAULT_JOB_MODEL, budget, no_mcp=False, prompt_argument=prompt,
                                                permission_mode=JOB_PERMISSION_MODE)
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_ROADMAP, ClaudeJobCommand(argv))

    def refine_roadmap(self, context, text, budget_usd=None):
        self.planning_requirement_service.ensure_planning_done(context)
        request = self.validate_text(text, "text")
        budget = self.validate_budget(budget_usd)
        self.ensure_repo_free_for_job(context)
        session = self.claude_job_service.get_last_session(context.repo_path, ROADMAP_JOB_TYPES)
        if not session:
            raise RoadmapSessionMissingError("no roadmap generated by the app in this project to refine: generate the roadmap first")
        argv = self.claude_client.build_command(DEFAULT_JOB_MODEL, budget, no_mcp=False, resume_session=session,
                                                permission_mode=JOB_PERMISSION_MODE)
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_ROADMAP_REFINE,
                                                 ClaudeJobCommand(argv, REFINE_ROADMAP_PREFIX + request, session))

    def map_codebase(self, context, budget_usd=None):
        budget = self.validate_budget(budget_usd)
        self.ensure_repo_free_for_job(context)
        snapshot = self.workspace_file_guard.take_snapshot(context.repo_path, MAP_GUARD_IGNORED_PATHS)
        argv = self.claude_client.build_command(DEFAULT_JOB_MODEL, budget, no_mcp=True, prompt_argument=MAP_CODEBASE_PROMPT,
                                                permission_mode=JOB_PERMISSION_MODE, hook_profile="spec")
        command = ClaudeJobCommand(argv, finalize=lambda result: self.finish_map(context, snapshot, result))
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_MAP, command)

    def finish_map(self, context, snapshot, result):
        """Runs in the job thread: undoes changes outside .specs/ and docs/codebase/ and fails the job when no map document was written."""
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, MAP_ALLOWED_FOLDERS)
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("map_guard_reverted", project=context.project.id, reverted=report["reverted"][:50],
                          removed=report["removed"][:50], unrestorable=report["unrestorable"][:50])
        result = dict(result)
        if result.get("ok") and touched:
            result["reply"] = str(result.get("reply") or "") + MAP_GUARD_NOTE % ", ".join(touched[:MAP_GUARD_FILES_SHOWN])
        if result.get("ok") and not run_hints.list_map_documents(os.path.join(context.repo_path, *run_hints.MAP_FOLDER.split("/"))):
            result.update(ok=False, error=MAP_NOTHING_WRITTEN)
        if result.get("ok"):  # after the undo above: this is the app (not the AI) writing the index and pointing CLAUDE.md at the map
            try:
                run_hints.write_map_index(context.repo_path, self.read_short_head(context.repo_path))
                run_hints.write_codebase_block(context.repo_path)
            except OSError as failure:
                self.log.warn("map_claude_md_block_failed", project=context.project.id, message=str(failure)[:200])
        return result

    def read_short_head(self, repo_path):
        """Short sha of HEAD for the "mapped at" marker; None when git cannot tell (the marker then says it is unknown)."""
        try:
            return self.project_progress_service.git_client.get_short_head(repo_path) or None
        except Exception as failure:  # the marker is a nicety: never fail the map for it
            self.log.warn("map_head_unreadable", message=str(failure)[:200])
            return None

    # ------------------------------------------------ support
    def ensure_repo_free_for_job(self, context):
        """The job runs in the REPO. With the loop running, or with the loop worktree already created (the state lives
        there), it is refused."""
        if self.project_progress_service.is_loop_running(context):
            raise LoopRunningError("loop running: generate or refine the roadmap with the loop stopped")
        if os.path.normcase(context.work_dir) != os.path.normcase(context.repo_path):
            raise LoopWorktreeExistsError("the loop worktree already exists (%s): the loop roadmap lives there; edit it in Files or "
                                          "remove the worktree before generating another one" % context.work_dir)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running in this project: wait for it to finish")

    def write_goal(self, repo_path, goal_text, map_first):
        path = os.path.join(loop_state.internal_dir(repo_path), GOAL_FILE_NAME)
        content = "# Roadmap goal\n\n%s\n\n---\nMap first: %s\nWritten by the harness app on %s\n" % (
            goal_text.strip(), "yes" if map_first else "no", now_iso())
        self.specs_file_repository.write_text_atomic(path, content)
        return path

    @staticmethod
    def validate_text(text, field_name):
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("empty %s" % field_name)
        if len(text) > GOAL_TEXT_LIMIT:
            raise InvalidRequestError("%s longer than %d characters" % (field_name, GOAL_TEXT_LIMIT))
        return text

    @staticmethod
    def validate_budget(budget_usd):
        if budget_usd is None or budget_usd == "":
            return DEFAULT_BUDGET_USD
        if isinstance(budget_usd, bool) or not isinstance(budget_usd, (int, float)) or not 0 < budget_usd <= MAX_BUDGET_USD:
            raise InvalidRequestError("budget_usd must be a number between 0 and %d" % MAX_BUDGET_USD)
        return float(budget_usd)
