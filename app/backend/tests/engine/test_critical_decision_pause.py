#!/usr/bin/env python3
"""A turn that writes a `level=critical` decision stops the loop (awaiting_user + notice); `important` does not; the script never auto-answers it."""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from test_loop import LoopCase  # noqa: E402
import run_loop  # noqa: E402

ROAD = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
FEATURES = {"a": {"paths": "apps/a"}, "b": {"paths": "apps/b"}}
CRITICAL = "- [a] turn=2 level=critical delete the old table? | chose: kept it, only renamed | revert: drop it by hand"
IMPORTANT = "- [a] turn=2 level=important which format | chose: json | revert: switch to csv"


class TestCriticalDecisionPause(LoopCase):
    def test_critical_line_stops_the_loop_and_notifies_with_the_decision(self):
        project = self.make(ROAD, {"features": FEATURES, "decision_lines": {"2": [CRITICAL]}})
        self.done(project.run("--wave", "1"), 10)
        loop = project.loop_json()
        self.assertEqual(loop["status"], "awaiting_user")
        self.assertEqual(loop["reason"], "critical decision: delete the old table? | chose: kept it, only renamed | revert: drop it by hand")
        self.assertEqual(loop["feature"], "a")
        self.assertEqual(loop["options"], ["keep as decided", "change it: <write how>"])
        self.assertEqual(project.meta()["turns"], 2, "the loop stopped at the end of the turn that wrote the line")
        notice = project.notice()
        self.assertIn("critical decision", notice)
        self.assertIn("delete the old table?", notice)

    def test_important_line_does_not_stop_the_loop(self):
        project = self.make(ROAD, {"features": FEATURES, "decision_lines": {"2": [IMPORTANT]}})
        self.done(project.run("--wave", "1"), 0)
        self.assertIn("which format", project.validate_text())

    def test_critical_question_is_never_auto_answered_and_the_answer_resumes(self):
        project = self.make(ROAD, {"features": FEATURES, "decision_lines": {"2": [CRITICAL]}})
        self.done(project.run("--wave", "1"), 10)
        self.assertNotIn("answered automatically", project.validate_text())  # the script left the question to the user
        self.done(project.run("--wave", "1", "--answer", "keep as decided"), 0)  # the old line is before the new mark: no second stop
        self.assertIn("keep as decided", project.answers_text())

    def test_pick_auto_answer_refuses_a_critical_decision(self):
        state = {"reason": run_loop.CRITICAL_DECISION_PREFIX + "x | chose: y | revert: z", "options": ["keep as decided", "change it: <write how>"]}
        self.assertIsNone(run_loop.pick_auto_answer(state))
        self.assertIsNotNone(run_loop.pick_auto_answer({"reason": "small dilemma", "options": ["a (recommended)", "b"]}))

    def test_engine_written_lines_carry_the_turn(self):
        project = self.make("## Stage 1\n- [x] status=pending\n", {"features": {"x": {"paths": "apps/x", "ask": True}}})
        self.done(project.run("--wave", "1"), 0)  # the question is small: answered automatically and noted
        self.assertRegex(project.validate_text(), r"- \[x\] turn=\d+ level=important question answered automatically")


if __name__ == "__main__":
    unittest.main()
