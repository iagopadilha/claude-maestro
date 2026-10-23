"""Checklist "where this project stands and what the next action is" (GET /p/<id>/api/progress).

Reads everything from disk on each call (cheap: small files and one `git rev-parse`). The checks the engine makes in the
preflight are NOT rewritten: approval = `loop_state.roadmap_approval` (the same as `run_loop.py --check`), test environment =
`run_loop.test_environment_problems`, grammar = `loop_state.parse_roadmap` + `loop_state.check_fields`.
The planning gates (what "Approve roadmap" requires) are built from the same facts (`planning_gates.build_planning_gates`).
"""
import os
import re
import threading
import time
from types import SimpleNamespace
from typing import Protocol

from ..domain.flows_document import check_flow_coverage
from ..domain.roadmap_settings import parse_roadmap_e2e
from ..repositories.specs_file_repository import list_test_environment_problems, loop_state, run_hints
from .planning_gates import (GATE_CONFORMANCE, GATE_FLOWS, GATE_GIT, GATE_MAP, GATE_REPOS, GATE_STRUCTURE, GATE_SUBPROJECTS,
                             are_gates_ok, build_planning_gates, build_subproject_conformance_gate, build_subproject_map_gate,
                             build_subprojects_gates, check_sent_imports_coverage, describe_map_documents, read_subproject_state)
from .planning_import_service import META_FILE_NAME, PlanningImportService
from .roadmap_service import extract_open_questions

MODE_NEW = "new"
MODE_NO_SPECS = "no_specs"
MODE_PLANNING = "planning"
MODE_READY_FOR_LOOP = "ready_for_loop"
MODE_IN_LOOP = "in_loop"
MODE_DONE = "done"
ACTION_STRUCTURE = "structure"
ACTION_SUBPROJECTS = "subprojects"
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
ACTION_BY_FAILING_GATE = {GATE_STRUCTURE: ACTION_STRUCTURE, GATE_SUBPROJECTS: ACTION_SUBPROJECTS, GATE_CONFORMANCE: ACTION_CONFORMANCE, GATE_MAP: ACTION_MAP,
                          GATE_REPOS: ACTION_REPOS, GATE_FLOWS: ACTION_FLOWS}
PLANNING_GATES_CACHE_SECONDS = 10  # how long the last gates may answer a chat send's prerequisites refusal without a new check
NAMES_THAT_ARE_NOT_CODE = {".git", ".specs", ".claude", ".vscode", ".idea", "claude.md", "readme.md", ".gitignore",
                           ".gitattributes", "license", "license.md"}


class IProjectProgressService(Protocol):
    def build_progress(self, context) -> dict: ...
    def build_progress_summary(self, context) -> dict: ...
    def get_loop_status(self, work_dir) -> dict: ...
    def is_loop_running(self, context) -> bool: ...
    def build_planning_gates(self, context) -> list: ...
    def read_cached_planning_gates(self, context, max_age_seconds=10) -> list | None: ...


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
        self.gates_cache = {}  # project id -> (monotonic time, gates): the last planning gates computed, for the chat send's cheap refusal
        self.gates_cache_lock = threading.Lock()
        self.readiness_provider = None  # set by the projects module: `provider(context) -> readiness result | None` (None = the gate `ready` is absent)
        self.subprojects_provider = None  # set by the projects module: `provider(context) -> SubprojectsService.describe dict | None`
        self.repo_readiness_provider = None  # set by the multi-repo module: `provider(context) -> [{name, missing, detail}]` of the extra edit repos
        self.coverage_recorder = None  # set by the projects module: `recorder(context, result)` = PlanningImportService.record_coverage
        self.repo_reference_warning_provider = None  # set by the multi-repo module: `provider(context) -> [text]` WARNINGs of reference repos without a map

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
        facts = self.read_project_facts(context)
        self.record_import_coverage(context, facts)
        gates = build_planning_gates(facts)
        with self.gates_cache_lock:
            self.gates_cache[context.project.id] = (time.monotonic(), gates)
        return gates

    def read_cached_planning_gates(self, context, max_age_seconds=PLANNING_GATES_CACHE_SECONDS):
        """The last gates computed for this project when they are at most `max_age_seconds` old, else None (in memory only)."""
        with self.gates_cache_lock:
            entry = self.gates_cache.get(context.project.id)
        if entry is None or time.monotonic() - entry[0] > max_age_seconds:
            return None
        return entry[1]

    def record_import_coverage(self, context, facts):
        """Writes the coverage line of the newest import's IMPORT-LOG.md (when it was sent and IMPORT-COVERAGE.md exists). Never raises."""
        imports = facts.get("imports") or []
        if self.coverage_recorder is None or not imports or not imports[0].get("sent_at") or not (facts.get("import_coverage_text") or "").strip():
            return
        try:
            self.coverage_recorder(context, check_sent_imports_coverage(facts)[0])
        except Exception as failure:  # noqa: BLE001
            self.log.warn("import_coverage_not_recorded", type=type(failure).__name__, message=str(failure)[:200])

    # ------------------------------------------------ checklist
    def build_progress_summary(self, context):
        progress = self.build_progress(context)
        return {key: progress[key] for key in ("mode", "next_action", "loop_status", "counts")}

    def build_progress(self, context):
        facts = self.read_project_facts(context)
        steps = self.build_steps(facts)
        mode, next_action = self.decide_mode_and_next_action(facts)
        progress = {"mode": mode, "next_action": next_action, "steps": steps, "loop_status": facts["loop"]["status"],
                    "counts": facts["counts"], "work_dir": context.work_dir,
                    "loop_worktree": context.work_dir != context.repo_path, "running_job": facts["running_job"]}
        progress.update(self.describe_subprojects_progress(facts))
        return progress

    @staticmethod
    def describe_subprojects_progress(facts):
        """Additive payload for a confirmed monorepo: `monorepo: true` (the front end switches its steps on it) and the `subprojects`
        summary. Nothing for a single project or a list still waiting for the user (the key is absent, not false)."""
        detected, confirmed, active, _ = read_subproject_state(facts)
        if not confirmed:
            return {}
        return {"monorepo": True,
                "subprojects": [{"path": project["path"], "stack": project.get("stack") or "unknown", "conformance": project.get("conformance"),
                                 "has_code": bool(project.get("has_code")), "map_files": len((project.get("map") or {}).get("files") or [])}
                                for project in active]}

    def read_project_facts(self, context):
        """Reads (without deciding anything) everything the checklist needs: one method per fact group."""
        repo, work = context.repo_path, context.work_dir
        folder_exists = os.path.isdir(repo)
        project_dir = os.path.join(work, ".specs", "project")
        roadmap_text = self.specs_file_repository.read_text(os.path.join(project_dir, "ROADMAP.md"))
        features, parse_errors = loop_state.parse_roadmap(roadmap_text or "")
        facts = {}
        facts.update(self.read_repository_facts(context, folder_exists))
        facts.update(self.read_map_facts(work, repo))
        facts.update(self.read_roadmap_facts(work, project_dir, roadmap_text, features, parse_errors, repo))
        facts.update(self.read_environment_facts(work, roadmap_text, folder_exists))
        facts.update(self.read_loop_facts(context, folder_exists, features))
        facts.update(self.read_subproject_facts(context))
        if read_subproject_state(facts)[1]:
            # D21: `docs/codebase` at the root of a confirmed monorepo holds SYSTEM.md (and maybe a leftover map): it is not "the map"
            facts.update({"map_files": [], "map_folder": "docs/codebase", "map_commit": None})
        return facts

    def read_subproject_facts(self, context):
        """`facts["subprojects"]` = `SubprojectsService.describe` (monorepo, confirmed, projects, error). No provider = a single project.
        A failure to read it never takes the checklist down: the facts carry the error and the repository counts as a single project."""
        if self.subprojects_provider is None:
            return {}
        try:
            described = self.subprojects_provider(context)
        except Exception as failure:  # noqa: BLE001
            self.log.warn("subprojects_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return {"subprojects": {"monorepo": False, "confirmed": False, "projects": [], "error": "%s: %s" % (type(failure).__name__, str(failure)[:200])}}
        return {"subprojects": described} if described else {}

    def read_repository_facts(self, context, folder_exists):
        """Folder, git, code, `.specs/` structure, Conformance and the providers of the other repos / readiness."""
        repo, work = context.repo_path, context.work_dir
        return {
            "work_dir": work,
            "folder_exists": folder_exists,
            "is_git": folder_exists and self.git_client.is_git_repo(repo),
            "has_specs": os.path.isdir(os.path.join(work, ".specs")),
            "has_code": folder_exists and self.repo_has_code(repo),
            "structure": self.specs_structure_service.check_structure(work),
            "conformance": self.specs_structure_service.get_conformance(work),
            "repos_unready": self.read_unready_repos(context),
            "repos_reference_warnings": self.read_reference_warnings(context),
            "readiness": self.read_readiness(context),
        }

    def read_roadmap_facts(self, work, project_dir, roadmap_text, features, parse_errors, repo=None):
        """ROADMAP, PROJECT.md, FLOWS.md and the approval."""
        flows_text = self.specs_file_repository.read_text(os.path.join(project_dir, "FLOWS.md"))
        field_errors = loop_state.check_fields(features)
        return {
            "roadmap_exists": roadmap_text is not None, "features": features, "roadmap_errors": parse_errors + field_errors,
            "roadmap_parse_errors": parse_errors, "roadmap_field_errors": field_errors,
            "open_questions": extract_open_questions(roadmap_text or ""),
            "project_text": self.specs_file_repository.read_text(os.path.join(project_dir, "PROJECT.md")),
            "flows_exists": flows_text is not None, "flow_coverage": check_flow_coverage(features, flows_text or ""),
            "approval": loop_state.roadmap_approval_for_run(work, repo),
            "imports": self.read_import_metas(work),
            "import_coverage_text": self.specs_file_repository.read_text(os.path.join(project_dir, "IMPORT-COVERAGE.md")) or "",
        }

    def read_import_metas(self, work):
        """The `import.json` of every spec import of the project, newest first (what the import coverage gate checks)."""
        root = PlanningImportService.imported_root(SimpleNamespace(work_dir=work))
        metas = []
        for entry in sorted(os.listdir(root), reverse=True) if os.path.isdir(root) else []:
            meta = self.specs_file_repository.read_json(os.path.join(root, entry, META_FILE_NAME))
            if meta.get("import_id") == entry:
                metas.append(meta)
        return metas

    @staticmethod
    def read_environment_facts(work, roadmap_text, folder_exists):
        """TEST-ENVIRONMENT.md problems (as required and as if required), E2E and regression flags."""
        return {
            "environment_problems": list_test_environment_problems(work, with_e2e=bool(parse_roadmap_e2e(roadmap_text))) if roadmap_text else [],
            "environment_all_problems": list_test_environment_problems(work, with_e2e=True) if folder_exists else [],  # as if required
            "e2e": bool(parse_roadmap_e2e(roadmap_text)),  # `**E2E:** yes` in the ROADMAP header (read-only in the planning payload)
            "regression_required": bool(roadmap_text and (re.search(r"\bregression=(yes|baseline)\b", roadmap_text)
                                                           or parse_roadmap_e2e(roadmap_text))),
            "environment_exists": os.path.isfile(os.path.join(work, ".specs", "project", "TEST-ENVIRONMENT.md")),
        }

    def read_loop_facts(self, context, folder_exists, features):
        """Loop status, the running job and the feature counters."""
        job = self.claude_job_service.get_running_job(context.repo_path) if folder_exists else None
        return {
            "loop": self.get_loop_status(context.work_dir),
            "running_job": {"id": job["id"], "type": job["type"], "started_at": job["started_at"]} if job else None,
            "counts": {"total": len(features), "verified": sum(1 for f in features.values() if f.get("status") == "verified"),
                       "blocked": sum(1 for f in features.values() if f.get("status") == "blocked")},
        }

    def read_readiness(self, context):
        """The cached readiness result (`readiness_service.get`: only reads, never runs); None without a provider. An unreadable cache
        counts as "did not run", never as ready."""
        if self.readiness_provider is None:
            return None
        try:
            return self.readiness_provider(context)
        except Exception as failure:  # noqa: BLE001
            self.log.warn("readiness_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return {"state": "error", "at": None, "problems": [], "warnings": [], "error": "readiness result unreadable"}

    def read_unready_repos(self, context):
        """Extra repos of a multi-repo project that miss Conformance or a map. A failure to read them never takes the checklist down."""
        if self.repo_readiness_provider is None:
            return []
        try:
            return list(self.repo_readiness_provider(context))
        except Exception as failure:
            self.log.warn("repo_readiness_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return []

    def read_reference_warnings(self, context):
        if self.repo_reference_warning_provider is None:
            return []
        try:
            return list(self.repo_reference_warning_provider(context))
        except Exception as failure:
            self.log.warn("repo_reference_warnings_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return []

    @staticmethod
    def read_map_facts(work, repo):
        """The codebase map: `docs/codebase` first, then the legacy `.specs/codebase` (a project mapped before keeps working)."""
        relative, root = run_hints.find_map_folder(work, repo)
        folder = os.path.join(root, *relative.split("/"))
        return {"map_files": run_hints.list_root_map_documents(root, relative), "map_folder": relative,
                "map_commit": run_hints.read_mapped_commit(folder)}

    def build_steps(self, facts):
        """The checklist, in order; one method per step that needs more than a line."""
        structure, approval, loop = facts["structure"], facts["approval"], facts["loop"]
        job = facts["running_job"]
        return [
            create_step("folder", "Project folder", facts["folder_exists"], "exists" if facts["folder_exists"] else "the folder no longer exists"),
            create_step("git", "Git repository", facts["is_git"], "ok" if facts["is_git"] else "the folder is not a git repository (the loop needs git)"),
            create_step("structure", ".specs/ structure", structure["ok"],
                        "complete" if structure["ok"] and not structure["missing"] else "missing: " + ", ".join(structure["missing"][:MISSING_ITEMS_SHOWN])),
            *self.build_subprojects_steps(facts),
            self.build_conformance_step(facts),
            self.build_map_step(facts, job),
            create_step("roadmap", "Roadmap", facts["features"] and not facts["roadmap_errors"], self.describe_roadmap(facts)),
            create_step("flows", "Flows (FLOWS.md)", self.has_every_flow(facts), self.describe_flows(facts),
                        optional=approval["approved"] and not self.has_every_flow(facts)),
            create_step("approval", "Approved roadmap", approval["approved"],
                        "approved on %s" % approval["at"] if approval["approved"] else approval["reason"]),
            self.build_test_environment_step(facts),
            create_step("loop", "Loop", loop["status"] == LOOP_STATUS_DONE,
                        loop["status"] + (": %s" % loop["reason"] if loop.get("reason") else "")),
        ]

    @staticmethod
    def build_subprojects_steps(facts):
        """[subprojects] for a detected monorepo (or an unreadable SUBPROJECTS.json), else []: same rule as the planning gate."""
        return [create_step(gate["id"], gate["label"], gate["ok"], gate["detail"]) for gate in build_subprojects_gates(facts)]

    def build_conformance_step(self, facts):
        if read_subproject_state(facts)[0]:
            gate = build_subproject_conformance_gate(facts)
            return create_step("conformance", gate["label"], gate["ok"], gate["detail"])
        conformance_detail = "Conformance: %s" % facts["conformance"] if facts["conformance"] else self.describe_conformance_suggestion(facts)
        step = create_step("conformance", "Conformance in CLAUDE.md", facts["conformance"], conformance_detail)
        if not facts["conformance"] and facts["folder_exists"]:
            step["suggestion"] = self.read_conformance_suggestion(facts)  # the installer preselects it (None = unknown)
        return step

    @staticmethod
    def build_map_step(facts, job):
        if read_subproject_state(facts)[0]:
            gate = build_subproject_map_gate(facts)
            return create_step("map", gate["label"], gate["ok"], gate["detail"],
                               optional=gate["optional"] or bool(facts["features"]))
        map_detail = (describe_map_documents(facts) if facts["map_files"]
                      else "repo with code and no map: map it before the roadmap" if facts["has_code"] else "repo without code: nothing to map")
        if job and job["type"] == MAP_JOB_TYPE:
            map_detail = "mapping (job %s)" % job["id"]
        return create_step("map", "Codebase map", facts["map_files"] and not (job and job["type"] == MAP_JOB_TYPE), map_detail,
                           optional=not (facts["has_code"] and not facts["features"]))

    @staticmethod
    def build_test_environment_step(facts):
        return create_step("test_environment", "Test environment", not facts["environment_problems"],
                           facts["environment_problems"][0] if facts["environment_problems"]
                           else "filled in" if facts["environment_exists"] else "not required (no feature asks for regression)",
                           optional=not facts["environment_problems"])

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
        detected, confirmed, _, _ = read_subproject_state(facts)
        if detected and not confirmed:
            return ACTION_SUBPROJECTS  # Conformance and the map are per subproject, behind the confirmed list
        if detected:
            if not build_subproject_conformance_gate(facts)["ok"]:
                return ACTION_CONFORMANCE
            map_gate = build_subproject_map_gate(facts)
            if not map_gate["ok"] and not map_gate["optional"] and not facts["features"]:
                return ACTION_MAP
        elif not facts["conformance"]:
            return ACTION_CONFORMANCE
        elif facts["has_code"] and not facts["map_files"] and not facts["features"]:
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

    def read_conformance_suggestion(self, facts):
        """{suggestion, stack, evidence} of the script, or None: the suggestion is only a help and never fails the progress."""
        try:
            return self.specs_structure_service.suggest_conformance(facts["work_dir"])
        except Exception:
            return None

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
