"""App readers resolve a feature folder with the engine helper: a slug with ':' reads `.specs/features/<slug with ':' as '-'>/`."""
import os
import shutil
import tempfile
import unittest

from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.spec_file_service import SpecFileService

SLUG = "backend:version-endpoint"
FOLDER = "backend-version-endpoint"


class TestFeatureFolderReaders(unittest.TestCase):
    def test_specs_tree_attaches_the_folder_files_to_the_slug_and_not_to_an_extra_feature(self):
        files = {FOLDER: [{"path": "features/%s/spec.md" % FOLDER}], "orphan-folder": [{"path": "features/orphan-folder/spec.md"}]}
        features = SpecFileService.build_features({SLUG: {"status": "pending", "stage": "1"}}, files)
        self.assertEqual([(item["slug"], item["in_roadmap"], len(item["files"])) for item in features], [(SLUG, True, 1), ("orphan-folder", False, 1)])

    def test_spec_reader_opens_the_folder_spec(self):
        work = tempfile.mkdtemp(prefix="folder-readers-")
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(os.path.join(work, ".specs", "features", FOLDER))
        with open(os.path.join(work, ".specs", "features", FOLDER, "spec.md"), "w", encoding="utf-8") as handle:
            handle.write("# Version endpoint\n")
        repository = LoopFileRepository.__new__(LoopFileRepository)
        self.assertEqual(repository.read_feature_spec_head(work, SLUG, 3), ["# Version endpoint\n"])


if __name__ == "__main__":
    unittest.main()
