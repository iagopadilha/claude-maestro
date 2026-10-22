"""Start from the ROADMAP loop settings: `GET loop/settings`, the options of check/start, the delivery choices in meta, models."""
import json
import os
import unittest

from harness_backend.domain.roadmap_settings import parse_loop_settings
from harness_backend.services.loop_control_service import convert_settings_to_loop_flags
from tests.api.support_commands import CHECK_READY, FAKE_RUN_LOOP_CHECKABLE, ROADMAP, CommandsTestCase, wait_for_process_to_end

HEADER = ("**E2E:** yes\n**Dispatch:** 2\n**Max spend per turn:** 7\n**Max spend for the loop:** 25\n**Max minutes per turn:** 30\n"
          "**Branch:** loop/x\n**Setup:** npm ci\n**When a feature gets stuck:** continue\n**Push and PR:** yes\n")


class LoopSettingsTests(CommandsTestCase):
    def prepare_fake(self, roadmap_header=""):
        self.fixture.write(os.path.join(self.fixture.project_dir, "ROADMAP.md"), roadmap_header + ROADMAP)
        fake = os.path.join(self.fixture.tmp, "fake_run_loop_checkable.py")
        self.fixture.write(fake, FAKE_RUN_LOOP_CHECKABLE)
        self.fixture.write(os.path.join(self.fixture.tmp, "check_output.txt"), CHECK_READY)
        self.fixture.scripts["run_loop"] = fake
        self.fixture.start()

    def read_record(self, name):
        path = os.path.join(self.fixture.tmp, name)
        self.fixture.wait_for_file(path)
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)

    def test_settings_route_without_lines_returns_defaults_missing_and_models(self):
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/settings")
        self.assertEqual(status, 200)
        self.assertEqual(data["settings"]["budget_loop"], 50.0)
        self.assertEqual(len(data["missing"]), 11)
        self.assertEqual(data["problems"], [])
        self.assertEqual(data["models"], {"orchestrator": "opus", "agents": "sonnet"})
        self.assertTrue(data["integration_branch"])
        self.assertEqual(data["delivery"], {"push_pr": True, "remove_worktree_on_finish": True})
        self.assertTrue(data["workspace"]["worktree_created"])
        self.assertIn("repos", data["workspace"])

    def test_settings_route_reports_problems_and_present_lines(self):
        self.prepare_fake("**Max spend per turn:** abc\n**E2E:** yes\n")
        _status, data = self.fixture.call("/api/loop/settings")
        self.assertEqual([item["name"] for item in data["problems"]], ["Max spend per turn"])
        self.assertTrue(data["settings"]["e2e"])
        self.assertNotIn("e2e", data["missing"])
        self.assertIn("dispatch", data["missing"])

    def test_start_uses_the_roadmap_settings_and_ignores_card_keys(self):
        self.prepare_fake(HEADER)
        status, data = self.fixture.call("/api/loop/start", "POST", {"wave": 8, "budget": 1, "max_cost": 1, "branch": "other", "unknown": 1})
        self.assertEqual(status, 200)
        argv = self.read_record("start_record.json")["argv"]
        self.assertEqual(argv, [self.fixture.repo, "--wave", "2", "--e2e", "--continue-on-blocked", "--budget", "7", "--max-cost", "25",
                                "--timeout", "30", "--branch", "loop/x", "--setup", "npm ci", "--orchestrator-model", "opus"])
        wait_for_process_to_end(data["pid"])

    def test_start_without_lines_matches_the_old_defaults(self):
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/start", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.read_record("start_record.json")["argv"], [self.fixture.repo, "--wave", "3", "--orchestrator-model", "opus"])
        wait_for_process_to_end(data["pid"])

    def test_empty_orchestrator_model_passes_an_empty_value(self):
        self.prepare_fake()
        self.fixture.module.loop_control_service.config.orchestrator_model = ""
        status, data = self.fixture.call("/api/loop/start", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual(self.read_record("start_record.json")["argv"][-2:], ["--orchestrator-model", ""])
        wait_for_process_to_end(data["pid"])

    def test_start_records_delivery_in_the_worktree_meta_and_keeps_other_keys(self):
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/start", "POST", {"push": False, "remove_worktree_on_finish": False})
        self.assertEqual(status, 200)
        self.read_record("start_record.json")
        with open(self.fixture.at("meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
        self.assertEqual(meta["delivery"], {"push_pr": False, "remove_worktree_on_finish": False})
        self.assertEqual(meta["turns"], 1)
        wait_for_process_to_end(data["pid"])

    def test_invalid_run_choice_is_400_and_push_flag_follows_the_choice(self):
        self.prepare_fake()
        self.assertEqual(self.fixture.call("/api/loop/start", "POST", {"push": "yes"})[0], 400)
        self.assertEqual(self.fixture.call("/api/loop/check", "POST", {"remove_worktree_on_finish": 1})[0], 400)
        self.assertEqual(self.fixture.call("/api/loop/check", "POST", {"push": False})[0], 200)
        with open(os.path.join(self.fixture.tmp, "check_argv.json"), encoding="utf-8") as handle:
            self.assertIn("--no-push", json.load(handle))

    def test_check_turns_invalid_settings_into_warnings(self):
        self.prepare_fake("**Max minutes per turn:** 9000\n")
        _status, data = self.fixture.call("/api/loop/check", "POST", {})
        self.assertTrue(data["ready"])
        self.assertTrue(any("Max minutes per turn" in item for item in data["warnings"]))


class SettingsToFlagsTests(unittest.TestCase):
    def test_flags_from_settings(self):
        settings = parse_loop_settings("**Setup:** --odd\n**Max spend for the loop:** 0\n")["settings"]
        self.assertEqual(convert_settings_to_loop_flags(settings, 1, False), ["--no-push", "--max-cost", "0", "--setup=--odd"])
        self.assertEqual(convert_settings_to_loop_flags(settings, 1, True, orchestrator_model=""),
                         ["--max-cost", "0", "--setup=--odd", "--orchestrator-model", ""])


if __name__ == "__main__":
    unittest.main()
