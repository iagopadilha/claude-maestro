"""Workspace payload (branches, setup state, setup files), repo list routes of the services (role, reason) and the planning gates of
edit / reference repositories. Local git repos only, fake job service, no claude."""
import json
import os
import unittest

from harness_backend.domain.project_errors import InvalidRequestError
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services import planning_gates
from harness_backend.services.workspace_service import WorkspaceService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import run_git, write_file
from .test_workspace_service import FakeEngineAdapter, InlineJobService


class FakeEntryRepository:
    def __init__(self, entry=None):
        self.entry = entry

    def read_repo_entry(self, work_dir, key):
        return self.entry


class FakeStructure:
    def __init__(self, conformance=None):
        self.conformance = conformance

    def get_conformance(self, folder):
        return self.conformance


class FakeSubprojectsService:
    def __init__(self, conformance=None, entry=None, registry=None):
        self.registry = registry
        self.subprojects_file_repository = FakeEntryRepository(entry)
        self.specs_structure_service = FakeStructure(conformance)
        self.map_queue_reader = None

    def resolve_target(self, context, repo_name=None):
        if repo_name and self.registry is not None:
            return repo_name, self.registry.resolve(context, repo_name).path
        return os.path.basename(context.repo_path), context.repo_path


class TestWorkspaceReposRoles(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.context = self.make_context(self.primary)
        self.workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client,
                                          InlineJobService(), FakeEngineAdapter(), self.log)

    def primary_row(self):
        return self.workspace.get_workspace(self.context)["repos"][0]

    def test_payload_carries_branches_and_integration_branch(self):
        payload = self.workspace.get_workspace(self.context)
        row = payload["repos"][0]
        self.assertEqual(payload["integration_branch"], "developer")
        self.assertTrue(payload["plan_folder"].endswith(os.path.join(".specs", "project")))
        self.assertEqual(row["path"], self.primary)
        self.assertEqual(row["base_branch"], row["current_branch"])  # no `developer` here: the current branch with its sha
        self.assertTrue(row["base_sha"])
        self.assertIsNone(row["upstream"])
        run_git(self.primary, "branch", "developer")
        row = self.primary_row()
        self.assertEqual(row["base_branch"], "developer")
        self.assertNotEqual(row["current_branch"], "developer")

    def test_setup_files_are_split_from_other_changes(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "stack")
        write_file(os.path.join(self.primary, "src.txt"), "changed")
        row = self.primary_row()
        self.assertEqual(row["setup_files"], ["docs/codebase/STACK.md"])
        self.assertEqual(row["other_changes"], [])  # an untracked non-setup file is not walked
        write_file(os.path.join(self.primary, "README.md"), "edited")
        run_git(self.primary, "add", "-A")
        run_git(self.primary, "commit", "-q", "-m", "x")
        write_file(os.path.join(self.primary, "README.md"), "edited again")
        self.assertEqual(self.primary_row()["other_changes"], ["README.md"])

    def test_setup_state_by_role_and_conformance(self):
        self.assertEqual(self.primary_row()["setup"], {"state": "not_set_up", "monorepo": False})  # no subprojects service attached
        self.workspace.subprojects_service = FakeSubprojectsService(conformance=None)
        self.assertEqual(self.primary_row()["setup"]["state"], "not_set_up")
        self.workspace.subprojects_service = FakeSubprojectsService(conformance="STANDARD")
        self.assertEqual(self.primary_row()["setup"], {"state": "set_up", "monorepo": False})  # no code: nothing to map
        write_file(os.path.join(self.primary, "main.py"), "print(1)")
        self.assertEqual(self.primary_row()["setup"]["state"], "map_missing")
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "stack")
        self.assertEqual(self.primary_row()["setup"]["state"], "set_up")

    def test_reference_needs_no_conformance_and_missing_map_is_not_a_block(self):
        reference = self.make_repo("lib", {"main.py": "x"})
        self.registry.add_repo(self.context, reference, [], None, "reference", "contracts live here")
        self.workspace.subprojects_service = FakeSubprojectsService(conformance=None, registry=self.registry)
        row = next(item for item in self.workspace.get_workspace(self.context)["repos"] if item["name"] == "lib")
        self.assertEqual((row["role"], row["reason"], row["setup"]["state"]), ("reference", "contracts live here", "map_missing"))

    def test_add_update_reason_and_validation(self):
        sibling = self.make_repo("api")
        added = self.registry.add_repo(self.context, sibling, None, None, "reference", "  reads the   contract ")
        self.assertEqual(added["repo"]["reason"], "reads the contract")
        self.assertEqual([repo["name"] for repo in added["repos"]], ["app", "api"])
        listing = self.registry.update_repo(self.context, "api", reason="now edited too", role="edit")
        api = next(repo for repo in listing["repos"] if repo["name"] == "api")
        self.assertEqual((api["role"], api["reason"]), ("edit", "now edited too"))
        with self.assertRaises(InvalidRequestError):
            self.registry.update_repo(self.context, "api")
        with self.assertRaises(InvalidRequestError):
            self.registry.add_repo(self.context, self.make_repo("web"), None, None, "bogus")
        with open(os.path.join(self.primary, ".specs", "project", "REPOS.json"), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual(saved["repos"][1]["reason"], "now edited too")

    def test_no_confirm_needed_and_old_scope_defined_files_still_work(self):
        self.assertTrue(self.registry.list_repos(self.context)["defined"])
        write_file(os.path.join(self.primary, ".specs", "project", "REPOS.json"), json.dumps({"scope_defined": True, "repos": []}))
        self.assertTrue(self.registry.list_repos(self.context)["defined"])


class TestPlanningGatesForRepos(unittest.TestCase):
    def test_edit_repo_blocks_and_reference_only_warns(self):
        blocked = planning_gates.build_repos_gates({"repos_unready": [{"detail": "api: no Conformance line"}],
                                                    "repos_reference_warnings": ["lib has no map"]})[0]
        self.assertFalse(blocked["ok"])
        self.assertIn("warning: lib has no map", blocked["items"])
        warned = planning_gates.build_repos_gates({"repos_unready": [], "repos_reference_warnings": ["lib has no map"]})[0]
        self.assertTrue(warned["ok"] and warned["optional"])
        self.assertTrue(planning_gates.are_gates_ok([warned]))
        self.assertEqual(planning_gates.build_repos_gates({}), [])


if __name__ == "__main__":
    unittest.main()
