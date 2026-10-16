"""GitClient against temporary git repositories (real git, no network, no remote)."""
import os
import shutil
import subprocess
import tempfile
import unittest

from harness_backend.infrastructure.git_client import GitClient
from harness_backend.infrastructure.process_runner import ProcessRunner

GIT_AVAILABLE = shutil.which("git") is not None


def run_git(folder, *args):
    return subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True, encoding="utf-8", check=True).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


@unittest.skipUnless(GIT_AVAILABLE, "git is not on the PATH")
class GitClientCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="git-client-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.client = GitClient(ProcessRunner())

    def create_repo(self, name="repo"):
        folder = os.path.join(self.root, name)
        os.makedirs(folder)
        run_git(folder, "init", "-q", "-b", "main")
        for key, value in (("user.name", "test"), ("user.email", "someone@example.com"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            run_git(folder, "config", key, value)
        write(os.path.join(folder, "README.md"), "base\n")
        run_git(folder, "add", "README.md")
        run_git(folder, "commit", "-q", "-m", "base")
        return folder


class TestRead(GitClientCase):
    def test_repo_branch_and_folder_that_is_not_a_repo(self):
        repo = self.create_repo()
        self.assertTrue(self.client.is_git_repo(repo))
        self.assertEqual(self.client.get_current_branch(repo), "main")
        loose = os.path.join(self.root, "loose")
        os.makedirs(loose)
        self.assertFalse(self.client.is_git_repo(loose))
        self.assertFalse(self.client.is_git_repo(os.path.join(self.root, "does-not-exist")))
        self.assertEqual(self.client.get_current_branch(loose), "")
        self.assertEqual(self.client.list_worktrees(loose), [])

    def test_uncommitted_change_tracked_and_untracked(self):
        repo = self.create_repo()
        self.assertFalse(self.client.has_uncommitted_changes(repo))
        write(os.path.join(repo, "new.txt"), "x")
        self.assertTrue(self.client.has_uncommitted_changes(repo))
        self.assertFalse(self.client.has_uncommitted_changes(repo, include_untracked=False))
        write(os.path.join(repo, "README.md"), "changed\n")
        self.assertTrue(self.client.has_uncommitted_changes(repo, include_untracked=False))

    def test_worktrees_with_main_first_and_branch_without_prefix(self):
        repo = self.create_repo("proj")
        sibling = os.path.join(self.root, "proj-worktree-loop")
        run_git(repo, "worktree", "add", "-q", "-b", "loop/test", sibling)
        worktrees = self.client.list_worktrees(sibling)
        self.assertEqual([w["is_main"] for w in worktrees], [True, False])
        self.assertEqual(os.path.normcase(os.path.realpath(worktrees[0]["path"])), os.path.normcase(os.path.realpath(repo)))
        self.assertEqual(worktrees[1]["branch"], "loop/test")
        self.assertEqual(len(worktrees[1]["head"]), 40)

    def test_unpushed_commits_against_the_base_and_without_reference(self):
        repo = self.create_repo()
        run_git(repo, "checkout", "-q", "-b", "loop/feature")
        for number in range(2):
            write(os.path.join(repo, "f%d.txt" % number), str(number))
            run_git(repo, "add", "-A")
            run_git(repo, "commit", "-q", "-m", "c%d" % number)
        self.assertEqual(self.client.count_unpushed_commits(repo), 2)
        self.assertEqual(self.client.count_unpushed_commits(repo, bases=("does-not-exist",)), None)


class TestInitRepo(GitClientCase):
    def test_init_with_initial_commit_and_user_identity(self):
        folder = os.path.join(self.root, "new")
        write(os.path.join(folder, "a.txt"), "a")
        ok, error = self.client.init_repo_with_initial_commit(folder, "chore: initial commit", user="someone")
        self.assertTrue(ok, error)
        self.assertTrue(self.client.is_git_repo(folder))
        self.assertEqual(run_git(folder, "log", "--format=%s|%an"), "chore: initial commit|someone")
        self.assertFalse(self.client.has_uncommitted_changes(folder))
        local = subprocess.run(["git", "-C", folder, "config", "--local", "user.name"], capture_output=True, text=True)
        self.assertEqual(local.stdout.strip(), "", "the identity applies to the commit only; nothing written to the repo config")

    def test_empty_folder_also_gets_a_commit(self):
        folder = os.path.join(self.root, "empty")
        os.makedirs(folder)
        ok, error = self.client.init_repo_with_initial_commit(folder, "start")
        self.assertTrue(ok, error)
        self.assertEqual(run_git(folder, "rev-list", "--count", "HEAD"), "1")

    def test_existing_repo_is_not_reinitialized(self):
        repo = self.create_repo()
        ok, error = self.client.init_repo_with_initial_commit(repo, "must not commit")
        self.assertEqual((ok, error), (True, ""))
        self.assertEqual(run_git(repo, "rev-list", "--count", "HEAD"), "1")

    def test_missing_folder_returns_an_error(self):
        ok, error = self.client.init_repo_with_initial_commit(os.path.join(self.root, "nothing"), "x")
        self.assertFalse(ok)
        self.assertIn("does not exist", error)


if __name__ == "__main__":
    unittest.main()
