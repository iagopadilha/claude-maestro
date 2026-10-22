"""A bare loop worktree (Create workspace, or the loop before its first turn) neither ends planning mode nor becomes the work dir:
planning keeps reading and writing the primary repo's `.specs/`, and Reset specs acts on that same `.specs/`. Only real loop state
(LOOP.json, a lock, a turn record, `turns` > 0 in meta.json) moves the project out of planning."""
import json
import os
import unittest

from harness_backend.domain.project import has_loop_started

from .support_projects import ProjectsTestCase, loop_file_path, read_file, run_git, write_file


class TestBareWorktree(ProjectsTestCase):
    def create_planning_project(self, name="bare"):
        repo, project_id = self.create_ready_project(name=name, approve=False)
        return repo, project_id

    def add_bare_worktree(self, repo):
        """What Create workspace leaves: a worktree with a copy of `.specs/` and a meta.json with turns 0."""
        worktree = repo + "-worktree-loop"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/bare", worktree)
        write_file(os.path.join(worktree, ".specs", "project", "ROADMAP.md"), "# stale copy made at creation\n")
        write_file(loop_file_path(worktree, "json", "meta.json"), json.dumps({"branch": "loop/bare", "base": "main", "turns": 0}))
        return worktree

    def planning_required(self, project_id):
        status, body = self.request("GET", "/p/%s/api/planning" % project_id)
        self.assertEqual(status, 200, body)
        return body["required"]

    def test_creating_the_workspace_does_not_leave_planning_mode(self):
        repo, project_id = self.create_planning_project()
        self.assertTrue(self.planning_required(project_id))
        worktree = self.add_bare_worktree(repo)
        self.assertFalse(has_loop_started(worktree))
        self.assertTrue(self.planning_required(project_id))

    def test_the_work_dir_stays_the_primary_until_a_loop_really_started(self):
        repo, project_id = self.create_planning_project()
        worktree = self.add_bare_worktree(repo)
        context = self.module.project_service.get_project_context(project_id)
        self.assertEqual(os.path.normcase(context.work_dir), os.path.normcase(repo))
        self.assertEqual(self.get_progress(project_id)["mode"], "planning")  # the panel reads the primary's plan
        write_file(loop_file_path(worktree, "json", "LOOP.json"), json.dumps({"status": "continue", "turn": 1}))
        context = self.module.project_service.get_project_context(project_id)
        self.assertEqual(os.path.normcase(context.work_dir), os.path.normcase(worktree))

    def test_each_kind_of_real_loop_state_counts(self):
        repo, _ = self.create_planning_project()
        worktree = self.add_bare_worktree(repo)
        markers = {"LOOP.json": lambda: write_file(loop_file_path(worktree, "json", "LOOP.json"), "{}"),
                   "lock": lambda: write_file(loop_file_path(worktree, "internal", "LOOP.lock"), "123"),
                   "turn record": lambda: write_file(loop_file_path(worktree, "internal", "turns", "001.json"), "{}"),
                   "meta turns": lambda: write_file(loop_file_path(worktree, "json", "meta.json"), json.dumps({"turns": 2}))}
        for name, create in markers.items():
            with self.subTest(marker=name):
                write_file(loop_file_path(worktree, "json", "meta.json"), json.dumps({"turns": 0}))
                for leftover in ("json/LOOP.json", "internal/LOOP.lock", "internal/turns/001.json"):
                    path = loop_file_path(worktree, *leftover.split("/"))
                    if os.path.exists(path):
                        os.remove(path)
                self.assertFalse(has_loop_started(worktree))
                create()
                self.assertTrue(has_loop_started(worktree))

    def test_a_started_loop_ends_planning_mode(self):
        repo, project_id = self.create_planning_project()
        worktree = self.add_bare_worktree(repo)
        write_file(loop_file_path(worktree, "json", "LOOP.json"), json.dumps({"status": "continue", "turn": 1}))
        self.assertFalse(self.planning_required(project_id))

    def test_reset_specs_acts_on_the_primary_specs_the_panel_reads_and_deletes_the_bare_worktree(self):
        repo, project_id = self.create_planning_project()
        worktree = self.add_bare_worktree(repo)
        status, body = self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {"confirm": os.path.basename(repo)})
        self.assertEqual(status, 200, body)
        self.assertIn(".specs/project/ROADMAP.md", body["moved"])
        self.assertTrue(os.path.isdir(os.path.join(repo, ".specs", ".trash")))
        self.assertNotIn("# stale copy made at creation", read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md")))
        self.assertEqual([item["path"] for item in body["worktrees_removed"]], [os.path.normpath(worktree)])  # the bare worktree goes away too
        self.assertFalse(os.path.exists(worktree))


if __name__ == "__main__":
    unittest.main()
