"""close_loop on a real two-repo project (local bare remotes, gh shim for the delivery): the per-repo can-close check and each reason,
the compare-link confirmation, the close order (backup, durable .specs back, last-run.json, worktrees removed non-primary first),
and one repo failing never stopping the other."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import MultiRepoWorldTestCase, delivery, git, loop_state, write  # noqa: E402
import close_loop  # noqa: E402


class CloseLoopWorldTestCase(MultiRepoWorldTestCase):
    def setUp(self):
        super().setUp()
        self.install_gh_shim()
        self.write_meta()
        self.aggregate = delivery.finalize_project(self.primary_work, self.meta)
        self.output_dir = os.path.join(self.root, "backups")

    def check(self):
        result = close_loop.can_close(self.primary)
        return result, {item["name"]: item for item in result["repos"]}

    def codes(self, item):
        return [reason["code"] for reason in item["reasons"]]

    def edit_delivery(self, name, **fields):
        path = loop_state.loop_path(self.worktrees[name], "delivery.json")
        data = self.read_json(path)
        data.update(fields)
        write(path, json.dumps(data))


class TestCanCloseCheck(CloseLoopWorldTestCase):
    def test_a_delivered_clean_project_can_close(self):
        result, by_name = self.check()
        self.assertTrue(result["can_close"], result)
        self.assertEqual(list(by_name), ["app", "api"])  # primary first
        for item in by_name.values():
            self.assertEqual((item["can_close"], item["reasons"], item["pr_status"]), (True, [], "PR created"))
            self.assertTrue(item["branch"].startswith("loop/"))

    def test_one_repo_blocking_blocks_the_project_but_not_the_other_repo(self):
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        result, by_name = self.check()
        self.assertFalse(result["can_close"])
        self.assertTrue(by_name["app"]["can_close"])
        self.assertEqual(self.codes(by_name["api"]), ["tree_dirty"])

    def test_the_plan_state_under_specs_is_not_a_dirty_tree(self):
        write(os.path.join(self.worktrees["api"], ".specs", "project", "ROADMAP.md"), "plan\n")
        self.assertTrue(self.check()[1]["api"]["can_close"])

    def test_a_commit_that_was_not_pushed_is_push_unconfirmed(self):
        write(os.path.join(self.worktrees["api"], "later.txt"), "after delivery\n")
        git(self.worktrees["api"], "add", "-A")
        git(self.worktrees["api"], "commit", "-q", "-m", "after the push")
        self.assertEqual(self.codes(self.check()[1]["api"]), ["push_unconfirmed"])

    def test_push_not_done_in_delivery_json_is_push_unconfirmed(self):
        self.edit_delivery("api", push="not done: no origin remote")
        self.assertEqual(self.codes(self.check()[1]["api"]), ["push_unconfirmed"])

    def test_a_branch_that_is_not_loop_star_is_never_closed(self):
        git(self.worktrees["api"], "checkout", "-q", "-b", "feature/mine")
        item = self.check()[1]["api"]
        self.assertEqual(self.codes(item), ["push_unconfirmed"])
        self.assertIn("never closed from here", item["reasons"][0]["text"])

    def test_missing_pr_and_compare_link_need_a_confirmation(self):
        self.edit_delivery("api", pr_status="not created", pr=None)
        item = self.check()[1]["api"]
        self.assertEqual((self.codes(item), item["needs_confirm"]), (["pr_missing"], False))
        self.edit_delivery("api", pr_status="compare link (PR not created)", pr="https://github.com/o/r/compare/developer...loop/api?expand=1")
        item = self.check()[1]["api"]
        self.assertEqual((self.codes(item), item["needs_confirm"]), (["pr_missing"], True))
        self.assertIn("compare link", item["reasons"][0]["text"])

    def test_a_changed_copied_file_keeps_the_worktree(self):
        write(os.path.join(self.repos["api"], ".git", "info", "exclude"), ".env\n")  # a git-ignored local file, as the copy list is meant for
        write(os.path.join(self.repos["api"], ".env"), "A=1\n")
        write(os.path.join(self.worktrees["api"], ".env"), "A=1\n")
        recorded = close_loop.file_hash(os.path.join(self.repos["api"], ".env"))
        self.meta["repos"]["api"]["copied"] = {".env": recorded}
        self.write_meta()
        self.assertEqual(self.check()[1]["api"]["reasons"], [])  # same content on both sides
        write(os.path.join(self.repos["api"], ".env"), "A=2\n")  # the ORIGINAL changed
        item = self.check()[1]["api"]
        self.assertEqual(self.codes(item), ["copied_changed"])
        self.assertIn(".env", item["reasons"][0]["text"])

    def test_loop_running_and_missing_worktree(self):
        write(loop_state.loop_path(self.primary_work, "LOOP.lock"), str(os.getpid()))
        result, by_name = self.check()
        self.assertFalse(result["can_close"])
        self.assertTrue(all("loop_running" in self.codes(item) for item in by_name.values()))
        os.remove(loop_state.loop_path(self.primary_work, "LOOP.lock"))
        git(self.repos["api"], "worktree", "remove", "--force", self.worktrees["api"])
        self.assertEqual(self.codes(self.check()[1]["api"]), ["worktree_missing"])


class TestCloseLoop(CloseLoopWorldTestCase):
    def test_close_backs_up_returns_the_plan_records_last_run_and_removes_worktrees(self):
        write(os.path.join(self.primary_work, ".specs", "features", "feature-one", "spec.md"), "# spec\n")
        write(os.path.join(self.primary_work, ".specs", "project", "loop", "internal", "secret.log"), "never goes home\n")
        write(os.path.join(self.primary_work, ".specs", "project", "ROADMAP.md"), "# Roadmap\n\n## Stage 1 - Base\n\n- [feature-one] status=verified scope=Small kind=back paths=src\n\nnote\n")
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertTrue(result["ok"], result)
        self.assertEqual((result["closed"], result["kept"]), (["app", "api"], []))
        for name in ("app", "api"):
            self.assertFalse(os.path.isdir(self.worktrees[name]), name)
            self.assertTrue(os.path.isdir(self.repos[name]))
            self.assertTrue(os.path.isdir(result["repos"][name]["backup"]))
            self.assertIn("loop/" + name, git(self.repos[name], "branch", "--list", "loop/" + name))  # local branches stay
        primary_specs = os.path.join(self.primary, ".specs")
        with open(os.path.join(primary_specs, "project", "ROADMAP.md"), encoding="utf-8") as handle:
            self.assertIn("note", handle.read())
        self.assertTrue(os.path.isfile(os.path.join(primary_specs, "features", "feature-one", "spec.md")))
        self.assertFalse(os.path.exists(os.path.join(primary_specs, "project", "loop", "internal", "secret.log")))
        last_run = self.read_json(close_loop.last_run_path(self.primary))
        self.assertTrue(last_run["multi"])
        self.assertEqual(sorted(last_run["repos"]), ["api", "app"])
        self.assertTrue(all(item["worktree_removed"] for item in last_run["repos"].values()))
        self.assertEqual(last_run["repos"]["api"]["branch"], "loop/api")
        self.assertEqual(last_run["repos"]["api"]["pr"], self.aggregate["repos"]["api"]["pr"])
        self.assertIn("project/ROADMAP.md", last_run["durable_files"])

    def test_after_closing_the_record_says_the_worktrees_are_gone(self):
        close_loop.close(self.primary, output_dir=self.output_dir)
        result, by_name = self.check()
        self.assertFalse(result["can_close"])
        self.assertEqual([self.codes(item) for item in by_name.values()], [["already_closed"]] * 2)  # an earlier close removed them (last-run.json), not a missing folder
        self.assertEqual(by_name["api"]["branch"], "loop/api")  # remembered from last-run.json

    def test_a_blocked_repo_is_kept_with_its_reasons_and_the_other_closes(self):
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertEqual((result["closed"], result["kept"]), (["app"], ["api"]))
        self.assertEqual(result["repos"]["api"]["reasons"][0]["code"], "tree_dirty")
        self.assertTrue(os.path.isdir(self.worktrees["api"]))
        self.assertFalse(os.path.isdir(self.worktrees["app"]))
        last_run = self.read_json(close_loop.last_run_path(self.primary))
        self.assertTrue(last_run["repos"]["app"]["worktree_removed"])
        self.assertFalse(last_run["repos"]["api"]["worktree_removed"])

    def test_only_closes_the_named_repos(self):
        result = close_loop.close(self.primary, only=["api"], output_dir=self.output_dir)
        self.assertEqual((result["closed"], result["kept"]), (["api"], ["app"]))
        self.assertTrue(os.path.isdir(self.worktrees["app"]))

    def test_a_compare_link_closes_only_with_the_confirmation(self):
        self.edit_delivery("api", pr_status="compare link (PR not created)", pr="https://github.com/o/r/compare/developer...loop/api?expand=1")
        refused = close_loop.close(self.primary, only=["api"], output_dir=self.output_dir)
        self.assertIn("api", refused["kept"])
        self.assertEqual(refused["repos"]["api"]["reasons"][0]["code"], "pr_missing")
        self.assertTrue(os.path.isdir(self.worktrees["api"]))
        confirmed = close_loop.close(self.primary, confirm_compare_link=True, only=["api"], output_dir=self.output_dir)
        self.assertEqual(confirmed["closed"], ["api"])

    def test_a_running_loop_refuses_the_whole_close(self):
        write(loop_state.loop_path(self.primary_work, "LOOP.lock"), str(os.getpid()))
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertEqual((result["ok"], result["code"]), (False, "loop_running"))
        self.assertTrue(os.path.isdir(self.worktrees["api"]))


if __name__ == "__main__":
    unittest.main()
