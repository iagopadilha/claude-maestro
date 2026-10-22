"""SubprojectsService on a scratch monorepo: detection stored unconfirmed, list edition, Conformance in each subproject's own CLAUDE.md,
root CLAUDE.md untouched, leftovers reported, bad files tolerated. No HTTP, no claude."""
import json
import os

from harness_backend.domain.project_errors import InvalidPathError, InvalidRequestError, LoopRunningError, RepoConflictError
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subprojects_service import SubprojectsNotConfirmedError, SubprojectsService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, write_file

TEAM_TEXT = "# Frontend\n\nTeam notes: keep the hooks small.\n"
ROOT_TEXT = "# catalogo\n\nRoot text.\n"
MONOREPO_FILES = {
    "CLAUDE.md": ROOT_TEXT,
    "backend/App.sln": "",
    "backend/Api/Program.cs": "class P {}\n",
    "backend/Data/Repo.cs": "class R {}\n",
    "backend/Domain/Model.cs": "class M {}\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}),
    "frontend/src/main.tsx": "export {}\n",
    "frontend/CLAUDE.md": TEAM_TEXT,
}


class TestSubprojectsService(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.repo = self.make_repo("catalogo", MONOREPO_FILES)
        os.makedirs(os.path.join(self.repo, ".specs", "project"), exist_ok=True)  # the structure step ran: a read may store the list
        self.context = self.make_context(self.repo)
        self.repository = SubprojectsFileRepository()
        structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)
        self.service = SubprojectsService(self.repository, structure, self.registry, self.loop_status_service, self.log)
        self.root_claude = os.path.join(self.repo, "CLAUDE.md")

    def confirm(self):
        return self.service.update_list(self.context, {"confirmed": True})

    @staticmethod
    def read_bytes(path):
        with open(path, "rb") as handle:
            return handle.read()

    @staticmethod
    def rows(description):
        return {row["path"]: row for row in description["projects"]}

    def test_describe_detects_two_and_stores_unconfirmed(self):
        description = self.service.describe(self.context)
        self.assertTrue(description["monorepo"])
        self.assertFalse(description["confirmed"])
        self.assertEqual(description["validated_by"], "script")
        rows = self.rows(description)
        self.assertEqual(sorted(rows), ["backend", "frontend"])
        self.assertEqual((rows["backend"]["stack"], rows["backend"]["skill"]), ("dotnet", "backend-dev"))
        self.assertEqual(rows["frontend"]["conformance"], None)
        self.assertEqual(rows["frontend"]["missing"], ["conformance", "map"])
        self.assertFalse(rows["frontend"]["ready"])
        stored = self.repository.read_repo_entry(self.repo, "catalogo")
        self.assertIs(stored["confirmed"], False)
        self.assertIsNone(description["job"])
        self.assertIsNone(description["queue"])

    def test_single_project_is_stored_as_not_monorepo(self):
        single = self.make_repo("single", {"app.sln": "", "src/a.cs": "x\n", "tests/t.csproj": ""})
        os.makedirs(os.path.join(single, ".specs", "project"), exist_ok=True)
        description = self.service.describe(self.make_context(single))
        self.assertFalse(description["monorepo"])
        self.assertEqual(description["projects"], [])
        self.assertIs(self.repository.read_repo_entry(single, "single")["monorepo"], False)

    def test_conformance_refused_before_confirmation(self):
        self.service.describe(self.context)
        with self.assertRaises(SubprojectsNotConfirmedError) as raised:
            self.service.set_conformance(self.context, "backend", "STANDARD")
        self.assertIn("confirm the list of subprojects first", str(raised.exception))
        self.assertIsInstance(raised.exception, InvalidRequestError)
        with self.assertRaises(SubprojectsNotConfirmedError):
            self.service.set_conformance_all(self.context)
        self.assertFalse(os.path.exists(os.path.join(self.repo, "backend", "CLAUDE.md")))

    def test_conformance_in_each_subproject_and_root_untouched(self):
        root_bytes = self.read_bytes(self.root_claude)
        self.service.describe(self.context)
        self.confirm()
        self.assertTrue(self.service.set_conformance(self.context, "backend", "STANDARD")["written"])
        self.service.set_conformance(self.context, "frontend", "LEGACY")
        backend_text = read_file(os.path.join(self.repo, "backend", "CLAUDE.md"))
        self.assertTrue(backend_text.startswith("# backend\n"))
        self.assertIn("Conformance: STANDARD", backend_text)
        frontend_text = read_file(os.path.join(self.repo, "frontend", "CLAUDE.md"))
        self.assertTrue(frontend_text.startswith(TEAM_TEXT))
        self.assertIn("Conformance: LEGACY", frontend_text)
        self.assertEqual(self.read_bytes(self.root_claude), root_bytes)
        rows = self.rows(self.service.describe(self.context))
        self.assertEqual((rows["backend"]["conformance"], rows["frontend"]["conformance"]), ("STANDARD", "LEGACY"))
        self.assertEqual(rows["backend"]["missing"], ["map"])

    def test_existing_conformance_counts_as_set_and_is_not_rewritten(self):
        write_file(os.path.join(self.repo, "frontend", "CLAUDE.md"), TEAM_TEXT + "\nConformance: LEGACY\n")
        self.service.describe(self.context)
        self.confirm()
        before = read_file(os.path.join(self.repo, "frontend", "CLAUDE.md"))
        result = self.service.set_conformance(self.context, "frontend", "STANDARD")
        self.assertEqual((result["written"], result["conformance"]), (False, "LEGACY"))
        self.assertEqual(read_file(os.path.join(self.repo, "frontend", "CLAUDE.md")), before)

    def test_set_conformance_all_uses_each_own_suggestion(self):
        self.service.describe(self.context)
        self.confirm()
        result = self.service.set_conformance_all(self.context)
        values = {item["path"]: item["value"] for item in result["written"]}
        self.assertEqual(values["backend"], "STANDARD")  # Api/Data/Domain folders
        self.assertEqual(values["frontend"], "LEGACY")  # no pages/components
        again = self.service.set_conformance_all(self.context)
        self.assertEqual(again["written"], [])
        self.assertEqual(len(again["kept"]), 2)

    def test_skipped_and_unknown_paths_refused(self):
        self.service.describe(self.context)
        self.service.update_list(self.context, {"projects": [{"path": "backend"}, {"path": "frontend", "skip": True}], "confirmed": True})
        with self.assertRaises(InvalidRequestError):
            self.service.set_conformance(self.context, "frontend", "LEGACY")
        with self.assertRaises(InvalidRequestError):
            self.service.set_conformance(self.context, "nothing", "LEGACY")
        with self.assertRaises(InvalidPathError):  # the existing value check of the structure service (400)
            self.service.set_conformance(self.context, "backend", "MAYBE")
        rows = self.rows(self.service.describe(self.context))
        self.assertTrue(rows["frontend"]["ready"])
        self.assertEqual(rows["frontend"]["missing"], [])

    def test_root_conformance_refused_for_confirmed_monorepo(self):
        structure = self.service.specs_structure_service
        self.service.describe(self.context)
        structure.write_conformance(self.repo, "STANDARD")  # not confirmed yet: the old behavior
        write_file(self.root_claude, ROOT_TEXT)
        self.confirm()
        with self.assertRaises(RepoConflictError) as raised:
            structure.write_conformance(self.repo, "STANDARD")
        self.assertIn("monorepo: set the Conformance per subproject", str(raised.exception))
        structure.initialize_structure(self.repo, "LEGACY", "catalogo")
        self.assertNotIn("Conformance", read_file(self.root_claude))

    def test_leftover_root_conformance_is_reported_until_the_list_is_confirmed_then_removed(self):
        block = "<!-- CONFORMANCE:BEGIN -->\n## Conformance\nConformance: LEGACY\n<!-- CONFORMANCE:END -->\n"
        write_file(self.root_claude, ROOT_TEXT + "\n" + block)
        write_file(os.path.join(self.repo, "docs", "codebase", "STACK.md"), "# old map\n")
        description = self.service.describe(self.context)
        self.assertFalse(description["root_conformance_leftover"])  # only for a confirmed monorepo
        self.assertEqual(read_file(self.root_claude), ROOT_TEXT + "\n" + block)
        description = self.confirm()
        self.assertFalse(description["root_conformance_leftover"])  # the app removed its own block on confirm
        self.assertEqual(read_file(self.root_claude), ROOT_TEXT)  # the rest is byte-identical
        self.assertTrue(self.service.describe(self.context)["root_map_leftover"])  # the old root map is still only reported

    def test_confirm_removes_only_what_is_between_the_markers(self):
        block = "<!-- CONFORMANCE:BEGIN -->\n## Conformance\nConformance: LEGACY\n<!-- CONFORMANCE:END -->\n"
        before = "# shop\n\nConformance: STANDARD (written by hand)\n\n"
        after = "\n## Notes\nkeep me\nlast line without newline"
        write_file(self.root_claude, before + block + after)
        self.confirm()
        self.assertEqual(read_file(self.root_claude), before + after[1:])
        write_file(self.root_claude, before)  # no markers: nothing to remove, nothing touched
        self.service.update_list(self.context, {"confirmed": True})
        self.assertEqual(read_file(self.root_claude), before)

    def test_not_a_monorepo_is_stored_confirmed_and_never_overridden(self):
        self.service.describe(self.context)
        description = self.service.update_list(self.context, {"monorepo": False})
        self.assertEqual((description["monorepo"], description["confirmed"], description["projects"]), (False, True, []))
        self.assertFalse(self.service.describe(self.context)["monorepo"])
        with self.assertRaises(RepoConflictError):
            self.service.detect_again(self.context)
        self.assertTrue(self.service.detect_again(self.context, force=True)["monorepo"])

    def test_detect_again_refused_when_confirmed_without_force(self):
        self.service.describe(self.context)
        self.confirm()
        with self.assertRaises(RepoConflictError):
            self.service.detect_again(self.context)
        self.assertFalse(self.service.detect_again(self.context, force=True)["confirmed"])

    def test_edit_add_remove_and_validation(self):
        write_file(os.path.join(self.repo, "tools", "seed", "seed.py"), "print(1)\n")
        write_file(os.path.join(self.repo, "tools", "seed", ".git"), "gitdir: x\n")
        self.service.describe(self.context)
        result = self.service.update_list(self.context, {"projects": [
            {"path": "backend", "name": "api", "stack": "nestjs"}, {"path": "frontend\\", "skip": True}, {"path": "tools\\seed"}]})
        rows = self.rows(result)
        self.assertEqual(sorted(rows), ["backend", "frontend", "tools/seed"])
        self.assertEqual((rows["backend"]["name"], rows["backend"]["stack"], rows["backend"]["skill"]), ("api", "nestjs", "backend-dev"))
        self.assertTrue(rows["frontend"]["skip"])
        self.assertTrue(any("tools/seed" in warning and ".git" in warning for warning in result["warnings"]))
        self.assertEqual(result["validated_by"], "user")
        removed = self.service.update_list(self.context, {"projects": [{"path": "backend"}]})
        self.assertEqual([row["path"] for row in removed["projects"]], ["backend"])

    def test_invalid_paths_and_values_refused(self):
        self.service.describe(self.context)
        for bad in ("../outside", "backend/../..", "C:\\x", "/abs", "missing-folder", "", "."):
            with self.assertRaises(InvalidRequestError, msg=bad):
                self.service.update_list(self.context, {"projects": [{"path": bad}]})
        with self.assertRaises(InvalidRequestError):
            self.service.update_list(self.context, {"projects": [{"path": "backend"}, {"path": "backend"}]})
        with self.assertRaises(InvalidRequestError):
            self.service.update_list(self.context, {"projects": [{"path": "backend"}, {"path": "backend/Api"}]})
        with self.assertRaises(InvalidRequestError):
            self.service.update_list(self.context, {"projects": [{"path": "backend", "stack": "cobol"}]})
        with self.assertRaises(InvalidRequestError):
            self.service.update_list(self.context, {"projects": [{"path": "backend", "name": "x"}, {"path": "frontend", "name": "x"}]})
        self.assertEqual(sorted(self.rows(self.service.describe(self.context))), ["backend", "frontend"])  # nothing was stored

    def test_confirm_with_zero_active_subprojects_refused(self):
        self.service.describe(self.context)
        with self.assertRaises(InvalidRequestError):
            self.service.update_list(self.context, {"projects": [{"path": "backend", "skip": True}], "confirmed": True})
        self.assertFalse(self.service.describe(self.context)["confirmed"])

    def test_unreadable_subprojects_file_is_an_error_field_not_an_exception(self):
        path = os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json")
        write_file(path, "{not json")
        description = self.service.describe(self.context)
        self.assertTrue(description["error"])
        self.assertFalse(description["monorepo"])
        self.assertEqual(description["projects"], [])
        self.assertEqual(read_file(path), "{not json")  # a read never overwrites it

    def test_callbacks_run_and_a_failing_one_does_not_break(self):
        calls = []
        self.service.add_after_list_confirmed(lambda *arguments: calls.append(("list", arguments[1])))
        self.service.add_after_list_confirmed(lambda *arguments: 1 / 0)
        self.service.add_after_conformance_written(lambda *arguments: calls.append(("conformance", arguments[3], arguments[4])))
        self.service.describe(self.context)
        self.confirm()
        self.service.set_conformance(self.context, "backend", "LEGACY")
        self.assertEqual(calls, [("list", "catalogo"), ("conformance", "backend", "LEGACY")])

    def test_loop_running_refuses_writes(self):
        self.service.describe(self.context)
        self.write_live_loop_lock(self.repo)
        with self.assertRaises(LoopRunningError):
            self.service.update_list(self.context, {"confirmed": True})
