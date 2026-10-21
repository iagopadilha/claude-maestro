"""BR-02: the uncommitted setup files (maps, CLAUDE.md with an app block, .specs plan files) are carried into a NEW worktree, never over a
file the branch has; other uncommitted files are not carried. Secrets (.env*) and the loop's runtime state are never setup files."""
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

APP_BLOCK = "<!-- maestro:context -->\nfacts\n<!-- /maestro:context -->\n"


class TestWorktreeCarriesSetupFiles(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="carry-setup-"))
        self.addCleanup(delete_tree, self.root)
        self.repo = os.path.join(self.root, "proj")
        write(os.path.join(self.repo, "README.md"), "# proj\n")
        write(os.path.join(self.repo, "src", "app.py"), "v1\n")
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "committed stack\n")
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "start")
        git(self.repo, "branch", "-M", "main")
        self.opts = types.SimpleNamespace(branch="loop/carry", dry_run=False, setup="")

    def create(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            work, meta = run_loop.ensure_worktree(self.repo, self.opts)
        return work, meta, output.getvalue()

    def test_setup_files_are_carried_and_other_changes_are_not(self):
        write(os.path.join(self.repo, "docs", "codebase", "ARCHITECTURE.md"), "new map\n")
        write(os.path.join(self.repo, "CLAUDE.md"), "# x\n" + APP_BLOCK)
        write(os.path.join(self.repo, ".specs", "features", "f1", "spec.md"), "spec\n")
        write(os.path.join(self.repo, ".specs", "project", "PROJECT.md"), "project\n")
        write(os.path.join(self.repo, "src", "app.py"), "v2 uncommitted\n")
        write(os.path.join(self.repo, "src", "new_untracked.py"), "x\n")
        work, meta, output = self.create()
        for path in ("docs/codebase/ARCHITECTURE.md", "CLAUDE.md", ".specs/features/f1/spec.md", ".specs/project/PROJECT.md"):
            self.assertTrue(os.path.isfile(os.path.join(work, *path.split("/"))), path)
        with open(os.path.join(work, "src", "app.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "v1\n")
        self.assertFalse(os.path.exists(os.path.join(work, "src", "new_untracked.py")))
        self.assertIn("1 other changed file in your folder NOT carried: src/app.py", output)
        self.assertIn("docs/codebase/ARCHITECTURE.md", meta["carried_setup_files"])

    def test_never_overwrites_a_file_the_branch_has(self):
        write(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "edited in the folder\n")
        work, meta, _ = self.create()
        with open(os.path.join(work, "docs", "codebase", "STACK.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "committed stack\n")
        self.assertNotIn("docs/codebase/STACK.md", meta["carried_setup_files"])

    def test_claude_md_without_an_app_block_secrets_and_loop_state_are_not_setup_files(self):
        write(os.path.join(self.repo, "CLAUDE.md"), "my own notes\n")
        write(os.path.join(self.repo, ".specs", "project", ".env.test"), "SECRET=1\n")
        write(os.path.join(self.repo, ".specs", "project", "loop", "json", "LOOP.json"), "{}")
        write(os.path.join(self.repo, ".specs", "project", "ROADMAP.md"), "# r\n")
        self.assertEqual([item["path"] for item in setup_files.list_setup_files(self.repo)], [".specs/project/ROADMAP.md"])

    def test_monorepo_subproject_files_are_setup_files(self):
        write(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"),
              json.dumps({"version": 1, "repos": {"proj": {"monorepo": True, "confirmed": True, "projects": [{"path": "backend"}]}}}))
        write(os.path.join(self.repo, "backend", "docs", "codebase", "STACK.md"), "map\n")
        write(os.path.join(self.repo, "backend", "CLAUDE.md"), "Conformance: STANDARD\n")
        work, _, _ = self.create()
        self.assertTrue(os.path.isfile(os.path.join(work, "backend", "docs", "codebase", "STACK.md")))
        self.assertTrue(os.path.isfile(os.path.join(work, "backend", "CLAUDE.md")))


if __name__ == "__main__":
    unittest.main()
