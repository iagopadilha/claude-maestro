"""Repository roles in the engine (`project_repos`): `role` load/save, `multi` derived, primary always edit, and the helpers the loop
uses (`edit_repos`, `reference_repos`, `repos_changed_by_roadmap`). Pure functions, temporary folders."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import loop_state  # noqa: E402
import project_repos  # noqa: E402


class TestRepoRoles(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="repo-roles-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.primary = os.path.join(self.root, "app")
        os.makedirs(self.primary)
        self.api, self.docs = os.path.join(self.root, "api"), os.path.join(self.root, "docs")

    def write_file(self, data):
        target = project_repos.repos_file_path(self.primary)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def test_old_file_without_role_loads_every_repo_as_edit_and_lists_all_even_with_multi_false(self):
        self.write_file({"multi": False, "repos": [{"name": "api", "path": self.api}, {"name": "docs", "path": self.docs}]})
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([(repo.name, repo.role) for repo in repos], [("app", "edit"), ("api", "edit"), ("docs", "edit")])
        self.assertTrue(project_repos.read_repos_file(self.primary)["multi"])

    def test_role_is_loaded_unknown_role_means_edit_and_primary_is_always_edit(self):
        self.write_file({"repos": [{"path": self.primary, "role": "reference"}, {"name": "api", "path": self.api, "role": "reference", "reason": "reads it"},
                                   {"name": "docs", "path": self.docs, "role": "bogus"}]})
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([repo.role for repo in repos], ["edit", "reference", "edit"])
        self.assertEqual(repos[1].reason, "reads it")

    def test_single_repo_is_not_multi_and_is_edit(self):
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([(repo.name, repo.role) for repo in repos], [("app", "edit")])
        self.assertFalse(project_repos.read_repos_file(self.primary)["multi"])

    def test_save_writes_role_reason_derived_multi_and_scope_source(self):
        api = project_repos.RepoRef(name="api", path=self.api, role="reference", reason="reads the handlers")
        project_repos.save_repos(self.primary, False, project_repos.single_repo(self.primary) + [api], scope_defined=True,
                                 scope_source=project_repos.SCOPE_SOURCE_PLANNING)
        with open(project_repos.repos_file_path(self.primary), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertTrue(saved["multi"])
        self.assertEqual(saved["scope_source"], "planning")
        self.assertEqual([item["role"] for item in saved["repos"]], ["edit", "reference"])
        self.assertEqual(saved["repos"][1]["reason"], "reads the handlers")
        project_repos.save_repos(self.primary, True, project_repos.load_repos(self.primary))  # None keeps the scope fields
        self.assertEqual(project_repos.read_repos_file(self.primary)["scope_source"], "planning")
        self.assertTrue(project_repos.is_scope_defined(self.primary))

    def test_saving_a_reference_primary_writes_edit(self):
        primary = project_repos.single_repo(self.primary)[0]
        primary.role = "reference"
        project_repos.save_repos(self.primary, False, [primary])
        self.assertEqual(project_repos.load_repos(self.primary)[0].role, "edit")

    def test_edit_and_reference_helpers(self):
        self.write_file({"repos": [{"name": "api", "path": self.api, "role": "reference"}, {"name": "docs", "path": self.docs}]})
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([repo.name for repo in project_repos.edit_repos(repos)], ["app", "docs"])
        self.assertEqual([repo.name for repo in project_repos.reference_repos(repos)], ["api"])

    def test_repos_changed_by_roadmap(self):
        features, errors = loop_state.parse_roadmap(
            "## Stage 1\n- [a] status=approved kind=back paths=api:src/orders,docs:readme\n"
            "- [b] status=approved kind=front paths=src/pages,C:/x/y\n- [c] status=approved kind=back paths=api:x\n")
        self.assertFalse(errors)
        self.assertEqual(project_repos.repos_changed_by_roadmap("app", features), {"api", "docs", "app"})
        self.assertEqual(project_repos.repos_changed_by_roadmap("app", [{"paths": "api:src"}]), {"api"})
        self.assertEqual(project_repos.repos_changed_by_roadmap("app", {"x": {"status": "approved"}}), set())


if __name__ == "__main__":
    unittest.main()
