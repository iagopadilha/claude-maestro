"""Checklist "where this project stands and what the next action is" (GET /p/<id>/api/progress).

Reads everything from disk on each call (cheap: small files and one `git rev-parse`). The checks the engine makes in the
preflight are NOT rewritten: approval = `loop_state.roadmap_approval` (the same as `run_loop.py --check`), test environment =
`run_loop.test_environment_problems`, grammar = `loop_state.parse_roadmap` + `loop_state.check_fields`.
The planning gates (what "Approve roadmap" requires) are built from the same facts (`planning_gates.build_planning_gates`).
"""
import os
import re
from typing import Protocol

from ..domain.flows_document import check_flow_coverage
from ..domain.roadmap_settings import parse_roadmap_e2e
from ..repositories.specs_file_repository import list_test_environment_problems, loop_state, run_hints
from .planning_gates import (GATE_CONFORMANCE, GATE_FLOWS, GATE_GIT, GATE_MAP, GATE_REPOS, GATE_STRUCTURE, are_gates_ok,
                             build_planning_gates, describe_map_documents)
from .roadmap_service import extract_open_questions

MODE_NEW = "new"
MODE_NO_SPECS = "no_specs"
MODE_PLANNING = "planning"
MODE_READY_FOR_LOOP = "ready_for_loop"
MODE_IN_LOOP = "in_loop"
MODE_DONE = "done"
ACTION_STRUCTURE = "structure"
ACTION_CONFORMANCE = "conformance"
ACTION_MAP = "map"
ACTION_ROADMAP = "roadmap"
ACTION_FLOWS = "flows"
ACTION_APPROVE = "approve"
ACTION_TEST_ENVIRONMENT = "test_environment"
ACTION_CHECK = "check"
ACTION_START_LOOP = "start_loop"
ACTION_FOLLOW = "follow"
LOOP_STATUS_RUNNING = "running"
LOOP_STATUS_STOPPED = "stopped"
LOOP_STATUS_ERROR = "error"
LOOP_STATUS_NO_LOOP = "no_loop"
LOOP_STATUS_CONTINUE = "continue"
LOOP_STATUS_DONE = "done"
LOOP_STATUSES_IN_PROGRESS = (LOOP_STATUS_CONTINUE, "awaiting_user", LOOP_STATUS_STOPPED, LOOP_STATUS_ERROR)
CLOSED_FEATURE_STATUSES = ("verified", "blocked")
MAP_JOB_TYPE = "map"
ROADMAP_JOB_TYPE_PREFIX = "roadmap"
MISSING_ITEMS_SHOWN = 6
ACTION_REPOS = "repos"
ACTION_BY_FAILING_GATE = {GATE_STRUCTURE: ACTION_STRUCTURE, GATE_CONFORMANCE: ACTION_CONFORMANCE, GATE_MAP: ACTION_MAP,
                          GATE_REPOS: ACTION_REPOS, GATE_FLOWS: ACTION_FLOWS}
NAMES_THAT_ARE_NOT_CODE = {".git", ".specs", ".claude", ".vscode", ".idea", "claude.md", "readme.md", ".gitignore",
                           ".gitattributes", "license", "license.md"}


class IProjectProgressService(Protocol):
    def build_progress(self, context) -> dict: ...
    def build_progress_summary(self, context) -> dict: ...
    def get_loop_status(self, work_dir) -> dict: ...
    def is_loop_running(self, context) -> bool: ...
    def build_planning_gates(self, context) -> list: ...


def create_step(step_id, title, ok, detail, optional=False):
    return {"id": step_id, "title": title, "ok": bool(ok), "optional": bool(optional), "detail": detail}


class ProjectProgressService:
    def __init__(self, specs_structure_service, specs_file_repository, git_client, process_runner, claude_job_service, log):
        self.specs_structure_service = specs_structure_service
        self.specs_file_repository = specs_file_repository
        self.git_client = git_client
        self.process_runner = process_runner
        self.claude_job_service = claude_job_service
        self.log = log
        self.repo_readiness_provider = None  # set by the multi-repo module: `provider(context) -> [{name, missing, detail}]` of the extra repos

    # ------------------------------------------------ loop status (same derivation as the panel's loop status)
    def get_loop_status(self, work_dir):
        loop_json = self.specs_file_repository.read_json(loop_state.state_file(work_dir, "LOOP.json"))
        meta = self.specs_file_repository.read_json(loop_state.loop_path(work_dir, "meta.json"))
        pid = self.specs_file_repository.read_loop_lock_pid(work_dir)
        running = self.process_runner.is_alive(pid)
        status = loop_json.get("status") if loop_json.get("status") in loop_state.LOOP_STATUS else None
        reason = loop_json.get("reason")
        if running:
            status = LOOP_STATUS_RUNNING
        elif status == LOOP_STATUS_CONTINUE:
            status = LOOP_STATUS_STOPPED  # the script died in the middle of the loop
            if meta.get("stopped") == LOOP_STATUS_ERROR and str(meta.get("finished_at") or "") >= str(loop_json.get("at") or ""):
                status, reason = LOOP_STATUS_ERROR, meta.get("reason")
        elif status is None:
            status = LOOP_STATUS_NO_LOOP
        return {"status": status, "running": running, "pid": pid if running else None, "reason": reason}

    def is_loop_running(self, context):
        return self.process_runner.is_alive(self.specs_file_repository.read_loop_lock_pid(context.work_dir))

    # ------------------------------------------------ planning gates
    def ensure_test_environment_template(self, context):
        """The planning chat edits TEST-ENVIRONMENT.md (and can only Edit, not create). Structure init already creates it; this is the
        idempotent fallback for projects initialized before that."""
        return self.specs_structure_service.create_test_environment_template(context.work_dir)

    def build_planning_gates(self, context):
        """The planning checklist (gate ids in the contract order; see planning_gates.py)."""
        return build_planning_gates(self.read_project_facts(context))

    # ------------------------------------------------ checklist
    def build_progress_summary(self, context):
        progress = self.build_progress(context)
        return {key: progress[key] for key in ("mode", "next_action", "loop_status", "counts")}

    def build_progress(self, context):
        facts = self.read_project_facts(context)
        steps = self.build_steps(facts)
        mode, next_action = self.decide_mode_and_next_action(facts)
        return {"mode": mode, "next_action": next_action, "steps": steps, "loop_status": facts["loop"]["status"],
                "counts": facts["counts"], "work_dir": context.work_dir,
                "loop_worktree": context.work_dir != context.repo_path, "running_job": facts["running_job"]}

    def read_project_facts(self, context):
        """Reads (without deciding anything) everything the checklist needs."""
        repo, work = context.repo_path, context.work_dir
        folder_exists = os.path.isdir(repo)
        project_dir = os.path.join(work, ".specs", "project")
        roadmap_text = self.specs_file_repository.read_text(os.path.join(project_dir, "ROADMAP.md"))
        flows_text = self.specs_file_repository.read_text(os.path.join(project_dir, "FLOWS.md"))
        features, parse_errors = loop_state.parse_roadmap(roadmap_text or "")
        field_errors = loop_state.check_fields(features)
        errors = parse_errors + field_errors
        job = self.claude_job_service.get_running_job(repo) if folder_exists else None
        return {
            "work_dir": work,
            "folder_exists": folder_exists,
            "is_git": folder_exists and self.git_client.is_git_repo(repo),
            "has_specs": os.path.isdir(os.path.join(work, ".specs")),
            "has_code": folder_exists and self.repo_has_code(repo),
            "structure": self.specs_structure_service.check_structure(work),
            "conformance": self.specs_structure_service.get_conformance(work),
            "repos_unready": self.read_unready_repos(context),
            **self.read_map_facts(work, repo),
            "roadmap_exists": roadmap_text is not None, "features": features, "roadmap_errors": errors,
            "roadmap_parse_errors": parse_errors, "roadmap_field_errors": field_errors,
            "open_questions": extract_open_questions(roadmap_text or ""),
            "project_text": self.specs_file_repository.read_text(os.path.join(project_dir, "PROJECT.md")),
            "flows_exists": flows_text is not None, "flow_coverage": check_flow_coverage(features, flows_text or ""),
            "approval": loop_state.roadmap_approval(work),
            "environment_problems": list_test_environment_problems(work, with_e2e=bool(parse_roadmap_e2e(roadmap_text))) if roadmap_text else [],
            "environment_all_problems": list_test_environment_problems(work, with_e2e=True) if folder_exists else [],  # as if required
            "e2e": bool(parse_roadmap_e2e(roadmap_text)),  # `**E2E:** yes` in the ROADMAP header (read-only in the planning payload)
            "regression_required": bool(roadmap_text and (re.search(r"\bregression=(yes|baseline)\b", roadmap_text)
                                                           or parse_roadmap_e2e(roadmap_text))),
            "environment_exists": os.path.isfile(os.path.join(work, ".specs", "project", "TEST-ENVIRONMENT.md")),
            "loop": self.get_loop_status(work),
            "running_job": {"id": job["id"], "type": job["type"], "started_at": job["started_at"]} if job else None,
            "counts": {"total": len(features), "verified": sum(1 for f in features.values() if f.get("status") == "verified"),
                       "blocked": sum(1 for f in features.values() if f.get("status") == "blocked")},
        }

    def read_unready_repos(self, context):
        """Extra repos of a multi-repo project that miss Conformance or a map. A failure to read them never takes the checklist down."""
        if self.repo_readiness_provider is None:
            return []
        try:
            return list(self.repo_readiness_provider(context))
        except Exception as failure:
            self.log.warn("repo_readiness_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return []

    @staticmethod
    def read_map_facts(work, repo):
        """The codebase map: `docs/codebase` first, then the legacy `.specs/codebase` (a project mapped before keeps working)."""
        relative, root = run_hints.find_map_folder(work, repo)
        folder = os.path.join(root, *relative.split("/"))
        return {"map_files": run_hints.list_map_documents(folder), "map_folder": relative,
                "map_commit": run_hints.read_mapped_commit(folder)}

    def build_steps(self, facts):
        structure, approval, loop = facts["structure"], facts["approval"], facts["loop"]
        job = facts["running_job"]
        conformance_detail = "Conformance: %s" % facts["conformance"] if facts["conformance"] else self.describe_conformance_suggestion(facts)
        map_detail = (describe_map_documents(facts) if facts["map_files"]
                      else "repo with code and no map: map it before the roadmap" if facts["has_code"] else "repo without code: nothing to map")
        if job and job["type"] == MAP_JOB_TYPE:
            map_detail = "mapping (job %s)" % job["id"]
        return [
            create_step("folder", "Project folder", facts["folder_exists"], "exists" if facts["folder_exists"] else "the folder no longer exists"),
            create_step("git", "Git repository", facts["is_git"], "ok" if facts["is_git"] else "the folder is not a git repository (the loop needs git)"),
            create_step("structure", ".specs/ structure", structure["ok"],
                        "complete" if structure["ok"] and not structure["missing"] else "missing: " + ", ".join(structure["missing"][:MISSING_ITEMS_SHOWN])),
            create_step("conformance", "Conformance in CLAUDE.md", facts["conformance"], conformance_detail),
            create_step("map", "Codebase map", facts["map_files"] and not (job and job["type"] == MAP_JOB_TYPE), map_detail,
                        optional=not (facts["has_code"] and not facts["features"])),
            create_step("roadmap", "Roadmap", facts["features"] and not facts["roadmap_errors"], self.describe_roadmap(facts)),
            create_step("flows", "Flows (FLOWS.md)", self.has_every_flow(facts), self.describe_flows(facts),
                        optional=approval["approved"] and not self.has_every_flow(facts)),
            create_step("approval", "Approved roadmap", approval["approved"],
                        "approved on %s" % approval["at"] if approval["approved"] else approval["reason"]),
            create_step("test_environment", "Test environment", not facts["environment_problems"],
                        facts["environment_problems"][0] if facts["environment_problems"]
                        else "filled in" if facts["environment_exists"] else "not required (no feature asks for regression)",
                        optional=not facts["environment_problems"]),
            create_step("loop", "Loop", loop["status"] == LOOP_STATUS_DONE,
                        loop["status"] + (": %s" % loop["reason"] if loop.get("reason") else "")),
        ]

    def decide_mode_and_next_action(self, facts):
        loop_status = facts["loop"]["status"]
        if loop_status == LOOP_STATUS_RUNNING or loop_status in LOOP_STATUSES_IN_PROGRESS:
            return MODE_IN_LOOP, ACTION_FOLLOW
        pending = self.find_first_pending_action(facts)
        features = facts["features"]
        all_closed = bool(features) and all(f.get("status") in CLOSED_FEATURE_STATUSES for f in features.values())
        if loop_status == LOOP_STATUS_DONE and all_closed:
            return MODE_DONE, ACTION_FOLLOW
        if pending is None:
            return MODE_READY_FOR_LOOP, ACTION_CHECK if loop_status == LOOP_STATUS_NO_LOOP else ACTION_START_LOOP
        if not facts["has_code"] and not features:
            return MODE_NEW, pending
        if not facts["has_specs"]:
            return MODE_NO_SPECS, pending
        return MODE_PLANNING, pending

    @staticmethod
    def find_first_pending_action(facts):
        """The flow order: structure -> conformance -> map (repo with code) -> roadmap -> approval -> test environment."""
        if not facts["structure"]["ok"]:
            return ACTION_STRUCTURE
        if not facts["conformance"]:
            return ACTION_CONFORMANCE
        if facts["has_code"] and not facts["map_files"] and not facts["features"]:
            return ACTION_MAP
        if not facts["features"] or facts["roadmap_errors"]:
            return ACTION_ROADMAP
        if not facts["approval"]["approved"]:
            return ProjectProgressService.find_planning_action(facts)
        if facts["environment_problems"]:
            return ACTION_TEST_ENVIRONMENT
        return None

    @staticmethod
    def find_planning_action(facts):
        """Roadmap not approved: `approve` only when every planning gate is ok; otherwise the first failing gate's action
        (flows / map / ...), `roadmap` for the planning conversation (project context, fields, open questions). A failing git gate has no
        action here (the git step shows it)."""
        gates = build_planning_gates(facts)
        if are_gates_ok(gates):
            return ACTION_APPROVE
        failing = [gate["id"] for gate in gates if not gate["ok"] and not gate["optional"] and gate["id"] != GATE_GIT]
        if not failing:
            return ACTION_APPROVE
        return ACTION_BY_FAILING_GATE.get(failing[0], ACTION_ROADMAP)

    # ------------------------------------------------ support
    @staticmethod
    def has_every_flow(facts):
        return bool(facts["flows_exists"] and facts["features"] and not facts["flow_coverage"]["missing"])

    @staticmethod
    def describe_flows(facts):
        if not facts["flows_exists"]:
            return "FLOWS.md missing"
        missing = facts["flow_coverage"]["missing"]
        if missing:
            return "without a flow: " + ", ".join(missing[:MISSING_ITEMS_SHOWN])
        return "%d flow(s), one per feature" % len(facts["features"]) if facts["features"] else "no feature in the ROADMAP yet"

    @staticmethod
    def describe_roadmap(facts):
        job = facts["running_job"]
        if job and job["type"].startswith(ROADMAP_JOB_TYPE_PREFIX):
            return "generating (job %s)" % job["id"]
        if not facts["roadmap_exists"]:
            return "ROADMAP.md missing"
        if facts["roadmap_errors"]:
            return "outside the grammar: " + facts["roadmap_errors"][0]
        if not facts["features"]:
            return "ROADMAP.md has no feature line"
        stages = len(dict.fromkeys(f.get("stage", "") for f in facts["features"].values()))
        return "%d feature(s) in %d stage(s)" % (len(facts["features"]), stages)

    def describe_conformance_suggestion(self, facts):
        if not facts["folder_exists"]:
            return "no Conformance line"
        try:
            suggestion = self.specs_structure_service.suggest_conformance(facts["work_dir"])
        except Exception as failure:  # the suggestion is only a help: it never takes the checklist down
            self.log.warn("conformance_suggestion_failed", type=type(failure).__name__)
            return "no Conformance line: choose STANDARD or LEGACY"
        return "no Conformance line (script suggestion: %s, stack %s)" % (suggestion["suggestion"], suggestion["stack"])

    @staticmethod
    def repo_has_code(repo):
        try:
            names = os.listdir(repo)
        except OSError:
            return False
        return any(name.lower() not in NAMES_THAT_ARE_NOT_CODE for name in names)
