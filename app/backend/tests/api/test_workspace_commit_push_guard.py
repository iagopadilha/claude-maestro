"""The legacy route POST workspace/repos/<name>/commit-push only acts on a LOOP WORKTREE (a `loop/*` branch): the user's own folder and
main/master are refused. Also: a reference repository is read only (no worktree, no uncommitted lists, never a create target)."""
import os

from harness_backend.domain.project_errors import RepoConflictError
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.workspace_service import (NOT_LOOP_BRANCH_CODE, STATE_READ_ONLY, USER_FOLDER_REFUSED_CODE, WorkspaceService)

from .support_multi_repo import MultiRepoTestCase
from .support_projects import run_git, write_file
from .test_workspace_service import FakeEngineAdapter, InlineJobService


class TestWorkspaceCommitPushGuard(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.context = self.make_context(self.primary)
        self.remote = os.path.join(self.temporary_dir, "remote.git")
        os.makedirs(self.remote)
        run_git(self.remote, "init", "--bare", "-q")
        run_git(self.primary, "remote", "add", "origin", self.remote)
        self.worktree = self.primary + "-worktree-loop"
        self.workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client,
                                          InlineJobService(), FakeEngineAdapter(), self.log)

    def remote_log(self):
        return run_git(self.remote, "log", "--oneline", "--all").stdout

    def test_user_folder_without_a_worktree_is_refused_and_nothing_is_committed(self):
        write_file(os.path.join(self.primary, "pending.txt"), "mine\n")
        with self.assertRaises(RepoConflictError) as raised:
            self.workspace.commit_and_push(self.context, "app", "wip")
        self.assertEqual(raised.exception.code, USER_FOLDER_REFUSED_CODE)
        self.assertIn("Commit setup files and push", str(raised.exception))
        self.assertTrue(self.git_client.has_uncommitted_changes(self.primary))
        self.assertEqual(self.remote_log(), "")

    def test_a_worktree_on_main_is_refused(self):
        run_git(self.primary, "checkout", "-q", "-b", "developer")
        run_git(self.primary, "worktree", "add", "-q", self.worktree, "main")
        write_file(os.path.join(self.worktree, "pending.txt"), "x\n")
        with self.assertRaises(RepoConflictError) as raised:
            self.workspace.commit_and_push(self.context, "app", "wip")
        self.assertEqual(raised.exception.code, NOT_LOOP_BRANCH_CODE)
        self.assertEqual(self.remote_log(), "")

    def test_a_worktree_on_a_non_loop_branch_is_refused(self):
        run_git(self.primary, "worktree", "add", "-q", "-b", "feature/x", self.worktree)
        with self.assertRaises(RepoConflictError) as raised:
            self.workspace.commit_and_push(self.context, "app", "wip")
        self.assertEqual(raised.exception.code, NOT_LOOP_BRANCH_CODE)

    def test_git_client_never_commits_on_main(self):
        write_file(os.path.join(self.primary, "pending.txt"), "x\n")
        outcome = self.git_client.commit_all_and_push(self.primary, "wip", True)
        self.assertFalse(outcome["ok"])
        self.assertFalse(outcome["committed"])
        self.assertTrue(self.git_client.has_uncommitted_changes(self.primary))

    def test_loop_worktree_still_commits_and_pushes_its_branch(self):
        run_git(self.primary, "worktree", "add", "-q", "-b", "loop/test", self.worktree)
        write_file(os.path.join(self.worktree, "pending.txt"), "loop work\n")
        result = self.workspace.commit_and_push(self.context, "app", "wip: save")
        self.assertEqual((result["branch"], result["committed"], result["pushed"]), ("loop/test", True, True))
        self.assertIn("wip: save", self.remote_log())
        self.assertEqual(run_git(self.remote, "branch", "--list").stdout.split(), ["loop/test"])

    def test_reference_repo_is_read_only_with_no_worktree_and_no_carried_files(self):
        library = self.make_repo("lib", {"main.py": "x\n"})
        self.registry.add_repo(self.context, library, [], None, "reference", "contracts")
        write_file(os.path.join(library, "docs", "codebase", "STACK.md"), "map\n")
        write_file(os.path.join(library, "main.py"), "changed\n")
        rows = {row["name"]: row for row in self.workspace.get_workspace(self.context)["repos"]}
        row = rows["lib"]
        self.assertEqual((row["state"], row["read_only"], row["worktree_exists"]), (STATE_READ_ONLY, True, False))
        self.assertEqual((row["dirty"], row["uncommitted_context"], row["setup_files"], row["other_changes"], row["notes"]), (False, [], [], [], []))
        self.assertFalse(rows["app"]["read_only"])

    def test_reference_repo_is_not_a_default_create_target(self):
        library = self.make_repo("lib", {"main.py": "x\n"})
        self.registry.add_repo(self.context, library, [], None, "reference", "contracts")
        from harness_backend.engine import project_repos
        targets = WorkspaceService.select_targets(project_repos.load_repos(self.primary), None)
        self.assertEqual([repo.name for repo in targets], ["app"])
