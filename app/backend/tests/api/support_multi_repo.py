"""Support for the multi-repo service tests: temporary git repos, a project context over them, no HTTP, no claude, no network."""
import os
import tempfile
import unittest

from harness_backend.domain.project import Project, ProjectContext
from harness_backend.infrastructure.git_client import GitClient
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.loop_status_service import LoopStatusService
from harness_backend.services.repo_registry_service import RepoRegistryService

from .support_projects import create_git_repo, delete_folder, loop_file_path, write_file


class StubProjectService:
    """list_projects / get_project_context over a fixed set of contexts (what LoopStatusService needs)."""

    def __init__(self):
        self.contexts = {}

    def add(self, context):
        self.contexts[context.project.id] = context
        return context

    def list_projects(self):
        return [context.project for context in self.contexts.values()]

    def get_project_context(self, project_id):
        return self.contexts[project_id]


class MultiRepoTestCase(unittest.TestCase):
    """Temporary folder with helpers: `make_repo(name)` (git repo with one commit), `make_context(repo)`, real registry service."""

    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="harness-multirepo-")
        self.addCleanup(delete_folder, self.temporary_dir)
        self.log = StructuredLog(os.path.join(self.temporary_dir, "logs"))
        self.git_client = GitClient(ProcessRunner())
        self.project_service = StubProjectService()
        self.loop_status_service = LoopStatusService(LoopFileRepository(), self.project_service, self.log)
        self.registry = RepoRegistryService(self.git_client, self.loop_status_service, self.log)

    def make_repo(self, name, files=None):
        return create_git_repo(os.path.join(self.temporary_dir, name), files)

    def make_context(self, repo_path, project_id=None):
        project = Project(id=project_id or os.path.basename(repo_path), name=os.path.basename(repo_path), repo_path=repo_path)
        return self.project_service.add(ProjectContext(project))

    @staticmethod
    def write_live_loop_lock(work_dir):
        write_file(loop_file_path(work_dir, "internal", "LOOP.lock"), str(os.getpid()))
