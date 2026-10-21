"""close_loop `force_remove` (Finish > Remove anyway) and the totals in last-run.json: the check reasons are lifted for a `loop/*`
worktree, but the verified backup and the plan going home still happen first, a failed backup keeps the worktree, and the local branch stays."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import MultiRepoWorldTestCase, delivery, git, loop_state, write  # noqa: E402
import close_loop  # noqa: E402

TOTALS = {"cost_usd": 1.25, "max_cost_usd": 50, "turns": 7}


class ForceRemoveTestCase(MultiRepoWorldTestCase):
    def setUp(self):
        super().setUp()
        self.install_gh_shim()
        self.write_meta(dict(self.meta, **TOTALS))
        delivery.finalize_project(self.primary_work, self.meta)
        self.output_dir = os.path.join(self.root, "backups")

    def close(self, **options):
        return close_loop.close(self.primary, output_dir=self.output_dir, **options)

    def last_run(self):
        return self.read_json(close_loop.last_run_path(self.primary))

    def dirty_api_and_add_unpushed_commit(self):
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        write(os.path.join(self.worktrees["api"], "later.txt"), "after the push\n")
        git(self.worktrees["api"], "add", "later.txt")
        git(self.worktrees["api"], "commit", "-q", "-m", "not pushed")


class TestForceRemove(ForceRemoveTestCase):
    def test_without_force_a_dirty_or_unpushed_repo_is_kept(self):
        self.dirty_api_and_add_unpushed_commit()
        result = self.close()
        self.assertEqual((result["closed"], result["kept"]), (["app"], ["api"]))
        self.assertTrue(os.path.isdir(self.worktrees["api"]))

    def test_force_closes_it_after_a_verified_backup_and_keeps_the_branch(self):
        self.dirty_api_and_add_unpushed_commit()
        result = self.close(force_remove=True)
        self.assertEqual(sorted(result["closed"]), ["api", "app"], result)
        self.assertTrue(result["repos"]["api"]["forced"])
        self.assertFalse(result["repos"]["app"]["forced"])
        self.assertFalse(os.path.isdir(self.worktrees["api"]))
        self.assertTrue(os.path.isdir(os.path.join(result["repos"]["api"]["backup"], "specs")) or os.path.isdir(result["repos"]["api"]["backup"]))
        self.assertIn("loop/api", git(self.repos["api"], "branch", "--list", "loop/api"))
        self.assertIn("not pushed", git(self.repos["api"], "log", "-1", "--format=%s", "loop/api"))

    def test_a_failed_backup_keeps_the_worktree_even_when_forced(self):
        self.dirty_api_and_add_unpushed_commit()
        with mock.patch.object(close_loop, "back_up_worktree", return_value=(os.path.join(self.root, "x"), "backup failed: disk full")):
            result = self.close(force_remove=True)
        self.assertEqual(result["closed"], [])
        self.assertTrue(os.path.isdir(self.worktrees["api"]))
        self.assertEqual(result["repos"]["api"]["status"], "failed")

    def test_a_worktree_outside_loop_star_is_never_forced(self):
        git(self.worktrees["api"], "checkout", "-q", "-b", "feature/mine")
        result = self.close(force_remove=True)
        self.assertIn("api", result["kept"])
        self.assertTrue(os.path.isdir(self.worktrees["api"]))

    def test_a_running_loop_refuses_even_when_forced(self):
        with mock.patch.object(close_loop, "loop_is_running", return_value=True):
            result = self.close(force_remove=True)
        self.assertEqual((result["ok"], result["code"]), (False, "loop_running"))


class TestAllowMissingPr(ForceRemoveTestCase):
    def no_pr(self):
        for name in ("app", "api"):
            path = loop_state.loop_path(self.worktrees[name], "delivery.json")
            data = self.read_json(path)
            data.update(pr_status="not created", pr=None)
            write(path, __import__("json").dumps(data))

    def test_without_the_option_a_missing_pr_keeps_the_repos(self):
        self.no_pr()
        self.assertEqual(self.close()["closed"], [])

    def test_the_option_closes_pushed_repos_without_force(self):
        self.no_pr()
        result = self.close(allow_missing_pr=True)
        self.assertEqual(sorted(result["closed"]), ["api", "app"])
        self.assertFalse(result["repos"]["api"]["forced"])

    def test_the_option_never_lifts_a_dirty_tree_or_an_unpushed_commit(self):
        self.no_pr()
        self.dirty_api_and_add_unpushed_commit()
        result = self.close(allow_missing_pr=True)
        self.assertEqual((result["closed"], result["kept"]), (["app"], ["api"]))
        self.assertTrue(os.path.isdir(self.worktrees["api"]))


class TestLastRunTotals(ForceRemoveTestCase):
    def test_last_run_records_cost_cap_and_turns(self):
        self.close()
        record = self.last_run()
        self.assertEqual({key: record[key] for key in TOTALS}, TOTALS)

    def test_the_cap_is_the_effective_one_when_the_meta_has_none(self):
        self.write_meta(dict(self.meta, cost_usd=1, turns=2))
        self.assertEqual(close_loop.effective_max_cost({"cost_usd": 1}, self.primary_work), 50.0)
        write(loop_state.loop_path(self.primary_work, "LOOP-CMD.json"), __import__("json").dumps({"argv": ["x", "--max-cost", "7.5"]}))
        self.assertEqual(close_loop.effective_max_cost({}, self.primary_work), 7.5)
        self.assertEqual(close_loop.effective_max_cost({"max_cost_usd": 20}, self.primary_work), 20)
        self.close()
        self.assertEqual(self.last_run()["max_cost_usd"], 7.5)

    def test_a_second_close_keeps_the_totals(self):
        self.close()
        self.close()
        self.assertEqual(self.last_run()["cost_usd"], 1.25)

    def test_the_cli_accepts_the_explicit_close_flag(self):
        with mock.patch.object(sys, "argv", ["close_loop.py", self.primary, "--close", "--check"]):
            with mock.patch("builtins.print"):
                self.assertEqual(close_loop.main(), 0)


if __name__ == "__main__":
    unittest.main()
