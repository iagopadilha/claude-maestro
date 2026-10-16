"""ProcessRunner: synchronous with timeout, stdin, environment, detached with a log, live pid and kill tree. No network."""
import os
import shutil
import sys
import tempfile
import time
import unittest

from harness_backend.infrastructure.process_runner import DETACHED_PROCESSES, START_FAILURE_CODE, TIMEOUT_CODE, ProcessRunner

PROCESS_DEATH_DEADLINE_SECONDS = 10


def read_text(path):
    with open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def wait_until(condition, deadline_seconds):
    limit = time.time() + deadline_seconds
    while time.time() < limit:
        if condition():
            return True
        time.sleep(0.1)
    return condition()


class TestRunAndWait(unittest.TestCase):
    def setUp(self):
        self.runner = ProcessRunner()
        self.folder = tempfile.mkdtemp(prefix="runner-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_returns_code_output_and_error(self):
        code, output, error = self.runner.run_and_wait(
            [sys.executable, "-c", "import sys; print('hi'); print('bad', file=sys.stderr); sys.exit(3)"], self.folder, 30)
        self.assertEqual((code, output.strip(), error.strip()), (3, "hi", "bad"))

    def test_runs_in_the_requested_folder_with_stdin_and_extra_env(self):
        code, output, _ = self.runner.run_and_wait(
            [sys.executable, "-c", "import os, sys; print(os.getcwd()); print(sys.stdin.read().upper()); print(os.environ['LOOP_TEST_X'])"],
            self.folder, 30, input_text="hello", extra_env={"LOOP_TEST_X": "value"})
        lines = output.splitlines()
        self.assertEqual(code, 0)
        self.assertEqual(os.path.normcase(os.path.realpath(lines[0])), os.path.normcase(os.path.realpath(self.folder)))
        self.assertEqual(lines[1:], ["HELLO", "value"])

    def test_replacement_env_replaces_the_whole_environment(self):
        os.environ["LOOP_TEST_INHERITED"] = "yes"
        self.addCleanup(os.environ.pop, "LOOP_TEST_INHERITED", None)
        env = {name: value for name, value in os.environ.items() if name != "LOOP_TEST_INHERITED"}
        _, output, _ = self.runner.run_and_wait([sys.executable, "-c", "import os; print(os.environ.get('LOOP_TEST_INHERITED', 'missing'))"],
                                                self.folder, 30, env=env)
        self.assertEqual(output.strip(), "missing")

    def test_timeout_kills_and_returns_the_timeout_code(self):
        start = time.time()
        code, _, error = self.runner.run_and_wait([sys.executable, "-c", "import time; time.sleep(60)"], self.folder, 1)
        self.assertEqual(code, TIMEOUT_CODE)
        self.assertIn("timed out", error)
        self.assertLess(time.time() - start, 30)

    def test_missing_binary_does_not_raise(self):
        code, output, error = self.runner.run_and_wait(["binary-that-does-not-exist-xyz"], self.folder, 10)
        self.assertEqual((code, output), (START_FAILURE_CODE, ""))
        self.assertIn("binary-that-does-not-exist-xyz", error)


class TestDetachedAndAlive(unittest.TestCase):
    def setUp(self):
        self.runner = ProcessRunner()
        self.folder = tempfile.mkdtemp(prefix="runner-detached-")
        self.addCleanup(shutil.rmtree, self.folder, True)

    def test_spawn_detached_writes_output_to_the_log_and_kill_tree_ends_it(self):
        log_path = os.path.join(self.folder, "logs", "output.log")
        pid = self.runner.spawn_detached(
            [sys.executable, "-c", "import os, sys, time; print('started', os.environ['LOOP_TEST_Y'], flush=True); time.sleep(60)"],
            self.folder, log_path, extra_env={"LOOP_TEST_Y": "detached"})
        self.addCleanup(self.runner.kill_tree, pid)
        self.assertTrue(self.runner.is_alive(pid))
        self.assertTrue(wait_until(lambda: "started detached" in read_text(log_path), 10))
        self.runner.kill_tree(pid)
        self.assertTrue(wait_until(lambda: not self.runner.is_alive(pid), PROCESS_DEATH_DEADLINE_SECONDS))

    def test_detached_stays_registered_until_it_exits_and_raises_no_live_process_warning(self):
        log_path = os.path.join(self.folder, "fast.log")
        pid = self.runner.spawn_detached([sys.executable, "-c", "print('end')"], self.folder, log_path)
        self.assertIn(pid, DETACHED_PROCESSES, "the Popen reference is kept (no ResourceWarning)")
        self.assertTrue(wait_until(lambda: not self.runner.is_alive(pid), PROCESS_DEATH_DEADLINE_SECONDS))
        self.assertNotIn(pid, DETACHED_PROCESSES, "a process that exited is reaped by is_alive")

    def test_kill_tree_reaps_the_detached_process(self):
        pid = self.runner.spawn_detached([sys.executable, "-c", "import time; time.sleep(60)"], self.folder, os.path.join(self.folder, "slow.log"))
        self.runner.kill_tree(pid)
        self.assertNotIn(pid, DETACHED_PROCESSES)
        self.assertFalse(self.runner.is_alive(pid))

    def test_is_alive_refuses_an_invalid_pid(self):
        for pid in (0, -1, None, "123", True):
            self.assertFalse(self.runner.is_alive(pid), pid)
        self.assertTrue(self.runner.is_alive(os.getpid()))

    def test_kill_tree_of_a_missing_pid_does_not_raise(self):
        self.runner.kill_tree(999999)
        self.runner.kill_tree(0)


if __name__ == "__main__":
    unittest.main()
