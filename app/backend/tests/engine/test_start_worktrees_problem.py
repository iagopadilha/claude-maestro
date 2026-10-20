"""Start creates the missing worktrees; a failure is a clear PROBLEM line and stops the start with a readable reason."""
import contextlib
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402


class TestEnsureWorktreesOrStop(unittest.TestCase):
    def test_failure_prints_a_problem_and_stops_with_the_reason(self):
        output = io.StringIO()
        with mock.patch.object(run_loop, "ensure_worktrees", side_effect=SystemExit("repo api: failed to create worktree: boom")), \
                contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stop:
            run_loop.ensure_worktrees_or_stop("repo", None)
        self.assertIn("[check] PROBLEM: worktree not created: repo api: failed to create worktree: boom", output.getvalue())
        self.assertIn("could not be created", stop.exception.code)

    def test_success_passes_the_result_through(self):
        with mock.patch.object(run_loop, "ensure_worktrees", return_value=("work", {})):
            self.assertEqual(run_loop.ensure_worktrees_or_stop("repo", None), ("work", {}))


if __name__ == "__main__":
    unittest.main()
