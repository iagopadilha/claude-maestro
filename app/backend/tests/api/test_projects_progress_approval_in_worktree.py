"""The checklist ("Approved roadmap") of a loop that ran in a worktree: the approval lives in the primary repo, the worktree may hold no copy
of it, and the panel must still show the roadmap as approved. The engine check that refuses a changed roadmap at start is unchanged."""
import json
import os
import unittest

from .support_projects import TWO_FEATURE_ROADMAP, ProjectsTestCase, create_git_repo, loop_file_path, run_git, write_file
from .test_projects_progress import steps_by_id

REORGANIZED_ROADMAP = TWO_FEATURE_ROADMAP.replace("## Stage 1", "## Stage 7") if "## Stage 1" in TWO_FEATURE_ROADMAP else TWO_FEATURE_ROADMAP + "\n- [extra] status=pending title=x scope=Small kind=back paths=src stage=2\n"


class TestApprovalWhenTheLoopRanInTheWorktree(ProjectsTestCase):
    def build(self, name, worktree_roadmap=TWO_FEATURE_ROADMAP, copy_approval=False):
        from harness_backend.repositories.specs_file_repository import loop_state
        repo = create_git_repo(os.path.join(self.projects_dir, name), {"src/app.py": "print(1)\n"})
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        loop_state.approve_roadmap(repo, "2026-10-15T09:00:00")  # the user approved in the panel: the file is written in the repo
        worktree = repo + "-worktree-loop"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/x", worktree)
        write_file(os.path.join(worktree, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        if copy_approval:
            loop_state.approve_roadmap(worktree, "2026-10-15T09:00:00")  # approved as it was, then the roadmap moved during the loop
        write_file(os.path.join(worktree, ".specs", "project", "ROADMAP.md"), worktree_roadmap)
        write_file(loop_file_path(worktree, "json", "LOOP.json"), json.dumps({"status": "done", "turn": 3, "at": "2026-10-15T10:00:00"}))
        return repo, worktree, loop_state

    def test_finished_loop_without_a_copy_of_the_approval_shows_approved(self):
        repo, worktree, loop_state = self.build("nocopy")
        self.assertFalse(os.path.exists(loop_file_path(worktree, "json", "ROADMAP-APPROVED.json")))
        self.assertFalse(loop_state.roadmap_approval(worktree)["approved"], "the engine check itself is not weakened")
        progress = self.get_progress(self.open_project(repo))
        self.assertEqual(progress["work_dir"], worktree)
        self.assertTrue(steps_by_id(progress)["approval"]["ok"], steps_by_id(progress)["approval"])
        self.assertIn("approved on 2026-10-15T09:00:00", steps_by_id(progress)["approval"]["detail"])

    def test_the_approval_state_of_the_panel_also_follows_the_primary(self):
        repo, worktree, loop_state = self.build("panel")
        self.assertTrue(loop_state.roadmap_approval_for_run(worktree, repo)["approved"])

    def test_a_worktree_with_its_own_approval_and_a_moved_roadmap_is_still_not_approved(self):
        repo, worktree, loop_state = self.build("moved", worktree_roadmap=REORGANIZED_ROADMAP, copy_approval=True)
        if loop_state.roadmap_signature(REORGANIZED_ROADMAP) == loop_state.roadmap_signature(TWO_FEATURE_ROADMAP):
            self.skipTest("the fixture roadmap has no signature-relevant edit")
        self.assertFalse(loop_state.roadmap_approval_for_run(worktree, repo)["approved"])

    def test_nothing_approved_anywhere_stays_not_approved(self):
        repo, worktree, loop_state = self.build("never")
        os.remove(loop_file_path(repo, "json", "ROADMAP-APPROVED.json"))
        self.assertFalse(loop_state.roadmap_approval_for_run(worktree, repo)["approved"])


if __name__ == "__main__":
    unittest.main()
