"""RD-01 / BR-02: the root .gitignore the app edits at approve (the `.specs/project/.env.test` line) is a setup file (app-generated config);
any other edit of it, and any other tracked change, stays an "other change" that is NOT carried."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import setup_files  # noqa: E402
import test_env_fallback  # noqa: E402
from test_worktree_base_branch import git, write  # noqa: E402


class TestGitignoreSetupFile(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.realpath(tempfile.mkdtemp(prefix="gitignore-setup-"))
        self.addCleanup(shutil.rmtree, self.repo, True)
        write(os.path.join(self.repo, ".gitignore"), "node_modules/\n")
        write(os.path.join(self.repo, "src", "app.py"), "v1\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")

    def paths(self):
        return [item["path"] for item in setup_files.list_setup_files(self.repo)]

    def test_the_line_the_app_adds_at_approve_is_a_setup_file_and_not_an_other_change(self):
        self.assertTrue(test_env_fallback.ensure_ignored(self.repo))
        self.assertEqual(self.paths(), [".gitignore"])
        self.assertEqual(setup_files.list_other_changes(self.repo, self.paths()), [])

    def test_a_user_edit_of_gitignore_is_an_other_change(self):
        test_env_fallback.ensure_ignored(self.repo)
        write(os.path.join(self.repo, ".gitignore"), "node_modules/\n.specs/project/.env.test\ndist/\n")
        self.assertEqual(self.paths(), [])
        self.assertEqual(setup_files.list_other_changes(self.repo, self.paths()), [".gitignore"])

    def test_other_tracked_changes_are_listed_next_to_the_gitignore_setup_file(self):
        test_env_fallback.ensure_ignored(self.repo)
        write(os.path.join(self.repo, "src", "app.py"), "v2\n")
        block = setup_files.uncommitted_blocks([type("Item", (), {
            "name": "proj", "path": self.repo, "primary": True, "role": "edit", "loop_worktree_path": lambda self_: os.path.join(self.repo, "-none-")})()])
        self.assertEqual(block, [{"repo": "proj", "setup_files": [".gitignore"], "other_changes": ["src/app.py"]}])
        self.assertIn("1 other changed file will NOT be carried: src/app.py", setup_files.block_text(block[0]))


if __name__ == "__main__":
    unittest.main()
