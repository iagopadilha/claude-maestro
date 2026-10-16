"""Wiring of the projects module: the ONLY place of the module that instantiates concrete classes.

Exposes `project_service` (IProjectService, consumed by the other modules), `controllers` and `busy_providers`
(running claude jobs prevent the idle shutdown).
"""
from ..api.controllers.project_controller import ProjectController
from ..api.controllers.roadmap_controller import RoadmapController
from ..api.controllers.specs_controller import SpecsController
from ..infrastructure.claude_client import ClaudeClient
from ..infrastructure.git_client import GitClient
from ..infrastructure.process_runner import ProcessRunner
from ..repositories.project_repository import ProjectRepository
from ..repositories.specs_file_repository import SpecsFileRepository
from .claude_job_service import DEFAULT_JOB_TIMEOUT_SECONDS, ClaudeJobService
from .folder_picker_service import FolderPickerService
from .project_progress_service import ProjectProgressService
from .project_service import ProjectService
from .roadmap_service import RoadmapService
from .spec_file_service import SpecFileService
from .specs_structure_service import SpecsStructureService


class ProjectsModule:
    def __init__(self, project_service, controllers, busy_providers, project_progress_service, roadmap_service, claude_job_service):
        self.project_service = project_service
        self.controllers = controllers
        self.busy_providers = busy_providers
        self.project_progress_service = project_progress_service
        self.roadmap_service = roadmap_service
        self.claude_job_service = claude_job_service


def build_projects_module(config, log, get_app_url, folder_dialog_argv=None, job_timeout_seconds=DEFAULT_JOB_TIMEOUT_SECONDS):
    """`folder_dialog_argv` and `job_timeout_seconds` only exist for tests (fake dialog, short job)."""
    process_runner = ProcessRunner()
    git_client = GitClient(process_runner)
    claude_client = ClaudeClient(process_runner, claude_binary=config.claude_binary)
    project_repository = ProjectRepository(config.projects_registry_path)
    specs_file_repository = SpecsFileRepository()

    specs_structure_service = SpecsStructureService(specs_file_repository, config.templates_dir, process_runner, log)
    claude_job_service = ClaudeJobService(claude_client, specs_file_repository, log, job_timeout_seconds)
    project_progress_service = ProjectProgressService(specs_structure_service, specs_file_repository, git_client, process_runner,
                                                      claude_job_service, log)
    roadmap_service = RoadmapService(specs_file_repository, project_progress_service, claude_job_service, claude_client, log)
    spec_file_service = SpecFileService(specs_file_repository, project_progress_service, log)
    folder_picker_service = FolderPickerService(process_runner, log, folder_dialog_argv)
    project_service = ProjectService(project_repository, specs_structure_service, specs_file_repository, git_client, process_runner,
                                     log, get_app_url)

    controllers = [
        ProjectController(project_service, project_progress_service, specs_structure_service, folder_picker_service, log),
        RoadmapController(project_service, roadmap_service, claude_job_service),
        SpecsController(project_service, spec_file_service),
    ]
    return ProjectsModule(project_service, controllers, [claude_job_service], project_progress_service, roadmap_service, claude_job_service)
