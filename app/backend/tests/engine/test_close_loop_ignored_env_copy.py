"""close_loop with git-ignored local files in the worktree's `.specs`: `.specs/project/.env.test` is written by the engine on every start
(engine-owned, never blocks, even when it differs from the repo's); any other ignored file blocks unless the original repo holds the same
bytes. The safe close then removes the worktree without --force."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import git, write  # noqa: E402
from test_close_loop_tracked_features import TrackedFeaturesTestCase  # noqa: E402
import close_loop  # noqa: E402

ENV_TEST = "API_URL=http://localhost:8000\n"
ENV_TEST_REWRITTEN = ENV_TEST + "LOCAL_URL=http://127.0.0.1:47960\n"
ENV_RELATIVE = os.path.join(".specs", "project", ".env.test")
LOCAL_RELATIVE = os.path.join(".specs", "project", "local.cfg")


class IgnoredEnvCopyTestCase(TrackedFeaturesTestCase):
    def setUp(self):
        super().setUp()
        for root in (self.primary, self.primary_work):
            write(os.path.join(root, ".gitignore"), ".env.test\nlocal.cfg\n")
        git(self.primary_work, "add", ".gitignore")
        git(self.primary_work, "commit", "-q", "-m", "ignore local test files")
        git(self.primary_work, "push", "-q", "origin", "loop/app")
        write(os.path.join(self.primary, ENV_RELATIVE), ENV_TEST)
        write(os.path.join(self.primary_work, ENV_RELATIVE), ENV_TEST_REWRITTEN)  # the engine completes it on every start


class TestIgnoredEnvCopy(IgnoredEnvCopyTestCase):
    def test_the_engine_env_test_is_not_foreign_even_when_it_differs_from_the_repo(self):
        self.assertIsNone(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan(), self.primary))
        self.assertIsNone(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan()))

    def test_remove_worktree_without_force_succeeds_and_the_repo_keeps_its_copy(self):
        self.assertIsNone(close_loop.remove_worktree(self.primary_entry(), False, self.saved_plan()))
        self.assertFalse(os.path.isdir(self.primary_work))
        with open(os.path.join(self.primary, ENV_RELATIVE), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), ENV_TEST)

    def test_another_ignored_file_identical_to_the_repo_is_not_foreign(self):
        for root in (self.primary, self.primary_work):
            write(os.path.join(root, LOCAL_RELATIVE), "x=1\n")
        self.assertIsNone(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan(), self.primary))
        self.assertIn("local.cfg", close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan()))

    def test_the_run_state_of_a_real_loop_does_not_block(self):
        """The untracked content a real loop left in the worktree (2026-10-21 run): loop/ run state, ignored or not, plus a phase file."""
        loop = os.path.join(self.primary_work, ".specs", "project", "loop")
        for relative in ("DEPENDENCIES.md", "GRAPH.md", "NOTICE.txt", "VALIDATE.md", os.path.join("internal", "guard.log"),
                         os.path.join("internal", "contract-merged.json"), os.path.join("internal", "turns", "001.json"),
                         os.path.join("internal", "snapshots", "001", "ROADMAP.md"), os.path.join("json", "LOOP-CMD.json")):
            write(os.path.join(loop, relative), "state\n")
        write(os.path.join(self.primary_work, ".specs", "project", "phases", "stage-01.md"), "# Stage 1\n")
        self.assertIsNone(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan(), self.primary))
        self.assertIsNone(close_loop.remove_worktree(self.primary_entry(), False, self.saved_plan()))
        self.assertFalse(os.path.isdir(self.primary_work))

    def test_an_untracked_file_outside_the_loop_folder_still_blocks(self):
        write(os.path.join(self.primary_work, ".specs", "project", "notes-of-the-user.md"), "mine\n")
        self.assertIn("notes-of-the-user.md", close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan(), self.primary))

    def test_another_ignored_file_that_differs_from_the_repo_still_blocks(self):
        write(os.path.join(self.primary, LOCAL_RELATIVE), "x=1\n")
        write(os.path.join(self.primary_work, LOCAL_RELATIVE), "x=2 edited in the worktree\n")
        self.assertIn("local.cfg", close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan(), self.primary))
        self.assertIn("local.cfg", close_loop.remove_worktree(self.primary_entry(), False, self.saved_plan()))
        self.assertTrue(os.path.isdir(self.primary_work))


if __name__ == "__main__":
    unittest.main()
