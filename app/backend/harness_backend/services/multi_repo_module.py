"""Wiring of the multi-repo pieces (repo list, workspace, Close loop routes, running-loops overview).

Built AFTER the loop read and loop commands modules because it consumes their services (`loop_status_service`,
`loop_worktree_service`). It exposes `controllers` and `busy_providers` like every module.
"""
import os
import sys

from ..api.controllers.follow_up_controller import FollowUpController
from ..api.controllers.planning_reopen_controller import PlanningReopenController
from ..api.controllers.repo_catalog_controller import RepoCatalogController
from ..api.controllers.repo_controller import RepoController
from ..api.controllers.repo_readiness_controller import RepoReadinessController
from ..api.controllers.setup_commit_controller import SetupCommitController
from ..api.controllers.workspace_controller import WorkspaceController
from ..infrastructure.claude_client import ClaudeClient
from ..infrastructure.git_client import GitClient
from ..infrastructure.process_runner import ProcessRunner
from ..infrastructure.workspace_file_guard import WorkspaceFileGuard
from ..repositories.loop_command_file_repository import LoopCommandFileRepository
from ..repositories.loop_file_repository import LoopFileRepository
from ..repositories.specs_file_repository import SpecsFileRepository
from .engine_script_service import EngineScriptService
from .follow_up_service import FollowUpService
from .planning_repos_service import PlanningReposService
from .planning_reopen_service import PlanningReopenService
from .repo_catalog_service import RepoCatalogService
from .repo_readiness_service import RepoReadinessService
from .repo_registry_service import RepoRegistryService
from .wrap_up_service import WrapUpService
from .setup_commit_service import SetupCommitService
from .workspace_service import EngineWorktreeAdapter, WorkspaceService

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import project_repos  # noqa: E402


class MultiRepoModule:
    def __init__(self, repo_registry_service, workspace_service, controllers, busy_providers, repo_catalog_service=None,
                 repo_readiness_service=None, planning_repos_service=None):
        self.planning_repos_service = planning_repos_service  # wave 2: planning_service.finish_reply calls apply_suggested_repos(context)
        self.repo_catalog_service = repo_catalog_service
        self.repo_readiness_service = repo_readiness_service
        self.repo_registry_service = repo_registry_service
        self.workspace_service = workspace_service
        self.controllers = controllers
        self.busy_providers = busy_providers


def build_multi_repo_module(config, log, project_service, loop_status_service, loop_worktree_service, claude_job_service,
                            loop_control_service=None, specs_structure_service=None, roadmap_service=None, project_progress_service=None,
                            planning_requirement_service=None, planning_repos_service=None, on_plan_changed=None,
                            subprojects_service=None, context_block_service=None, decisions_service=None):
    process_runner = ProcessRunner()
    git_client = GitClient(process_runner)
    repo_registry_service = RepoRegistryService(git_client, loop_status_service, log)
    planning_repos_service = planning_repos_service or PlanningReposService(git_client, log)  # the projects module's own: one instance
    repo_registry_service.after_change = on_plan_changed  # repo added/removed/role changed: capabilities block + readiness
    workspace_service = WorkspaceService(repo_registry_service, loop_status_service, LoopFileRepository(), git_client, claude_job_service,
                                         EngineWorktreeAdapter(config, process_runner), log,
                                         start_in_progress_provider=(lambda context: loop_control_service.has_live_relaunch(context.project.id))
                                         if loop_control_service else None)

    def list_known_repo_paths():
        """Every repo path listed in the REPOS.json of every known project (so unscanned repos show in the catalog as never scanned)."""
        paths = []
        for project in project_service.list_projects():
            try:
                _, stored = repo_registry_service.read_stored(project_service.get_project_context(project.id))
            except Exception:  # one unreadable project does not hide the others
                continue
            paths.extend(repo.path for repo in stored)
        return list(dict.fromkeys(paths))

    claude_client = ClaudeClient(process_runner, claude_binary=config.claude_binary, guard_path=config.guard_path)
    repo_catalog_service = RepoCatalogService(claude_client, WorkspaceFileGuard(), log, known_paths_provider=list_known_repo_paths)
    if loop_control_service is not None:  # Start asks for the repository definition before it creates anything
        loop_control_service.scope_defined_provider = lambda context: project_repos.is_scope_defined(context.repo_path)
    wrap_up_service = None
    controllers = [RepoController(project_service, repo_registry_service),
                   RepoCatalogController(repo_catalog_service)]
    if decisions_service is not None:  # Finish (wrap-up) reads the decisions the loop panel shows: the composition's one instance
        wrap_up_service = WrapUpService(loop_worktree_service, loop_status_service, LoopFileRepository(), LoopCommandFileRepository(),
                                        EngineScriptService(config, process_runner), decisions_service, claude_job_service, git_client, log,
                                        loop_control_service=loop_control_service)
    controllers.append(WorkspaceController(project_service, workspace_service, loop_worktree_service, loop_status_service,
                                           wrap_up_service=wrap_up_service))
    controllers.append(SetupCommitController(project_service, SetupCommitService(repo_registry_service, loop_status_service, claude_job_service,
                                                                                 git_client, log)))
    if loop_control_service is not None:  # Follow-up: a new stage on the finished loop, started through the same start route
        follow_up_service = FollowUpService(SpecsFileRepository(), claude_job_service, claude_client, WorkspaceFileGuard(),
                                            loop_status_service, loop_control_service, log)
        controllers.append(FollowUpController(project_service, follow_up_service,
                                              repo_usage_guard=getattr(loop_status_service, "ensure_repos_free", None)))
    if planning_requirement_service is not None:  # Reopen planning: back to planning after the approval (removes the loop worktrees)
        controllers.append(PlanningReopenController(project_service, PlanningReopenService(
            git_client, loop_status_service, loop_worktree_service, planning_requirement_service, claude_job_service, log)))
    repo_readiness_service = None
    if specs_structure_service is not None and roadmap_service is not None:
        repo_readiness_service = RepoReadinessService(repo_registry_service, specs_structure_service, roadmap_service, claude_job_service,
                                                      loop_status_service, log)
        if subprojects_service is not None:  # a secondary repo that is a monorepo: per-subproject lines; ?repo= resolves through the registry
            repo_readiness_service.attach_subprojects_service(subprojects_service)
            subprojects_service.attach_repo_registry_service(repo_registry_service)
            workspace_service.subprojects_service = subprojects_service  # the setup state of each repository in the Workspace payload
        if context_block_service is not None:  # the maestro:context block at the root of a non-monorepo secondary edit repo
            repo_readiness_service.attach_context_block_service(context_block_service)
        controllers.append(RepoReadinessController(project_service, repo_readiness_service))
        if project_progress_service is not None:  # the planning gate "repos" (an extra repo without Conformance/map blocks the approval)
            project_progress_service.repo_readiness_provider = repo_readiness_service.list_unready
            project_progress_service.repo_reference_warning_provider = repo_readiness_service.list_reference_warnings
    return MultiRepoModule(repo_registry_service, workspace_service, controllers, [], repo_catalog_service, repo_readiness_service,
                           planning_repos_service)
