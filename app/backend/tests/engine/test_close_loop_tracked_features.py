"""close_loop in a repo whose loop COMMITTED `.specs/features/<slug>/` (a real loop always does): tracked files with no local change are branch
content, not foreign `.specs` state, so the safe close removes the worktree without --force. A modified tracked non-plan file and an untracked
non-plan file still block."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import git, write  # noqa: E402
from test_close_loop_multi_repo import CloseLoopWorldTestCase, delivery  # noqa: E402
import close_loop  # noqa: E402

CONTRACT = "- [add] route GET /health - liveness (Api/Health.cs)\n"


class TrackedFeaturesTestCase(CloseLoopWorldTestCase):
    def setUp(self):
        super().setUp()
        work = self.primary_work
        write(os.path.join(work, ".specs", "features", "x", "contract.md"), CONTRACT)
        write(os.path.join(work, ".specs", "features", "x", "spec.md"), "# spec\n")
        git(work, "add", "-f", ".specs/features")
        git(work, "commit", "-q", "-m", "commit the feature specs")
        git(work, "push", "-q", "origin", "loop/app")
        delivery.finalize_project(work, self.meta)

    def saved_plan(self):
        """What a close does before removing: the plan goes home and the verified list is what may stay modified/untracked."""
        close_loop.copy_durable_files(self.primary_work, self.primary, "")
        return close_loop.verified_plan(self.primary_work, self.primary, close_loop.durable_files(self.primary_work))

    def primary_entry(self):
        return next(item for item in close_loop.project_entries(self.primary)[0] if item["primary"])


class TestTrackedFeatureFiles(TrackedFeaturesTestCase):
    def test_committed_clean_feature_files_are_not_foreign(self):
        self.assertIsNone(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan()))

    def test_safe_close_removes_the_worktree_without_force_and_copies_the_plan_back(self):
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertEqual((result["closed"], result["kept"]), (["app", "api"], []), result)
        self.assertFalse(result["repos"]["app"].get("forced"))
        self.assertFalse(os.path.isdir(self.primary_work))
        self.assertTrue(os.path.isfile(os.path.join(self.primary, ".specs", "features", "x", "spec.md")))  # the plan went home

    def test_remove_worktree_without_force_succeeds(self):
        self.assertIsNone(close_loop.remove_worktree(self.primary_entry(), False, self.saved_plan()))
        self.assertFalse(os.path.isdir(self.primary_work))

    def test_a_modified_tracked_non_plan_file_still_blocks(self):
        write(os.path.join(self.primary_work, ".specs", "features", "x", "contract.md"), CONTRACT + "edited\n")
        self.assertEqual(close_loop.foreign_content_in_specs(self.primary_work, self.saved_plan()).replace("\\", "/"), ".specs/features/x/contract.md")
        self.assertIn("contract.md", close_loop.remove_worktree(self.primary_entry(), False, self.saved_plan()))
        self.assertTrue(os.path.isdir(self.primary_work))

    def test_an_untracked_non_plan_file_still_blocks(self):
        write(os.path.join(self.primary_work, ".specs", "features", "x", "notes.txt"), "scratch\n")
        plan = self.saved_plan()
        self.assertIn("notes.txt", close_loop.foreign_content_in_specs(self.primary_work, plan))
        # (clear_engine_specs itself lets git decide when `.specs` is gitignored, as before: git worktree remove drops ignored files)


if __name__ == "__main__":
    unittest.main()
