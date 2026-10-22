"""SubprojectsFileRepository on a temporary folder: byte-stable round trip, tolerant reads, suggested file, queue."""
import os
import shutil
import tempfile
import unittest

from harness_backend.repositories.subprojects_file_repository import SubprojectsFileError, SubprojectsFileRepository

CATALOG_ENTRY = {"monorepo": True, "confirmed": True, "validated_by": "ai",
                 "projects": [{"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev",
                               "skip": False, "reason": "açaí", "has_code": True}]}


class SubprojectsFileRepositoryCase(unittest.TestCase):
    def setUp(self):
        self.work_dir = tempfile.mkdtemp(prefix="subprojects-repo-")
        self.addCleanup(shutil.rmtree, self.work_dir, True)
        self.repository = SubprojectsFileRepository()

    def raw(self, path):
        with open(path, "rb") as handle:
            return handle.read()

    def test_absent_files_read_as_none(self):
        self.assertIsNone(self.repository.read_subprojects(self.work_dir))
        self.assertIsNone(self.repository.read_repo_entry(self.work_dir, "catalogo"))
        self.assertIsNone(self.repository.read_suggested(self.work_dir))
        self.assertIsNone(self.repository.read_map_queue(self.work_dir))

    def test_round_trip_is_byte_stable_utf8_lf_and_creates_folders(self):
        self.repository.write_repo_entry(self.work_dir, "catalogo", CATALOG_ENTRY)
        path = self.repository.subprojects_path(self.work_dir)
        first = self.raw(path)
        self.assertNotIn(b"\r", first)
        self.assertTrue(first.endswith(b"\n"))
        self.assertIn("açaí".encode("utf-8"), first)
        data = self.repository.read_subprojects(self.work_dir)
        self.assertEqual(data["version"], 1)
        self.assertEqual(self.repository.read_repo_entry(self.work_dir, "catalogo"), CATALOG_ENTRY)
        self.repository.write_subprojects(self.work_dir, data)
        self.assertEqual(self.raw(path), first)
        self.assertEqual([name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")], [])

    def test_write_repo_entry_keeps_the_other_repositories(self):
        self.repository.write_repo_entry(self.work_dir, "catalogo", CATALOG_ENTRY)
        self.repository.write_repo_entry(self.work_dir, "other", {"monorepo": False, "confirmed": True, "projects": []})
        self.repository.write_repo_entry(self.work_dir, "catalogo", {"monorepo": False, "confirmed": True, "projects": []})
        repos = self.repository.read_subprojects(self.work_dir)["repos"]
        self.assertEqual(sorted(repos), ["catalogo", "other"])
        self.assertFalse(repos["catalogo"]["monorepo"])
        self.assertIsNone(self.repository.read_repo_entry(self.work_dir, "missing"))

    def test_unreadable_files_return_an_error_object(self):
        path = self.repository.subprojects_path(self.work_dir)
        os.makedirs(os.path.dirname(path))
        for content in (b"{not json", b"\xff\xfe\x00bad", b"[1, 2]", b'{"version": 1}'):
            with open(path, "wb") as handle:
                handle.write(content)
            for result in (self.repository.read_subprojects(self.work_dir),
                           self.repository.read_repo_entry(self.work_dir, "catalogo")):
                self.assertIsInstance(result, SubprojectsFileError, content)
                self.assertTrue(result.error)
        os.remove(path)
        os.makedirs(path)  # a folder where the file should be: unreadable, still no exception
        self.assertIsInstance(self.repository.read_subprojects(self.work_dir), SubprojectsFileError)

    def test_write_over_an_unusable_file_starts_a_new_one(self):
        path = self.repository.subprojects_path(self.work_dir)
        os.makedirs(os.path.dirname(path))
        with open(path, "wb") as handle:
            handle.write(b"{broken")
        self.repository.write_repo_entry(self.work_dir, "catalogo", CATALOG_ENTRY)
        self.assertEqual(self.repository.read_repo_entry(self.work_dir, "catalogo"), CATALOG_ENTRY)

    def test_suggested_read_and_delete(self):
        path = self.repository.suggested_path(self.work_dir)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('{"projects": [], "notes": "x"}')
        self.assertEqual(self.repository.read_suggested(self.work_dir), {"projects": [], "notes": "x"})
        self.assertTrue(self.repository.delete_suggested(self.work_dir))
        self.assertFalse(os.path.exists(path))
        self.assertFalse(self.repository.delete_suggested(self.work_dir))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("nope")
        self.assertIsInstance(self.repository.read_suggested(self.work_dir), SubprojectsFileError)

    def test_map_queue_round_trip_creates_the_internal_folders(self):
        queue_state = {"queue": ["frontend"], "current": "backend", "done": [], "failed": {"x": "falhou"}}
        self.repository.write_map_queue(self.work_dir, queue_state)
        path = self.repository.map_queue_path(self.work_dir)
        self.assertTrue(path.endswith(os.path.join("loop", "internal", "subprojects", "map-queue.json")))
        first = self.raw(path)
        self.assertEqual(self.repository.read_map_queue(self.work_dir), queue_state)
        self.repository.write_map_queue(self.work_dir, self.repository.read_map_queue(self.work_dir))
        self.assertEqual(self.raw(path), first)
        with open(path, "wb") as handle:
            handle.write(b"{")
        self.assertIsInstance(self.repository.read_map_queue(self.work_dir), SubprojectsFileError)


if __name__ == "__main__":
    unittest.main()
