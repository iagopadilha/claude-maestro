"""planning_repos_service: the planning chat's REPOS-SUGGESTED.json applied to REPOS.json (validation, merge, rejections, role switch)."""
import json
import os
import unittest

from harness_backend.domain.project_errors import RepoConflictError
from harness_backend.services.planning_repos_service import PlanningReposService, ensure_can_switch_to_reference, load_last_rejections

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, write_file


class TestPlanningReposService(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("primary")
        self.context = self.make_context(self.primary)
        self.api = self.make_repo("api")
        self.web = self.make_repo("web")
        self.service = PlanningReposService(self.git_client, self.log)

    def suggest(self, repos):
        write_file(os.path.join(self.primary, ".specs", "project", "REPOS-SUGGESTED.json"), json.dumps({"repos": repos}))

    def repos_file(self):
        return json.loads(read_file(os.path.join(self.primary, ".specs", "project", "REPOS.json")))

    def test_no_file_applies_nothing(self):
        self.assertEqual(self.service.apply_suggested_repos(self.context), {"applied": [], "rejected": []})
        self.assertFalse(os.path.exists(os.path.join(self.primary, ".specs", "project", "REPOS.json")))

    def test_applies_roles_marks_scope_defined_by_planning_and_the_registry_sees_it(self):
        self.suggest([{"path": self.api, "role": "edit", "reason": "adds a route"}, {"path": self.web, "role": "reference", "reason": "reads it"}])
        result = self.service.apply_suggested_repos(self.context)
        self.assertEqual([(row["name"], row["role"]) for row in result["applied"]], [("api", "edit"), ("web", "reference")])
        self.assertEqual(result["rejected"], [])
        saved = self.repos_file()
        self.assertTrue(saved["scope_defined"])
        self.assertEqual(saved["scope_source"], "planning")
        self.assertEqual([item["role"] for item in saved["repos"]], ["edit", "edit", "reference"])
        listing = self.registry.list_repos(self.context)
        self.assertTrue(listing["defined"])
        self.assertEqual([(repo["name"], repo["role"], repo["reason"]) for repo in listing["repos"]],
                         [("primary", "edit", None), ("api", "edit", "adds a route"), ("web", "reference", "reads it")])

    def test_role_missing_means_edit(self):
        self.suggest([{"path": self.api}])
        self.assertEqual(self.service.apply_suggested_repos(self.context)["applied"][0]["role"], "edit")

    def test_bad_paths_are_rejected_with_a_reason_and_persisted(self):
        plain = os.path.join(self.temporary_dir, "plain")
        os.makedirs(plain)
        self.suggest([{"path": os.path.join(self.temporary_dir, "ghost"), "role": "edit"}, {"path": plain, "role": "edit"},
                      {"role": "edit"}, {"path": self.api, "role": "write"}, {"path": self.web, "role": "reference"}])
        result = self.service.apply_suggested_repos(self.context)
        self.assertEqual([row["name"] for row in result["applied"]], ["web"])
        reasons = {os.path.basename(row["path"]) or row["path"]: row["reason"] for row in result["rejected"]}
        self.assertEqual(reasons["ghost"], "folder does not exist")
        self.assertEqual(reasons["plain"], "not a git repository")
        self.assertEqual(reasons["api"], "role must be edit or reference")
        self.assertEqual(len(result["rejected"]), 4)
        self.assertEqual(load_last_rejections(self.context), result["rejected"])
        self.assertTrue(self.repos_file()["scope_defined"])

    def test_all_rejected_leaves_repos_file_untouched(self):
        self.suggest([{"path": os.path.join(self.temporary_dir, "ghost"), "role": "edit"}])
        self.service.apply_suggested_repos(self.context)
        self.assertFalse(os.path.exists(os.path.join(self.primary, ".specs", "project", "REPOS.json")))
        self.assertEqual(len(load_last_rejections(self.context)), 1)

    def test_primary_stays_edit_and_is_not_listed_twice(self):
        self.suggest([{"path": self.primary, "role": "reference", "reason": "the main repo"}, {"path": self.api, "role": "edit"}])
        result = self.service.apply_suggested_repos(self.context)
        self.assertEqual([row["name"] for row in result["applied"]], ["api"])
        saved = self.repos_file()["repos"]
        self.assertEqual([(item["name"], item["role"]) for item in saved], [("primary", "edit"), ("api", "edit")])
        self.assertEqual(saved[0]["reason"], "the main repo")

    def test_duplicates_collapse_with_edit_winning(self):
        self.suggest([{"path": self.api, "role": "reference", "reason": "read"}, {"path": self.api + os.sep, "role": "edit", "reason": "change"}])
        result = self.service.apply_suggested_repos(self.context)
        self.assertEqual([(row["name"], row["role"]) for row in result["applied"]], [("api", "edit")])
        self.assertEqual(len(self.repos_file()["repos"]), 2)

    def test_a_new_suggestion_updates_a_listed_repo_and_an_unchanged_file_is_not_reapplied(self):
        self.suggest([{"path": self.api, "role": "edit"}])
        self.service.apply_suggested_repos(self.context)
        self.registry.set_role(self.context, "api", "reference")
        self.assertEqual(self.service.apply_suggested_repos(self.context)["applied"], [])  # the stale file does not undo the panel
        self.assertEqual(self.repos_file()["repos"][1]["role"], "reference")
        self.suggest([{"path": self.api, "role": "edit", "reason": "now it changes"}])
        self.assertEqual(self.service.apply_suggested_repos(self.context)["applied"][0]["role"], "edit")

    def test_edit_to_reference_is_refused_while_the_repo_has_a_loop_worktree(self):
        self.suggest([{"path": self.api, "role": "edit"}])
        self.service.apply_suggested_repos(self.context)
        os.makedirs(self.api + "-worktree-loop")
        self.suggest([{"path": self.api, "role": "reference"}])
        result = self.service.apply_suggested_repos(self.context)
        self.assertEqual(result["applied"], [])
        self.assertIn("close the loop first", result["rejected"][0]["reason"])
        self.assertEqual(self.repos_file()["repos"][1]["role"], "edit")
        with self.assertRaises(RepoConflictError):
            ensure_can_switch_to_reference(self.registry.resolve(self.context, "api"))

    def test_broken_files_become_one_rejection(self):
        for content in ("{not json", json.dumps({"repos": "x"})):
            with self.subTest(content=content):
                write_file(os.path.join(self.primary, ".specs", "project", "REPOS-SUGGESTED.json"), content)
                result = self.service.apply_suggested_repos(self.context)
                self.assertEqual((result["applied"], len(result["rejected"])), ([], 1))

    def test_a_clean_application_clears_the_previous_rejections(self):
        self.suggest([{"path": os.path.join(self.temporary_dir, "ghost"), "role": "edit"}, {"path": self.api, "role": "edit"}])
        self.service.apply_suggested_repos(self.context)
        self.assertEqual(len(load_last_rejections(self.context)), 1)
        self.suggest([{"path": self.api, "role": "reference", "reason": "only read"}])
        self.service.apply_suggested_repos(self.context)
        self.assertEqual(load_last_rejections(self.context), [])

    def test_listing_returns_the_last_rejections_for_the_workspace_bar(self):
        self.suggest([{"path": os.path.join(self.temporary_dir, "ghost"), "role": "edit"}])
        self.service.apply_suggested_repos(self.context)
        self.assertEqual(self.registry.list_repos(self.context)["rejected"][0]["reason"], "folder does not exist")


if __name__ == "__main__":
    unittest.main()
