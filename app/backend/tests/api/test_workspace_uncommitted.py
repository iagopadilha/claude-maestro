"""`uncommitted_context` of the workspace payload and of the loop check response: present while the worktree is not created,
empty once it exists. Scratch git repos, no claude, no real engine."""
import json
import os

from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.loop_control_service import LoopControlService
from harness_backend.services.workspace_service import WorkspaceService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import run_git, write_file


class IdleJobService:
    def get_running_job(self, repo_path):
        return None


class TestWorkspaceUncommitted(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.context = self.make_context(self.primary)
        self.workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client,
                                          IdleJobService(), None, self.log)

    def only_repo(self):
        return self.workspace.get_workspace(self.context)["repos"][0]

    def test_field_is_present_and_empty_without_pending_files(self):
        self.assertEqual(self.only_repo()["uncommitted_context"], [])

    def test_lists_pending_context_files_next_to_dirty(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        write_file(os.path.join(self.primary, "CLAUDE.md"), "claude\n")
        write_file(os.path.join(self.primary, ".specs", "x.md"), "plan\n")
        repo = self.only_repo()
        self.assertTrue(repo["dirty"])
        self.assertEqual([(item["path"], item["kind"]) for item in repo["uncommitted_context"]],
                         [("CLAUDE.md", "claude"), ("docs/codebase/STACK.md", "map")])

    def test_monorepo_entry_of_the_primary_is_used(self):
        entry = {"monorepo": True, "confirmed": True, "projects": [{"path": "backend", "skip": False}]}
        write_file(os.path.join(self.primary, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"version": 1, "repos": {"app": entry}}))
        write_file(os.path.join(self.primary, "backend", "CLAUDE.md"), "claude\n")
        write_file(os.path.join(self.primary, "CLAUDE.md"), "root\n")
        self.assertEqual([item["path"] for item in self.only_repo()["uncommitted_context"]], ["backend/CLAUDE.md"])

    def test_hidden_when_the_worktree_exists(self):
        run_git(self.primary, "worktree", "add", "-q", "-b", "loop/test", self.primary + "-worktree-loop")
        write_file(os.path.join(self.primary, "CLAUDE.md"), "claude\n")
        self.assertEqual(self.only_repo()["uncommitted_context"], [])


class TestCheckResponse(MultiRepoTestCase):
    def test_check_lists_per_repo_and_hides_after_worktree(self):
        primary = self.make_repo("app")
        context = self.make_context(primary)
        write_file(os.path.join(primary, "CLAUDE.md"), "claude\n")
        described = LoopControlService.describe_uncommitted_context_by_repo(context)
        self.assertEqual([(repo["name"], [item["path"] for item in repo["uncommitted_context"]]) for repo in described],
                         [("app", ["CLAUDE.md"])])
        run_git(primary, "worktree", "add", "-q", "-b", "loop/test", primary + "-worktree-loop")
        self.assertEqual(LoopControlService.describe_uncommitted_context_by_repo(context)[0]["uncommitted_context"], [])
