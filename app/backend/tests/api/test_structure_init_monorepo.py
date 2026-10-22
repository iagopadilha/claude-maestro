"""The structure step of a monorepo (detected, list not confirmed yet) never writes a Conformance block at the root; a single
project and a repository marked "Not a monorepo" keep writing it as before."""
import json
import os

from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.specs_structure_service import SpecsStructureService

from .support_multi_repo import MultiRepoTestCase
from .test_subprojects_service import MONOREPO_FILES


class TestStructureInitMonorepo(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)

    def test_detected_monorepo_root_gets_no_conformance(self):
        repo = self.make_repo("catalogo", MONOREPO_FILES)
        self.structure.initialize_structure(repo, "STANDARD", "catalogo")
        with open(os.path.join(repo, "CLAUDE.md"), encoding="utf-8") as handle:
            self.assertNotIn("Conformance:", handle.read())

    def test_not_a_monorepo_choice_writes_the_root_conformance(self):
        repo = self.make_repo("catalogo", MONOREPO_FILES)
        path = os.path.join(repo, ".specs", "project", "SUBPROJECTS.json")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "repos": {"catalogo": {"monorepo": False, "confirmed": True, "projects": []}}}, handle)
        self.structure.initialize_structure(repo, "LEGACY", "catalogo")
        with open(os.path.join(repo, "CLAUDE.md"), encoding="utf-8") as handle:
            self.assertIn("Conformance: LEGACY", handle.read())

    def test_single_project_writes_the_root_conformance(self):
        repo = self.make_repo("single", {"app.sln": "", "src/a.cs": "x\n"})
        self.structure.initialize_structure(repo, "STANDARD", "single")
        with open(os.path.join(repo, "CLAUDE.md"), encoding="utf-8") as handle:
            self.assertIn("Conformance: STANDARD", handle.read())


class TestStructureInitMonorepoRunBlock(TestStructureInitMonorepo):
    def test_detected_monorepo_root_gets_no_run_block(self):
        repo = self.make_repo("catalogo", MONOREPO_FILES)
        self.structure.initialize_structure(repo, None, "catalogo")
        with open(os.path.join(repo, "CLAUDE.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertNotIn("How to run", text)
        self.assertIn("@.specs/project/RULES.md", text)
