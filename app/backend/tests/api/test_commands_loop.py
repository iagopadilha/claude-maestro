"""Loop commands per project (`/p/<id>/api/...`): note, pause, answer, resume, loop/check, loop/start, instruction,
whiteboard, shutdown no-op, body limit and unknown project. Port of CommandTests/LoopControlTests/InstructionTests/
BodyLimitTests of the legacy panel, with the project prefix."""
import json
import os
import time
import unittest

from harness_backend.domain.roadmap_settings import parse_loop_settings
from harness_backend.services.loop_control_service import convert_settings_to_loop_flags
from tests.api.support_commands import CHECK_READY, FAKE_RUN_LOOP_CHECKABLE, PROJECT_ID, CommandsTestCase, wait_for_process_to_end


class CommandTests(CommandsTestCase):
    def test_note_writes_the_block_in_the_write_answer_format(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        status, data = self.fixture.call("/api/note", "POST", {"text": "  use option B  "})
        self.assertEqual((status, data), (200, {"ok": True}))
        self.assertRegex(self.fixture.read_answers(), r"^\n## Answer \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d \[pending\]\nQuestion: \(user note from the panel\)\nUser answer: use option B\n$")
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "  "})[0], 400)

    def test_note_only_works_with_the_loop_running(self):
        self.fixture.start()
        self.fixture.set_loop("done")
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "x"})[0], 409)
        self.assertEqual(self.fixture.read_answers(), "")

    def test_pause_creates_the_file_only_with_the_loop_running(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/pause", "POST")[0], 409)
        self.assertFalse(os.path.exists(self.fixture.at("PAUSE")))
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/pause", "POST"), (200, {"ok": True}))
        self.assertTrue(os.path.exists(self.fixture.at("PAUSE")))
        self.assertEqual(os.path.getsize(self.fixture.at("PAUSE")), 0)

    def test_answer_is_409_when_not_awaiting(self):
        self.fixture.start()
        for status in (None, "done", "error"):
            if status:
                self.fixture.set_loop(status)
            code, data = self.fixture.call("/api/answer", "POST", {"text": "hi"})
            self.assertEqual(code, 409, status)
            self.assertFalse(data["ok"])
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/answer", "POST", {"text": "hi"})[0], 409)

    def test_answer_by_text_and_by_option_relaunch_with_answer(self):
        self.fixture.start()
        output = self.fixture.prepare_relaunch()
        self.fixture.set_loop("awaiting_user", options=["first", "second (recommended)"])
        status, data = self.fixture.call("/api/answer", "POST", {"text": "do it this way"})
        self.assertEqual(status, 200)
        self.assertGreater(data["pid"], 0)
        self.assertEqual(self.fixture.wait_for_file(output).split("\n"), ["--wave", "2", "--answer", "do it this way"])
        self.assertTrue(os.path.exists(self.fixture.at("monitor-relaunch.log")))
        self.assertEqual(self.fixture.read_answers(), "", "run_loop.py writes the answer, not the app")
        os.remove(output)
        wait_for_process_to_end(data["pid"])
        status, data = self.fixture.call("/api/answer", "POST", {"option": 2})
        self.assertEqual(status, 200)
        self.assertEqual(self.fixture.wait_for_file(output).split("\n")[-1], "second (recommended)")
        wait_for_process_to_end(data["pid"])
        self.assertEqual(self.fixture.call("/api/answer", "POST", {"option": 3})[0], 400)
        self.assertEqual(self.fixture.call("/api/answer", "POST", {})[0], 400)
        self.assertEqual(self.fixture.call("/api/answer", "POST", {"option": True})[0], 400)

    def test_resume_relaunches_without_answer_and_refuses_with_the_loop_running(self):
        self.fixture.start()
        output = self.fixture.prepare_relaunch()
        self.fixture.set_loop("error", reason="x")
        status, data = self.fixture.call("/api/resume", "POST")
        self.assertEqual(status, 200)
        self.assertEqual(self.fixture.wait_for_file(output).split("\n"), ["--wave", "2"])
        wait_for_process_to_end(data["pid"])
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/resume", "POST")[0], 409)

    def test_resume_without_loop_cmd_is_409_not_500(self):
        self.fixture.start()
        self.fixture.set_loop("error", reason="x")
        status, data = self.fixture.call("/api/resume", "POST")
        self.assertEqual(status, 409)
        self.assertIn("LOOP-CMD", data["error"])

    def test_live_relaunched_loop_keeps_the_app_on_and_blocks_a_new_relaunch(self):
        module = self.fixture.start()
        self.fixture.prepare_relaunch()
        self.fixture.set_loop("error", reason="x")
        module.loop_control_service.relaunched_pid_by_project[PROJECT_ID] = os.getpid()
        self.assertTrue(module.loop_control_service.is_busy())
        self.assertTrue(self.fixture.activity_monitor.is_any_provider_busy())
        self.assertEqual(self.fixture.call("/api/resume", "POST")[0], 409)
        module.loop_control_service.relaunched_pid_by_project[PROJECT_ID] = 0
        self.assertFalse(module.loop_control_service.is_busy())


class LoopControlTests(CommandsTestCase):
    def prepare_fake(self, check_output=CHECK_READY):
        fake = os.path.join(self.fixture.tmp, "fake_run_loop_checkable.py")
        self.fixture.write(fake, FAKE_RUN_LOOP_CHECKABLE)
        self.fixture.write(os.path.join(self.fixture.tmp, "check_output.txt"), check_output)
        self.fixture.scripts["run_loop"] = fake
        self.fixture.start()
        return fake

    def read_record(self, name, seconds=15):
        path = os.path.join(self.fixture.tmp, name)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                try:
                    with open(path, encoding="utf-8") as handle:
                        return json.load(handle)
                except ValueError:
                    pass
            time.sleep(0.1)
        self.fail("the fake run_loop did not write %s" % name)

    def test_check_and_start_refuse_invalid_run_choices_with_400_and_ignore_other_keys(self):
        self.prepare_fake()
        for path in ("/api/loop/check", "/api/loop/start"):
            for body in ({"push": 1}, {"push": "yes"}, {"remove_worktree_on_finish": "no"}, {"remove_worktree_on_finish": 0}):
                status, data = self.fixture.call(path, "POST", body)
                self.assertEqual(status, 400, "%s %s" % (path, body))
                self.assertFalse(data["ok"])
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "check_argv.json")), "validation fails before running anything")
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "start_record.json")))
        # card-era keys are decided in the ROADMAP now: ignored, not refused
        self.assertEqual(self.fixture.call("/api/loop/check", "POST", {"wave": 0, "budget": -1, "branch": "a..b", "unknown": 1, "repo": "C:/other"})[0], 200)
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "3"])

    def test_valid_settings_become_argv_without_a_shell(self):
        text = ("**E2E:** yes\n**Dispatch:** 8\n**Max spend per turn:** 5\n**Max spend for the loop:** 0\n**Max minutes per turn:** 600\n"
                "**Branch:** loop/a.b_c-1\n**Setup:** npm ci && rm -rf /\n**When a feature gets stuck:** continue\n")
        settings = parse_loop_settings(text)["settings"]
        self.assertEqual(convert_settings_to_loop_flags(settings, 8, False),
                         ["--wave", "8", "--e2e", "--no-push", "--continue-on-blocked", "--max-cost", "0", "--timeout", "600",
                          "--branch", "loop/a.b_c-1", "--setup", "npm ci && rm -rf /"])
        defaults = parse_loop_settings("")["settings"]
        self.assertEqual(convert_settings_to_loop_flags(defaults, 1, True), [])
        self.assertEqual(convert_settings_to_loop_flags(parse_loop_settings("**Setup:** --x")["settings"], 1, True), ["--setup=--x"])

    def test_check_parses_problems_warnings_and_command(self):
        self.prepare_fake("[check] WARNING: no remote\n[check] PROBLEM: dirty tree\n[check] PROBLEM: no git identity\nloose line\n")
        status, data = self.fixture.call("/api/loop/check", "POST", {"wave": 2})
        self.assertEqual(status, 200)
        self.assertEqual((data["ok"], data["ready"]), (True, False))
        self.assertEqual(data["problems"], ["dirty tree", "no git identity"])
        self.assertEqual(data["warnings"], ["no remote"])

    def test_ready_check_returns_the_command_and_passes_the_flags_to_run_loop(self):
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/check", "POST", {"push": False})
        self.assertEqual(status, 200)
        self.assertTrue(data["ready"])
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["warnings"], ["no remote"])
        self.assertEqual(data["command"], 'python "run_loop.py" "repo" --wave 2')
        argv = self.read_record("check_argv.json")
        self.assertEqual(argv, [self.fixture.repo, "--check", "--wave", "3", "--no-push"])
        self.assertFalse(os.path.exists(self.fixture.at("LOOP.lock")))

    def write_roadmap(self, text):
        self.fixture.write(os.path.join(self.fixture.project_dir, "ROADMAP.md"), text)

    def check_argv_for(self, body):
        status, _ = self.fixture.call("/api/loop/check", "POST", body)
        self.assertEqual(status, 200)
        return self.read_record("check_argv.json")

    def test_e2e_comes_from_the_roadmap(self):
        self.prepare_fake()
        self.write_roadmap("# Roadmap\n\n**Goal:** x\n\n**E2E:** yes\n")
        self.assertEqual(self.check_argv_for({}), [self.fixture.repo, "--check", "--wave", "3", "--e2e"])

    def test_e2e_no_or_absent_in_the_roadmap_adds_no_flag(self):
        self.prepare_fake()
        self.write_roadmap("# Roadmap\n\n**Goal:** x\n\n**E2E:** no\n")
        self.assertEqual(self.check_argv_for({}), [self.fixture.repo, "--check", "--wave", "3"])

    def test_e2e_in_the_body_is_ignored(self):
        self.prepare_fake()
        self.write_roadmap("**E2E:** yes\n")
        self.assertEqual(self.check_argv_for({"e2e": False}), [self.fixture.repo, "--check", "--wave", "3", "--e2e"])
        self.write_roadmap("**E2E:** no\n")
        self.assertEqual(self.check_argv_for({"e2e": True}), [self.fixture.repo, "--check", "--wave", "3"])

    def test_ready_check_with_the_command_on_the_same_line(self):
        self.prepare_fake("[check] READY. Command: python run_loop.py repo\n")
        data = self.fixture.call("/api/loop/check", "POST", {})[1]
        self.assertEqual((data["ready"], data["command"]), (True, "python run_loop.py repo"))

    def test_check_with_an_empty_body_is_accepted_and_output_without_ready_becomes_a_problem(self):
        self.prepare_fake("Traceback: it broke\n")
        status, data = self.fixture.call("/api/loop/check", "POST", {})
        self.assertEqual(status, 200)
        self.assertFalse(data["ready"])
        self.assertEqual(data["problems"], ["Traceback: it broke"])

    def test_start_launches_detached_with_the_project_url_and_without_answer(self):
        self.prepare_fake()
        self.write_roadmap("**E2E:** yes\n**Dispatch:** 2\n**Max spend per turn:** 5\n**Max spend for the loop:** 30\n**Max minutes per turn:** 45\n"
                           "**Branch:** loop/x\n**Setup:** npm ci\n**When a feature gets stuck:** continue\n")
        status, data = self.fixture.call("/api/loop/start", "POST", {"push": False})
        self.assertEqual(status, 200)
        self.assertGreater(data["pid"], 0)
        record = self.read_record("start_record.json")
        self.assertEqual(record["monitor_url"], self.fixture.project_url)
        self.assertTrue(record["monitor_url"].endswith("/p/%s/" % PROJECT_ID))
        self.assertEqual(record["argv"], [self.fixture.repo, "--wave", "2", "--e2e", "--no-push", "--continue-on-blocked", "--max-cost", "30",
                                         "--timeout", "45", "--branch", "loop/x", "--setup", "npm ci", "--orchestrator-model", "opus"])
        self.assertNotIn("--answer", record["argv"])
        self.assertNotIn("--check", record["argv"])
        self.assertTrue(os.path.exists(self.fixture.at("monitor-relaunch.log")))
        wait_for_process_to_end(data["pid"])

    def test_start_ignores_the_body_repo_and_uses_the_project_one(self):
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/start", "POST", {"repo": self.fixture.tmp})
        self.assertEqual(status, 200)
        self.assertEqual(self.read_record("start_record.json")["argv"][0], self.fixture.repo)
        wait_for_process_to_end(data["pid"])

    def test_start_with_a_failed_preflight_is_409_with_problems_and_does_not_launch(self):
        self.prepare_fake("[check] PROBLEM: dirty tree\n[check] WARNING: no remote\n")
        status, data = self.fixture.call("/api/loop/start", "POST", {"wave": 1})
        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        self.assertEqual((data["problems"], data["warnings"]), (["dirty tree"], ["no remote"]))
        time.sleep(0.5)
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "start_record.json")))

    def test_start_with_a_live_lock_is_409_without_running_even_the_check(self):
        self.prepare_fake()
        self.fixture.set_lock_alive()
        status, data = self.fixture.call("/api/loop/start", "POST", {"wave": 2})
        self.assertEqual(status, 409)
        self.assertFalse(data["ok"])
        time.sleep(0.5)
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "start_record.json")))
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "check_argv.json")))

    def test_check_and_start_without_a_token_are_403(self):
        self.prepare_fake()
        for path in ("/api/loop/check", "/api/loop/start"):
            self.assertEqual(self.fixture.call(path, "POST", {"wave": 2}, token=None)[0], 403)
            self.assertEqual(self.fixture.call(path, "POST", {"wave": 2}, token="wrong")[0], 403)
        time.sleep(0.3)
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "check_argv.json")))
        self.assertFalse(os.path.exists(os.path.join(self.fixture.tmp, "start_record.json")))


class InstructionTests(CommandsTestCase):
    def test_running_writes_a_note_with_feature_and_attachments(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        status, data = self.fixture.call("/api/instruction", "POST", {"text": " swap X for Y ", "feature": "service-a",
                                                                       "attachments": ["question: a? answer: b", "other"]})
        self.assertEqual((status, data), (200, {"ok": True, "action": "note"}))
        self.assertRegex(self.fixture.read_answers(), r"^\n## Answer \d{4}-\d\d-\d\dT\d\d:\d\d:\d\d \[pending\]\nQuestion: \(user instruction from the panel\)\n"
                                                      r"User answer: \[service-a\] swap X for Y\nAttached context:\n- question: a\? answer: b\n- other\n$")
        self.fixture.call("/api/instruction", "POST", {"text": "simple"})
        self.assertIn("User answer: simple\n", self.fixture.read_answers())

    def test_awaiting_user_equals_answer_with_the_composed_text(self):
        self.fixture.start()
        output = self.fixture.prepare_relaunch()
        self.fixture.set_loop("awaiting_user", options=["a"])
        status, data = self.fixture.call("/api/instruction", "POST", {"text": "do B", "feature": "f1", "attachments": ["ctx"]})
        self.assertEqual((status, data["action"]), (200, "answer"))
        self.assertGreater(data["pid"], 0)
        self.assertEqual(self.fixture.wait_for_file(output), chr(10).join(["--wave", "2", "--answer", "[f1] do B", "Attached context:", "- ctx"]))
        self.assertEqual(self.fixture.read_answers(), "", "run_loop.py writes the answer in this case")
        wait_for_process_to_end(data["pid"])

    def test_error_stopped_and_done_write_the_note_and_resume(self):
        self.fixture.start()
        output = self.fixture.prepare_relaunch()
        for loop_status in ("error", "stopped", "done"):
            self.fixture.set_loop("continue" if loop_status == "stopped" else loop_status)
            if os.path.exists(output):
                os.remove(output)
            status, data = self.fixture.call("/api/instruction", "POST", {"text": "resume with %s" % loop_status})
            self.assertEqual((status, data["action"]), (200, "note+resume"), loop_status)
            self.assertEqual(self.fixture.wait_for_file(output).split("\n"), ["--wave", "2"])
            wait_for_process_to_end(data["pid"])
        self.assertEqual(self.fixture.read_answers().count("[pending]"), 3)
        self.assertIn("User answer: resume with done", self.fixture.read_answers())

    def test_409_without_loop_without_loop_cmd_and_with_a_live_lock(self):
        module = self.fixture.start()
        status, data = self.fixture.call("/api/instruction", "POST", {"text": "x"})
        self.assertEqual(status, 409)
        self.assertIn("new loop", data["error"].lower())
        self.fixture.set_loop("error", reason="x")
        status, data = self.fixture.call("/api/instruction", "POST", {"text": "x"})
        self.assertEqual(status, 409)
        self.assertIn("LOOP-CMD", data["error"])
        self.assertEqual(self.fixture.read_answers(), "", "a 409 leaves no block behind")
        self.fixture.prepare_relaunch()
        module.loop_control_service.relaunched_pid_by_project[PROJECT_ID] = os.getpid()  # a loop launched from here still alive
        self.assertEqual(self.fixture.call("/api/instruction", "POST", {"text": "x"})[0], 409)
        self.assertEqual(self.fixture.read_answers(), "")

    def test_validation_of_text_feature_and_attachments(self):
        self.fixture.start()
        self.fixture.set_lock_alive()

        def call(body):
            return self.fixture.call("/api/instruction", "POST", body)[0]

        self.assertEqual(call({"text": "  "}), 400)
        self.assertEqual(call({"text": "x", "feature": "../x"}), 400)
        self.assertEqual(call({"text": "x", "attachments": ["a"] * 6}), 400)
        self.assertEqual(call({"text": "x", "attachments": ["a" * 2001]}), 400)
        self.assertEqual(call({"text": "x", "attachments": "not-a-list"}), 400)
        self.assertEqual(call({"text": "x", "attachments": ["a" * 2000] * 5}), 200)
        self.assertEqual(self.fixture.call("/api/instruction", "POST", {"text": "x"}, token=None)[0], 403)


class BodyLimitTests(CommandsTestCase):
    def test_body_above_64kb_gets_413_and_the_client_receives_the_response(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "y" * (70 * 1024)})[0], 413)
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "y" * (2 * 1024 * 1024)})[0], 413)
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "ok"})[0], 200)


class ProjectAndShutdownTests(CommandsTestCase):
    def test_unknown_project_is_404_on_every_command_route(self):
        self.fixture.start()
        routes = [("POST", "/answer"), ("POST", "/resume"), ("POST", "/note"), ("POST", "/pause"), ("POST", "/loop/check"),
                  ("POST", "/loop/start"), ("POST", "/instruction"), ("POST", "/shutdown"), ("GET", "/whiteboard"), ("POST", "/whiteboard"),
                  ("POST", "/chat/send"), ("GET", "/chat/job/j-000000000000"), ("GET", "/chat/threads"), ("GET", "/chat/thread/t-000000000000"),
                  ("GET", "/actions"), ("POST", "/action/propose"), ("GET", "/action/job/j-000000000000"), ("POST", "/action/run")]
        for method, path in routes:
            status, data = self.fixture.call(path, method, {}, project_id="does-not-exist")
            self.assertEqual((status, data), (404, {"ok": False, "error": "project not found"}), path)

    def test_legacy_shutdown_is_a_no_op_with_token(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/shutdown", "POST"), (200, {"ok": True}))
        self.assertEqual(self.fixture.call("/api/shutdown", "POST", token=None)[0], 403)
        self.assertEqual(self.fixture.call("/api/whiteboard")[0], 200, "the app stays up")


class WhiteboardTests(CommandsTestCase):
    def board_path(self):
        return os.path.join(self.fixture.loop, "internal", "whiteboard.json")

    def test_get_without_a_board_returns_version_0_and_null_data(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/whiteboard"), (200, {"ok": True, "data": None, "version": 0, "updated_at": None}))

    def test_post_writes_atomically_increments_the_version_and_409_with_an_old_base(self):
        self.fixture.start()
        status, data = self.fixture.call("/api/whiteboard", "POST", {"data": {"elements": [1, 2]}, "base_version": 0})
        self.assertEqual((status, data["ok"], data["version"]), (200, True, 1))
        self.assertTrue(data["updated_at"])
        with open(self.board_path(), encoding="utf-8") as handle:
            saved = json.load(handle)
        self.assertEqual((saved["data"], saved["version"]), ({"elements": [1, 2]}, 1))
        self.assertFalse(os.path.exists(self.board_path() + ".tmp"))
        status, data = self.fixture.call("/api/whiteboard")
        self.assertEqual((data["data"], data["version"]), ({"elements": [1, 2]}, 1))
        status, data = self.fixture.call("/api/whiteboard", "POST", {"data": {"other": True}, "base_version": 0})
        self.assertEqual((status, data["ok"], data["version"]), (409, False, 1))
        status, data = self.fixture.call("/api/whiteboard", "POST", {"data": {}, "base_version": 1})
        self.assertEqual((status, data["version"]), (200, 2))

    def test_validation_token_and_8mb_limit_only_on_this_route(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", {"data": [1], "base_version": 0})[0], 400)
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", {"data": {}, "base_version": "0"})[0], 400)
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", {"data": {}})[0], 400)
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", {"data": {}, "base_version": 0}, token=None)[0], 403)
        large = {"data": {"x": "y" * (1024 * 1024)}, "base_version": 0}
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", large)[0], 200, "1 MB passes (8 MB limit)")
        huge = {"data": {"x": "y" * (9 * 1024 * 1024)}, "base_version": 1}
        self.assertEqual(self.fixture.call("/api/whiteboard", "POST", huge)[0], 413)
        self.assertEqual(self.fixture.call("/api/whiteboard")[1]["version"], 1)
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/note", "POST", {"text": "y" * (1024 * 1024)})[0], 413, "the other routes stay at 64 KB")


if __name__ == "__main__":
    unittest.main()
