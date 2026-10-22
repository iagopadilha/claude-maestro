"""Setup files committed on the user's branch still reach the new worktree (BR-02 gap), the app-generated root files of a monorepo are setup
files, the readiness list has no duplicate warning and names a reference repo without a map."""
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
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402
import setup_files  # noqa: E402
from test_worktree_base_branch import delete_tree, git, write  # noqa: E402
from test_worktrees_by_role import WorldCase  # noqa: E402

RULES_IMPORT = "# proj\n\n@.specs/project/RULES.md\n"
NO_MAP_WARNING = "reference repo docs has no codebase map: agents will read the raw code; map it from Workspace"


class TestCarryCommittedSetupFiles(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="carry-committed-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "branch", "-M", "main")
        git(self.repo, "branch", "developer")
        git(self.repo, "checkout", "-q", "-b", "my-work")
        self.opts = types.SimpleNamespace(branch="loop/carry", dry_run=False, setup="")

    def create(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return run_loop.ensure_worktree(self.repo, self.opts)

    def test_setup_files_committed_on_the_current_branch_are_carried_and_recorded(self):
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "committed map\n")
        write(os.path.join(self.repo, ".specs", "project", "PROJECT.md"), "plan\n")
        git(self.repo, "add", "-f", "-A")
        git(self.repo, "commit", "-q", "-m", "setup")
        work, meta = self.create()
        for path in ("docs/codebase/STACK.md", ".specs/project/PROJECT.md"):
            self.assertTrue(os.path.isfile(os.path.join(work, *path.split("/"))), path)
        self.assertIn("docs/codebase/STACK.md", meta["carried_setup_files"])  # .specs is copied by the engine itself, not carried

    def test_a_file_the_worktree_branch_has_is_never_overwritten(self):
        git(self.repo, "checkout", "-q", "developer")
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "developer map\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "map on developer")
        git(self.repo, "checkout", "-q", "my-work")
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "my-work map\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "other map")
        work, meta = self.create()
        with open(os.path.join(work, "docs", "codebase", "STACK.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "developer map\n")
        self.assertNotIn("docs/codebase/STACK.md", meta["carried_setup_files"])


class TestMonorepoRootFiles(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="mono-root-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "mono")
        write(os.path.join(self.repo, "backend", "app.py"), "x\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        write(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"),
              json.dumps({"version": 1, "repos": {"mono": {"monorepo": True, "confirmed": True, "projects": [{"path": "backend"}]}}}))
        write(os.path.join(self.repo, "CLAUDE.md"), RULES_IMPORT)
        write(os.path.join(self.repo, "docs", "codebase", "README.md"), "index\n")
        write(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md"), "system\n")

    def test_root_claude_md_with_the_rules_import_and_root_maps_are_listed(self):
        paths = [item["path"] for item in setup_files.list_setup_files(self.repo)]
        for path in ("CLAUDE.md", "docs/codebase/README.md", "docs/codebase/SYSTEM.md"):
            self.assertIn(path, paths)

    def test_root_claude_md_the_user_wrote_is_not_a_setup_file(self):
        write(os.path.join(self.repo, "CLAUDE.md"), "my own notes\n")
        self.assertNotIn("CLAUDE.md", [item["path"] for item in setup_files.list_setup_files(self.repo)])

    def test_they_are_carried_into_the_worktree(self):
        opts = types.SimpleNamespace(branch="loop/mono", dry_run=False, setup="")
        with contextlib.redirect_stdout(io.StringIO()):
            work, meta = run_loop.ensure_worktree(self.repo, opts)
        for path in ("CLAUDE.md", "docs/codebase/README.md", "docs/codebase/SYSTEM.md"):
            self.assertTrue(os.path.isfile(os.path.join(work, *path.split("/"))), path)  # CLAUDE.md is also copied by the engine itself
        self.assertTrue({"docs/codebase/README.md", "docs/codebase/SYSTEM.md"} <= set(meta["carried_setup_files"]))


class FlagsOff(types.SimpleNamespace):
    """Options of the loop with every flag the test does not set switched off."""

    def __getattr__(self, name):
        return False


class TestReadinessWarnings(WorldCase):
    def preflight_opts(self):
        return FlagsOff(claude="claude", planning=True, push=False, e2e=False, setup="", branch="", dry_run=False, only_repos=None,
                        repo_path=self.repos["app"], max_cost=50, orchestrator_model="opus", permission_mode="acceptEdits")

    def test_a_warning_reported_by_two_checks_is_listed_once_in_order(self):
        text = "sample data no .env defines: $SAMPLE_ID"
        for name, value in (("test_environment_warnings", lambda root, opts: [text, "other"]),
                            ("planning_readiness", lambda *args: ([], [text, "last"]))):
            original = getattr(run_loop, name)
            self.addCleanup(setattr, run_loop, name, original)
            setattr(run_loop, name, value)
        self.configure({}, "src")
        _, warns = run_loop.preflight(self.repos["app"], self.preflight_opts())
        self.assertEqual(warns.count(text), 1)
        self.assertLess(warns.index(text), warns.index("other"))
        self.assertLess(warns.index("other"), warns.index("last"))

    def test_a_reference_repo_without_a_map_gets_its_own_warning_and_a_mapped_one_does_not(self):
        self.configure({"docs": "reference", "idle": "reference"}, "src")
        write(os.path.join(self.repos["idle"], "docs", "codebase", "STACK.md"), "map\n")
        repos = run_loop.project_repos.load_repos(self.repos["app"])
        problems, warns = run_loop.extra_repos_preflight(repos, self.opts(push=False), run_loop.roadmap_features(self.repos["app"]))
        self.assertEqual(problems, [])
        self.assertEqual(warns, [NO_MAP_WARNING])


if __name__ == "__main__":
    unittest.main()
