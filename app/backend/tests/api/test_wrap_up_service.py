"""WrapUpService on a real two-repo project (local bare remotes, real delivery and close scripts, no gh: LOOP_PR_MODE=link): the read
payload, Finish safe / keep_worktree / remove_anyway, the refusals (uncommitted files, typed confirmation, unreviewed critical, a running
loop, a running follow-up) and the receipt. The decisions service and the job service are fakes."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ENGINE_TESTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "engine")
sys.path.insert(0, ENGINE_TESTS_DIR)
from multi_repo_world import MultiRepoWorldTestCase, git, loop_state, write  # noqa: E402

from harness_backend.config.app_config import load_app_config  # noqa: E402
from harness_backend.domain.project import Project, ProjectContext  # noqa: E402
from harness_backend.domain.project_errors import InvalidRequestError, LoopRunningError, RepoConflictError  # noqa: E402
from harness_backend.infrastructure.git_client import GitClient  # noqa: E402
from harness_backend.infrastructure.process_runner import ProcessRunner  # noqa: E402
from harness_backend.infrastructure.structured_log import StructuredLog  # noqa: E402
from harness_backend.repositories.loop_command_file_repository import LoopCommandFileRepository  # noqa: E402
from harness_backend.repositories.loop_file_repository import LoopFileRepository  # noqa: E402
from harness_backend.services.engine_script_service import EngineScriptService  # noqa: E402
from harness_backend.services.loop_status_service import LoopStatusService  # noqa: E402
from harness_backend.services.loop_worktree_service import LoopWorktreeService  # noqa: E402
from harness_backend.services.wrap_up_service import WrapUpService  # noqa: E402


class FakeDecisions:
    def __init__(self, unreviewed=()):
        self.unreviewed = list(unreviewed)

    def unreviewed_critical(self, context):
        return self.unreviewed

    def open_counts(self, context):
        return {"open_critical": len(self.unreviewed), "open_important": 0}


class FakeJobs:
    def __init__(self, running=None):
        self.running = running

    def get_running_job(self, repo_path):
        return self.running


class WrapUpWorldTestCase(MultiRepoWorldTestCase):
    def setUp(self):
        super().setUp()
        os.environ["LOOP_PR_MODE"] = "link"  # the delivery pushes and makes a compare link; no gh, no network
        self.data = tempfile.mkdtemp(prefix="wrap-up-data-")
        self.addCleanup(shutil.rmtree, self.data, True)
        self.write_meta(dict(self.meta, turns=9, cost_usd=2.5, max_cost_usd=50, delivery={"push_pr": True, "remove_worktree_on_finish": True}))
        write(loop_state.state_file(self.primary_work, "LOOP.json"), json.dumps({"status": "done", "turn": 9, "at": "2026-10-20T10:00:00"}))
        config = load_app_config(user_data_dir=self.data, output_dir=os.path.join(self.data, "out"), open_browser=False)
        log = StructuredLog(os.path.join(self.data, "logs"))
        file_repository = LoopFileRepository()
        self.status_service = LoopStatusService(file_repository, None, log)
        runner = ProcessRunner()
        scripts = EngineScriptService(config, runner)
        command_repository = LoopCommandFileRepository()
        worktrees = LoopWorktreeService(config, log, GitClient(runner), scripts, command_repository, self.status_service)
        self.decisions, self.jobs = FakeDecisions(), FakeJobs()
        self.service = WrapUpService(worktrees, self.status_service, file_repository, command_repository, scripts, self.decisions, self.jobs,
                                     GitClient(runner), log)
        self.context = ProjectContext(Project("p1", "app", self.primary))

    def finish(self, **body):
        return self.service.finish(self.context, body)

    def deliver_in_process(self):
        """A delivery that already ran with a PR (gh shim): a local bare origin has no compare link, so a PR is what makes a repo closable."""
        from multi_repo_world import delivery
        self.install_gh_shim()
        os.environ.pop("LOOP_PR_MODE")  # "link" would skip the gh shim
        delivery.finalize_project(self.primary_work, self.read_json(loop_state.loop_path(self.primary_work, "meta.json")))
        os.environ["LOOP_PR_MODE"] = "link"

    def lock_loop(self):
        write(loop_state.loop_path(self.primary_work, "LOOP.lock"), str(os.getpid()))


class TestGetWrapUp(WrapUpWorldTestCase):
    def test_payload_keys_and_values(self):
        payload = self.service.get_wrap_up(self.context)
        self.assertEqual(payload["status"], "done")
        summary = payload["summary"]
        self.assertEqual((summary["features_done"], summary["cost_usd"], summary["max_cost_usd"], summary["turns"]), (1, 2.5, 50, 9))
        self.assertEqual(payload["delivery"], {"push_pr": True, "remove_worktree_on_finish": True, "done": False, "integration_branch": "developer"})
        self.assertEqual(payload["unreviewed_critical"], 0)
        by_repo = {item["repo"]: item for item in payload["check"]["repos"]}
        self.assertEqual(sorted(by_repo), ["api", "app"])
        self.assertFalse(by_repo["api"]["pushed"])
        self.assertTrue(by_repo["api"]["unpushed"])

    def test_old_runs_without_delivery_default_to_deliver_and_remove(self):
        self.write_meta(dict(self.meta, turns=1))
        self.assertEqual(self.service.get_wrap_up(self.context)["delivery"]["remove_worktree_on_finish"], True)


class TestEffectiveCap(WrapUpWorldTestCase):
    def cap(self):
        return self.service.get_wrap_up(self.context)["summary"]["max_cost_usd"]

    def test_the_cap_falls_back_to_the_saved_command_then_to_50(self):
        self.write_meta(dict(self.meta, turns=9, cost_usd=2.5))
        self.assertEqual(self.cap(), 50)
        write(loop_state.loop_path(self.primary_work, "LOOP-CMD.json"), json.dumps({"argv": ["run_loop.py", "repo", "--max-cost", "12"]}))
        self.assertEqual(self.cap(), 12)
        self.write_meta(dict(self.meta, turns=9, cost_usd=2.5, max_cost_usd=30))
        self.assertEqual(self.cap(), 30)

    def test_the_receipt_and_last_run_carry_the_effective_cap(self):
        self.write_meta(dict(self.meta, turns=9, cost_usd=2.5, delivery={"push_pr": True, "remove_worktree_on_finish": True}))
        write(loop_state.loop_path(self.primary_work, "LOOP-CMD.json"), json.dumps({"argv": ["run_loop.py", "repo", "--max-cost=12"]}))
        result = self.finish(mode="safe")
        self.assertEqual(result["receipt"]["max_cost_usd"], 12)
        self.assertEqual(self.read_json(loop_state.loop_path(self.primary, "last-run.json"))["max_cost_usd"], 12)


class TestFinishSafe(WrapUpWorldTestCase):
    def test_delivers_then_closes_and_keeps_the_branch(self):
        self.deliver_in_process()
        result = self.finish(mode="safe", accept_unreviewed=False)
        receipt = result["receipt"]
        self.assertTrue(result["ok"])
        self.assertTrue(receipt["worktree_removed"])
        self.assertEqual((receipt["branch"], receipt["cost_usd"], receipt["max_cost_usd"], receipt["turns"]), ("loop/app", 2.5, 50, 9))
        self.assertTrue(receipt["pr_url"] and os.path.isdir(receipt["backup_dir"]))
        for name in ("app", "api"):
            self.assertFalse(os.path.isdir(self.worktrees[name]))
            self.assertIn("loop/" + name, git(self.repos[name], "branch", "--list", "loop/" + name))
            self.assertIn("loop/" + name, git(self.remotes[name], "branch", "--list"))
        self.assertEqual(self.read_json(loop_state.loop_path(self.primary, "last-run.json"))["cost_usd"], 2.5)

    def test_an_uncommitted_file_refuses_and_lists_it(self):
        self.deliver_in_process()
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        with self.assertRaises(RepoConflictError) as caught:
            self.finish(mode="safe")
        reasons = caught.exception.extras["reasons"]
        self.assertEqual([(item["repo"], item["kind"], item["files"]) for item in reasons], [("api", "uncommitted", ["unsaved.txt"])])
        self.assertTrue(os.path.isdir(self.worktrees["api"]) and os.path.isdir(self.worktrees["app"]))

    def test_a_pushed_branch_with_no_pr_closes_without_force_and_keeps_the_branch(self):
        """Local bare origin: the delivery pushes, no PR and no compare link exist. That is a note, not a blocker."""
        result = self.finish(mode="safe")
        self.assertTrue(result["receipt"]["worktree_removed"])
        self.assertTrue(os.path.isdir(result["receipt"]["backup_dir"]))
        for name in ("app", "api"):
            self.assertFalse(os.path.isdir(self.worktrees[name]))
            self.assertIn("loop/" + name, git(self.remotes[name], "branch", "--list"))
            self.assertIn("loop/" + name, git(self.repos[name], "branch", "--list", "loop/" + name))
        self.assertIsNone(result["receipt"]["pr_url"])

    def test_the_check_reports_a_missing_pr_as_a_non_blocking_note(self):
        self.finish(mode="keep_worktree")  # pushes; the local origin gives no PR and no link
        repos = {item["repo"]: item for item in self.service.get_wrap_up(self.context)["check"]["repos"]}
        self.assertTrue(repos["app"]["pushed"])
        self.assertEqual([(note["kind"], note["blocking"]) for note in repos["app"]["notes"]], [("pr_missing", False)])

    def test_remove_worktree_off_only_delivers(self):
        self.write_meta(dict(self.meta, turns=9, delivery={"push_pr": True, "remove_worktree_on_finish": False}))
        result = self.finish(mode="safe")
        self.assertFalse(result["receipt"]["worktree_removed"])
        self.assertTrue(os.path.isdir(self.worktrees["api"]))
        self.assertIn("loop/api", git(self.remotes["api"], "branch", "--list"))


class TestFinishOtherModes(WrapUpWorldTestCase):
    def test_keep_worktree_delivers_and_keeps_everything(self):
        result = self.finish(mode="keep_worktree")
        self.assertEqual((result["receipt"]["worktree_removed"], result["receipt"]["backup_dir"]), (False, None))
        self.assertTrue(os.path.isdir(self.worktrees["app"]))
        self.assertIn("loop/app", git(self.remotes["app"], "branch", "--list"))

    def test_remove_anyway_needs_the_repo_folder_name(self):
        for text in ("", "wrong"):
            with self.assertRaises(InvalidRequestError):
                self.finish(mode="remove_anyway", confirm_text=text)
        self.assertTrue(os.path.isdir(self.worktrees["app"]))

    def test_remove_anyway_backs_up_then_removes_dirty_work(self):
        write(os.path.join(self.worktrees["api"], "unsaved.txt"), "dirty\n")
        result = self.finish(mode="remove_anyway", confirm_text="app")
        self.assertTrue(result["receipt"]["worktree_removed"])
        self.assertTrue(os.path.isdir(result["receipt"]["backup_dir"]))
        self.assertFalse(os.path.isdir(self.worktrees["api"]))
        self.assertIn("loop/api", git(self.repos["api"], "branch", "--list", "loop/api"))
        self.assertNotIn("loop/app", git(self.remotes["app"], "branch", "--list"))  # no delivery in this mode

    def test_a_bad_mode_is_a_400(self):
        with self.assertRaises(InvalidRequestError):
            self.finish(mode="delete")


class TestRefusals(WrapUpWorldTestCase):
    def test_unreviewed_critical_blocks_until_accepted(self):
        self.decisions.unreviewed = [{"id": "abc"}]
        with self.assertRaises(RepoConflictError) as caught:
            self.finish(mode="keep_worktree")
        self.assertEqual(caught.exception.extras["reasons"][0]["kind"], "unreviewed_critical")
        self.assertEqual(self.service.get_wrap_up(self.context)["unreviewed_critical"], 1)
        self.assertTrue(self.finish(mode="keep_worktree", accept_unreviewed=True)["ok"])

    def test_a_running_loop_refuses(self):
        self.lock_loop()
        with self.assertRaises(LoopRunningError):
            self.finish(mode="safe")

    def test_a_running_follow_up_refuses(self):
        self.jobs.running = {"id": "j-1", "type": "follow_up"}
        with self.assertRaises(RepoConflictError) as caught:
            self.finish(mode="safe")
        self.assertIn("follow-up round is running", str(caught.exception))
        self.assertTrue(os.path.isdir(self.worktrees["app"]))

    def test_no_worktree_refuses(self):
        context = ProjectContext(Project("p2", "x", os.path.join(self.root, "nowhere")))
        with self.assertRaises(RepoConflictError):
            self.service.finish(context, {"mode": "safe"})


if __name__ == "__main__":
    unittest.main()
