#!/usr/bin/env python3
"""Every orchestrator argv (normal, correction/resume) carries `--model <value>`; an empty value sends no flag."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
from test_loop import LoopCase  # noqa: E402
import run_loop  # noqa: E402

ROAD = "## Stage 1\n- [a] status=pending\n"


class Opts:
    claude = "claude"
    permission_mode = "auto"
    budget = 5.0


class TestOrchestratorModel(LoopCase):
    def make_logged(self, extra=None):
        scenario = dict({"features": {"a": {"paths": "apps/a"}}}, **(extra or {}))
        project = self.make(ROAD, scenario)
        project.argv_log = project.scenario.replace("scenario.json", "argv.jsonl")
        with open(project.scenario, "w", encoding="utf-8") as handle:
            json.dump(dict(scenario, argv_log=project.argv_log), handle)
        return project

    def argvs(self, project):
        with open(project.argv_log, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def model_of(self, argv):
        return argv[argv.index("--model") + 1] if "--model" in argv else None

    def test_default_is_opus_on_every_turn(self):
        project = self.make_logged()
        self.done(project.run("--wave", "1"), 0)
        argvs = self.argvs(project)
        self.assertTrue(argvs)
        self.assertEqual({self.model_of(argv) for argv in argvs}, {"opus"})

    def test_option_value_is_used(self):
        project = self.make_logged()
        self.done(project.run("--wave", "1", "--orchestrator-model", "sonnet"), 0)
        self.assertEqual({self.model_of(argv) for argv in self.argvs(project)}, {"sonnet"})

    def test_empty_value_sends_no_flag(self):
        project = self.make_logged()
        self.done(project.run("--wave", "1", "--orchestrator-model", ""), 0)
        self.assertEqual({self.model_of(argv) for argv in self.argvs(project)}, {None})

    def test_correction_turn_in_the_same_session_carries_the_model_too(self):
        project = self.make_logged({"forget_loop_json": True})
        self.done(project.run("--wave", "1"), 0)
        argvs = self.argvs(project)
        self.assertTrue([argv for argv in argvs if "--resume" in argv], "a correction turn ran")
        self.assertEqual({self.model_of(argv) for argv in argvs}, {"opus"})

    def test_claude_command_helper(self):
        opts = Opts()
        self.assertEqual(run_loop.model_arguments(opts), ["--model", "opus"])  # attribute missing: the default
        opts.orchestrator_model = ""
        self.assertEqual(run_loop.model_arguments(opts), [])
        opts.orchestrator_model = "opus"
        argv = run_loop.claude_command(opts, "go")
        self.assertEqual(argv[argv.index("--model") + 1], "opus")
        self.assertLess(argv.index("--model"), argv.index("--disallowedTools"))


if __name__ == "__main__":
    unittest.main()
