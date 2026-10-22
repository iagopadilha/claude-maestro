"""Turn environment (engine/turn_environment.py + run_loop.push_lock_env): the secrets that only came from the app .env stay out of the turn.

Everything lives in temporary folders (fake app .env, fake project); the real ~/.loop-harness is never read.
"""
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402
import turn_environment  # noqa: E402

PASSWORD_VALUE = "hunter2-must-never-be-printed"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TurnEnvironmentTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="turnenv-")
        self.root = os.path.join(self.tmp, "project")
        os.makedirs(self.root)
        self.env_file = os.path.join(self.tmp, "app.env")
        write(self.env_file, "LOOP_SMTP_PASSWORD=%s\nLOOP_NOTIFY_CHANNELS=toast\nLOOP_WEBHOOK_URL=https://hook.example/x\n"
                             "TEST_USER_1_PASSWORD=pw1\nQA_API_TOKEN=tok\nREAL_SECRET=from-file\n" % PASSWORD_VALUE)
        self.environ = {"PATH": "/bin", "LOOP_SMTP_PASSWORD": PASSWORD_VALUE, "LOOP_NOTIFY_CHANNELS": "toast",
                        "LOOP_WEBHOOK_URL": "https://hook.example/x", "TEST_USER_1_PASSWORD": "pw1", "QA_API_TOKEN": "tok",
                        "REAL_SECRET": "the-user-own-value"}

    def build(self, environ=None):
        return turn_environment.build(self.environ if environ is None else environ, self.root, [self.env_file])

    def test_password_is_removed_and_channels_are_kept(self):
        result = self.build()
        self.assertNotIn("LOOP_SMTP_PASSWORD", result["env"])
        self.assertEqual("toast", result["env"]["LOOP_NOTIFY_CHANNELS"])
        self.assertEqual("/bin", result["env"]["PATH"])
        self.assertIn("LOOP_SMTP_PASSWORD", result["removed"])
        self.assertIn("LOOP_WEBHOOK_URL", result["removed"])
        self.assertNotIn(PASSWORD_VALUE, " ".join(result["removed"]))

    def test_real_variable_with_a_different_value_is_kept(self):
        result = self.build()
        self.assertEqual("the-user-own-value", result["env"]["REAL_SECRET"])
        self.assertNotIn("REAL_SECRET", result["removed"])

    def test_test_user_variable_is_kept(self):
        self.assertEqual("pw1", self.build()["env"]["TEST_USER_1_PASSWORD"])

    def test_guard_settings_reach_the_turn_even_with_a_secret_looking_name(self):
        write(self.env_file, "GUARD_SECRET_PATHS=~/private\n")
        result = turn_environment.build({"GUARD_SECRET_PATHS": "~/private"}, self.root, [self.env_file])
        self.assertEqual("~/private", result["env"]["GUARD_SECRET_PATHS"])
        self.assertEqual([], result["removed"])

    def test_name_referenced_in_test_environment_is_kept(self):
        write(os.path.join(self.root, ".specs", "project", "TEST-ENVIRONMENT.md"), "Token: $QA_API_TOKEN and ${LOOP_WEBHOOK_URL}\n")
        result = self.build()
        self.assertEqual("tok", result["env"]["QA_API_TOKEN"])
        self.assertIn("LOOP_WEBHOOK_URL", result["env"])
        self.assertEqual(["LOOP_SMTP_PASSWORD"], result["removed"])

    def test_internal_error_passes_the_full_environment(self):
        result = turn_environment.build(self.environ, self.root, 5)  # an int is not a list of paths
        self.assertEqual(self.environ, result["env"])
        self.assertEqual([], result["removed"])
        self.assertTrue(result["error"])


class PushLockEnvTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="turnenv-lock-")
        self.root = os.path.join(self.tmp, "repo")
        os.makedirs(os.path.join(self.root, ".specs"))
        subprocess.run(["git", "init", "-q", self.root], check=True)
        subprocess.run(["git", "-C", self.root, "remote", "add", "origin", "https://example.com/x.git"], check=True)
        self.env_file = os.path.join(self.tmp, "app.env")
        write(self.env_file, "LOOP_SMTP_PASSWORD=%s\nLOOP_NOTIFY_CHANNELS=toast\n" % PASSWORD_VALUE)
        self.saved = {name: os.environ.get(name) for name in ("LOOP_ENV_FILE", "LOOP_SMTP_PASSWORD", "LOOP_NOTIFY_CHANNELS")}
        os.environ["LOOP_ENV_FILE"] = self.env_file
        os.environ["LOOP_SMTP_PASSWORD"] = PASSWORD_VALUE
        os.environ["LOOP_NOTIFY_CHANNELS"] = "toast"
        self.reported_before = list(run_loop.TURN_ENVIRONMENT_REPORTED)
        del run_loop.TURN_ENVIRONMENT_REPORTED[:]

    def tearDown(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        run_loop.TURN_ENVIRONMENT_REPORTED[:] = self.reported_before

    def test_push_lock_and_headless_are_kept_and_the_log_names_without_value(self):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed):
            env = run_loop.push_lock_env(self.root)
            run_loop.push_lock_env(self.root)  # second turn: no second line
        self.assertNotIn("LOOP_SMTP_PASSWORD", env)
        self.assertEqual("toast", env["LOOP_NOTIFY_CHANNELS"])
        self.assertEqual("1", env["LOOP_HEADLESS"])
        self.assertEqual(os.path.join(os.path.abspath(self.root), ".specs"), env["LOOP_SPECS_DIR"])
        self.assertIn("LOOP_APP_HOME", env)
        self.assertEqual("remote.origin.pushurl", env["GIT_CONFIG_KEY_0"])
        self.assertEqual("1", env["GIT_CONFIG_COUNT"])
        text = printed.getvalue()
        self.assertEqual(1, text.count("[loop] turn environment: 1 secret-looking variable(s) from the app .env left out (LOOP_SMTP_PASSWORD)"))
        self.assertNotIn(PASSWORD_VALUE, text)


if __name__ == "__main__":
    unittest.main()
