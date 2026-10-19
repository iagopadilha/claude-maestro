"""Workspace of a project: per-repo states, the create job (engine step lines -> steps/result) and the read-only loop-start job
parsed from the lines after the LAST `[loop-launch]` marker of monitor-relaunch.log. Fake engine adapter, no real worktree creation."""
import os
import unittest

from harness_backend.domain.project_errors import InvalidRepoError, JobInProgressError, RepoConflictError
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services import workspace_service
from harness_backend.services.claude_job_service import JOB_TYPE_WORKSPACE_CREATE
from harness_backend.services.loop_control_service import LAUNCH_MARKER_PREFIX
from harness_backend.services.workspace_service import WorkspaceService, parse_step_line

from .support_multi_repo import MultiRepoTestCase
from .support_projects import delete_folder, loop_file_path, run_git, write_file


class InlineJobService:
    """claude_job_service stand-in: start_task_job runs the task right away in the calling thread."""

    def __init__(self):
        self.jobs = {}
        self.running = None
        self.refuse = False

    def start_task_job(self, repo_path, job_type, task):
        if self.refuse:
            raise JobInProgressError("a job is already running in this project: wait for it to finish")
        steps = []
        outcome = task(steps.append)
        job_id = "j-%d" % (len(self.jobs) + 1)
        self.jobs[job_id] = {"id": job_id, "type": job_type, "state": "done" if outcome["ok"] else "error", "steps": steps,
                             "result": outcome["result"], "error": outcome["error"]}
        return {"id": job_id}

    def get_running_job(self, repo_path):
        return self.running

    def get_job(self, repo_path, job_id):
        return self.jobs[job_id]


class FakeEngineAdapter:
    """Prints the given engine lines for every call and records what it was asked to create."""

    def __init__(self, lines=(), failure=None):
        self.lines, self.failure, self.calls = list(lines), failure, []

    def run(self, primary_repo, repo_names, log_path, on_output_line, timeout_seconds=0):
        self.calls.append(list(repo_names))
        for line in self.lines:
            on_output_line(line)
        return self.failure


class TestWorkspaceService(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.context = self.make_context(self.primary)
        self.job_service = InlineJobService()
        self.adapter = FakeEngineAdapter()
        self.start_in_progress = False
        self.workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client,
                                          self.job_service, self.adapter, self.log, start_in_progress_provider=lambda context: self.start_in_progress)

    def add_sibling(self, name="api"):
        sibling = self.make_repo(name)
        self.registry.add_repo(self.context, sibling, [])
        return sibling

    def states(self):
        return {repo["name"]: repo["state"] for repo in self.workspace.get_workspace(self.context)["repos"]}

    def make_worktree(self, repo_path):
        worktree = repo_path + "-worktree-loop"
        run_git(repo_path, "worktree", "add", "-q", "-b", "loop/test", worktree)
        return worktree

    # ------------------------------------------------ states
    def test_single_repo_project_is_not_created_then_ready(self):
        self.assertEqual(self.states(), {"app": "not_created"})
        self.make_worktree(self.primary)
        workspace = self.workspace.get_workspace(self.context)
        self.assertFalse(workspace["multi"])
        self.assertEqual(workspace["repos"][0]["state"], "ready")
        self.assertEqual(workspace["repos"][0]["branch"], "loop/test")
        self.assertIsNone(workspace["job"])

    def test_every_state(self):
        sibling = self.add_sibling("api")
        write_file(os.path.join(self.primary, "pending.txt"), "uncommitted\n")
        workspace = self.workspace.get_workspace(self.context)
        by_name = {repo["name"]: repo for repo in workspace["repos"]}
        self.assertEqual(by_name["app"]["state"], "not_created")
        self.assertTrue(by_name["app"]["dirty"])
        self.assertEqual(by_name["app"]["notes"], [workspace_service.UNCOMMITTED_NOTE])
        self.assertTrue(workspace["multi"])
        worktree = self.make_worktree(sibling)
        self.assertEqual(self.states()["api"], "ready")
        write_file(os.path.join(worktree, "work.txt"), "in progress\n")
        self.assertEqual(self.states()["api"], "dirty")
        write_file(os.path.join(worktree, ".specs", "x.md"), "plan state only\n")  # .specs changes alone never count
        os.remove(os.path.join(worktree, "work.txt"))
        self.assertEqual(self.states()["api"], "ready")
        self.write_live_loop_lock(self.primary)
        self.assertEqual(self.states()["api"], "loop_running")
        os.remove(loop_file_path(self.primary, "internal", "LOOP.lock"))
        run_git(self.primary, "worktree", "prune")
        delete_folder(sibling)
        self.assertEqual(self.states()["api"], "missing")

    # ------------------------------------------------ step lines
    def test_parse_step_line_tagged_and_legacy_forms(self):
        self.assertEqual(parse_step_line("[loop] [api] worktree-add: creating", "app"), {"repo": "api", "kind": "worktree-add", "text": "creating"})
        self.assertEqual(parse_step_line("[loop] check: ok", "app")["repo"], "app")
        self.assertEqual(parse_step_line("[loop] reusing worktree C:/w on branch loop/a", "app")["kind"], "reuse")
        ready = parse_step_line("[loop] worktree created: C:/w (branch loop/a, base developer; from HEAD)", "app")
        self.assertEqual((ready["kind"], ready["text"]), ("ready", "path=C:/w branch=loop/a base=developer"))
        self.assertEqual(parse_step_line("[loop] one-time setup: npm ci", "app"), {"repo": "app", "kind": "setup", "text": "npm ci"})
        self.assertIsNone(parse_step_line("[loop] turn 1 starting", "app"))
        self.assertIsNone(parse_step_line("random output", "app"))

    # ------------------------------------------------ create job
    def test_create_job_collects_steps_and_result_from_engine_lines(self):
        self.add_sibling("api")
        self.adapter.lines = ["[loop] [app] ready: path=/w/app branch=loop/x base=developer", "[loop] [api] reattach: loop/x",
                              "[loop] [api] copy: .specs, .env, CLAUDE.md", "[loop] [api] ready: path=/w/api branch=loop/x base=main", "noise"]
        job_id = self.workspace.create_workspace(self.context, None)["job_id"]
        job = self.workspace.get_create_job(self.context, job_id)["job"]
        self.assertEqual(job["state"], "done")
        self.assertEqual(self.adapter.calls, [["app", "api"]])
        self.assertEqual([step["kind"] for step in job["steps"]], ["ready", "reattach", "copy", "ready"])
        api = job["result"]["repos"]["api"]
        self.assertEqual((api["worktree"], api["branch"], api["base"], api["reattached"], api["copied"]), ("/w/api", "loop/x", "main", True, [".env"]))

    def test_create_job_fails_when_a_repo_was_not_reported(self):
        self.add_sibling("api")
        self.adapter.lines = ["[loop] [app] ready: path=/w/app branch=loop/x base=developer", "[loop] [api] error: no space"]
        job = self.workspace.get_create_job(self.context, self.workspace.create_workspace(self.context, ["app", "api"])["job_id"])["job"]
        self.assertEqual(job["state"], "error")
        self.assertIn("not created: api", job["error"])

    def test_create_job_skips_an_existing_worktree_with_a_reuse_step(self):
        self.make_worktree(self.primary)
        job = self.workspace.get_create_job(self.context, self.workspace.create_workspace(self.context, ["app"])["job_id"])["job"]
        self.assertEqual(job["state"], "done")
        self.assertEqual([step["kind"] for step in job["steps"]], ["reuse"])
        self.assertEqual(self.adapter.calls, [])  # the engine is never asked to recreate it

    def test_create_job_refusals(self):
        with self.assertRaises(InvalidRepoError) as unknown:
            self.workspace.create_workspace(self.context, ["ghost"])
        self.assertEqual(unknown.exception.code, workspace_service.UNKNOWN_REPO_CODE)
        with self.assertRaises(InvalidRepoError):
            self.workspace.create_workspace(self.context, "app")
        self.job_service.refuse = True
        with self.assertRaises(RepoConflictError) as busy:
            self.workspace.create_workspace(self.context, None)
        self.assertEqual(busy.exception.code, workspace_service.BUSY_CODE)
        self.job_service.refuse = False
        self.write_live_loop_lock(self.primary)
        with self.assertRaises(RepoConflictError):
            self.workspace.create_workspace(self.context, None)

    # ------------------------------------------------ loop-start job (read from monitor-relaunch.log)
    def write_relaunch_log(self, lines):
        write_file(loop_file_path(self.primary, "internal", "monitor-relaunch.log"), "\n".join(lines) + "\n")

    def test_loop_start_job_reads_only_after_the_last_launch_marker(self):
        self.write_live_loop_lock(self.primary)
        self.write_relaunch_log(["[loop-launch] 2026-10-19T10:00:00 start", "[loop] [app] worktree-add: OLD launch", "[loop] [app] error: OLD failure",
                                 "[loop-launch] 2026-10-19T11:00:00 resume", "[loop] [app] check: current launch", "[loop] [app] worktree-add: creating"])
        job = self.workspace.get_workspace(self.context)["job"]
        self.assertEqual(job["id"], workspace_service.LOOP_START_JOB_ID)
        self.assertEqual(job["state"], "running")
        self.assertEqual([step["text"] for step in job["steps"]], ["current launch", "creating"])
        self.assertEqual(self.workspace.get_create_job(self.context, "loop-start")["job"]["id"], "loop-start")

    def test_loop_start_job_reports_an_error_step_and_ends_at_the_first_turn(self):
        self.write_live_loop_lock(self.primary)
        self.write_relaunch_log(["[loop-launch] x start", "[loop] [app] worktree-add: creating", "[loop] [app] error: disk full"])
        job = self.workspace.get_workspace(self.context)["job"]
        self.assertEqual((job["state"], job["error"]), ("error", "disk full"))
        self.write_relaunch_log(["[loop-launch] x start", "[loop] [app] worktree-add: creating", "[loop] turn 1 starting"])
        self.assertIsNone(self.workspace.get_workspace(self.context)["job"])

    def test_loop_start_job_without_marker_reads_the_whole_tail_and_needs_a_live_loop(self):
        self.write_relaunch_log(["[loop] [app] worktree-add: creating"])
        self.assertIsNone(self.workspace.get_workspace(self.context)["job"])  # nobody alive
        self.start_in_progress = True  # launched by the app, lock not written yet
        self.assertEqual(len(self.workspace.get_workspace(self.context)["job"]["steps"]), 1)
        from harness_backend.domain.project_errors import JobNotFoundError
        self.start_in_progress = False
        with self.assertRaises(JobNotFoundError):
            self.workspace.get_create_job(self.context, "loop-start")

    def test_the_launch_marker_line_is_what_the_loop_control_service_writes(self):
        self.assertEqual(LAUNCH_MARKER_PREFIX, "[loop-launch]")


if __name__ == "__main__":
    unittest.main()
