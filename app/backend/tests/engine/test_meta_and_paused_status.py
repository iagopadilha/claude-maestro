#!/usr/bin/env python3
"""App-owned meta keys survive the engine saves, delivery-pending.json is merged at worktree creation, `paused` is a legal LOOP status the panel reads."""
import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_loop import LoopCase, loop_file  # noqa: E402
import loop_state  # noqa: E402
import run_loop  # noqa: E402
from harness_backend.services.loop_status_service import LoopStatusService  # noqa: E402

ROAD = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n- [c] status=pending\n"
SCENARIO = {"features": {name: {"paths": "apps/" + name} for name in "abc"}, "cost": 0.10}


class FakeLoopFiles:
    """Just what LoopStatusService.get_loop_summary reads."""
    def __init__(self, loop_json, meta):
        self.loop_json, self.meta = loop_json, meta

    def read_loop_json(self, work_dir):
        return self.loop_json

    def read_meta(self, work_dir):
        return self.meta

    def read_lock_pid(self, work_dir):
        return None

    def is_process_alive(self, pid):
        return False


class Context:
    work_dir = "unused"


class TestPausedStatus(unittest.TestCase):
    def test_paused_is_a_legal_loop_status(self):
        self.assertIn("paused", loop_state.LOOP_STATUS)
        state = {"status": "paused", "turn": 3, "reason": "cost cap reached: US$ 0.30 of 0.25"}
        self.assertEqual(loop_state.validate_loop_json(state), [])

    def test_panel_status_reports_paused_with_its_reason(self):
        reason = "cost cap reached: US$ 0.30 of 0.25"
        service = LoopStatusService(FakeLoopFiles({"status": "paused", "turn": 3, "reason": reason}, {"stopped": "paused"}), None, None)
        summary = service.get_loop_summary(Context())
        self.assertEqual((summary["status"], summary["reason"], summary["running"]), ("paused", reason, False))


class TestMetaKeys(LoopCase):
    def test_save_meta_keeps_the_app_owned_keys_written_meanwhile(self):
        folder = tempfile.mkdtemp(prefix="meta-")
        self.addCleanup(__import__("shutil").rmtree, folder, True)
        path = os.path.join(folder, "meta.json")
        stale = {"turns": 2, "cost_usd": 1.0}
        run_loop.save_json(path, dict(stale, delivery={"push_pr": True, "remove_worktree_on_finish": False}, max_cost_usd=80))
        run_loop.save_meta(path, dict(stale, max_cost_usd=10))  # in-memory copy is older than the disk
        saved = json.load(open(path, encoding="utf-8"))
        self.assertEqual(saved["delivery"], {"push_pr": True, "remove_worktree_on_finish": False})
        self.assertEqual(saved["max_cost_usd"], 80)
        self.assertEqual(saved["turns"], 2)

    def test_delivery_and_raised_cap_survive_a_full_run(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        meta = project.meta()
        meta.update(delivery={"push_pr": False, "remove_worktree_on_finish": True}, max_cost_usd=10.0)
        json.dump(meta, open(loop_file(project.work, "json", "meta.json"), "w", encoding="utf-8"))
        self.done(project.run("--wave", "1"), 0)
        after = project.meta()
        self.assertEqual(after["delivery"], {"push_pr": False, "remove_worktree_on_finish": True})
        self.assertEqual(after["max_cost_usd"], 10.0)

    def test_pending_delivery_is_merged_at_creation_and_deleted(self):
        project = self.make(ROAD, SCENARIO)
        pending = os.path.join(project.repo, ".specs", "project", "loop", "internal", "delivery-pending.json")
        os.makedirs(os.path.dirname(pending), exist_ok=True)
        json.dump({"push_pr": False, "remove_worktree_on_finish": True}, open(pending, "w", encoding="utf-8"))
        self.done(project.run("--wave", "1"), 0)
        self.assertEqual(project.meta()["delivery"], {"push_pr": False, "remove_worktree_on_finish": True})
        self.assertFalse(os.path.exists(pending))

    def test_empty_orchestrator_model_is_accepted(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--orchestrator-model", ""), 0)


if __name__ == "__main__":
    unittest.main()
