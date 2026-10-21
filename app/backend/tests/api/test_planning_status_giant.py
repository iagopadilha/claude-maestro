"""Planning status block: giant files per subproject and the SYSTEM.md state (GIANT-02, SYS-06); the root map facts of a confirmed
monorepo never count SYSTEM.md as the project map."""
import json
import os
import tempfile
import unittest

from harness_backend.services.planning_gates import describe_gates_for_ai
from harness_backend.services.project_progress_service import ProjectProgressService

from .test_projects_monorepo_progress import MONOREPO_FILES, SINGLE_FILES
from .support_projects import ProjectsTestCase, create_git_repo, write_file

INVENTORY_PARTS = (".specs", "project", "loop", "internal", "inventory")


def described_monorepo():
    return {"monorepo": True, "confirmed": True, "projects": [
        {"path": "backend", "stack": "dotnet", "conformance": "STANDARD", "has_code": True, "map": {"files": ["STACK.md"]},
         "giant_files": 7, "giant": [{"path": "backend/Legacy.cs", "lines": 20000}, {"path": "backend/Big.cs", "lines": 3000}]},
        {"path": "frontend", "stack": "react", "conformance": "STANDARD", "has_code": True, "map": {"files": ["STACK.md"]}, "giant_files": 0, "giant": []}]}


class TestPlanningStatusGiant(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.work_dir = self.directory.name

    def test_status_lists_giant_files_per_subproject_and_the_system_state(self):
        text = describe_gates_for_ai([], work_dir=self.work_dir, subprojects=described_monorepo())
        self.assertIn("giant files: 7 - backend/Legacy.cs (20000 lines), backend/Big.cs (3000 lines)", text)
        self.assertIn("docs/codebase/SYSTEM.md (system map): missing", text)
        self.assertIn("refactor-*", text)
        self.assertIn("note=giant-accepted", text)
        write_file(os.path.join(self.work_dir, "docs", "codebase", "SYSTEM.md"), "# System\n")
        self.assertIn("docs/codebase/SYSTEM.md (system map): exists",
                      describe_gates_for_ai([], work_dir=self.work_dir, subprojects=described_monorepo()))

    def test_single_project_without_inventory_shows_nothing_new(self):
        text = describe_gates_for_ai([], work_dir=self.work_dir, subprojects=None)
        self.assertNotIn("Giant", text)
        self.assertNotIn("SYSTEM", text)
        self.assertNotIn("Subprojects", text)

    def test_single_project_with_root_inventory_lists_giants_but_no_monorepo_lines(self):
        write_file(os.path.join(self.work_dir, *INVENTORY_PARTS, "_root.json"), json.dumps({"giant": [{"path": "src/big.py", "lines": 4000}]}))
        text = describe_gates_for_ai([], work_dir=self.work_dir, subprojects=None)
        self.assertIn("1 giant file(s): src/big.py (4000 lines)", text)
        self.assertIn("note=giant-accepted", text)
        self.assertNotIn("SYSTEM", text)
        self.assertNotIn("Subprojects", text)


class TestRootMapFactsOfMonorepo(ProjectsTestCase):
    def test_confirmed_monorepo_with_only_system_md_has_no_root_map(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "mono"), MONOREPO_FILES)
        project_id = self.open_project(repo)
        self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        context = self.module.project_service.get_project_context(project_id)
        self.module.subprojects_service.update_list(context, {"confirmed": True})
        write_file(os.path.join(repo, "docs", "codebase", "SYSTEM.md"), "# System\n")
        write_file(os.path.join(repo, "docs", "codebase", "README.md"), "# Index\n")
        facts = ProjectProgressService.read_map_facts(repo, repo)
        self.assertEqual(facts["map_files"], [])

    def test_single_project_root_map_is_unchanged(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "single"), SINGLE_FILES)
        write_file(os.path.join(repo, "docs", "codebase", "STACK.md"), "# Stack\n")
        self.assertTrue(ProjectProgressService.read_map_facts(repo, repo)["map_files"])


if __name__ == "__main__":
    unittest.main()
