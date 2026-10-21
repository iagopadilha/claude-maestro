"""RepoReadinessService with a secondary repo that is a monorepo (keyed entry in the primary's SUBPROJECTS.json): per-subproject lines,
unconfirmed list, root Conformance refused, and the maestro:context block of a non-monorepo secondary edit repo."""
import json
import os

from harness_backend.domain.project_errors import RepoConflictError
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.context_block_service import ContextBlockService
from harness_backend.services.repo_readiness_service import RepoReadinessService
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subprojects_service import SubprojectsService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, write_file
from .test_repo_readiness import StubJobs, StubRoadmapService

START = "<!-- maestro:context -->"
MONOREPO_FILES = {
    "backend/App.sln": "",
    "backend/Api/Program.cs": "class P {}\n",
    "backend/Data/Repo.cs": "class R {}\n",
    "backend/Domain/Model.cs": "class M {}\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}),
    "frontend/src/main.tsx": "export {}\n",
}


class TestRepoReadinessMonorepo(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.shop = self.make_repo("shop", MONOREPO_FILES)
        self.api = self.make_repo("api", {"service.py": "print('x')\n"})
        self.context = self.make_context(self.primary)
        self.registry.add_repo(self.context, self.shop, [])
        self.registry.add_repo(self.context, self.api, [])
        structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)
        self.repository = SubprojectsFileRepository()
        self.subprojects = SubprojectsService(self.repository, structure, None, self.loop_status_service, self.log)
        self.subprojects.attach_repo_registry_service(self.registry)
        self.readiness = RepoReadinessService(self.registry, structure, StubRoadmapService(), StubJobs(), self.loop_status_service, self.log)
        self.readiness.attach_subprojects_service(self.subprojects)
        self.shop_entry = self.subprojects.describe(self.context, "shop")  # detects and stores the secondary's entry

    def confirm_shop(self):
        self.subprojects.update_list(self.context, {"confirmed": True}, "shop")

    def unready(self):
        return {item["detail"]: item for item in self.readiness.list_unready(self.context)}

    def test_secondary_entry_is_keyed_and_its_rows_resolve_in_that_repo(self):
        self.assertTrue(self.shop_entry["monorepo"])
        self.assertEqual(self.shop_entry["repo"], "shop")
        self.assertEqual(sorted(row["path"] for row in self.shop_entry["projects"]), ["backend", "frontend"])
        self.assertTrue(all(row["exists"] for row in self.shop_entry["projects"]))
        self.assertIsNone(self.repository.read_repo_entry(self.primary, "app"))
        self.assertTrue(os.path.isfile(os.path.join(self.primary, ".specs", "project", "SUBPROJECTS.json")))

    def test_unconfirmed_monorepo_secondary_is_one_line(self):
        details = self.unready()
        self.assertIn("shop: confirm the list of subprojects first", details)
        self.assertFalse([detail for detail in details if detail.startswith("shop/")])

    def test_confirmed_monorepo_secondary_reports_each_missing_subproject(self):
        self.confirm_shop()
        write_file(os.path.join(self.shop, "backend", "CLAUDE.md"), "# backend\n\nConformance: STANDARD\n")
        details = self.unready()
        self.assertEqual(details["shop/backend: no codebase map"]["missing"], ["map"])
        self.assertEqual(details["shop/frontend: no Conformance, no codebase map"]["missing"], ["conformance", "map"])
        self.assertNotIn("shop: confirm the list of subprojects first", details)
        write_file(os.path.join(self.shop, "backend", "docs", "codebase", "STACK.md"), "# stack\n")
        self.assertNotIn("shop/backend: no codebase map", self.unready())

    def test_non_monorepo_secondary_is_unchanged(self):
        self.assertEqual(self.unready()["api: no Conformance line (STANDARD or LEGACY), no codebase map"]["missing"], ["conformance", "map"])

    def test_reference_monorepo_is_skipped(self):
        self.registry.set_role(self.context, "shop", "reference")
        self.assertNotIn("shop: confirm the list of subprojects first", self.unready())
        self.confirm_shop()
        self.assertFalse([detail for detail in self.unready() if detail.startswith("shop")])

    def test_root_conformance_refused_for_a_confirmed_monorepo_secondary_only(self):
        self.readiness.set_conformance(self.context, "shop", "STANDARD")  # unconfirmed list: the old route still writes
        self.confirm_shop()
        with self.assertRaises(RepoConflictError) as refusal:
            self.readiness.set_conformance(self.context, "shop", "LEGACY")
        self.assertEqual(refusal.exception.code, "monorepo_root_conformance")
        self.assertIn("monorepo", str(refusal.exception))
        self.assertEqual(self.readiness.set_conformance(self.context, "api", "LEGACY")["conformance"], "LEGACY")

    def test_non_monorepo_secondary_edit_repo_gets_the_context_block_after_conformance(self):
        self.readiness.attach_context_block_service(ContextBlockService(log=self.log))
        self.readiness.set_conformance(self.context, "api", "LEGACY")
        text = read_file(os.path.join(self.api, "CLAUDE.md"))
        self.assertEqual(text.count(START), 1)
        self.assertIn("Conformance: LEGACY", text)
        self.registry.set_role(self.context, "api", "reference")
        os.remove(os.path.join(self.api, "CLAUDE.md"))
        self.readiness.set_conformance(self.context, "api", "STANDARD")
        self.assertNotIn(START, read_file(os.path.join(self.api, "CLAUDE.md")))  # a reference repo is not touched
