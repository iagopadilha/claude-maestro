"""Files an earlier turn left uncommitted: the engine checkpoints the ones under an in-flight feature's paths, stops on the rest,
and the panel recovery (`--leftovers commit|discard`) settles everything. Real git, temporary repo, no claude."""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402

ROADMAP = "## Stage 1\n- [a] status=building kind=back paths=src/a\n- [b] status=pending kind=back paths=src/b\n"


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True).stdout


class LeftoversCase(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="leftovers-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        for key, value in (("user.name", "t"), ("user.email", "t@t.t"), ("commit.gpgsign", "false")):
            sh(["git", "config", key, value], self.repo)
        self.write("src/a/keep.txt", "base")
        self.write(".specs/project/ROADMAP.md", ROADMAP)
        sh(["git", "add", "-A"], self.repo)
        sh(["git", "commit", "-q", "-m", "base"], self.repo)
        self.meta = {}
        self.opts = argparse.Namespace(leftovers="", primary_name="")

    def write(self, name, text):
        path = os.path.join(self.repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_files_under_the_paths_of_the_building_feature_are_committed(self):
        self.write("src/a/keep.txt", "changed")
        self.write("src/a/new.txt", "new")
        self.assertEqual(run_loop.settle_dirty_worktrees(self.repo, self.meta, 4, self.opts), [])
        self.assertEqual(sh(["git", "status", "--porcelain"], self.repo), "")
        self.assertIn("checkpoint", sh(["git", "log", "-1", "--format=%s"], self.repo))
        self.assertEqual(self.meta["checkpoints"][0]["turn"], 3)

    def test_files_outside_every_in_flight_path_still_stop_the_loop(self):
        self.write("src/a/new.txt", "mine")
        self.write("src/b/other.txt", "feature b is pending")
        self.write("stray.txt", "x")
        dirty = run_loop.settle_dirty_worktrees(self.repo, self.meta, 4, self.opts)
        self.assertEqual(len(dirty), 1)
        self.assertEqual(sorted(run_loop.row_path(row) for row in dirty[0][1]), ["src/b/", "stray.txt"])
        self.assertEqual(sh(["git", "status", "--porcelain", "src/a"], self.repo), "")

    def test_recovery_commit_takes_everything(self):
        self.write("stray.txt", "x")
        self.opts.leftovers = "commit"
        self.assertEqual(run_loop.settle_dirty_worktrees(self.repo, self.meta, 4, self.opts), [])
        self.assertEqual(sh(["git", "status", "--porcelain"], self.repo), "")

    def test_recovery_discard_removes_untracked_and_restores_tracked(self):
        self.write("stray.txt", "x")
        self.write("src/a/keep.txt", "changed")
        self.opts.leftovers = "discard"
        self.assertEqual(run_loop.settle_dirty_worktrees(self.repo, self.meta, 4, self.opts), [])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "stray.txt")))
        with open(os.path.join(self.repo, "src", "a", "keep.txt"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "base")

    def test_background_agents_are_awaited(self):
        self.assertEqual(run_loop.push_lock_env(self.repo)["CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"], "0")


if __name__ == "__main__":
    unittest.main()
