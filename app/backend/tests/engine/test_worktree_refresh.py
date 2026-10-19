"""run_loop.ensure_worktree reusing a worktree that never ran a loop: `.specs`, `CLAUDE.md` and `.claude` are copied again from the
primary repo (the copy made at creation can be stale) and meta.json survives. Once a loop ran there, nothing is overwritten."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import loop_state  # noqa: E402
import run_loop  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class TestRefreshUnusedWorktree(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="refresh-"))
        self.addCleanup(self.delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        os.makedirs(self.repo)
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, ".gitignore"), ".specs/\nCLAUDE.md\n.claude/\n")
        for arguments in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "-m", "start"], ["branch", "-M", "main"]):
            subprocess.run(["git", "-C", self.repo] + arguments, capture_output=True, check=True)
        write(os.path.join(self.repo, ".specs", "project", "ROADMAP.md"), "# v1\n")
        write(os.path.join(self.repo, "CLAUDE.md"), "claude v1\n")
        write(os.path.join(self.repo, ".claude", "settings.json"), "{\"v\": 1}")
        self.opts = types.SimpleNamespace(branch="loop/refresh", dry_run=False, setup="")
        self.work, _ = run_loop.ensure_worktree(self.repo, self.opts)

    @staticmethod
    def delete_tree(path):
        import stat

        def clear_read_only(function, target, _):
            os.chmod(target, stat.S_IWRITE)
            function(target)
        shutil.rmtree(path, onerror=clear_read_only)

    def change_primary(self):
        write(os.path.join(self.repo, ".specs", "project", "ROADMAP.md"), "# v2\n")
        write(os.path.join(self.repo, ".specs", "project", "NEW.md"), "new file\n")
        write(os.path.join(self.repo, "CLAUDE.md"), "claude v2\n")
        write(os.path.join(self.repo, ".claude", "settings.json"), "{\"v\": 2}")

    def test_a_worktree_born_before_the_plan_changed_is_refreshed_on_reuse(self):
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "ROADMAP.md")), "# v1\n")
        self.change_primary()
        write(os.path.join(self.work, ".specs", "project", "ONLY-IN-WORKTREE.md"), "stale\n")
        meta_before = read(loop_state.loop_path(self.work, "meta.json"))
        write(os.path.join(self.repo, ".specs", "project", "NEW.md"), "new file\n")
        run_loop.ensure_worktree(self.repo, self.opts)
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "ROADMAP.md")), "# v2\n")
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "NEW.md")), "new file\n")
        self.assertFalse(os.path.exists(os.path.join(self.work, ".specs", "project", "ONLY-IN-WORKTREE.md")))
        self.assertEqual(read(os.path.join(self.work, "CLAUDE.md")), "claude v2\n")
        self.assertEqual(read(os.path.join(self.work, ".claude", "settings.json")), "{\"v\": 2}")
        self.assertEqual(read(loop_state.loop_path(self.work, "meta.json")), meta_before)  # the loop's own record survives

    def test_a_worktree_where_a_loop_ran_is_never_overwritten(self):
        for marker, create in {"LOOP.json": lambda: write(loop_state.loop_path(self.work, "LOOP.json"), "{}"),
                               "turns": lambda: write(loop_state.loop_path(self.work, "meta.json"), json.dumps({"branch": "loop/refresh", "turns": 3}))}.items():
            with self.subTest(marker=marker):
                write(os.path.join(self.work, ".specs", "project", "ROADMAP.md"), "# progress made by the loop\n")
                create()
                self.change_primary()
                run_loop.ensure_worktree(self.repo, self.opts)
                self.assertEqual(read(os.path.join(self.work, ".specs", "project", "ROADMAP.md")), "# progress made by the loop\n")
                self.assertEqual(read(os.path.join(self.work, "CLAUDE.md")), "claude v1\n")

    def test_the_loop_files_of_the_worktree_survive_and_the_approval_follows_the_primary(self):
        write(loop_state.loop_path(self.work, "PAUSE"), "")
        write(loop_state.loop_path(self.work, "LOOP-CMD.json"), "{\"argv\": []}")
        write(loop_state.loop_path(self.work, "monitor-relaunch.log"), "[loop-launch] x\n")
        write(loop_state.loop_path(self.work, "ROADMAP-APPROVED.json"), "{\"stale\": true}")  # approval since reset in the primary
        write(loop_state.loop_path(self.repo, "ANSWERS.md"), "answers made in the primary\n")
        self.change_primary()
        run_loop.ensure_worktree(self.repo, self.opts)
        for name in ("PAUSE", "LOOP-CMD.json", "monitor-relaunch.log"):
            self.assertTrue(os.path.exists(loop_state.loop_path(self.work, name)), name)
        self.assertFalse(os.path.exists(loop_state.loop_path(self.work, "ROADMAP-APPROVED.json")))
        self.assertEqual(read(loop_state.loop_path(self.work, "ANSWERS.md")), "answers made in the primary\n")
        write(loop_state.loop_path(self.repo, "ROADMAP-APPROVED.json"), "{\"approved\": true}")  # approved in the primary: it comes over
        run_loop.ensure_worktree(self.repo, self.opts)
        self.assertEqual(read(loop_state.loop_path(self.work, "ROADMAP-APPROVED.json")), "{\"approved\": true}")

    def test_worktree_never_ran(self):
        self.assertTrue(run_loop.worktree_never_ran(self.work))
        write(loop_state.loop_path(self.work, "LOCK-FREE-CHECK.json"), "{}")  # other files do not count
        self.assertTrue(run_loop.worktree_never_ran(self.work))
        write(os.path.join(loop_state.turns_dir(self.work), "001.json"), "{}")
        self.assertFalse(run_loop.worktree_never_ran(self.work))

    def test_dry_run_refreshes_nothing(self):
        self.change_primary()
        run_loop.ensure_worktree(self.repo, types.SimpleNamespace(branch="loop/refresh", dry_run=True, setup=""))
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "ROADMAP.md")), "# v1\n")


if __name__ == "__main__":
    unittest.main()
