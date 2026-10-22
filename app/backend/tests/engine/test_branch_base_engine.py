"""ROADMAP `Branch base:` in the engine: `choose_base` (where a new worktree starts) and the delivery PR target (option, ROADMAP, env, default)."""
import os
import subprocess
import sys
import tempfile
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import delivery  # noqa: E402
import run_loop  # noqa: E402
from test_worktree_base_branch import delete_tree, git, write  # noqa: E402

ENVIRONMENT_NAMES = ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "LOOP_PR_MODE")


class TestBranchBaseEngine(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="branch-base-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, ".gitignore"), ".specs/\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "remote", "add", "origin", "https://github.com/org/proj.git")
        git(self.repo, "config", "user.name", "tester")
        git(self.repo, "config", "user.email", "tester@example.com")
        self.previous_environment = {name: os.environ.pop(name, None) for name in ENVIRONMENT_NAMES}
        self.addCleanup(self.restore_environment)
        self.original_load = delivery.notify.envload.load
        self.original_get = delivery.notify.envload.get
        delivery.notify.envload.load = lambda: None  # the real app .env of the machine must not leak in
        delivery.notify.envload.get = lambda name, default="": default
        self.addCleanup(self.restore_envload)

    def restore_envload(self):
        delivery.notify.envload.load = self.original_load
        delivery.notify.envload.get = self.original_get

    def restore_environment(self):
        for name, value in self.previous_environment.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def write_roadmap(self, header_line):
        write(os.path.join(self.repo, ".specs", "project", "ROADMAP.md"), "# Roadmap\n\n**Goal:** x\n\n%s\n" % header_line)

    # ---------------------------------------------------------------- choose_base
    def test_choose_base_uses_the_roadmap_branch_when_it_exists_locally(self):
        git(self.repo, "branch", "developer")
        git(self.repo, "branch", "release-train")
        self.assertEqual(run_loop.choose_base(self.repo, "release-train"), ("release-train", ""))

    def test_choose_base_warns_naming_both_when_the_roadmap_branch_is_missing(self):
        git(self.repo, "branch", "developer")  # the default exists, but the ROADMAP asked for another one
        base, warning = run_loop.choose_base(self.repo, "release-train")
        self.assertEqual(base, "main")
        self.assertIn("`release-train`", warning)
        self.assertIn("`main`", warning)

    def test_choose_base_without_option_keeps_the_environment_then_developer(self):
        git(self.repo, "branch", "developer")
        self.assertEqual(run_loop.choose_base(self.repo)[0], "developer")
        git(self.repo, "branch", "staging")
        os.environ["LOOP_INTEGRATION_BRANCH"] = "staging"
        self.assertEqual(run_loop.choose_base(self.repo)[0], "staging")

    def test_base_branch_option_is_read_from_the_options_and_defaults_to_empty(self):
        self.assertEqual(run_loop.base_branch_of(types.SimpleNamespace(base_branch="x")), "x")
        self.assertEqual(run_loop.base_branch_of(types.SimpleNamespace()), "")

    # ---------------------------------------------------------------- delivery
    def test_integration_branch_order_option_env_default(self):
        self.assertEqual(delivery.integration_branch(), "developer")
        os.environ["LOOP_PR_BASE"] = "from-env"
        self.assertEqual(delivery.integration_branch(), "from-env")
        self.assertEqual(delivery.integration_branch("from-option"), "from-option")

    def test_pr_target_is_the_option(self):
        result = delivery.finalize(self.repo, "loop/x", dry_run=True, base="release-train")
        self.assertTrue(result["push"].startswith("dry-run"), result)
        self.assertIn("/compare/release-train...loop/x", result["pr"])

    def test_pr_target_is_read_from_the_roadmap_when_no_option_and_beats_the_environment(self):
        self.write_roadmap("**Branch base:** staging")
        os.environ["LOOP_PR_BASE"] = "from-env"
        result = delivery.finalize(self.repo, "loop/x", dry_run=True)
        self.assertIn("/compare/staging...loop/x", result["pr"])

    def test_the_roadmap_branch_is_protected_from_being_the_feature_branch(self):
        self.write_roadmap("**Branch base:** staging")
        result = delivery.finalize(self.repo, "staging", dry_run=True)
        self.assertTrue(result["push"].startswith("refused"), result)

    def test_finalize_project_passes_the_option_down(self):
        result = delivery.finalize_project(self.repo, {"branch": "loop/x"}, dry_run=True, base="release-train")
        self.assertIn("/compare/release-train...loop/x", result["pr"])


if __name__ == "__main__":
    unittest.main()
