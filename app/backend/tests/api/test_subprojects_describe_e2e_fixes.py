"""Fixes found in the E2E of the monorepo feature: a read never creates `.specs/` (the structure step does), and the description
carries what the AI validation changed (`differences`, `validation_error`) so the table can show it."""
import os
import shutil

from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subprojects_service import SubprojectsService

from .support_multi_repo import MultiRepoTestCase
from .test_subprojects_service import MONOREPO_FILES


class TestDescribeFixes(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("catalogo", MONOREPO_FILES)
        self.context = self.make_context(self.repo)
        self.repository = SubprojectsFileRepository()
        structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)
        self.service = SubprojectsService(self.repository, structure, self.registry, self.loop_status_service, self.log)

    def test_describe_without_specs_detects_but_writes_nothing(self):
        shutil.rmtree(os.path.join(self.context.work_dir, ".specs"), ignore_errors=True)
        description = self.service.describe(self.context)
        self.assertTrue(description["monorepo"])
        self.assertFalse(os.path.exists(os.path.join(self.context.work_dir, ".specs")))

    def test_describe_with_specs_stores_the_detection(self):
        os.makedirs(os.path.join(self.context.work_dir, ".specs", "project"), exist_ok=True)
        self.service.describe(self.context)
        self.assertIsInstance(self.repository.read_repo_entry(self.context.work_dir, "catalogo"), dict)

    def test_description_carries_ai_differences_and_validation_error(self):
        os.makedirs(os.path.join(self.context.work_dir, ".specs", "project"), exist_ok=True)
        entry = self.service.describe(self.context) and self.repository.read_repo_entry(self.context.work_dir, "catalogo")
        entry.update(differences=[{"path": "frontend", "change": "renamed: frontend -> web"}], validation_error="unreadable suggestion")
        self.repository.write_repo_entry(self.context.work_dir, "catalogo", entry)
        description = self.service.describe(self.context)
        self.assertEqual(description["differences"], [{"path": "frontend", "change": "renamed: frontend -> web"}])
        self.assertEqual(description["validation_error"], "unreadable suggestion")
