"""BR-01: a new loop worktree starts from the integration branch (`developer`) when it exists locally, else from the current branch with
a WARNING naming it. The base and its sha are recorded in meta.json. A re-attach keeps the recorded base."""
import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402


def git(folder, *arguments):
    return subprocess.run(["git", "-C", folder, "-c", "user.name=t", "-c", "user.email=t@l"] + list(arguments), capture_output=True, text=True, check=True).stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def delete_tree(path):
    def clear_read_only(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=clear_read_only)


class TestWorktreeBaseBranch(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="base-branch-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, ".gitignore"), ".specs/\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "branch", "-M", "main")
        self.opts = types.SimpleNamespace(branch="loop/base", dry_run=False, setup="")
        self.previous_environment = {name: os.environ.pop(name, None) for name in ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH")}
        self.addCleanup(self.restore_environment)

    def restore_environment(self):
        for name, value in self.previous_environment.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def create(self, tag=""):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            work, meta = run_loop.ensure_worktree(self.repo, self.opts, tag=tag)
        return work, meta, output.getvalue()

    def test_starts_from_developer_when_it_exists_even_if_the_folder_is_on_main(self):
        git(self.repo, "checkout", "-q", "-b", "developer")
        write(os.path.join(self.repo, "dev.txt"), "only on developer\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "dev work")
        developer_sha = git(self.repo, "rev-parse", "--short", "developer")
        git(self.repo, "checkout", "-q", "main")
        work, meta, output = self.create()
        self.assertEqual(meta["base"], "developer")
        self.assertEqual(meta["base_sha"], developer_sha)
        self.assertTrue(os.path.isfile(os.path.join(work, "dev.txt")))
        self.assertNotIn("warn", output)

    def test_without_developer_it_starts_from_the_current_branch_and_warns_naming_it(self):
        git(self.repo, "checkout", "-q", "-b", "feature/x")
        work, meta, output = self.create()
        self.assertEqual(meta["base"], "feature/x")
        self.assertEqual(meta["base_sha"], git(self.repo, "rev-parse", "--short", "feature/x"))
        self.assertIn("warn: the integration branch `developer` does not exist locally", output)
        self.assertIn("`feature/x`", output)

    def test_a_configured_integration_branch_wins_over_developer(self):
        git(self.repo, "branch", "developer")
        git(self.repo, "branch", "staging")
        os.environ["LOOP_INTEGRATION_BRANCH"] = "staging"
        _, meta, _ = self.create()
        self.assertEqual(meta["base"], "staging")

    def test_tagged_multi_repo_step_lines_name_the_base(self):
        git(self.repo, "branch", "developer")
        _, meta, output = self.create(tag="proj")
        self.assertIn("[loop] [proj] check: git ok; current branch main; the worktree starts from developer", output)
        self.assertEqual(meta["base"], "developer")

    def test_reattach_keeps_the_recorded_base(self):
        git(self.repo, "branch", "developer")
        self.opts.branch = ""
        work, meta, _ = self.create()
        git(self.repo, "worktree", "remove", "--force", work)
        run_loop.save_json(run_loop.loop_state.loop_path(self.repo, run_loop.LAST_RUN_NAME),
                           {"repos": {"proj": {"branch": meta["branch"], "base": "recorded-base", "finished": False}}})
        _, again, _ = self.create()
        self.assertEqual(again["branch"], meta["branch"])
        self.assertEqual(again["base"], "recorded-base")

    def test_readiness_warns_once_naming_the_current_branch(self):
        repos = run_loop.project_repos.load_repos(self.repo)
        self.assertIn("`main`", run_loop.base_branch_warnings(repos)[0])
        git(self.repo, "branch", "developer")
        self.assertEqual(run_loop.base_branch_warnings(repos), [])


if __name__ == "__main__":
    unittest.main()
