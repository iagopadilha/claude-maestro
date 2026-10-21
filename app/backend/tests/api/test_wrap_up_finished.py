"""Wrap-up Finish records the run as finished (last-run.json) so the next Start begins fresh; keep_worktree and a plain close do not."""
import contextlib
import io
import os
import sys
import types
import unittest

ENGINE_TESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
sys.path.insert(0, ENGINE_TESTS_DIR)
from multi_repo_world import loop_state, write  # noqa: E402
import run_loop  # noqa: E402

from harness_backend.domain.project_errors import InvalidRequestError  # noqa: E402
from tests.api import test_wrap_up_service  # noqa: E402


class FinishedRecordTests(test_wrap_up_service.WrapUpWorldTestCase):
    def last_run(self):
        return self.read_json(loop_state.loop_path(self.primary, "last-run.json"))

    def start_primary(self):
        opts = types.SimpleNamespace(setup="", branch="", dry_run=False, repo_path=self.primary, push=False)
        with contextlib.redirect_stdout(io.StringIO()):
            return run_loop.ensure_worktree(self.primary, opts)[1]["branch"]

    def test_safe_finish_marks_every_repo_and_the_next_start_is_new(self):
        self.deliver_in_process()
        self.assertTrue(self.finish(mode="safe")["receipt"]["worktree_removed"])
        record = self.last_run()
        self.assertTrue(record["finished"])
        self.assertTrue(all(item.get("finished") is True for item in record["repos"].values()))
        self.assertNotEqual(self.start_primary(), "loop/app")

    def test_remove_anyway_marks_finished(self):
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        self.assertTrue(self.finish(mode="remove_anyway", confirm_text="app")["receipt"]["worktree_removed"])
        self.assertTrue(self.last_run()["finished"])

    def test_keep_worktree_writes_no_finished_record(self):
        self.finish(mode="keep_worktree")
        self.assertFalse(os.path.exists(loop_state.loop_path(self.primary, "last-run.json")))

    def test_a_plain_close_is_not_finished_and_the_next_start_reattaches(self):
        self.deliver_in_process()
        self.service.loop_worktree_service.close_loop(self.context, {"confirm_compare_link": True, "allow_missing_pr": True})
        self.assertNotIn("finished", self.last_run())
        self.assertEqual(self.start_primary(), "loop/app")

    def test_the_service_passes_the_finished_flag_to_the_engine(self):
        self.deliver_in_process()
        self.service.loop_worktree_service.close_loop(self.context, {"finished": True})
        self.assertTrue(self.last_run()["finished"])

    def test_the_flag_must_be_boolean(self):
        with self.assertRaises(InvalidRequestError):
            self.service.loop_worktree_service.close_loop(self.context, {"finished": "yes"})


if __name__ == "__main__":
    unittest.main()
