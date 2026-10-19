"""delivery.finalize_project: one push + PR per repo worktree (local bare remotes, `gh` shim), the sibling links in each PR body
(`gh pr edit`), failure isolation, the aggregate delivery.json, and the unchanged single-repo path."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import MultiRepoWorldTestCase, delivery, git, loop_state, write  # noqa: E402


class TestFinalizeProject(MultiRepoWorldTestCase):
    def test_every_repo_is_pushed_and_gets_its_own_pr_with_the_sibling_links(self):
        self.install_gh_shim()
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        self.assertEqual(sorted(aggregate["repos"]), ["api", "app"])
        for name in ("app", "api"):
            result = aggregate["repos"][name]
            self.assertTrue(result["push"].startswith("done"), result)
            self.assertEqual(result["pr_status"], "PR created")
            self.assertEqual(result["branch"], "loop/" + name)
            self.assertEqual(result["base"], "developer")
            self.assertIn("loop/" + name, git(self.remotes[name], "branch", "--list", "loop/" + name))  # really on the remote
        self.assertEqual((aggregate["pr"], aggregate["push"]), (aggregate["repos"]["app"]["pr"], aggregate["repos"]["app"]["push"]))  # top level = primary
        creates = [call for call in self.read_gh_calls() if call["argv"][:2] == ["pr", "create"]]
        self.assertEqual(len(creates), 2)
        for call in creates:
            self.assertEqual(call["argv"][call["argv"].index("--base") + 1], "developer")  # never the branch of the user's folder
            self.assertNotIn("Related PRs", call["body"])  # siblings did not exist yet at creation
        edits = [call for call in self.read_gh_calls() if call["argv"][:2] == ["pr", "edit"]]
        self.assertEqual(len(edits), 2)
        bodies = {call["argv"][2]: call["body"] for call in edits}
        urls = {name: aggregate["repos"][name]["pr"] for name in ("app", "api")}
        self.assertIn("## Related PRs", bodies[urls["app"]])
        self.assertIn("- api: " + urls["api"], bodies[urls["app"]])
        self.assertNotIn("- app: ", bodies[urls["app"]])  # a PR never lists itself
        self.assertIn("- app: " + urls["app"], bodies[urls["api"]])

    def test_delivery_json_is_the_aggregate_in_the_primary_and_per_repo_elsewhere(self):
        self.install_gh_shim()
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        primary_file = self.read_json(loop_state.loop_path(self.primary_work, "delivery.json"))
        self.assertEqual(sorted(primary_file["repos"]), ["api", "app"])
        self.assertEqual(primary_file["pr"], aggregate["pr"])
        api_file = self.read_json(loop_state.loop_path(self.worktrees["api"], "delivery.json"))
        self.assertEqual(api_file["branch"], "loop/api")
        self.assertNotIn("repos", api_file)

    def test_a_missing_worktree_fails_only_that_repo(self):
        self.install_gh_shim()
        self.meta["repos"]["api"]["worktree"] = os.path.join(self.root, "gone")
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        self.assertEqual(aggregate["repos"]["api"]["push"], "not done: worktree missing")
        self.assertTrue(aggregate["repos"]["app"]["push"].startswith("done"))
        self.assertEqual([call["argv"][:2] for call in self.read_gh_calls() if call["argv"][:2] == ["pr", "edit"]], [])  # one link only: no siblings to list

    def test_a_push_failure_in_one_repo_does_not_stop_the_other(self):
        self.install_gh_shim()
        git(self.worktrees["api"], "remote", "set-url", "origin", os.path.join(self.root, "no-such-remote.git"))
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        self.assertTrue(aggregate["repos"]["api"]["push"].startswith("FAILED"))
        self.assertTrue(aggregate["repos"]["app"]["push"].startswith("done"))
        self.assertEqual(aggregate["repos"]["app"]["pr_status"], "PR created")

    def test_open_features_hold_every_push(self):
        write(os.path.join(self.primary_work, ".specs", "project", "ROADMAP.md"),
              "# Roadmap\n\n## Stage 1 - Base\n\n- [feature-one] status=building scope=Small kind=back paths=src\n")
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        for result in aggregate["repos"].values():
            self.assertTrue(result["push"].startswith("not done: there are still open features"), result)
        self.assertEqual(git(self.remotes["app"], "branch", "--list", "loop/*"), "")

    def test_protected_branch_is_refused(self):
        self.meta["repos"]["api"]["branch"] = "main"
        git(self.worktrees["api"], "checkout", "-q", "-b", "main")
        aggregate = delivery.finalize_project(self.primary_work, self.meta, dry_run=True)
        self.assertTrue(aggregate["repos"]["api"]["push"].startswith("refused: main is a protected branch"))
        self.assertTrue(aggregate["repos"]["app"]["push"].startswith("dry-run"))

    def test_without_a_repos_map_it_is_the_single_repo_delivery(self):
        self.install_gh_shim()
        result = delivery.finalize_project(self.primary_work, {"branch": "loop/app"})
        self.assertNotIn("repos", result)
        self.assertEqual(result["pr_status"], "PR created")
        self.assertEqual([call["argv"][:2] for call in self.read_gh_calls()], [["pr", "list"], ["pr", "create"]])  # no `pr edit` without siblings

    def test_a_single_repo_in_the_map_creates_no_related_prs_block(self):
        self.install_gh_shim()
        del self.meta["repos"]["api"]
        aggregate = delivery.finalize_project(self.primary_work, self.meta)
        self.assertEqual(list(aggregate["repos"]), ["app"])
        self.assertEqual([call for call in self.read_gh_calls() if call["argv"][:2] == ["pr", "edit"]], [])

    def test_related_prs_block_lists_only_siblings_with_a_link(self):
        self.assertEqual(delivery.related_prs_block({}, "app"), [])
        self.assertEqual(delivery.related_prs_block({"app": "u1"}, "app"), [])
        self.assertEqual(delivery.related_prs_block({"app": "u1", "api": "u2", "web": None}, "app"), ["", "## Related PRs", "", "- api: u2"])


if __name__ == "__main__":
    unittest.main()
