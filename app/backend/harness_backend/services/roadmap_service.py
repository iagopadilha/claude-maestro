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
from .map_job_runner import MAP_GUARD_IGNORED_PATHS, MapJobRunner
from .planning_gates import GATE_READY, create_readiness_gate
from .claude_job_service import JOB_TYPE_ROADMAP, JOB_TYPE_ROADMAP_REFINE, ROADMAP_JOB_TYPES, ClaudeJobCommand

GOAL_FILE_NAME = "roadmap-goal.md"
GOAL_RELATIVE_PATH = "/".join((".specs", "project", loop_state.LOOP_FOLDER_NAME, loop_state.INTERNAL_FOLDER_NAME, GOAL_FILE_NAME))
GENERATE_ROADMAP_PROMPT = "/orchestrator roadmap --headless --goal-file " + GOAL_RELATIVE_PATH
MAP_FIRST_FLAG = "--map-first"
REFINE_ROADMAP_PREFIX = ("Continue in this session's `/orchestrator roadmap --headless` mode: adjust .specs/project/ROADMAP.md "
                         "and .specs/project/FLOWS.md (one flow per feature), and PROJECT.md if needed, following the request below, in the grammar of references/state.md. Do not ask "
                         "(doubts go to the `## Open questions` section of the ROADMAP), never approve the roadmap, and finish.\n\n"
                         "User request:\n")
ROADMAP_PERMISSION_MODE = "dontAsk"  # what the rules do not pre-approve is denied (nobody answers a prompt in -p)
ROADMAP_HOOK_PROFILE = "planning"  # guard.py profile: Write/Edit only inside .specs/
ROADMAP_EDITABLE_PATHS = tuple(".specs/project/%s" % name for name in ("ROADMAP.md", "FLOWS.md", "PROJECT.md", "RULES.md", "TEST-ENVIRONMENT.md"))
ROADMAP_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Skill"] + ["Edit(/%s)" % path for path in ROADMAP_EDITABLE_PATHS]
ROADMAP_DISALLOWED_TOOLS = ["Bash", "PowerShell", "NotebookEdit", "Agent", "WebFetch"]
# Map-first variant: Phase 1 dispatches brownfield-mapper agents (Agent) and sizes them with `harness.py scale` (Bash), and writes
# docs/codebase/. The guard profile still confines the writes. Neither `planning` (no docs/codebase) nor `spec` (state-write blocks
# ROADMAP.md) fits: this profile is `planning` rules + `map-docs` and must exist in plugin guard.py PROFILES.
ROADMAP_MAP_FIRST_HOOK_PROFILE = "roadmapmap"
ROADMAP_MAP_FIRST_EDITABLE_PATHS = ROADMAP_EDITABLE_PATHS + (run_hints.MAP_FOLDER,)
ROADMAP_MAP_FIRST_ALLOWED_TOOLS = ROADMAP_ALLOWED_TOOLS + ["Agent", "Bash", "Edit(/%s/**)" % run_hints.MAP_FOLDER]
ROADMAP_MAP_FIRST_DISALLOWED_TOOLS = ["PowerShell", "NotebookEdit", "WebFetch"]
DEFAULT_JOB_MODEL = ""# empty = the account's default model, as in run_loop
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
                 workspace_file_guard, log, readiness_service=None, project_capabilities_service=None):
        self.readiness_service = readiness_service  # cached `run_loop.py --check --planning`; refuses the approval when enforced and not READY
        self.project_capabilities_service = project_capabilities_service  # rewrites the marked block of PROJECT.md at approval
        self.specs_file_repository = specs_file_repository
        self.project_progress_service = project_progress_service
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.planning_requirement_service = planning_requirement_service
        self.workspace_file_guard = workspace_file_guard
        self.log = log
        self.map_job_runner = MapJobRunner(claude_job_service, claude_client, workspace_file_guard, log, self.read_short_head)

    def read_short_head(self, repo_path):
        return self.project_progress_service.git_client.get_short_head(repo_path)

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
        self.ensure_ready_to_run(context)
        self.write_capabilities(context)
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

    def ensure_ready_to_run(self, context):
        """Server side re-check of the `ready` gate (the last moment the user is present): refuses with the first problem."""
        if not self.readiness_service or not self.readiness_service.enforced:
            return
        gate = create_readiness_gate(self.readiness_service.get(context))
        if not gate["ok"]:
            self.log.info("roadmap_approval_refused", project=context.project.id, gates=[GATE_READY])
            raise PlanningGatesError("the roadmap cannot be approved yet: %s (%s)" % (gate["label"], gate["detail"]),
                                     [item for item in self.project_progress_service.build_planning_gates(context)
                                      if item["id"] != GATE_READY] + [gate], PLANNING_GATES_CODE)

    def write_capabilities(self, context):
        if not self.project_capabilities_service:
            return
        try:
            self.project_capabilities_service.write(context)
        except OSError as failure:  # PROJECT.md is a guide for the agents: never block the approval for it
            self.log.warn("capabilities_block_not_written", project=context.project.id, message=str(failure)[:200])

    # ------------------------------------------------ jobs
    def generate_roadmap(self, context, goal, map_first=False, budget_usd=None):
        self.planning_requirement_service.ensure_planning_done(context)
        goal_text = self.validate_text(goal, "goal")
        budget = self.validate_budget(budget_usd)
        self.ensure_repo_free_for_job(context)
        self.write_goal(context.repo_path, goal_text, bool(map_first))
        prompt = GENERATE_ROADMAP_PROMPT + (" " + MAP_FIRST_FLAG if map_first else "")
        argv = self.build_confined_argv(budget, prompt_argument=prompt, map_first=bool(map_first))
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_ROADMAP,
                                                 self.create_confined_command(context.repo_path, argv, map_first=bool(map_first)))

    def refine_roadmap(self, context, text, budget_usd=None):
        self.planning_requirement_service.ensure_planning_done(context)
        request = self.validate_text(text, "text")
        budget = self.validate_budget(budget_usd)
        self.ensure_repo_free_for_job(context)
        session = self.claude_job_service.get_last_session(context.repo_path, ROADMAP_JOB_TYPES)
        if not session:
            raise RoadmapSessionMissingError("no roadmap generated by the app in this project to refine: generate the roadmap first")
        argv = self.build_confined_argv(budget, resume_session=session)
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_ROADMAP_REFINE,
                                                 self.create_confined_command(context.repo_path, argv, REFINE_ROADMAP_PREFIX + request, session))

    def build_confined_argv(self, budget, prompt_argument="", resume_session="", map_first=False):
        """Same confinement as the planning chat: hook profile `planning` (Write/Edit only inside .specs/), dontAsk, only the roadmap
        documents editable, no Bash/Agent/web, no MCP (the roadmap job reads files only)."""
        allowed = ROADMAP_MAP_FIRST_ALLOWED_TOOLS if map_first else ROADMAP_ALLOWED_TOOLS
        disallowed = ROADMAP_MAP_FIRST_DISALLOWED_TOOLS if map_first else ROADMAP_DISALLOWED_TOOLS
        return self.claude_client.build_command(DEFAULT_JOB_MODEL, budget, list(allowed), list(disallowed),
                                                no_mcp=True, prompt_argument=prompt_argument, resume_session=resume_session,
                                                permission_mode=ROADMAP_PERMISSION_MODE,
                                                hook_profile=ROADMAP_MAP_FIRST_HOOK_PROFILE if map_first else ROADMAP_HOOK_PROFILE)

    def create_confined_command(self, repo_path, argv, stdin_text="", resumed_session="", map_first=False):
        """The snapshot is taken in the job thread (before claude) and everything outside the roadmap documents is undone afterwards."""
        holder = {}
        editable = ROADMAP_MAP_FIRST_EDITABLE_PATHS if map_first else ROADMAP_EDITABLE_PATHS

        def take_snapshot(update_progress):
            holder["snapshot"] = self.workspace_file_guard.take_snapshot(repo_path, MAP_GUARD_IGNORED_PATHS)

        def revert_changes(result):
            report = self.workspace_file_guard.revert_unexpected_changes(holder["snapshot"], list(editable))
            if report["reverted"] or report["removed"] or report["unrestorable"]:
                self.log.warn("roadmap_guard_reverted", reverted=report["reverted"][:50], removed=report["removed"][:50])
            if map_first and result.get("ok") and run_hints.list_map_documents(os.path.join(repo_path, *run_hints.MAP_FOLDER.split("/"))):
                try:  # the app (not the AI) writes the map index and the CLAUDE.md block, as after a map job
                    run_hints.write_map_index(repo_path, self.read_short_head(repo_path))
                    run_hints.write_codebase_block(repo_path)
                except Exception as failure:  # a nicety: never fail the roadmap for it
                    self.log.warn("roadmap_map_index_failed", message=str(failure)[:200])
            return result

        return ClaudeJobCommand(argv, stdin_text, resumed_session, before_start=take_snapshot, finalize=revert_changes)

    def map_codebase(self, context, budget_usd=None):
        """Only validates and registers the job: the safety snapshot and claude run in the job thread (MapJobRunner).
        The setup map has no spend cap: `budget_usd` is accepted and ignored (never a 400), the argv has no `--max-budget-usd`."""
        self.log.info("map_requested", project=context.project.id)
        try:
            self.ensure_repo_free_for_job(context)
        except Exception as refusal:
            self.log.info("map_refused", project=context.project.id, reason=type(refusal).__name__, message=str(refusal)[:200])
            raise
        return self.map_job_runner.start(context.repo_path, context.project.id, None)

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
