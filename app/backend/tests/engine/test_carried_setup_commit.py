"""BR-02: the setup files carried into a NEW worktree are committed on the loop branch at once (one setup commit with exactly those files),
so the worktree is clean and the safe close removes it without force. Gitignored carried files are not force-added."""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import run_loop  # noqa: E402
from test_worktree_base_branch import delete_tree, git, write  # noqa: E402

APP_BLOCK = "<!-- maestro:context -->\nfacts\n<!-- /maestro:context -->\n"


class TestCarriedSetupCommit(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="carry-commit-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, "src", "app.py"), "v1\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "config", "user.name", "Loop Tester")
        git(self.repo, "config", "user.email", "loop@example.test")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "branch", "-M", "developer")
        self.opts = types.SimpleNamespace(branch="loop/carry", dry_run=False, setup="")

    def create(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_loop.ensure_worktree(self.repo, self.opts)

    def ignore_on_base_only(self):
        """`developer` ignores the architecture map; the user's folder (another branch) does not, so the file is carried and then ignored there."""
        write(os.path.join(self.repo, ".gitignore"), "docs/codebase/ARCHITECTURE.md\n")
        git(self.repo, "add", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "ignore the map")
        git(self.repo, "checkout", "-q", "-b", "feature/y")
        git(self.repo, "rm", "-q", ".gitignore")
        git(self.repo, "commit", "-q", "-m", "no ignore on the feature branch")

    def commits_after_base(self, work):
        return git(work, "log", "--format=%s", "developer..HEAD").splitlines()

    def test_uncommitted_and_committed_on_feature_setup_files_make_one_setup_commit(self):
        # committed on the user's current branch (a worktree from `developer` would never get them) + uncommitted ones
        git(self.repo, "checkout", "-q", "-b", "feature/x")
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "stack\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "maps on the feature branch")
        write(os.path.join(self.repo, "docs", "codebase", "ARCHITECTURE.md"), "arch\n")
        write(os.path.join(self.repo, "CLAUDE.md"), "# x\n" + APP_BLOCK)
        write(os.path.join(self.repo, "src", "other.py"), "not setup\n")
        work, meta = self.create()
        self.assertEqual(self.commits_after_base(work), ["chore(setup): carry codebase maps and agent context"])
        files = sorted(git(work, "show", "--name-only", "--format=", "HEAD").split())
        self.assertEqual(files, sorted(meta["carried_setup_files"] + ["CLAUDE.md"]))
        self.assertEqual(files, ["CLAUDE.md", "docs/codebase/ARCHITECTURE.md", "docs/codebase/STACK.md"])
        self.assertEqual(git(work, "log", "-1", "--format=%an <%ae>"), "Loop Tester <loop@example.test>")
        self.assertEqual(git(work, "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).specs"), "")

    def test_safe_close_cleanup_removes_the_worktree_without_force(self):
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "stack\n")
        work, _ = self.create()
        self.assertIsNone(close_loop.foreign_content_in_specs(work, ()))
        close_loop.clear_engine_specs(work, (), self.repo)
        code, _, err = run_loop.git(self.repo, "worktree", "remove", work)
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.isdir(work))

    def test_nothing_carried_makes_no_commit(self):
        work, meta = self.create()
        self.assertEqual(meta["carried_setup_files"], [])
        self.assertEqual(self.commits_after_base(work), [])

    def test_gitignored_carried_files_are_not_force_added(self):
        self.ignore_on_base_only()
        write(os.path.join(self.repo, "docs", "codebase", "ARCHITECTURE.md"), "arch\n")
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "stack\n")
        work, meta = self.create()
        self.assertEqual(git(work, "show", "--name-only", "--format=", "HEAD").split(), ["docs/codebase/STACK.md"])
        self.assertIn("docs/codebase/ARCHITECTURE.md", meta["carried_setup_files"])
        self.assertEqual(git(work, "ls-files", "docs/codebase/ARCHITECTURE.md"), "")

    def test_only_ignored_carried_files_make_no_commit(self):
        self.ignore_on_base_only()
        write(os.path.join(self.repo, "docs", "codebase", "ARCHITECTURE.md"), "arch\n")
        work, _ = self.create()
        self.assertEqual(self.commits_after_base(work), [])


if __name__ == "__main__":
    unittest.main()
