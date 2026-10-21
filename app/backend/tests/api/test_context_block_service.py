"""ContextBlockService on scratch repositories: which folders get the block (confirmed monorepo subprojects, single project root),
which never do (monorepo root, skipped subproject, unconfirmed list) and the triggers (Confirm list, Conformance, map jobs)."""
import json
import os

from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.context_block_service import ContextBlockService
from harness_backend.services.map_job_runner import MapJobRunner
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subprojects_service import SubprojectsService
from harness_backend.services.system_map_service import SystemMapService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import write_file

TEAM_TEXT = "# Frontend\n\nTeam notes: keep the hooks small.\n"
ROOT_TEXT = "# catalogo\n\nRoot text.\n"
START = "<!-- maestro:context -->"
MONOREPO_FILES = {
    "CLAUDE.md": ROOT_TEXT,
    "backend/App.sln": "",
    "backend/Api/Program.cs": "class P {}\n",
    "backend/Data/Repo.cs": "class R {}\n",
    "backend/Domain/Model.cs": "class M {}\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "18"}, "scripts": {"test": "vitest"}}),
    "frontend/src/main.tsx": "export {}\n",
    "frontend/CLAUDE.md": TEAM_TEXT,
}


def read_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


class PassGuard:
    def revert_unexpected_changes(self, snapshot, allowed_folders):
        return {"reverted": [], "removed": [], "unrestorable": []}


class TestContextBlockService(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("catalogo", MONOREPO_FILES)
        self.context = self.make_context(self.repo)
        self.repository = SubprojectsFileRepository()
        self.structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), ProcessRunner(), self.log)
        self.subprojects = SubprojectsService(self.repository, self.structure, self.registry, self.loop_status_service, self.log)
        self.service = ContextBlockService(log=self.log)
        self.subprojects.attach_context_block_service(self.service)
        self.structure.attach_context_block_service(self.service)

    def block_of(self, relative):
        path = os.path.join(self.repo, *relative.split("/"), "CLAUDE.md") if relative else os.path.join(self.repo, "CLAUDE.md")
        return read_text(path) if os.path.exists(path) else ""

    def confirm(self):
        return self.subprojects.update_list(self.context, {"confirmed": True})

    def test_confirm_list_writes_subprojects_and_not_the_root(self):
        self.subprojects.describe(self.context)
        self.assertEqual(self.block_of("frontend"), TEAM_TEXT)  # unconfirmed: nothing anywhere
        self.assertEqual(self.block_of("backend"), "")
        self.confirm()
        frontend = self.block_of("frontend")
        self.assertTrue(frontend.startswith(TEAM_TEXT))
        self.assertEqual(frontend.count(START), 1)
        self.assertIn("Test: `npm test`", frontend)
        self.assertEqual(self.block_of("backend").count(START), 1)
        self.assertTrue(self.block_of("backend").startswith("# backend\n"))
        self.assertEqual(self.block_of(""), ROOT_TEXT)

    def test_skipped_subproject_gets_nothing(self):
        self.subprojects.describe(self.context)
        self.subprojects.update_list(self.context, {"projects": [{"path": "backend"}, {"path": "frontend", "skip": True}]})
        self.confirm()
        self.assertEqual(self.block_of("frontend"), TEAM_TEXT)
        self.assertEqual(self.block_of("backend").count(START), 1)

    def test_conformance_write_refreshes_that_subproject(self):
        self.confirm()
        self.assertIn("Conformance is not set yet", self.block_of("frontend"))
        self.subprojects.set_conformance(self.context, "frontend", "LEGACY")
        frontend = self.block_of("frontend")
        self.assertEqual(frontend.count(START), 1)
        self.assertIn("This project's Conformance is LEGACY;", frontend)
        self.assertTrue(frontend.startswith(TEAM_TEXT))
        self.assertEqual(self.block_of(""), ROOT_TEXT)

    def test_system_map_pointer_when_the_root_has_it(self):
        self.confirm()
        write_file(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md"), "# System\n")
        written = self.service.refresh_repo(self.repo, "catalogo", subprojects_root=self.repo)
        self.assertCountEqual(written, ["backend", "frontend"])
        self.assertIn("`../docs/codebase/SYSTEM.md`", self.block_of("frontend"))
        self.assertEqual(self.service.refresh_repo(self.repo, "catalogo", subprojects_root=self.repo), [])  # idempotent
        self.assertEqual(self.service.system_pointer(self.repo, "backend/Api"), "../../docs/codebase/SYSTEM.md")

    def test_system_map_finish_refreshes_every_subproject(self):
        self.confirm()
        write_file(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md"), "# System\n")
        runner = MapJobRunner(None, None, PassGuard(), self.log)
        system = SystemMapService(runner, self.subprojects, None, None, self.log)
        system.attach_context_block_service(self.service)
        holder = {"snapshot": object(), "written_before": None}
        result = system.finish("p", self.repo, holder, (".specs", "docs/codebase/SYSTEM.md"), {"ok": True, "reply": ""},
                               context=self.context, repo_key="catalogo")
        self.assertTrue(result["ok"])
        self.assertIn("../docs/codebase/SYSTEM.md", self.block_of("frontend"))
        self.assertIn("../docs/codebase/SYSTEM.md", self.block_of("backend"))
        self.assertEqual(self.block_of(""), ROOT_TEXT)

    def test_single_project_root_gets_block_after_map_and_conformance(self):
        repo = self.make_repo("single", {"README.md": "x\n", "src/app.py": "print(1)\n", "CLAUDE.md": "# single\n\nTeam text.\n"})
        context = self.make_context(repo)
        write_file(os.path.join(repo, "docs", "codebase", "STACK.md"), "# Stack\n")
        runner = MapJobRunner(None, None, PassGuard(), self.log)
        runner.attach_context_block_service(self.service)
        result = runner.finish("p", repo, repo, os.path.join(repo, "docs", "codebase"), {"snapshot": object()}, (".specs", "docs/codebase"),
                               {"ok": True})
        self.assertTrue(result["ok"])
        text = read_text(os.path.join(repo, "CLAUDE.md"))
        self.assertTrue(text.startswith("# single\n\nTeam text.\n"))
        self.assertEqual(text.count(START), 1)
        self.assertIn("`docs/codebase/STACK.md`", text)
        self.structure.write_project_conformance(context, "STANDARD")
        text = read_text(os.path.join(repo, "CLAUDE.md"))
        self.assertEqual(text.count(START), 1)
        self.assertIn("Conformance is STANDARD;", text)
        self.assertIn("Conformance: STANDARD", text)  # the real block of classify_structure is still there
        self.assertEqual(self.service.refresh_repo(repo, "single", subprojects_root=repo), [])  # idempotent

    def test_monorepo_root_never_gets_a_block_and_damaged_file_is_left_alone(self):
        self.confirm()
        self.assertFalse(self.service.refresh_folder(self.repo, "catalogo", "", subprojects_root=self.repo))
        damaged = TEAM_TEXT + START + "\nhalf\n"
        write_file(os.path.join(self.repo, "frontend", "CLAUDE.md"), damaged)
        self.assertEqual(self.service.refresh_repo(self.repo, "catalogo", subprojects_root=self.repo), ["backend"][:0])
        self.assertEqual(self.block_of("frontend"), damaged)

    def test_never_raises(self):
        self.assertEqual(self.service.refresh_repo(os.path.join(self.temporary_dir, "nowhere"), "x"), [])
        self.assertFalse(self.service.refresh_folder(None, "x", "a"))
