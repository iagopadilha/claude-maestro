"""Wiring of the projects module: the ONLY place of the module that instantiates concrete classes.

Exposes `project_service` (IProjectService, consumed by the other modules), `planning_requirement_service`
(IPlanningRequirementService, the planning mode rule the other modules enforce), `controllers` and `busy_providers`
(running claude jobs, planning replies included, prevent the idle shutdown).
"""
import os

from ..api.controllers.planning_controller import PlanningController
from ..api.controllers.project_controller import ProjectController
from ..api.controllers.roadmap_controller import RoadmapController
from ..api.controllers.specs_controller import SpecsController
from ..api.controllers.subprojects_controller import SubprojectsController
from ..api.controllers.test_environment_controller import TestEnvironmentController
from ..infrastructure.claude_client import ClaudeClient
from ..infrastructure.git_client import GitClient
from ..infrastructure.process_runner import ProcessRunner
from ..infrastructure.workspace_file_guard import WorkspaceFileGuard
from ..repositories.planning_thread_repository import PlanningThreadRepository
from ..repositories.project_repository import ProjectRepository
from ..repositories.specs_file_repository import SpecsFileRepository
from ..repositories.subprojects_file_repository import SubprojectsFileRepository
from ..repositories.test_environment_repository import TestEnvironmentRepository
from .code_inventory_service import CodeInventoryService
from .context_block_service import ContextBlockService
from .claude_job_service import DEFAULT_JOB_TIMEOUT_SECONDS, ClaudeJobService
from .folder_picker_service import FolderPickerService
from .planning_requirement_service import PlanningRequirementService
from .planning_import_service import PlanningImportService
from .planning_repos_service import PlanningReposService
from .planning_service import PlanningService
from .project_capabilities_service import ProjectCapabilitiesService
from .project_progress_service import ProjectProgressService
from .project_service import ProjectService
from .readiness_service import ReadinessService
from .roadmap_service import RoadmapService
from .spec_file_service import SpecFileService
from .loop_worktree_removal_service import LoopWorktreeRemovalService
from .specs_reset_service import SpecsResetService
from .specs_structure_service import SpecsStructureService
from .standards_service import StandardsService
from .map_job_runner import attach_code_inventory
from .subproject_map_service import SubprojectMapService
from .subproject_validation_service import SubprojectValidationService
from .subprojects_service import SubprojectsService, subprojects_engine
from .system_map_service import SystemMapService, attach_system_map
from .test_environment_service import TestEnvironmentService
from .claude_call_log_service import build_shared_claude_call_log_service


class ProjectsModule:
    def __init__(self, project_service, controllers, busy_providers, project_progress_service, roadmap_service, claude_job_service,
                 planning_requirement_service, planning_service, readiness_service=None, project_capabilities_service=None,
                 planning_repos_service=None, on_plan_changed=None, subprojects_service=None, subproject_validation_service=None,
                 subproject_map_service=None, system_map_service=None, code_inventory_service=None, context_block_service=None,
                 standards_service=None):
        self.standards_service = standards_service  # shared with the subprojects controller and (via the composition) the repo-readiness one
        self.subproject_validation_service = subproject_validation_service  # `repo_registry_service` is set by the composition (multi-repo module)
        self.subproject_map_service = subproject_map_service
        self.system_map_service = system_map_service
        self.code_inventory_service = code_inventory_service
        self.context_block_service = context_block_service
        self.subprojects_service = subprojects_service  # shared with the subprojects controller (one instance)
        self.planning_repos_service = planning_repos_service  # the multi-repo module reuses it (one instance)
        self.on_plan_changed = on_plan_changed  # fn(context): rewrites the capabilities block and schedules a readiness check
        self.readiness_service = readiness_service
        self.project_capabilities_service = project_capabilities_service
        self.project_service = project_service
        self.planning_requirement_service = planning_requirement_service
        self.planning_service = planning_service
        self.controllers = controllers
        self.busy_providers = busy_providers
        self.project_progress_service = project_progress_service
        self.roadmap_service = roadmap_service
        self.claude_job_service = claude_job_service


def build_projects_module(config, log, get_app_url, folder_dialog_argv=None, job_timeout_seconds=DEFAULT_JOB_TIMEOUT_SECONDS):
    """`folder_dialog_argv` and `job_timeout_seconds` only exist for tests (fake dialog, short job)."""
    process_runner = ProcessRunner()
    git_client = GitClient(process_runner)
    claude_client = ClaudeClient(process_runner, claude_binary=config.claude_binary, guard_path=config.guard_path,
                                 fallback_binaries=config.claude_fallback_binaries,
                                 call_recorder=build_shared_claude_call_log_service(config).record_call)
    project_repository = ProjectRepository(config.projects_registry_path)
    specs_file_repository = SpecsFileRepository()

    specs_structure_service = SpecsStructureService(specs_file_repository, config.templates_dir, process_runner, log)
    claude_job_service = ClaudeJobService(claude_client, specs_file_repository, log, job_timeout_seconds)
    project_progress_service = ProjectProgressService(specs_structure_service, specs_file_repository, git_client, process_runner,
                                                      claude_job_service, log)
    planning_requirement_service = PlanningRequirementService(specs_file_repository, process_runner)
    readiness_service = ReadinessService(process_runner, lambda: config.engine_script_path("run_loop"), log,
                                         enforced=bool(getattr(config, "planning_readiness_enforced", True)))
    project_capabilities_service = ProjectCapabilitiesService(specs_file_repository, log)
    # the repo registry is only needed for a secondary repository (`repo_name`); the checklist always reads the primary
    subprojects_service = SubprojectsService(SubprojectsFileRepository(specs_file_repository), specs_structure_service, None,
                                             project_progress_service, log)

    def describe_subprojects(context):
        """`facts["subprojects"]`: detects and stores the list on the first read. Before the `.specs/` structure exists nothing is
        written: the detection is shown unconfirmed and stored once the structure is initialized."""
        if not os.path.isdir(context.repo_path):
            return None
        if os.path.isdir(os.path.join(context.work_dir, ".specs")):
            description = subprojects_service.describe(context)
            if description and description.get("monorepo") and description.get("confirmed"):
                try:
                    description["system"] = system_map_service.status(context)  # the map gate waits for the system map when it is due
                except Exception as failure:  # noqa: BLE001
                    log.warn("system_map_status_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return description
        key, folder = subprojects_service.resolve_target(context)
        detected = subprojects_engine.detect(folder)
        return subprojects_service.build_description(key, folder, {"monorepo": bool(detected["monorepo"]), "confirmed": False,
                                                                   "validated_by": "script", "reason": detected.get("reason", ""),
                                                                   "projects": list(detected["projects"]), "notes": list(detected.get("warnings") or [])})
    project_progress_service.subprojects_provider = describe_subprojects
    # `enforced` is looked up on every call: the gate `ready` exists only while the approval depends on the check
    project_progress_service.readiness_provider = lambda context: readiness_service.get(context) if readiness_service.enforced else None

    def on_plan_changed(context):
        """After a change that moves what the loop can use (repo or role change, test environment values): rewrite the capabilities
        block of PROJECT.md, then check readiness again in the background. Never raises: it is a side effect of the real change."""
        try:
            project_capabilities_service.write(context)
        except Exception as failure:  # noqa: BLE001
            log.warn("capabilities_block_not_written", project=context.project.id, message=str(failure)[:200])
        try:
            readiness_service.schedule(context)
        except Exception as failure:  # noqa: BLE001
            log.warn("readiness_not_scheduled", project=context.project.id, message=str(failure)[:200])
    roadmap_service = RoadmapService(specs_file_repository, project_progress_service, claude_job_service, claude_client,
                                     planning_requirement_service, WorkspaceFileGuard(), log, readiness_service,
                                     project_capabilities_service)
    subproject_validation_service = SubprojectValidationService(claude_job_service, claude_client, subprojects_service.subprojects_file_repository,
                                                                project_progress_service, None, log)
    subproject_map_service = SubprojectMapService(roadmap_service.map_job_runner, subprojects_service.subprojects_file_repository,
                                                  subprojects_service, claude_job_service, roadmap_service.ensure_repo_free_for_job, log,
                                                  max_parallel_maps=getattr(config, "max_parallel_maps", 6))
    system_map_service = SystemMapService(roadmap_service.map_job_runner, subprojects_service, claude_job_service,
                                          roadmap_service.ensure_repo_free_for_job, log)
    attach_system_map(subproject_map_service, system_map_service)
    code_inventory_service = CodeInventoryService(config, log)
    attach_code_inventory(roadmap_service.map_job_runner, subproject_map_service, code_inventory_service)
    subprojects_service.code_inventory_service = code_inventory_service
    subprojects_service.map_queue_reader = subproject_map_service.read_queue
    subprojects_service.add_after_list_confirmed(project_capabilities_service.after_list_confirmed)
    context_block_service = ContextBlockService(code_inventory_service=code_inventory_service, log=log)
    subprojects_service.attach_context_block_service(context_block_service)
    specs_structure_service.attach_context_block_service(context_block_service)
    roadmap_service.map_job_runner.attach_context_block_service(context_block_service)
    subproject_map_service.attach_context_block_service(context_block_service)
    system_map_service.attach_context_block_service(context_block_service)
    test_environment_service = TestEnvironmentService(TestEnvironmentRepository(specs_file_repository), specs_structure_service, log)
    test_environment_service.after_change = on_plan_changed
    planning_repos_service = PlanningReposService(git_client, log)
    planning_service = PlanningService(PlanningThreadRepository(), specs_file_repository, project_progress_service,
                                       planning_requirement_service, claude_job_service, claude_client, WorkspaceFileGuard(), log,
                                       test_environment_service, planning_repos_service=planning_repos_service,
                                       project_capabilities_service=project_capabilities_service, readiness_service=readiness_service)
    planning_import_service = PlanningImportService(specs_file_repository, planning_service, log)
    project_progress_service.coverage_recorder = planning_import_service.record_coverage
    spec_file_service = SpecFileService(specs_file_repository, project_progress_service, log)
    specs_reset_service = SpecsResetService(specs_file_repository, specs_structure_service, project_progress_service, claude_job_service, log,
                                            LoopWorktreeRemovalService(git_client, specs_file_repository, log))
    folder_picker_service = FolderPickerService(process_runner, log, folder_dialog_argv)
    project_service = ProjectService(project_repository, specs_structure_service, specs_file_repository, git_client, process_runner,
                                     log, get_app_url)

    standards_service = StandardsService(subprojects_service, log)
    controllers = [
        ProjectController(project_service, project_progress_service, specs_structure_service, folder_picker_service, log,
                          specs_reset_service),
        RoadmapController(project_service, roadmap_service, claude_job_service, build_shared_claude_call_log_service(config)),
        SpecsController(project_service, spec_file_service),
        PlanningController(project_service, planning_service, planning_import_service, readiness_service),
        TestEnvironmentController(project_service, test_environment_service),
        SubprojectsController(project_service, subprojects_service, subproject_validation_service, subproject_map_service,
                              system_map_service, claude_job_service, standards_service),
    ]
    return ProjectsModule(project_service, controllers, [claude_job_service], project_progress_service, roadmap_service, claude_job_service,
                          planning_requirement_service, planning_service, readiness_service, project_capabilities_service,
                          planning_repos_service, on_plan_changed, subprojects_service, subproject_validation_service,
                          subproject_map_service, system_map_service, code_inventory_service, context_block_service,
                          standards_service)
