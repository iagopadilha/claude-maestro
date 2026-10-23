"""Root `install.py`: every check mocked (no real git, claude, gh or harness.py runs), data folder in a temp dir."""
import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest

REPO_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
INSTALL_SCRIPT = os.path.join(REPO_ROOT_DIR, "install.py")


def load_install_module():
    spec = importlib.util.spec_from_file_location("install_under_test", INSTALL_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


install = load_install_module()


class FakeWorld:
    """`which` + `run` doubles: tools present/absent, `--version` answers, harness.py exit codes; records every call."""

    def __init__(self, missing=(), silent=(), harness_codes=None):
        self.missing = set(missing)
        self.silent = set(silent)
        self.harness_codes = dict(harness_codes or {})
        self.calls = []

    def which(self, name):
        return None if name in self.missing else "/fake/bin/" + name

    def run(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[-1] == "--version":
            name = os.path.basename(argv[0])
            if name in self.silent:
                return subprocess.CompletedProcess(argv, 1, "", "")
            return subprocess.CompletedProcess(argv, 0, "%s version 9.9.9\n" % name, "")
        command = argv[2]
        return subprocess.CompletedProcess(argv, self.harness_codes.get(command, 0), "", "")

    def harness_commands(self):
        return [argv[2] for argv in self.calls if argv[-1] != "--version"]


class InstallCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="install-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.data_dir = os.path.join(self.tmp, "data")
        self.lines = []

    def installer(self, world, assume_yes=True, answer="y", python_version=(3, 12, 0), toolkit="/fake/toolkit"):
        def ask(question):
            self.lines.append("ASK " + question)
            return answer
        return install.Installer(assume_yes=assume_yes, data_dir=self.data_dir, which=world.which, run=world.run, ask=ask,
                                 out=self.lines.append, python_version=python_version, environ={}, find_toolkit=lambda: toolkit)

    def output(self):
        return "\n".join(self.lines)


class TestInstallScript(InstallCase):
    def test_everything_present_passes_creates_the_data_folder_and_prints_the_next_steps(self):
        world = FakeWorld()
        self.assertEqual(self.installer(world).main(), 0, self.output())
        self.assertTrue(os.path.isfile(os.path.join(self.data_dir, ".env")))
        self.assertEqual(world.harness_commands(), ["install", "doctor"])
        install_call = next(argv for argv in world.calls if argv[-1] != "--version")
        self.assertEqual(install_call[-2:], ["--data-dir", self.data_dir])
        text = self.output()
        for command in install.PLUGIN_COMMANDS + install.TOOLKIT_COMMANDS:
            self.assertIn(command, text)
        self.assertIn("PASS  claude-dev-toolkit found: /fake/toolkit", text)
        self.assertIn("python app/harness.py serve", text)
        self.assertIn("ALL PASSED", text)
        self.assertNotIn("FAIL ", text)

    def test_toolkit_lines_come_after_the_maestro_ones(self):
        self.assertEqual(self.installer(FakeWorld()).main(), 0, self.output())
        text = self.output()
        self.assertEqual(install.TOOLKIT_COMMANDS, ("/plugin marketplace add iagopadilha/claude-dev-toolkit",
                                                    "/plugin install claude-dev-toolkit@claude-dev-toolkit"))
        self.assertLess(text.index(install.PLUGIN_COMMANDS[-1]), text.index(install.TOOLKIT_COMMANDS[0]))

    def test_a_missing_toolkit_is_only_a_warning_and_doctor_is_told_so(self):
        world = FakeWorld()
        self.assertEqual(self.installer(world, toolkit="").main(), 0, self.output())
        text = self.output()
        self.assertIn("WARN  claude-dev-toolkit not found", text)
        self.assertIn("ALL PASSED", text)
        for command in install.TOOLKIT_COMMANDS:
            self.assertIn(command, text)
        doctor_call = next(argv for argv in world.calls if argv[-1] != "--version" and argv[2] == "doctor")
        self.assertIn("--toolkit-optional", doctor_call)

    def test_a_found_toolkit_leaves_the_doctor_call_alone(self):
        world = FakeWorld()
        self.installer(world).main()
        doctor_call = next(argv for argv in world.calls if argv[-1] != "--version" and argv[2] == "doctor")
        self.assertNotIn("--toolkit-optional", doctor_call)

    def test_existing_env_is_never_overwritten(self):
        os.makedirs(self.data_dir)
        env_path = os.path.join(self.data_dir, ".env")
        with open(env_path, "w", encoding="utf-8") as handle:
            handle.write("LOOP_NOTIFY_CHANNELS=file\n")
        self.assertEqual(self.installer(FakeWorld()).main(), 0, self.output())
        with open(env_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "LOOP_NOTIFY_CHANNELS=file\n")
        self.assertIn("left untouched", self.output())

    def test_missing_claude_is_a_hard_failure_and_nothing_is_written(self):
        world = FakeWorld(missing={"claude"})
        self.assertEqual(self.installer(world).main(), 1)
        self.assertIn("FAIL  `claude` not found", self.output())
        self.assertFalse(os.path.exists(self.data_dir))
        self.assertEqual(world.harness_commands(), [])

    def test_claude_that_does_not_answer_version_fails(self):
        self.assertEqual(self.installer(FakeWorld(silent={"claude"})).main(), 1)
        self.assertIn("`claude --version` did not answer", self.output())

    def test_missing_git_fails(self):
        self.assertEqual(self.installer(FakeWorld(missing={"git"})).main(), 1)
        self.assertIn("FAIL  `git` not found", self.output())

    def test_missing_gh_is_only_a_warning(self):
        self.assertEqual(self.installer(FakeWorld(missing={"gh"})).main(), 0, self.output())
        self.assertIn("WARN  `gh` not found", self.output())

    def test_old_python_fails(self):
        self.assertEqual(self.installer(FakeWorld(), python_version=(3, 9, 18)).main(), 1)
        self.assertIn("FAIL  Python 3.9.18 found", self.output())

    def test_doctor_problem_makes_the_exit_code_non_zero(self):
        world = FakeWorld(harness_codes={"doctor": 1})
        self.assertEqual(self.installer(world).main(), 1)
        self.assertIn("harness.py doctor reported a problem", self.output())

    def test_harness_install_failure_skips_doctor(self):
        world = FakeWorld(harness_codes={"install": 1})
        self.assertEqual(self.installer(world).main(), 1)
        self.assertEqual(world.harness_commands(), ["install"])

    def test_without_yes_it_asks_once_and_no_cancels(self):
        world = FakeWorld()
        self.assertEqual(self.installer(world, assume_yes=False, answer="n").main(), 1)
        self.assertEqual(sum(line.startswith("ASK ") for line in self.lines), 1)
        self.assertFalse(os.path.exists(self.data_dir))
        self.assertEqual(world.harness_commands(), [])

    def test_without_yes_enter_accepts(self):
        self.assertEqual(self.installer(FakeWorld(), assume_yes=False, answer="").main(), 0, self.output())

    def test_never_asks_for_a_token(self):
        self.installer(FakeWorld(), assume_yes=False, answer="").main()
        asked = " ".join(line for line in self.lines if line.startswith("ASK ")).lower()
        for word in ("token", "api key", "password"):
            self.assertNotIn(word, asked)

    def test_data_dir_defaults_to_the_variable_then_home(self):
        world = FakeWorld()
        from_variable = install.Installer(which=world.which, run=world.run, environ={"LOOP_DATA_DIR": self.data_dir})
        self.assertEqual(from_variable.data_dir, os.path.abspath(self.data_dir))
        default = install.Installer(which=world.which, run=world.run, environ={})
        self.assertEqual(default.data_dir, os.path.join(os.path.expanduser("~"), ".loop-harness"))

    def test_arguments(self):
        options = install.parse_arguments(["--yes", "--data-dir", self.data_dir])
        self.assertTrue(options.yes)
        self.assertEqual(options.data_dir, self.data_dir)


if __name__ == "__main__":
    unittest.main()
