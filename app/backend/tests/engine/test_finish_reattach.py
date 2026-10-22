"""A wrap-up Finish marks last-run.json `finished`; the next start then creates a NEW loop branch instead of re-attaching to the old one.
A plain close (not finished) keeps re-attaching. Real git repos in temporary folders; no remote needed for the run_loop cases."""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import MultiRepoWorldTestCase, delete_tree, delivery, git, loop_state, write  # noqa: E402
import close_loop  # noqa: E402
import project_repos  # noqa: E402
import run_loop  # noqa: E402


class TestCloseWritesFinishedFlags(MultiRepoWorldTestCase):
    def setUp(self):
        super().setUp()
        self.install_gh_shim()
        self.write_meta()
        delivery.finalize_project(self.primary_work, self.meta)
        self.output_dir = os.path.join(self.root, "backups")

    def last_run(self):
        return self.read_json(loop_state.loop_path(self.primary, "last-run.json"))

    def test_close_without_finished_writes_no_flag(self):
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertEqual(sorted(result["closed"]), ["api", "app"])
        record = self.last_run()
        self.assertNotIn("finished", record)
        self.assertFalse(any("finished" in item for item in record["repos"].values()))

    def test_close_with_finished_marks_the_run_and_every_repo(self):
        result = close_loop.close(self.primary, output_dir=self.output_dir, finished=True)
        self.assertEqual(sorted(result["closed"]), ["api", "app"])
        record = self.last_run()
        self.assertTrue(record["finished"])
        self.assertEqual({name: item.get("finished") for name, item in record["repos"].items()}, {"app": True, "api": True})

    def test_the_cli_flag_finished_reaches_the_record(self):
        script = os.path.join(os.path.dirname(close_loop.__file__), "close_loop.py")
        done = subprocess.run([sys.executable, script, self.primary, "--close", "--finished", "--out", self.output_dir], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(self.last_run()["finished"])


class TestRunLoopReattachRule(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="finish-reattach-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = self.build("app")
        self.api = self.build("api")
        git(self.repo, "branch", "loop/old")
        git(self.api, "branch", "loop/old")
        self.primary_name = project_repos.short_name_of(self.repo)

    def build(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        git(path, "init", "-q")
        write(os.path.join(path, "README.md"), name + "\n")
        write(os.path.join(path, ".gitignore"), ".specs/\n")
        git(path, "add", "-A")
        git(path, "commit", "-q", "-m", "base")
        git(path, "branch", "-M", "main")
        return path

    def record(self, name, finished):
        entry = {"branch": "loop/old", "base": "main", "worktree_removed": True}
        if finished:
            entry["finished"] = True
        write(loop_state.loop_path(self.repo, "last-run.json"), json.dumps({"repos": {name: entry}}))

    def opts(self, **extra):
        values = dict(setup="", branch="", dry_run=False, repo_path=self.repo, push=False)
        values.update(extra)
        return types.SimpleNamespace(**values)

    def start_single(self, **extra):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            work, meta = run_loop.ensure_worktree(self.repo, self.opts(**extra))
        return work, meta, output.getvalue()

    def test_single_repo_after_a_finished_run_starts_a_new_branch(self):
        self.record(self.primary_name, finished=True)
        work, meta, _ = self.start_single()
        self.assertNotEqual(meta["branch"], "loop/old")
        self.assertTrue(meta["branch"].startswith("loop/"))
        self.assertEqual(git(work, "branch", "--show-current"), meta["branch"])

    def test_single_repo_after_a_plain_close_reattaches(self):
        self.record(self.primary_name, finished=False)
        work, meta, output = self.start_single()
        self.assertEqual(meta["branch"], "loop/old")
        self.assertEqual(git(work, "branch", "--show-current"), "loop/old")
        self.assertIn("reattach", output)

    def test_the_rule_helper_matches(self):
        self.record(self.primary_name, finished=False)
        self.assertEqual(run_loop.reattach_branch_of(self.repo, self.primary_name), "loop/old")
        self.record(self.primary_name, finished=True)
        self.assertEqual(run_loop.reattach_branch_of(self.repo, self.primary_name), "")

    def extra_dry_run(self):
        item = project_repos.RepoRef(name="api", path=self.api)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            run_loop.ensure_extra_worktree(item, self.opts(dry_run=True), "loop/old", {})
        return output.getvalue()

    def test_multi_repo_extra_worktree_after_a_finished_run_is_created_new(self):
        self.record("api", finished=True)
        self.assertIn("git worktree add -b loop/old", self.extra_dry_run())

    def test_multi_repo_extra_worktree_after_a_plain_close_reattaches(self):
        self.record("api", finished=False)
        text = self.extra_dry_run()
        self.assertIn("git worktree add", text)
        self.assertNotIn("-b loop/old", text)


if __name__ == "__main__":
    unittest.main()
