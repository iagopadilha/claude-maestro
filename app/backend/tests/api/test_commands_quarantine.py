"""Quarantine of a feature (`/p/<id>/api/quarantine/<slug>...`) and the `error` / `quarantine` chat scopes: detail (attempts, failing tests,
uncommitted files, skip impact, summary), the read-only AI summary job, retry/skip through the loop's answer mechanics and revert of listed
files. Fake claude and fake run_loop only; git runs for real in a temporary work folder."""
import json
import os
import re
import subprocess
import time
from types import SimpleNamespace

from harness_backend.repositories.claude_transcript_repository import ClaudeTranscriptRepository
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.agent_history_service import AgentHistoryService
from harness_backend.services.error_center_service import ErrorCenterService
from harness_backend.services.pipeline_service import PipelineService
from harness_backend.services.quarantine_module import build_quarantine_module
from harness_backend.services.roadmap_view_service import RoadmapViewService

from .support_commands import CommandsTestCase, FakeLoopStatusService
from .test_commands_chat import FAKE_CHAT_CLAUDE, assistant_row, tool_use, user_row

SESSION = "sess-turn-8"
VERIFIER_RETURN = ("FEATURE: service-c\nVERDICT: FAIL\nSUMMARY: 2 tests failed\n"
                   "FAILED tests/test_wave.py::test_x - AssertionError: assert 1 == 2\n"
                   "FAILED tests/test_wave.py::test_y - AssertionError: expected 5 but got 3")
PAUSE_OPTIONS = ["try the feature again", "skip the feature and carry on with the others"]


class QuarantineTests(CommandsTestCase):
    def setUp(self):
        super().setUp()
        self.setup_work_repository()
        self.setup_verifier_attempt()

    # ------------------------------------------------ world
    def git(self, *args):
        return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", self.fixture.work] + list(args), capture_output=True,
                              text=True, check=True).stdout.strip()

    def setup_work_repository(self):
        work = self.fixture.work
        self.fixture.write(os.path.join(work, "src", "c.py"), "original\n")
        self.git("init")
        self.git("add", "-A")
        self.git("commit", "-m", "base")
        self.fixture.write(os.path.join(work, "src", "c.py"), "edited by the failing executor\n")
        self.fixture.write(os.path.join(work, "src", "new.py"), "left behind\n")
        self.fixture.write(os.path.join(self.fixture.project_dir, "STATE.md"), "state changed after the commit\n")  # .specs is never listed

    def setup_verifier_attempt(self):
        fixture = self.fixture
        self.projects = os.path.join(fixture.tmp, "claude-projects")
        slug_dir = os.path.join(self.projects, re.sub(r"[^A-Za-z0-9]", "-", fixture.work))
        fixture.write_json("turns/008.json", {"session_id": SESSION, "total_cost_usd": 0.5, "duration_ms": 9000, "result": "verifier failed"})
        rows = [user_row("2026-10-15T15:00:00.000Z", "Role: verifier\nSCOPE_FEATURE: service-c\nRun the tests."),
                assistant_row("2026-10-15T15:00:05.000Z", "msg_1", [tool_use("tu_1", "SubagentHandback", message=VERIFIER_RETURN)], stop="tool_use")]
        subagents = os.path.join(slug_dir, SESSION, "subagents")
        os.makedirs(subagents, exist_ok=True)
        with open(os.path.join(subagents, "agent-ver111.jsonl"), "w", encoding="utf-8", newline="\n") as handle:
            handle.write("".join(json.dumps(row) + "\n" for row in rows))
        fixture.write(os.path.join(subagents, "agent-ver111.meta.json"), json.dumps({"agentType": "verifier"}))

    def prepare(self, mode="ok"):
        self.stub_dir = os.path.join(self.fixture.tmp, "stub")
        self.fixture.write(os.path.join(self.stub_dir, "fake_claude.py"), FAKE_CHAT_CLAUDE)
        self.fixture.write(os.path.join(self.stub_dir, "mode.txt"), mode)
        self.fixture.claude_binary = os.path.join(self.stub_dir, "fake_claude.py")
        module = self.fixture.start(transcripts=self.projects)
        repository, status = LoopFileRepository(), FakeLoopStatusService()
        transcripts = ClaudeTranscriptRepository(self.projects, self.fixture.log)
        pipeline = PipelineService(status, repository, RoadmapViewService(repository), AgentHistoryService(repository, transcripts, self.fixture.log),
                                   self.fixture.log)
        read_module = SimpleNamespace(loop_status_service=status, loop_file_repository=repository, pipeline_service=pipeline,
                                      claude_transcript_repository=transcripts, error_center_service=ErrorCenterService(status, repository))
        self.quarantine = build_quarantine_module(self.fixture.config, self.fixture.log, self.fixture.project_service, read_module, module,
                                                  module.controllers[0].planning_requirement_service)
        for controller in self.quarantine.controllers:
            controller.register_routes(self.fixture.server.router)
        return module

    def pause(self):
        self.fixture.set_loop("awaiting_user", feature="service-c", options=PAUSE_OPTIONS,
                              reason="Quarantine after 2 failures: service-c. The loop paused for you to decide.")

    def records(self):
        path = os.path.join(self.stub_dir, "record.jsonl")
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def wait_for_job(self, job, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            status, data = self.fixture.call("/api/chat/job/" + job)
            self.assertEqual(status, 200)
            if data["state"] != "running":
                return data
            time.sleep(0.1)
        self.fail("the job did not finish")

    # ------------------------------------------------ detail
    def test_detail_of_a_blocked_feature(self):
        self.prepare()
        self.pause()
        status, detail = self.fixture.call("/api/quarantine/service-c")
        self.assertEqual(status, 200, detail)
        self.assertEqual((detail["id"], detail["failures"], detail["step"], detail["step_agent"], detail["turn"]), ("service-c", 2, 4, "verifier", 8))
        self.assertTrue(detail["reason"].startswith("Quarantine after 2 failures: service-c"))
        attempt, = detail["attempts"]
        self.assertEqual((attempt["turn"], attempt["agent"], attempt["status"], attempt["agent_id"]), (8, "verifier", "FAIL", "v008:ver111"))
        self.assertEqual(attempt["out"], "2 tests failed")
        names = {test["name"]: test for test in detail["failed_tests"]}
        self.assertEqual((names["tests/test_wave.py::test_x"]["got"], names["tests/test_wave.py::test_x"]["expected"]), ("1", "2"))
        self.assertEqual((names["tests/test_wave.py::test_y"]["expected"], names["tests/test_wave.py::test_y"]["got"]), ("5", "3"))
        self.assertEqual(sorted(detail["uncommitted"]), ["src/c.py", "src/new.py"])
        self.assertEqual(detail["skip_impact"], ["service-d"])
        self.assertEqual(detail["options"], ["try again", "skip and continue"])
        self.assertEqual(detail["summary"]["source"], "facts")
        self.assertIn("2 failing test(s)", detail["summary"]["text"])
        self.assertIn("service-d", detail["summary"]["text"])
        self.assertIsNone(detail["summary"]["at"])

    def test_detail_is_404_when_the_feature_is_not_blocked_or_unknown(self):
        self.prepare()
        self.assertEqual(self.fixture.call("/api/quarantine/service-a")[0], 404)
        self.assertEqual(self.fixture.call("/api/quarantine/nope")[0], 404)
        self.assertEqual(self.fixture.call("/api/quarantine/service-c", project_id="other")[0], 404)

    # ------------------------------------------------ summary
    def test_summary_runs_a_read_only_claude_and_caches_the_text(self):
        self.prepare()
        status, started = self.fixture.call("/api/quarantine/service-c/summary", "POST", {})
        self.assertEqual(status, 202)
        job = self.wait_for_job(started["job"])
        self.assertEqual((job["state"], job["reply"]), ("ok", "test reply"))
        record, = self.records()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")], ["Read", "Grep", "Glob"])
        self.assertEqual(argv[argv.index("--strict-mcp-config") + 1:], ["--mcp-config", '{"mcpServers":{}}'])
        self.assertTrue(record["stdin"].startswith("You are in READ-ONLY mode."))
        self.assertIn("Quarantined feature: service-c", record["stdin"])
        self.assertIn("2 or 3 plain sentences", record["stdin"])
        self.assertNotIn(self.fixture.server.token, json.dumps(record))
        status, detail = self.fixture.call("/api/quarantine/service-c")
        self.assertEqual((detail["summary"]["text"], detail["summary"]["source"]), ("test reply", "ai"))
        self.assertTrue(detail["summary"]["at"])
        self.assertTrue(os.path.isfile(os.path.join(self.fixture.loop, "internal", "quarantine", "service-c.summary.json")))

    def test_summary_failure_is_an_error_job_and_nothing_is_cached(self):
        self.prepare("error")
        status, started = self.fixture.call("/api/quarantine/service-c/summary", "POST", {})
        job = self.wait_for_job(started["job"])
        self.assertEqual(job["state"], "error")
        self.assertEqual(self.fixture.call("/api/quarantine/service-c")[1]["summary"]["source"], "facts")

    def test_summary_requires_the_token_and_a_blocked_feature(self):
        self.prepare()
        self.assertEqual(self.fixture.call("/api/quarantine/service-c/summary", "POST", {}, token=None)[0], 403)
        self.assertEqual(self.fixture.call("/api/quarantine/service-a/summary", "POST", {})[0], 404)

    # ------------------------------------------------ retry and skip
    def test_retry_answers_the_pause_with_the_guidance_in_ANSWERS(self):
        self.prepare()
        self.pause()
        output = self.fixture.prepare_relaunch()
        status, data = self.fixture.call("/api/quarantine/service-c/retry", "POST", {"guidance": "use the new API"})
        self.assertEqual((status, data["ok"]), (200, True), data)
        argv = self.fixture.wait_for_file(output).splitlines()
        self.assertEqual(argv[argv.index("--answer") + 1], "[service-c] try the feature again. Guidance from the user: use the new API")

    def test_retry_without_guidance_is_just_the_option(self):
        self.prepare()
        self.pause()
        output = self.fixture.prepare_relaunch()
        self.assertEqual(self.fixture.call("/api/quarantine/service-c/retry", "POST", {})[0], 200)
        argv = self.fixture.wait_for_file(output).splitlines()
        self.assertEqual(argv[argv.index("--answer") + 1], "[service-c] try the feature again")

    def test_skip_answers_with_the_skip_option(self):
        self.prepare()
        self.pause()
        output = self.fixture.prepare_relaunch()
        status, data = self.fixture.call("/api/quarantine/service-c/skip", "POST", {})
        self.assertEqual(status, 200, data)
        argv = self.fixture.wait_for_file(output).splitlines()
        self.assertEqual(argv[argv.index("--answer") + 1], "[service-c] skip the feature and carry on with the others")

    def test_retry_and_skip_validate_before_touching_the_loop(self):
        self.prepare()
        self.pause()
        self.assertEqual(self.fixture.call("/api/quarantine/service-c/retry", "POST", {"guidance": 5})[0], 400)
        self.assertEqual(self.fixture.call("/api/quarantine/service-c/retry", "POST", {"guidance": "x" * 4001})[0], 400)
        self.assertEqual(self.fixture.call("/api/quarantine/service-a/retry", "POST", {})[0], 404)
        self.assertEqual(self.fixture.call("/api/quarantine/service-a/skip", "POST", {})[0], 404)
        self.assertEqual(self.fixture.call("/api/quarantine/service-c/skip", "POST", {}, token=None)[0], 403)
        self.assertEqual(self.fixture.read_answers(), "")

    # ------------------------------------------------ revert
    def test_revert_restores_tracked_and_deletes_untracked_files_of_the_list(self):
        self.prepare()
        status, data = self.fixture.call("/api/quarantine/service-c/revert", "POST", {"paths": ["src/c.py", "src\\new.py"]})
        self.assertEqual((status, data), (200, {"ok": True, "reverted": ["src/c.py", "src/new.py"], "failed": []}))
        with open(os.path.join(self.fixture.work, "src", "c.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().replace("\r\n", "\n"), "original\n")
        self.assertFalse(os.path.exists(os.path.join(self.fixture.work, "src", "new.py")))
        self.assertEqual(self.fixture.call("/api/quarantine/service-c")[1]["uncommitted"], [])

    def test_revert_refuses_paths_outside_the_list_and_the_specs_folder(self):
        self.prepare()
        for paths in (["src/base/other.py"], ["../outside.txt"], [".specs/project/STATE.md"], ["src/c.py", "nope.py"]):
            status, data = self.fixture.call("/api/quarantine/service-c/revert", "POST", {"paths": paths})
            self.assertEqual(status, 400, (paths, data))
        for body in ({}, {"paths": []}, {"paths": "src/c.py"}, {"paths": [3]}):
            self.assertEqual(self.fixture.call("/api/quarantine/service-c/revert", "POST", body)[0], 400)
        with open(os.path.join(self.fixture.work, "src", "c.py"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().strip(), "edited by the failing executor")

    def test_revert_is_refused_while_the_loop_runs(self):
        self.prepare()
        self.fixture.set_loop("continue")
        self.fixture.set_lock_alive()
        status, data = self.fixture.call("/api/quarantine/service-c/revert", "POST", {"paths": ["src/c.py"]})
        self.assertEqual(status, 409, data)
        self.assertTrue(os.path.exists(os.path.join(self.fixture.work, "src", "new.py")))

    # ------------------------------------------------ chat scopes
    def send(self, scope, text="why?"):
        return self.fixture.call("/api/chat/send", "POST", {"thread": None, "scope": scope, "text": text})

    def test_quarantine_scope_builds_the_context_on_the_server(self):
        self.prepare()
        status, sent = self.send({"type": "quarantine", "feature": "service-c"})
        self.assertEqual(status, 202, sent)
        self.wait_for_job(sent["job"])
        record, = self.records()
        self.assertTrue(record["stdin"].startswith("You are in READ-ONLY mode."))
        self.assertIn("Quarantined feature: service-c", record["stdin"])
        self.assertIn("tests/test_wave.py::test_x", record["stdin"])
        self.assertIn("User question:\nwhy?", record["stdin"])
        self.assertNotIn("--resume", record["argv"])
        status, thread = self.fixture.call("/api/chat/thread/" + sent["thread"])
        self.assertEqual(thread["scope"]["feature"], "service-c")

    def test_error_scope_uses_the_error_item_and_the_events_of_its_turn(self):
        self.fixture.write_json("turns/009.json", {"exit_code": 1, "stderr": "boom with token=abc123secret", "turn": 9})
        self.prepare()
        status, sent = self.send({"type": "error", "error_id": "turn-9-failed"})
        self.assertEqual(status, 202, sent)
        self.wait_for_job(sent["job"])
        stdin = self.records()[0]["stdin"]
        self.assertIn("Turn 9 failed", stdin)
        self.assertIn("boom", stdin)
        self.assertNotIn("abc123secret", stdin)

    def test_error_and_quarantine_scopes_validate_their_target(self):
        self.prepare()
        self.assertEqual(self.send({"type": "error", "error_id": "no-such-error"})[0], 404)
        self.assertEqual(self.send({"type": "quarantine", "feature": "service-a"})[0], 404)
        self.assertEqual(self.send({"type": "error"})[0], 400)
        self.assertEqual(self.send({"type": "quarantine", "feature": "../x"})[0], 400)
        self.assertEqual(self.send({"type": "quarantine", "feature": 3})[0], 400)
