"""BR-02: setup files carried into the worktree are not "dirty tree outside .specs/" leftovers before turn 1."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402
from test_worktree_base_branch import delete_tree, git, write  # noqa: E402


class TestCarriedSetupFilesAreNotLeftovers(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="carried-")
        git(self.work, "init", "-b", "main")
        write(os.path.join(self.work, "README.md"), "x\n")
        git(self.work, "add", "-A")
        git(self.work, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-m", "init")
        write(os.path.join(self.work, "frontend", "docs", "codebase", "EXTRA.md"), "extra\n")
        write(os.path.join(self.work, "stray.txt"), "stray\n")

    def tearDown(self):
        delete_tree(self.work)

    def test_carried_file_is_ignored_and_a_stray_file_still_stops(self):
        meta = {"carried_setup_files": ["frontend/docs/codebase/EXTRA.md"]}
        rows = [row for _, found in run_loop.dirty_worktrees(self.work, meta) for row in found]
        self.assertEqual(1, len(rows))
        self.assertIn("stray.txt", rows[0])

    def test_without_the_meta_entry_the_carried_file_is_dirty(self):
        rows = [row for _, found in run_loop.dirty_worktrees(self.work, {}) for row in found]
        self.assertTrue(any("frontend/" in row for row in rows))


if __name__ == "__main__":
    unittest.main()
