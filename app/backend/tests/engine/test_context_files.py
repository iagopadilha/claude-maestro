"""Uncommitted agent-context files (engine/context_files.py): scratch git repos only, never a real repository."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import context_files  # noqa: E402


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def git(folder, *args):
    return subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True)


MONOREPO_ENTRY = {"monorepo": True, "confirmed": True, "projects": [
    {"path": "backend", "skip": False}, {"path": "frontend", "skip": False}, {"path": "tools", "skip": True}]}


class ContextFilesTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="context-files-")
        self.addCleanup(shutil.rmtree, self.root, True)
        git(self.root, "init", "-q")
        git(self.root, "config", "user.email", "t@t")
        git(self.root, "config", "user.name", "t")
        write(os.path.join(self.root, "frontend", "CLAUDE.md"), "old\n")
        write(os.path.join(self.root, "README.md"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "init")

    def paths(self, items):
        return [item["path"] for item in items]


class TestMonorepo(ContextFilesTestCase):
    def test_lists_untracked_map_and_modified_claude_only_in_scope(self):
        write(os.path.join(self.root, "backend", "docs", "codebase", "STACK.md"))
        write(os.path.join(self.root, "frontend", "CLAUDE.md"), "new\n")
        write(os.path.join(self.root, "tools", "CLAUDE.md"))  # skipped subproject
        write(os.path.join(self.root, "CLAUDE.md"))  # root CLAUDE.md is not listed for a monorepo
        write(os.path.join(self.root, "docs", "codebase", "SYSTEM.md"))
        write(os.path.join(self.root, "docs", "codebase", "STACK.md"))  # root map is not a monorepo file
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"))
        items = context_files.uncommitted_context(self.root, MONOREPO_ENTRY)
        self.assertEqual(items, [
            {"path": "backend/docs/codebase/STACK.md", "kind": "map", "state": "untracked"},
            {"path": "docs/codebase/SYSTEM.md", "kind": "system", "state": "untracked"},
            {"path": "frontend/CLAUDE.md", "kind": "claude", "state": "modified"}])

    def test_huge_untracked_folder_is_not_listed(self):
        for number in range(50):
            write(os.path.join(self.root, "frontend", "node_modules", "pkg%d" % number, "docs", "codebase", "A.md"))
        write(os.path.join(self.root, "frontend", "docs", "codebase", "MAP.md"))
        self.assertEqual(self.paths(context_files.uncommitted_context(self.root, MONOREPO_ENTRY)), ["frontend/docs/codebase/MAP.md"])

    def test_entry_is_read_from_the_stored_file_when_not_given(self):
        write(os.path.join(self.root, ".specs", "project", "SUBPROJECTS.json"),
              json.dumps({"version": 1, "repos": {os.path.basename(self.root): MONOREPO_ENTRY}}))
        write(os.path.join(self.root, "backend", "CLAUDE.md"))
        self.assertEqual(self.paths(context_files.uncommitted_context(self.root)), ["backend/CLAUDE.md"])

    def test_only_one_git_call_and_nothing_staged(self):
        write(os.path.join(self.root, "backend", "CLAUDE.md"))
        real_run = subprocess.run
        with mock.patch.object(context_files.subprocess, "run", side_effect=real_run) as spy:
            context_files.uncommitted_context(self.root, MONOREPO_ENTRY)
        self.assertEqual(spy.call_count, 1)
        self.assertEqual(spy.call_args[0][0][3], "status")
        self.assertEqual(git(self.root, "diff", "--cached", "--name-only").stdout.strip(), "")


class TestSingleProject(ContextFilesTestCase):
    def test_root_map_and_claude_are_listed(self):
        write(os.path.join(self.root, "docs", "codebase", "STACK.md"))
        write(os.path.join(self.root, "CLAUDE.md"))
        write(os.path.join(self.root, "docs", "codebase", "SYSTEM.md"))  # plain map file for a single project
        write(os.path.join(self.root, "backend", "CLAUDE.md"))  # not at the root
        items = context_files.uncommitted_context(self.root)
        self.assertEqual([(item["path"], item["kind"]) for item in items], [
            ("CLAUDE.md", "claude"), ("docs/codebase/STACK.md", "map"), ("docs/codebase/SYSTEM.md", "map")])

    def test_committed_files_are_not_listed(self):
        write(os.path.join(self.root, "CLAUDE.md"))
        git(self.root, "add", "-A")
        git(self.root, "commit", "-q", "-m", "claude")
        self.assertEqual(context_files.uncommitted_context(self.root), [])


class TestFailure(ContextFilesTestCase):
    def test_git_missing_gives_empty_list(self):
        write(os.path.join(self.root, "CLAUDE.md"))
        with mock.patch.object(context_files, "GIT_BINARY", "git-that-does-not-exist-xyz"):
            self.assertEqual(context_files.uncommitted_context(self.root), [])

    def test_not_a_repository_gives_empty_list(self):
        folder = tempfile.mkdtemp(prefix="context-files-plain-")
        self.addCleanup(shutil.rmtree, folder, True)
        write(os.path.join(folder, "CLAUDE.md"))
        self.assertEqual(context_files.uncommitted_context(folder), [])


class TestTexts(unittest.TestCase):
    def items(self, count):
        return [{"path": "m/docs/codebase/F%02d.md" % number, "kind": "map", "state": "untracked"} for number in range(count)]

    def test_warning_text_is_empty_without_items(self):
        self.assertEqual(context_files.uncommitted_warning_text([]), "")

    def test_warning_text_lists_up_to_twelve_then_counts_the_rest(self):
        text = context_files.uncommitted_warning_text(self.items(15))
        self.assertTrue(text.startswith(
            "These files are not committed. The loop worktree is created when the loop starts, from the last commit, "
            "so the agents will not see them. Commit them first. "))
        self.assertIn("F11.md", text)
        self.assertNotIn("F12.md", text)
        self.assertTrue(text.endswith(" and 3 more"))
        self.assertNotIn("more", context_files.uncommitted_warning_text(self.items(12)))

    def test_chip_summary(self):
        items = self.items(3) + [{"path": "backend/CLAUDE.md", "kind": "claude", "state": "untracked"},
                                 {"path": "docs/codebase/SYSTEM.md", "kind": "system", "state": "untracked"}]
        self.assertEqual(context_files.uncommitted_chip_summary(items), "maps: 3 files, CLAUDE.md: backend, SYSTEM.md")
        self.assertEqual(context_files.uncommitted_chip_summary(self.items(1)), "maps: 1 file")
        self.assertEqual(context_files.uncommitted_chip_summary([{"path": "CLAUDE.md", "kind": "claude", "state": "modified"}]), "CLAUDE.md")


if __name__ == "__main__":
    unittest.main()
