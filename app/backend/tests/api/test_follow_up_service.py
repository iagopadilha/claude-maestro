"""FollowUpService and FollowUpController: the `follow_up` claude job (fake claude: `--model sonnet`, worktree cwd, budget 1 / max 5), the
finalize that accepts only an appended `Follow-up <n>` stage (+ FLOWS additions) and otherwise restores both files, the draft, `run` (a new
round through the injected loop control with the delivery choices of meta) and the refusals with a loop or a job running."""
import json
import os
import shutil
import tempfile
import time
import unittest

from harness_backend.api.controllers.follow_up_controller import FollowUpController
from harness_backend.api.router import Request, Router, normalize_response
from harness_backend.domain.project import Project, ProjectContext
from harness_backend.domain.project_errors import InvalidRequestError, JobInProgressError, LoopRunningError, RepoConflictError
from harness_backend.engine import decisions as decisions_engine
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.repositories.specs_file_repository import SpecsFileRepository, loop_state
from harness_backend.services.claude_job_service import ClaudeJobService
from harness_backend.services.follow_up_service import FollowUpService

ROADMAP = """# Roadmap

**E2E:** no

## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base title="Base"
"""
FLOWS = "# Flows\n\n- base: the base flow\n"
NEW_STAGE = ('\n## Stage Follow-up-1 - Follow-up 1\n\n'
             '- [extra-report] status=pending scope=Small kind=back paths=src/report deps=base title="Extra report"\n')
NEW_FLOW = "- extra-report: the report flow\n"
DECISION_LINE = "- [base] turn=2 level=critical Which store? | chose: sqlite | revert: switch the driver"


class FakeClaudeClient(ClaudeClient):
    """The real argv builder; `run_prompt` runs `behavior(folder)` instead of claude and records what it was given."""

    def __init__(self):
        super().__init__(ProcessRunner(), claude_binary="claude-fake")
        self.behavior = lambda folder: "done"
        self.calls = []

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        self.calls.append({"argv": argv, "folder": folder, "stdin": stdin_text})
        reply = self.behavior(folder)
        return {"ok": True, "reply": reply, "cost_usd": 0.1, "session_id": "", "error": ""}


class FakeLoopStatus:
    def __init__(self, work):
        self.work, self.running, self.meta = work, False, {"turns": 3, "delivery": {"push_pr": False, "remove_worktree_on_finish": False}}

    def is_loop_running(self, context):
        return self.running

    def get_loop_summary(self, context):
        return {"status": "done", "meta": self.meta}


class FakeLoopControl:
    def __init__(self):
        self.bodies = []
        self.delivery = {"push_pr": False, "remove_worktree_on_finish": False}

    def read_delivery(self, context):
        return self.delivery

    def start_new_loop(self, context, body):
        self.bodies.append(body)
        return 202, {"ok": True}


class FollowUpTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="follow-up-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "project")
        self.work = self.repo + "-worktree-loop"
        os.makedirs(self.repo)
        self.write(self.path("ROADMAP.md"), ROADMAP)
        self.write(self.path("FLOWS.md"), FLOWS)
        self.write(loop_state.loop_path(self.work, "meta.json"), json.dumps({"turns": 3}))
        specs = SpecsFileRepository()
        log = StructuredLog(os.path.join(self.tmp, "logs"))
        self.client, self.status, self.control = FakeClaudeClient(), FakeLoopStatus(self.work), FakeLoopControl()
        self.jobs = ClaudeJobService(self.client, specs, log)
        self.service = FollowUpService(specs, self.jobs, self.client, WorkspaceFileGuard(), self.status, self.control, log)
        self.context = ProjectContext(Project("p1", "project", self.repo))

    def path(self, name):
        return os.path.join(self.work, ".specs", "project", name)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def read(self, path):
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def send(self, **body):
        job = self.service.send_follow_up(self.context, dict({"text": "add a report"}, **body))["job"]
        return self.wait_for(job["id"])

    def wait_for(self, job_id):
        """Waits on the in-memory registry and reads the record once: polling the file while the job thread replaces it can make the
        replace fail on Windows (a race of the job service itself, not of the follow-up code)."""
        for _ in range(400):
            if job_id not in self.jobs.running_jobs:
                return self.jobs.get_job(self.repo, job_id)
            time.sleep(0.025)
        self.fail("the job never finished")

    def append_stage(self, folder):
        for name, text in (("ROADMAP.md", NEW_STAGE), ("FLOWS.md", NEW_FLOW)):
            with open(os.path.join(folder, ".specs", "project", name), "a", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
        return "added extra-report"


class TestSend(FollowUpTestCase):
    def test_an_appended_stage_is_accepted_and_the_job_is_pinned(self):
        self.client.behavior = self.append_stage
        job = self.send()
        self.assertEqual(job["state"], "ok", job)
        call = self.client.calls[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1")
        self.assertEqual(os.path.normcase(call["folder"]), os.path.normcase(self.work))
        self.assertNotIn("Write", argv[argv.index("--allowedTools"):argv.index("--disallowedTools")])
        self.assertIn("Follow-up-1", call["stdin"])
        self.assertIn("extra-report", self.read(self.path("ROADMAP.md")))
        state = self.service.get_follow_up(self.context)
        self.assertEqual(state["stage"]["name"], "Follow-up 1")
        self.assertEqual([item["slug"] for item in state["stage"]["features"]], ["extra-report"])
        self.assertEqual(state["last_job"]["id"], job["id"])

    def test_editing_an_earlier_stage_restores_the_files_and_fails_the_job(self):
        def behavior(folder):
            self.append_stage(folder)
            self.write(self.path("ROADMAP.md"), self.read(self.path("ROADMAP.md")).replace("status=verified", "status=pending"))
            return "oops"
        self.client.behavior = behavior
        job = self.send()
        self.assertEqual(job["state"], "error")
        self.assertIn("earlier part of the ROADMAP", job["error"])
        self.assertEqual((self.read(self.path("ROADMAP.md")), self.read(self.path("FLOWS.md"))), (ROADMAP, FLOWS))

    def test_editing_an_earlier_flows_line_is_reverted(self):
        def behavior(folder):
            self.append_stage(folder)
            self.write(self.path("FLOWS.md"), "# Flows\n\n- base: CHANGED\n" + NEW_FLOW)
            return "oops"
        self.client.behavior = behavior
        job = self.send()
        self.assertEqual(job["state"], "error")
        self.assertEqual(self.read(self.path("FLOWS.md")), FLOWS)
        self.assertEqual(self.read(self.path("ROADMAP.md")), ROADMAP)

    def test_a_wrong_stage_number_or_no_stage_is_reverted(self):
        self.client.behavior = lambda folder: self.append_stage(folder) and self.write(
            self.path("ROADMAP.md"), ROADMAP + NEW_STAGE.replace("Follow-up-1", "Follow-up-4").replace("Follow-up 1", "Follow-up 4"))
        self.assertEqual(self.send()["state"], "error")
        self.assertEqual(self.read(self.path("ROADMAP.md")), ROADMAP)
        self.client.behavior = lambda folder: "nothing"
        self.assertEqual(self.send()["state"], "error")

    def test_a_reused_slug_is_reverted(self):
        self.client.behavior = lambda folder: self.write(
            self.path("ROADMAP.md"), ROADMAP + NEW_STAGE.replace("extra-report", "base"))
        self.assertEqual(self.send()["state"], "error")
        self.assertEqual(self.read(self.path("ROADMAP.md")), ROADMAP)

    def test_a_file_outside_the_two_documents_is_undone(self):
        def behavior(folder):
            self.append_stage(folder)
            self.write(os.path.join(folder, "src", "code.py"), "x = 1\n")
            return "ok"
        self.client.behavior = behavior
        self.assertEqual(self.send()["state"], "ok")
        self.assertFalse(os.path.exists(os.path.join(self.work, "src", "code.py")))

    def test_the_chosen_decisions_go_into_the_prompt(self):
        self.write(decisions_engine.validate_path(self.work), DECISION_LINE + "\n")
        decision_id = decisions_engine.parse(DECISION_LINE)[0][0]["id"]
        self.client.behavior = self.append_stage
        self.send(text="", decisions=[decision_id])
        self.assertIn("Which store?", self.client.calls[0]["stdin"])
        with self.assertRaises(InvalidRequestError):
            self.service.send_follow_up(self.context, {"text": "x", "decisions": ["nope"]})

    def test_the_draft_text_is_kept_on_failure_cleared_on_success_and_decisions_survive(self):
        draft_path = os.path.join(self.work, ".specs", "project", "loop", "internal", "follow-up-draft.json")
        self.write(draft_path, json.dumps({"text": "", "decisions": ["abc"]}))
        self.client.behavior = lambda folder: "nothing appended"
        self.send(text="keep me")
        self.assertEqual(json.loads(self.read(draft_path)), {"text": "keep me", "decisions": ["abc"]})
        self.client.behavior = self.append_stage
        self.send(text="keep me")
        self.assertEqual(json.loads(self.read(draft_path)), {"text": "", "decisions": ["abc"]})
        self.assertEqual(self.service.get_follow_up(self.context)["draft"], {"text": "", "decisions": ["abc"]})

    def test_validation(self):
        for body in ({"text": ""}, {"text": "x", "budget_usd": 6}, {"text": "x", "budget_usd": 0}, {"text": 5}, {"text": "x", "decisions": "a"}):
            with self.assertRaises(InvalidRequestError):
                self.service.send_follow_up(self.context, body)
        self.client.behavior = self.append_stage
        self.send(budget_usd=5)
        self.assertEqual(self.client.calls[0]["argv"][self.client.calls[0]["argv"].index("--max-budget-usd") + 1], "5")


class TestRefusals(FollowUpTestCase):
    def test_loop_running_job_running_and_no_worktree(self):
        self.status.running = True
        with self.assertRaises(LoopRunningError):
            self.service.send_follow_up(self.context, {"text": "x"})
        with self.assertRaises(LoopRunningError):
            self.service.run_follow_up(self.context)
        self.status.running = False
        release = []
        self.client.behavior = lambda folder: (time.sleep(0.5), release.append(1))
        job = self.service.send_follow_up(self.context, {"text": "x"})["job"]
        with self.assertRaises(JobInProgressError):
            self.service.send_follow_up(self.context, {"text": "y"})
        with self.assertRaises(JobInProgressError):
            self.service.run_follow_up(self.context)
        self.wait_for(job["id"])
        shutil.rmtree(self.work)
        with self.assertRaises(RepoConflictError):
            self.service.send_follow_up(self.context, {"text": "x"})


class TestRun(FollowUpTestCase):
    def test_run_without_a_stage_is_refused(self):
        with self.assertRaises(RepoConflictError):
            self.service.run_follow_up(self.context)
        self.assertEqual(self.control.bodies, [])

    def test_run_starts_a_round_with_the_delivery_choices_and_renews_the_approval(self):
        self.client.behavior = self.append_stage
        self.send()
        self.assertFalse(loop_state.roadmap_approval(self.work)["approved"])
        status, payload = self.service.run_follow_up(self.context)
        self.assertEqual((status, payload), (202, {"ok": True}))
        self.assertEqual(self.control.bodies, [{"push": False, "remove_worktree_on_finish": False}])
        self.assertTrue(loop_state.roadmap_approval(self.work)["approved"])

    def test_run_defaults_to_push_and_remove_for_old_runs(self):
        self.client.behavior = self.append_stage
        self.send()
        self.control.delivery = {"push_pr": True, "remove_worktree_on_finish": True}
        self.service.run_follow_up(self.context)
        self.assertEqual(self.control.bodies, [{"push": True, "remove_worktree_on_finish": True}])


class TestController(FollowUpTestCase):
    def setUp(self):
        super().setUp()
        context = self.context

        class Projects:
            def get_project_context(self, project_id):
                return context
        self.controller = FollowUpController(Projects(), self.service)

    def call(self, handler, method, body=None):
        return normalize_response(handler(Request(method, "/p/p1/api/follow-up", {"project_id": "p1"}, {}, body or {})))

    def test_routes_are_registered(self):
        router = Router()
        self.controller.register_routes(router)
        for method, path in (("GET", "follow-up"), ("POST", "follow-up/send"), ("POST", "follow-up/run")):
            self.assertIsNotNone(router.find_route(method, "/p/p1/api/" + path)[0])

    def test_get_send_run_and_the_refusals_map_to_statuses(self):
        self.assertEqual(self.call(self.controller.get_follow_up, "GET").status, 200)
        self.assertEqual(self.call(self.controller.send_follow_up, "POST", {"text": ""}).status, 400)
        self.client.behavior = self.append_stage
        response = self.call(self.controller.send_follow_up, "POST", {"text": "add"})
        self.assertEqual((response.status, response.body["ok"], response.body["job"]["type"]), (202, True, "follow_up"))
        self.wait_for(response.body["job"]["id"])
        self.assertEqual(self.call(self.controller.run_follow_up, "POST").status, 202)
        self.status.running = True
        self.assertEqual(self.call(self.controller.run_follow_up, "POST").status, 409)
        self.assertEqual(self.call(self.controller.send_follow_up, "POST", {"text": "x"}).status, 409)


if __name__ == "__main__":
    unittest.main()
