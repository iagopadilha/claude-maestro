"""close_loop in a repo that TRACKS `.specs/project`: the orchestrator leaves ROADMAP.md (and untracked plan files) modified in the worktree.
Those are exactly the durable plan files the close copies back and backs up, so after the copy-back and the backup they do not block the
removal (no --force); any other modified file still does."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import git, write  # noqa: E402
from test_close_loop_multi_repo import CloseLoopWorldTestCase, delivery, loop_state  # noqa: E402
import close_loop  # noqa: E402

EDITED_ROADMAP = "# Roadmap\n\n## Stage 1 - Base\n\n- [feature-one] status=verified scope=Small kind=back paths=src\n\nedited by the orchestrator\n"


class TrackedPlanTestCase(CloseLoopWorldTestCase):
    def setUp(self):
        super().setUp()
        work = self.primary_work
        git(work, "add", "-f", ".specs/project/ROADMAP.md", ".specs/project/FLOWS.md")  # the repo tracks the plan
        git(work, "commit", "-q", "-m", "track the plan")
        git(work, "push", "-q", "origin", "loop/app")
        delivery.finalize_project(work, self.meta)
        write(os.path.join(work, ".specs", "project", "ROADMAP.md"), EDITED_ROADMAP)  # modified, tracked, uncommitted
        write(os.path.join(work, ".specs", "features", "feature-one", "spec.md"), "# spec\n")  # untracked durable file

    def roadmap_in_primary(self):
        with open(os.path.join(self.primary, ".specs", "project", "ROADMAP.md"), encoding="utf-8") as handle:
            return handle.read()


class TestTrackedPlanFiles(TrackedPlanTestCase):
    def test_modified_tracked_plan_files_do_not_block_the_safe_close(self):
        self.assertTrue(close_loop.can_close(self.primary)["can_close"])
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertEqual((result["closed"], result["kept"]), (["app", "api"], []), result)
        self.assertFalse(result["repos"]["app"].get("forced"))
        self.assertFalse(os.path.isdir(self.primary_work))
        self.assertEqual(self.roadmap_in_primary(), EDITED_ROADMAP)  # the plan went home
        self.assertTrue(os.path.isfile(os.path.join(self.primary, ".specs", "features", "feature-one", "spec.md")))
        backup = result["repos"]["app"]["backup"]
        with open(os.path.join(backup, "specs", "project", "ROADMAP.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), EDITED_ROADMAP)  # and a backup holds it

    def test_a_modified_source_file_still_blocks_the_close(self):
        write(os.path.join(self.primary_work, "README.md"), "# changed\n")
        result = close_loop.close(self.primary, output_dir=self.output_dir)
        self.assertIn("app", result["kept"])
        self.assertEqual(result["repos"]["app"]["status"], "kept", result)  # the check says tree_dirty
        self.assertTrue(os.path.isdir(self.primary_work))

    def test_a_modified_source_file_blocks_removal_even_when_the_check_is_bypassed(self):
        write(os.path.join(self.primary_work, "README.md"), "# changed\n")
        entry = next(item for item in close_loop.project_entries(self.primary)[0] if item["primary"])
        error = close_loop.remove_worktree(entry, False, [".specs/project/ROADMAP.md"])
        self.assertIn("README.md", error)
        self.assertTrue(os.path.isdir(self.primary_work))

    def test_a_plan_file_that_was_not_verified_as_saved_blocks_the_removal(self):
        entry = next(item for item in close_loop.project_entries(self.primary)[0] if item["primary"])
        error = close_loop.remove_worktree(entry, False, [])
        self.assertIn("ROADMAP.md", error)
        self.assertTrue(os.path.isdir(self.primary_work))


class TestVerifiedPlan(TrackedPlanTestCase):
    def test_only_files_identical_in_the_repository_count_as_saved(self):
        close_loop.copy_durable_files(self.primary_work, self.primary, "")
        durable = close_loop.durable_files(self.primary_work)
        write(os.path.join(self.primary, ".specs", "project", "FLOWS.md"), "# different\n")
        saved = close_loop.verified_plan(self.primary_work, self.primary, durable)
        self.assertIn(".specs/project/ROADMAP.md", saved)
        self.assertNotIn(".specs/project/FLOWS.md", saved)


if __name__ == "__main__":
    unittest.main()
