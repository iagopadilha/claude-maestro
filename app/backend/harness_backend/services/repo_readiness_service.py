"""Readiness of the repos of a multi-repo project: the minimum a repo needs before the loop works in it.

The primary repo has its own checklist (`ProjectProgressService`). Every OTHER repo listed in a multi-repo project gets the same two
prerequisites here: a `Conformance: STANDARD|LEGACY` line in its CLAUDE.md and, when it has code, a codebase map (`docs/codebase`).
`list_unready` feeds the planning gate "repos"; `set_conformance` and `map_repo` are the two actions the panel offers per repo.
The folder read and written is the repo's loop worktree when one exists (like the primary's work dir), else the repo folder itself.
"""
import os
from typing import Protocol

from ..domain.project import Project, ProjectContext
from ..engine import project_repos
from ..domain.project_errors import InvalidRepoError, LoopRunningError, RepoConflictError
from .project_progress_service import ProjectProgressService
from .specs_structure_service import MONOREPO_ROOT_CONFORMANCE_CODE, MONOREPO_ROOT_CONFORMANCE_MESSAGE
from ..repositories.subprojects_file_repository import SubprojectsFileError

MISSING_CONFORMANCE = "conformance"
MISSING_MAP = "map"
MISSING_FOLDER = "folder"
MISSING_SUBPROJECTS = "subprojects"
MAP_JOB_TYPE = "map"
NOT_GIT_CODE = "not_git"
FOLDER_MISSING_CODE = "folder_missing"
UNREADY_ITEMS_SHOWN = 6


class IRepoReadinessService(Protocol):
    def describe_repos(self, context) -> dict: ...
    def list_unready(self, context) -> list: ...
    def list_reference_warnings(self, context) -> list: ...
    def set_conformance(self, context, name, value) -> dict: ...
    def map_repo(self, context, name, budget_usd=None) -> dict: ...


class RepoReadinessService:
    def __init__(self, repo_registry_service, specs_structure_service, roadmap_service, claude_job_service, loop_status_service, log):
        self.repo_registry_service = repo_registry_service
        self.specs_structure_service = specs_structure_service
        self.roadmap_service = roadmap_service
        self.claude_job_service = claude_job_service
        self.loop_status_service = loop_status_service
        self.log = log
        self.subprojects_service = None  # optional (`attach_subprojects_service`): the table of a secondary repo that is a monorepo
        self.context_block_service = None  # optional (`attach_context_block_service`): the `maestro:context` block of a secondary repo root

    def attach_subprojects_service(self, subprojects_service):
        self.subprojects_service = subprojects_service

    def attach_context_block_service(self, context_block_service):
        self.context_block_service = context_block_service

    # ------------------------------------------------ read
    def describe_repos(self, context):
        """{"multi", "repos": [readiness of every repo the engine sees, primary first]} (a single-repo project: only the primary)."""
        multi, _ = self.repo_registry_service.read_stored(context)
        repos = [self.describe_repo(repo, context) for repo in self.repo_registry_service.list_effective_repos(context)]
        return {"ok": True, "multi": multi, "repos": repos, "ready": all(repo["ready"] for repo in repos)}

    def list_unready(self, context):
        """The NON-primary `edit` repos that miss something: [{name, missing: [conformance|map], detail}]. [] for a single-repo
        project. A `reference` repo is only read by the agents, so it needs no Conformance nor map. A secondary repo that is a
        monorepo (its entry in the primary's SUBPROJECTS.json) reports one item per subproject that misses something (`missing` is
        what that subproject lacks, `detail` reads "<repo>/<path>: no Conformance, no codebase map"), or one item while its list is
        not confirmed yet."""
        unready = []
        for repo in self.describe_repos(context)["repos"]:
            if repo["primary"] or repo["role"] == project_repos.ROLE_REFERENCE:
                continue
            if repo.get("monorepo"):
                unready.extend(self.list_unready_subprojects(context, repo))
            elif not repo["ready"]:
                unready.append({"name": repo["name"], "missing": repo["missing"], "detail": repo["detail"]})
        return unready

    def list_reference_warnings(self, context):
        """[text] one WARNING per `reference` repo with code and no codebase map. Never a problem: the agents can read the raw code."""
        warnings = []
        for repo in self.describe_repos(context)["repos"]:
            if repo["role"] == project_repos.ROLE_REFERENCE and not repo["primary"] and repo["exists"] and repo["has_code"] and not repo["map"]["files"]:
                warnings.append("%s is a reference repository without a codebase map: optional, but a map helps the agents understand it" % repo["name"])
        return warnings

    def list_unready_subprojects(self, context, repo):
        if not repo["monorepo"]["confirmed"]:
            return [{"name": repo["name"], "missing": [MISSING_SUBPROJECTS], "detail": "%s: confirm the list of subprojects first" % repo["name"]}]
        lines = []
        for row in self.subprojects_service.describe(context, repo["name"])["projects"]:
            if row["ready"]:
                continue
            parts = []
            if MISSING_FOLDER in row["missing"]:
                parts.append("the folder does not exist")
            if MISSING_CONFORMANCE in row["missing"]:
                parts.append("no Conformance")
            if MISSING_MAP in row["missing"]:
                parts.append("no codebase map")
            lines.append({"name": repo["name"], "missing": list(row["missing"]),
                          "detail": "%s/%s: %s" % (repo["name"], row["path"], ", ".join(parts))})
        return lines

    def read_monorepo_state(self, context, repo):
        """{"confirmed": bool} for a secondary repo whose entry in the primary's SUBPROJECTS.json says monorepo, else None."""
        if self.subprojects_service is None or repo.primary or context is None:
            return None
        entry = self.subprojects_service.subprojects_file_repository.read_repo_entry(context.work_dir, repo.name)
        if isinstance(entry, SubprojectsFileError) or not isinstance(entry, dict) or entry.get("monorepo") is not True:
            return None
        return {"confirmed": entry.get("confirmed") is True}

    def describe_repo(self, repo, context=None):
        """The primary is described too (same facts, for a complete table); its own checklist stays the project's `progress`."""
        folder = self.work_folder(repo)
        exists = os.path.isdir(folder)
        conformance = self.specs_structure_service.get_conformance(folder) if exists else None
        has_code = exists and ProjectProgressService.repo_has_code(folder)
        map_facts = ProjectProgressService.read_map_facts(folder, folder) if exists else {"map_files": [], "map_folder": "", "map_commit": None}
        job = self.claude_job_service.get_running_job(repo.path) if exists else None
        map_running = bool(job and job.get("type") == MAP_JOB_TYPE)
        monorepo = self.read_monorepo_state(context, repo)
        missing = []
        if monorepo and monorepo["confirmed"]:
            pass  # the Conformance and the map live per subproject (`list_unready` reports them)
        elif exists and not conformance:
            missing.append(MISSING_CONFORMANCE)
        if not (monorepo and monorepo["confirmed"]) and exists and has_code and not map_facts["map_files"]:
            missing.append(MISSING_MAP)
        suggestion = None
        if exists and not conformance and not monorepo:
            try:
                suggestion = self.specs_structure_service.suggest_conformance(folder)
            except Exception as failure:  # a hint only: it never takes the table down
                self.log.warn("repo_conformance_suggestion_failed", repo=repo.name, type=type(failure).__name__)
        return {"name": repo.name, "primary": repo.primary, "role": repo.role, "path": repo.path, "folder": folder, "exists": exists, "has_code": has_code,
                "conformance": conformance, "suggestion": suggestion,
                "map": {"files": list(map_facts["map_files"]), "folder": map_facts["map_folder"], "commit": map_facts["map_commit"],
                        "running": map_running, "job": job["id"] if map_running else None},
                "monorepo": monorepo, "missing": missing, "ready": exists and not missing and not map_running, "detail": self.describe_missing(repo, exists, missing, map_running)}

    @staticmethod
    def describe_missing(repo, exists, missing, map_running):
        if not exists:
            return "%s: the folder does not exist" % repo.name
        parts = []
        if MISSING_CONFORMANCE in missing:
            parts.append("no Conformance line (STANDARD or LEGACY)")
        if MISSING_MAP in missing:
            parts.append("no codebase map")
        if map_running:
            parts.append("mapping now")
        return "%s: %s" % (repo.name, ", ".join(parts)) if parts else "%s: ready" % repo.name

    @staticmethod
    def work_folder(repo):
        worktree = repo.loop_worktree_path()
        return worktree if os.path.isdir(worktree) else repo.path

    # ------------------------------------------------ actions
    def set_conformance(self, context, name, value):
        """Writes the Conformance block in the CLAUDE.md of one repo (the primary has its own route: /p/<id>/api/conformance)."""
        repo = self.resolve_secondary(context, name)
        self.ensure_loop_stopped(context)
        folder = self.work_folder(repo)
        if not os.path.isdir(folder):
            raise InvalidRepoError("the repo folder does not exist: %s" % folder, code=FOLDER_MISSING_CODE)
        monorepo = self.read_monorepo_state(context, repo)
        if monorepo and monorepo["confirmed"]:  # the folder's own .specs says nothing about it: the primary's SUBPROJECTS.json does
            raise RepoConflictError(MONOREPO_ROOT_CONFORMANCE_MESSAGE, code=MONOREPO_ROOT_CONFORMANCE_CODE)
        written = self.specs_structure_service.write_conformance(folder, value)
        self.specs_structure_service.ensure_specs_in_gitignore(folder)  # defining the Conformance always leaves `.specs/` out of git
        if repo.role != project_repos.ROLE_REFERENCE and not monorepo:
            self.refresh_context_block(context, repo, folder)
        self.log.info("repo_conformance_set", project=context.project.id, repo=repo.name, value=written)
        return {"ok": True, "name": repo.name, "conformance": written}

    def map_repo(self, context, name, budget_usd=None):
        """Starts the codebase map job of one repo (claude runs in THAT repo; it writes only `docs/codebase`, like the primary's map)."""
        repo = self.resolve_secondary(context, name)
        self.ensure_loop_stopped(context)
        if not os.path.isdir(repo.path):
            raise InvalidRepoError("the repo folder does not exist: %s" % repo.path, code=FOLDER_MISSING_CODE)
        if os.path.isdir(repo.loop_worktree_path()):
            raise RepoConflictError("the worktree of %s already exists: map it from the loop worktree or remove the worktree first" % repo.name,
                                    code="worktree_exists")
        self.specs_structure_service.ensure_specs_in_gitignore(repo.path)  # the job record lives in the repo's .specs: keep git clean of it
        repo_context = ProjectContext(Project(id="%s/%s" % (context.project.id, repo.name), name=repo.name, repo_path=repo.path))
        job = self.roadmap_service.map_codebase(repo_context, budget_usd)
        self.log.info("repo_map_started", project=context.project.id, repo=repo.name, job=job["id"])
        return {"ok": True, "name": repo.name, "job": job["id"], "detail": job}

    # ------------------------------------------------ support
    def refresh_context_block(self, context, repo, folder):
        """The `maestro:context` block at the root of a non-monorepo secondary edit repo (a failure is logged, never raised)."""
        if self.context_block_service is None:
            return
        try:
            self.context_block_service.refresh_folder(folder, repo.name, "", subprojects_root=context.work_dir)
        except Exception as failure:
            self.log.warn("repo_context_block_failed", repo=repo.name, type=type(failure).__name__, message=str(failure)[:200])

    def resolve_secondary(self, context, name):
        repo = self.repo_registry_service.resolve(context, name)
        if repo.primary:
            raise RepoConflictError("the primary repo uses the project checklist, not this route", code="primary")
        return repo

    def ensure_loop_stopped(self, context):
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("a loop is running in this project: stop it first", code="loop_running")
