"""WorkspaceFileGuard: snapshot before a job and the undo of every change outside the allowed files."""
import os
import shutil
import tempfile
import unittest

from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard


def write(path, content):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(content)


def read(path):
    with open(path, "rb") as handle:
        return handle.read()


class WorkspaceFileGuardTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="harness-guard-")
        self.addCleanup(shutil.rmtree, self.root, True)
        write(os.path.join(self.root, "README.md"), b"readme\n")
        write(os.path.join(self.root, "src", "app.py"), b"print(1)\n")
        write(os.path.join(self.root, "big.bin"), b"x" * 64)
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"), b"old roadmap\n")
        write(os.path.join(self.root, ".git", "HEAD"), b"ref: refs/heads/main\n")
        write(os.path.join(self.root, ".specs", "project", "loop", "internal", "jobs", "j.json"), b"{}")

    def test_reverts_creations_changes_and_deletions_outside_the_allowed_files(self):
        guard = WorkspaceFileGuard(backup_file_limit_bytes=32)
        snapshot = guard.take_snapshot(self.root, (".git", ".specs/project/loop/internal"))
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"), b"new roadmap\n")
        write(os.path.join(self.root, ".specs", "project", "FLOWS.md"), b"# Flows\n")
        write(os.path.join(self.root, "README.md"), b"hacked\n")
        os.remove(os.path.join(self.root, "src", "app.py"))
        write(os.path.join(self.root, "new", "deep", "evil.txt"), b"evil\n")
        write(os.path.join(self.root, "big.bin"), b"y" * 64)
        write(os.path.join(self.root, ".git", "HEAD"), b"changed by git\n")
        write(os.path.join(self.root, ".specs", "project", "loop", "internal", "jobs", "j.json"), b"{\"state\": \"ok\"}")

        report = guard.revert_unexpected_changes(snapshot, [".specs/project/ROADMAP.md", ".specs/project/FLOWS.md"])

        self.assertEqual(sorted(report["reverted"]), ["README.md", "src/app.py"])
        self.assertEqual(report["removed"], ["new/deep/evil.txt"])
        self.assertEqual(report["unrestorable"], ["big.bin"], "larger than the copy limit: only reported")
        self.assertEqual(read(os.path.join(self.root, "README.md")), b"readme\n")
        self.assertEqual(read(os.path.join(self.root, "src", "app.py")), b"print(1)\n")
        self.assertFalse(os.path.exists(os.path.join(self.root, "new")), "folders born for the removed file go too")
        self.assertEqual(read(os.path.join(self.root, ".specs", "project", "ROADMAP.md")), b"new roadmap\n")
        self.assertEqual(read(os.path.join(self.root, ".specs", "project", "FLOWS.md")), b"# Flows\n")
        self.assertEqual(read(os.path.join(self.root, ".git", "HEAD")), b"changed by git\n", "ignored paths are never touched")
        self.assertIn(b"ok", read(os.path.join(self.root, ".specs", "project", "loop", "internal", "jobs", "j.json")))

    def test_nothing_changed_reports_nothing(self):
        guard = WorkspaceFileGuard()
        snapshot = guard.take_snapshot(self.root, (".git",))
        os.utime(os.path.join(self.root, "README.md"))  # same content, new mtime: not a change
        self.assertEqual(guard.revert_unexpected_changes(snapshot, []), {"reverted": [], "removed": [], "unrestorable": []})


if __name__ == "__main__":
    unittest.main()
