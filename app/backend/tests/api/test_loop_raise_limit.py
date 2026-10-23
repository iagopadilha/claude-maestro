"""`POST loop/raise-limit`: validate above the spent total, write meta.max_cost_usd, resume the same loop; orchestrator_model config."""
import json
import os
import unittest

from harness_backend.config.app_config import load_app_config
from tests.api.support_commands import FAKE_RUN_LOOP, CommandsTestCase, wait_for_process_to_end


class RaiseLimitTests(CommandsTestCase):
    def prepare_paused(self, cost=0.30):
        self.fixture.write_json("meta.json", {"turns": 3, "cost_usd": cost, "delivery": {"push_pr": True, "remove_worktree_on_finish": True}})
        self.fixture.set_loop("paused", reason="cost cap reached: US$ 0.30 of 0.25")
        fake = os.path.join(self.fixture.tmp, "fake_run_loop.py")
        output = os.path.join(self.fixture.tmp, "argv.txt")
        self.fixture.write(fake, FAKE_RUN_LOOP)
        import sys
        self.fixture.scripts["run_loop"] = fake
        self.fixture.write_json("LOOP-CMD.json", {"argv": [sys.executable, fake, self.fixture.repo, "--wave", "2"], "cwd": self.fixture.repo})
        self.fixture.start()
        return output

    def read_meta(self):
        with open(self.fixture.at("meta.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_raising_above_the_total_writes_the_cap_resumes_and_keeps_the_rest(self):
        output = self.prepare_paused()
        status, data = self.fixture.call("/api/loop/raise-limit", "POST", {"max_cost_usd": 1.5})
        self.assertEqual((status, data), (202, {"ok": True, "cost_usd": 0.30, "max_cost_usd": 1.5}))
        self.assertEqual(self.fixture.wait_for_file(output).split("\n"), ["--wave", "2"])
        meta = self.read_meta()
        self.assertEqual((meta["max_cost_usd"], meta["cost_usd"], meta["turns"]), (1.5, 0.30, 3))
        self.assertTrue(meta["delivery"]["push_pr"])

    def test_a_value_not_above_the_total_or_invalid_is_400_and_changes_nothing(self):
        self.prepare_paused()
        for value in (0.30, 0.1, 0, -1, "5", True, None, float("nan")):
            status, _data = self.fixture.call("/api/loop/raise-limit", "POST", {"max_cost_usd": value})
            self.assertEqual(status, 400, value)
        self.assertNotIn("max_cost_usd", self.read_meta())

    def test_refused_while_running(self):
        self.prepare_paused()
        self.fixture.set_lock_alive()
        self.assertEqual(self.fixture.call("/api/loop/raise-limit", "POST", {"max_cost_usd": 5})[0], 409)
        self.assertNotIn("max_cost_usd", self.read_meta())

    def test_old_error_status_is_accepted(self):
        self.prepare_paused()
        self.fixture.set_loop("continue")
        self.fixture.write_json("meta.json", {"turns": 3, "cost_usd": 1.0, "stopped": "error", "finished_at": "2026-10-15T11:00:00",
                                              "reason": "total cost cap exceeded: US$ 1.00 > 0.50 (--max-cost)"})
        status, data = self.fixture.call("/api/loop/raise-limit", "POST", {"max_cost_usd": 2})
        self.assertEqual(status, 202)
        wait_for_process_to_end(self.fixture.module.loop_control_service.relaunched_pid_by_project[self.fixture.project.id])


class OrchestratorModelConfigTests(unittest.TestCase):
    def load(self, config_json):
        import tempfile
        folder = tempfile.mkdtemp(prefix="harness-config-")
        if config_json is not None:
            with open(os.path.join(folder, "config.json"), "w", encoding="utf-8") as handle:
                json.dump(config_json, handle)
        return load_app_config(user_data_dir=folder)

    def test_default_explicit_and_empty(self):
        self.assertEqual(self.load(None).orchestrator_model, "opus")
        self.assertEqual(self.load({"orchestrator_model": "sonnet"}).orchestrator_model, "sonnet")
        self.assertEqual(self.load({"orchestrator_model": ""}).orchestrator_model, "")
        self.assertEqual(self.load({"orchestrator_model": 5}).orchestrator_model, "opus")


if __name__ == "__main__":
    unittest.main()
