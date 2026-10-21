"""WorkspaceFileGuard `ignored_names`: build and dependency folders are ignored at ANY depth and never descended."""
import os
import shutil
import tempfile
import unittest
from unittest import mock

from harness_backend.infrastructure.workspace_file_guard import DEFAULT_IGNORED_NAMES, WorkspaceFileGuard

BUILD_FOLDER_NAMES = ("node_modules", "bin", "obj")


def write(path, content=b"x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


class IgnoredNamesTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="harness-guard-names-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(os.path.join(self.root, "frontend", "src", "app.js"), b"app")
        write(os.path.join(self.root, "frontend", "node_modules", "lib", "index.js"))
        write(os.path.join(self.root, "backend", "Api", "Program.cs"), b"program")
        write(os.path.join(self.root, "backend", "Api", "bin", "Debug", "Api.dll"))
        write(os.path.join(self.root, "backend", "Api", "obj", "project.assets.json"))

    def test_nested_build_folders_are_not_in_the_snapshot(self):
        snapshot = WorkspaceFileGuard().take_snapshot(self.root)
        self.assertEqual(sorted(snapshot.files), ["backend/Api/Program.cs", "frontend/src/app.js"])
        self.assertFalse([folder for folder in snapshot.folders if any(name in folder.split("/") for name in BUILD_FOLDER_NAMES)])

    def test_ignored_folders_are_never_descended(self):
        visited = []
        real_walk = os.walk

        def recording_walk(top, *args, **kwargs):
            for current, subfolders, files in real_walk(top, *args, **kwargs):
                visited.append(os.path.relpath(current, self.root).replace("\\", "/"))
                yield current, subfolders, files

        with mock.patch("harness_backend.infrastructure.workspace_file_guard.os.walk", recording_walk):
            WorkspaceFileGuard().take_snapshot(self.root)
        self.assertFalse([path for path in visited if any(name in path.split("/") for name in BUILD_FOLDER_NAMES)], visited)

    def test_changes_inside_ignored_names_are_not_reverted(self):
        guard = WorkspaceFileGuard()
        snapshot = guard.take_snapshot(self.root)
        write(os.path.join(self.root, "frontend", "node_modules", "lib", "index.js"), b"changed")
        write(os.path.join(self.root, "backend", "Api", "bin", "new.dll"))
        write(os.path.join(self.root, "frontend", "src", "evil.js"))
        report = guard.revert_unexpected_changes(snapshot, [])
        self.assertEqual(report["removed"], ["frontend/src/evil.js"])
        self.assertTrue(os.path.exists(os.path.join(self.root, "backend", "Api", "bin", "new.dll")))

    def test_default_list_and_custom_names(self):
        self.assertEqual(set(DEFAULT_IGNORED_NAMES),
                         {"node_modules", "bin", "obj", "dist", "build", ".git", ".vs", ".next", "coverage", "__pycache__"})
        snapshot = WorkspaceFileGuard().take_snapshot(self.root, (), ignored_names=("src",))
        self.assertIn("frontend/node_modules/lib/index.js", snapshot.files)
        self.assertNotIn("frontend/src/app.js", snapshot.files)


if __name__ == "__main__":
    unittest.main()
