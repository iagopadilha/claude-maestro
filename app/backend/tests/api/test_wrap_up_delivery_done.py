"""Wrap-up `delivery.done` comes from the confirmed push (a local bare origin gives no PR and no compare link), and a safe Finish with
"Remove the worktree" off runs the delivery only and keeps the worktree."""
import os
import unittest

from tests.api import test_wrap_up_service as base
from tests.api.test_wrap_up_service import git


class TestDeliveryDone(base.WrapUpWorldTestCase):
    def test_not_done_before_any_push(self):
        self.assertFalse(self.service.get_wrap_up(self.context)["delivery"]["done"])

    def test_done_after_a_push_with_no_pr_or_compare_link(self):
        self.finish(mode="keep_worktree")
        payload = self.service.get_wrap_up(self.context)
        self.assertTrue(payload["delivery"]["done"])
        self.assertIsNone(payload["summary"]["pr_url"])
        self.assertIsNone(payload["summary"]["compare_url"])

    def test_done_follows_the_confirmed_push_even_without_a_delivery_record(self):
        self.finish(mode="keep_worktree")
        self.service.read_delivery_record = lambda context: {}
        self.assertTrue(self.service.get_wrap_up(self.context)["delivery"]["done"])


class TestSafeFinishKeepingTheWorktree(base.WrapUpWorldTestCase):
    def test_safe_with_remove_worktree_off_delivers_and_keeps_the_worktree(self):
        self.write_meta(dict(self.meta, turns=9, delivery={"push_pr": True, "remove_worktree_on_finish": False}))
        result = self.finish(mode="safe")
        receipt = result["receipt"]
        self.assertTrue(result["ok"])
        self.assertFalse(receipt["worktree_removed"])
        self.assertIsNone(receipt["backup_dir"])
        for name in ("app", "api"):
            self.assertTrue(os.path.isdir(self.worktrees[name]))
            self.assertIn("loop/" + name, git(self.remotes[name], "branch", "--list"))  # the delivery ran
        self.assertTrue(self.service.get_wrap_up(self.context)["delivery"]["done"])
        self.assertFalse(self.service.get_wrap_up(self.context)["delivery"]["remove_worktree_on_finish"])

    def test_safe_with_remove_worktree_off_is_not_blocked_by_an_uncommitted_file(self):
        self.write_meta(dict(self.meta, turns=9, delivery={"push_pr": True, "remove_worktree_on_finish": False}))
        base.write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        self.assertFalse(self.finish(mode="safe")["receipt"]["worktree_removed"])
        self.assertTrue(os.path.isfile(os.path.join(self.worktrees["api"], "unsaved.txt")))


if __name__ == "__main__":
    unittest.main()
