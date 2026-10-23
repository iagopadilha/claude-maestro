#!/usr/bin/env python3
"""The spend cap is a resumable pause: `paused` + "cost cap reached", one notice, cost kept across runs, raised cap in meta wins, 80 % warning once."""
import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_loop import LoopCase, loop_file  # noqa: E402
import run_loop  # noqa: E402

ROAD = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n- [c] status=pending\n"
SCENARIO = {"features": {name: {"paths": "apps/" + name} for name in "abc"}, "cost": 0.10}


class TestCostCapPause(LoopCase):
    def raise_cap(self, project, value):
        meta = project.meta()
        meta["max_cost_usd"] = value
        with open(loop_file(project.work, "json", "meta.json"), "w", encoding="utf-8") as handle:
            json.dump(meta, handle)

    def test_cap_passed_pauses_with_the_reason_and_a_notice(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        meta = project.meta()
        self.assertEqual(meta["stopped"], "paused")
        self.assertEqual(meta["reason"], "cost cap reached: US$ 0.30 of 0.25")
        self.assertAlmostEqual(meta["cost_usd"], 0.30, places=4)
        self.assertEqual(meta["turns"], 3)
        loop = project.loop_json()
        self.assertEqual((loop["status"], loop["reason"]), ("paused", meta["reason"]))
        notice = project.notice()
        self.assertIn("0.30", notice)
        self.assertIn("0.25", notice)
        self.assertIn("painel", notice)

    def test_warning_at_80_percent_is_recorded_once_per_cap(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        meta = project.meta()
        self.assertTrue(meta["cost_warned_at"])  # 0.20 >= 80 % of 0.25
        self.assertEqual(meta["cost_warned_cap"], 0.25)

    def test_warning_helper_sends_one_notice(self):
        sent = []
        original = run_loop.notify_wait
        run_loop.notify_wait = lambda subject, body: sent.append((subject, body))
        self.addCleanup(setattr, run_loop, "notify_wait", original)
        meta = {}
        self.assertFalse(run_loop.warn_near_cost_cap(meta, 0.10, 0.25))
        self.assertTrue(run_loop.warn_near_cost_cap(meta, 0.20, 0.25))
        self.assertFalse(run_loop.warn_near_cost_cap(meta, 0.22, 0.25))
        self.assertEqual(len(sent), 1)
        self.assertIn("0.20", sent[0][1])
        self.assertIn("0.25", sent[0][1])
        self.assertEqual(sent[0][0], "spend at 80 % of the cap")  # the subject is not %-formatted: no doubled percent sign
        self.assertTrue(run_loop.warn_near_cost_cap(meta, 0.85, 1.0), "a raised cap warns again at its own 80 %")

    def test_raised_cap_in_meta_wins_and_the_total_is_kept(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        self.raise_cap(project, 10.0)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 0)  # the option is lower than the spent total: the raised cap wins
        meta = project.meta()
        self.assertGreater(meta["cost_usd"], 0.30, "the total continued from 0.30, never reset")
        self.assertEqual(meta["max_cost_usd"], 10.0)

    def test_resume_over_the_cap_without_raising_pauses_before_spending(self):
        project = self.make(ROAD, SCENARIO)
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        turns = project.meta()["turns"]
        self.done(project.run("--wave", "1", "--max-cost", "0.25"), 10)
        self.assertEqual(project.meta()["turns"], turns)

    def test_cap_helpers(self):
        class Opts:
            max_cost = 50.0
        self.assertEqual(run_loop.effective_cost_cap({}, Opts), 50.0)
        self.assertEqual(run_loop.effective_cost_cap({"max_cost_usd": 80}, Opts), 80.0)
        Opts.max_cost = 0
        self.assertEqual(run_loop.effective_cost_cap({}, Opts), 0.0)
        self.assertEqual(run_loop.cost_cap_reason(0.3, 0.25), "cost cap reached: US$ 0.30 of 0.25")

    def test_default_option_is_50(self):
        source = open(os.path.join(os.path.dirname(run_loop.__file__), "loop_cli.py"), encoding="utf-8").read()  # the parser lives in loop_cli.py
        self.assertEqual(float(re.search(r'"--max-cost", type=float, default=([\d.]+)', source).group(1)), 50.0)


if __name__ == "__main__":
    unittest.main()
