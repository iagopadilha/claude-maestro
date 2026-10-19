"""Reopen planning (`GET planning/reopen-check`, `POST planning/reopen`) over a real temp git repo with a real loop worktree:
what it refuses (loop running, dirty/unpushed without confirm, a folder the app did not create), what it keeps (spec files, the
planning chat, ROADMAP statuses, the loop branch) and that a later approval works again. No claude is called."""
import json
import os
import shutil

from harness_backend.api.controllers.planning_reopen_controller import PlanningReopenController
from harness_backend.repositories.loop_command_file_repository import LoopCommandFileRepository
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.infrastructure.git_client import GitClient
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.services.loop_status_service import LoopStatusService
from harness_backend.services.loop_worktree_service import LoopWorktreeService
from harness_backend.services.planning_reopen_service import PlanningReopenService

from .support_projects import ProjectsTestCase, loop_file_path, mark_loop_already_ran, read_file, run_git, write_file

BRANCH = "loop/test"
THREAD = {"messages": [{"role": "user", "text": "hello"}], "session_id": "s-1"}


class TestReopenPlanning(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        git_client = GitClient(ProcessRunner())
        status_service = LoopStatusService(LoopFileRepository(), self.module.project_service, self.log)
        worktree_service = LoopWorktreeService(self.config, self.log, git_client, None, LoopCommandFileRepository(),
                                               loop_status_service=status_service)
        service = PlanningReopenService(git_client, status_service, worktree_service, self.module.planning_requirement_service,
                                        self.module.claude_job_service, self.log)
        PlanningReopenController(self.module.project_service, service).register_routes(self.router)

    # ------------------------------------------------ helpers
    def make_loop_worktree(self, repo):
        """Worktree on loop/test, a loop already ran in it: ROADMAP with api-base verified, the approval and a planning chat."""
        worktree = repo + "-worktree-loop"
        self.assertEqual(run_git(repo, "worktree", "add", "-q", "-b", BRANCH, worktree).returncode, 0)
        shutil.copytree(os.path.join(repo, ".specs"), os.path.join(worktree, ".specs"), dirs_exist_ok=True)
        roadmap = os.path.join(worktree, ".specs", "project", "ROADMAP.md")
        write_file(roadmap, read_file(roadmap).replace("[api-base] status=pending", "[api-base] status=verified"))
        write_file(loop_file_path(worktree, "internal", "planning", "thread.json"), json.dumps(THREAD))
        mark_loop_already_ran(worktree)
        return worktree

    def get_planning(self, project_id):
        status, body = self.request("GET", "/p/%s/api/planning" % project_id)
        self.assertEqual(status, 200, body)
        return body

    def reopen(self, project_id, confirm=None):
        return self.request("POST", "/p/%s/api/planning/reopen" % project_id,
                            {} if confirm is None else {"confirm_remove_worktrees": confirm})

    def check(self, project_id):
        status, body = self.request("GET", "/p/%s/api/planning/reopen-check" % project_id)
        self.assertEqual(status, 200, body)
        return body

    def branch_exists(self, repo):
        return BRANCH in run_git(repo, "branch", "--list", BRANCH).stdout

    # ------------------------------------------------ no worktree
    def test_without_worktree_it_just_reopens_and_keeps_the_specs(self):
        repo, project_id = self.create_ready_project()
        self.assertFalse(self.get_planning(project_id)["required"])
        checked = self.check(project_id)
        self.assertEqual((checked["ok"], checked["can_reopen"], checked["blocked_by"], checked["worktrees"]), (True, True, None, []))
        status, body = self.reopen(project_id)
        self.assertEqual((status, body), (200, {"ok": True, "reopened": True, "removed": []}))
        planning = self.get_planning(project_id)
        self.assertTrue(planning["required"])
        self.assertFalse(planning["approved"])
        for name in ("ROADMAP.md", "FLOWS.md", "PROJECT.md", "RULES.md"):
            self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "project", name)), name)

    def test_reopen_is_idempotent(self):
        _, project_id = self.create_ready_project()
        self.assertEqual(self.reopen(project_id)[0], 200)
        self.assertEqual(self.reopen(project_id)[0], 200)
        self.assertTrue(self.get_planning(project_id)["required"])

    # ------------------------------------------------ with a worktree
    def test_clean_worktree_is_removed_branch_and_plan_are_kept_and_reapproval_works(self):
        repo, project_id = self.create_ready_project()
        mark_loop_already_ran(repo)  # a project that ran loops: the marker, not "never ran", must put it back in planning
        worktree = self.make_loop_worktree(repo)
        self.assertEqual(self.get_planning(project_id)["thread"]["messages"][0]["text"], "hello")
        checked = self.check(project_id)
        self.assertTrue(checked["can_reopen"])
        item = checked["worktrees"][0]
        self.assertEqual((item["branch"], item["dirty"], item["unpushed"], item["can_remove"], item["reasons"]), (BRANCH, False, 0, True, []))
        self.assertTrue(os.path.isdir(worktree), "the check creates and removes nothing")

        status, body = self.reopen(project_id)
        self.assertEqual(status, 200, body)
        self.assertEqual([(entry["repo"], os.path.normcase(entry["path"]), entry["branch_kept"]) for entry in body["removed"]],
                         [(os.path.basename(repo), os.path.normcase(os.path.normpath(worktree)), BRANCH)])
        self.assertFalse(os.path.isdir(worktree))
        self.assertTrue(os.path.isdir(repo))
        self.assertTrue(self.branch_exists(repo))
        project_dir = os.path.join(repo, ".specs", "project")
        self.assertIn("[api-base] status=verified", read_file(os.path.join(project_dir, "ROADMAP.md")))  # verified features stay verified
        self.assertTrue(os.path.isfile(os.path.join(project_dir, "FLOWS.md")))
        self.assertFalse(os.path.exists(loop_file_path(repo, "json", "LOOP.json")), "no loop state is brought back: the next loop starts clean")
        planning = self.get_planning(project_id)
        self.assertTrue(planning["required"])
        self.assertFalse(planning["approved"])
        self.assertEqual(planning["thread"]["messages"][0]["text"], "hello")  # the chat history survived

        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual(status, 200, body)
        planning = self.get_planning(project_id)
        self.assertFalse(planning["required"])
        self.assertTrue(planning["approved"])

    def test_dirty_worktree_needs_confirmation(self):
        repo, project_id = self.create_ready_project()
        worktree = self.make_loop_worktree(repo)
        write_file(os.path.join(worktree, "work.txt"), "not committed")
        checked = self.check(project_id)
        self.assertTrue(checked["can_reopen"])
        self.assertTrue(checked["worktrees"][0]["dirty"])
        self.assertEqual([reason["code"] for reason in checked["worktrees"][0]["reasons"]], ["tree_dirty"])
        status, body = self.reopen(project_id)
        self.assertEqual((status, body["ok"], body["code"]), (409, False, "needs_confirm"))
        self.assertTrue(body["worktrees"][0]["dirty"])
        self.assertTrue(os.path.isdir(worktree))
        self.assertTrue(self.get_planning(project_id)["approved"], "refused: nothing changed")
        status, body = self.reopen(project_id, False)
        self.assertEqual((status, body["code"]), (409, "needs_confirm"))
        status, body = self.reopen(project_id, True)
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.isdir(worktree))
        self.assertTrue(self.branch_exists(repo))

    def test_unpushed_commits_need_confirmation_and_the_branch_keeps_them(self):
        repo, project_id = self.create_ready_project()
        worktree = self.make_loop_worktree(repo)
        write_file(os.path.join(worktree, "feature.txt"), "work")
        run_git(worktree, "add", "feature.txt")
        run_git(worktree, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-q", "-m", "loop work")
        item = self.check(project_id)["worktrees"][0]
        self.assertEqual((item["dirty"], item["unpushed"]), (False, 1))
        self.assertEqual([reason["code"] for reason in item["reasons"]], ["unpushed_commits"])
        status, body = self.reopen(project_id)
        self.assertEqual((status, body["code"]), (409, "needs_confirm"))
        self.assertEqual(body["worktrees"][0]["unpushed"], 1)
        self.assertTrue(os.path.isdir(worktree))
        status, body = self.reopen(project_id, True)
        self.assertEqual(status, 200, body)
        self.assertEqual(body["removed"][0]["branch_kept"], BRANCH)
        self.assertFalse(os.path.isdir(worktree))
        self.assertIn("loop work", run_git(repo, "log", BRANCH, "--format=%s").stdout)  # the commit is still on the branch

    def test_running_loop_blocks_check_and_reopen(self):
        repo, project_id = self.create_ready_project()
        worktree = self.make_loop_worktree(repo)
        self.write_live_loop_lock(worktree)
        checked = self.check(project_id)
        self.assertEqual((checked["can_reopen"], checked["blocked_by"]), (False, "loop_running"))
        self.assertFalse(checked["worktrees"][0]["can_remove"])
        status, body = self.reopen(project_id, True)
        self.assertEqual((status, body["ok"], body["code"]), (409, False, "loop_running"))
        self.assertTrue(os.path.isdir(worktree))
        self.assertTrue(self.get_planning(project_id)["approved"])

    def test_a_folder_the_app_did_not_create_is_never_removed(self):
        repo, project_id = self.create_ready_project()
        foreign = repo + "-worktree-loop"
        write_file(os.path.join(foreign, "mine.txt"), "the user's own files")  # a plain folder, not a git worktree
        checked = self.check(project_id)
        self.assertFalse(checked["can_reopen"])
        self.assertEqual(checked["worktrees"][0]["reasons"][0]["code"], "not_app_worktree")
        status, body = self.reopen(project_id, True)
        self.assertEqual((status, body["code"]), (409, "worktree_not_removable"))
        self.assertEqual(read_file(os.path.join(foreign, "mine.txt")), "the user's own files")
        self.assertTrue(self.get_planning(project_id)["approved"])

    def test_confirm_must_be_a_boolean(self):
        _, project_id = self.create_ready_project()
        status, body = self.request("POST", "/p/%s/api/planning/reopen" % project_id, {"confirm_remove_worktrees": "yes"})
        self.assertEqual((status, body["ok"]), (400, False))


if __name__ == "__main__":
    import unittest
    unittest.main()
