"""CLI `harness.py`: serve reusing a live app (fake app and real app), check, install, doctor and usage errors.

Everything in a subprocess with temporary LOOP_DATA_DIR, LOOP_SKILL_DIR and LOOP_ENV_FILE: nothing touches ~/.claude nor
~/.loop-harness. The test that starts the WHOLE app (composition) skips while the other agents' modules do not import yet.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request

from tests.engine.fake_app import FAKE_APP_TOKEN, STUB_APP

APP_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
HARNESS = os.path.join(APP_ROOT_DIR, "harness.py")
GUARD_FAKE = "import json, sys\nrequest = json.load(sys.stdin)\nsys.exit(2 if 'git push' in request.get('tool_input', {}).get('command', '') else 0)\n"
SECRET_VALUE = "doctor-secret-value-xyz"
APP_START_DEADLINE_SECONDS = 30


def app_modules_import():
    """True when the whole app (composition and the wave-2 modules it imports) imports."""
    try:
        import harness_backend.composition  # noqa: F401
        return True
    except Exception:
        return False


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def request_local(url, method="GET", body=None, token=""):
    request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8") if body is not None else None, method=method)
    if token:
        request.add_header("X-Monitor-Token", token)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=10) as response:
        return json.loads(response.read())


class HarnessCliCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="harness-cli-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = os.path.join(self.root, "data")
        self.skill = os.path.join(self.root, "skill")
        os.makedirs(os.path.join(self.skill, "hooks"))
        with open(os.path.join(self.skill, "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("fake skill\n")
        with open(os.path.join(self.skill, "hooks", "guard.py"), "w", encoding="utf-8") as handle:
            handle.write(GUARD_FAKE)
        self.env_file = os.path.join(self.root, "test.env")
        with open(self.env_file, "w", encoding="utf-8") as handle:
            handle.write("LOOP_SMTP_PASSWORD=%s\nLOOP_NOTIFY_REQUIRED=\nLOOP_MONITOR_OPEN=off\n" % SECRET_VALUE)
        self.project = os.path.join(self.root, "project")
        os.makedirs(self.project)

    def env(self):
        env = {name: value for name, value in os.environ.items() if not name.startswith(("LOOP_", "GUARD_"))}
        env.update(LOOP_DATA_DIR=self.data, LOOP_SKILL_DIR=self.skill, LOOP_ENV_FILE=self.env_file, PYTHONIOENCODING="utf-8")
        return env

    def run_harness(self, *args, timeout=120):
        return subprocess.run([sys.executable, HARNESS] + list(args), capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(), timeout=timeout)

    def kill_pid(self, pid):
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"] if os.name == "nt" else ["kill", "-9", str(pid)], capture_output=True)

    def stop_process(self, process):
        self.kill_pid(process.pid)
        try:
            process.wait(10)
        except subprocess.TimeoutExpired:
            pass

    def start_fake_app(self):
        folder = os.path.join(self.root, "fake-app")
        os.makedirs(folder)
        script = os.path.join(folder, "harness.py")
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(STUB_APP)
        process = subprocess.Popen([sys.executable, script, "serve"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=self.env())
        self.addCleanup(process.stdout.close)
        self.addCleanup(self.stop_process, process)
        app_url = process.stdout.readline().strip().split("=", 1)[1]
        return folder, app_url


class TestServeReusesLiveApp(HarnessCliCase):
    def test_registers_the_project_in_the_live_app_prints_the_url_and_exits(self):
        app_folder, app_url = self.start_fake_app()
        start = time.time()
        result = self.run_harness("serve", "--project", self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertLess(time.time() - start, 60)
        project_url = re.search(r"^MONITOR_URL=(\S+)$", result.stdout, re.M).group(1)
        self.assertTrue(project_url.startswith(app_url + "p/"), (project_url, app_url))
        self.assertIn("APP_URL=" + app_url, result.stdout)
        with open(os.path.join(app_folder, "open.txt"), encoding="utf-8") as handle:
            self.assertEqual(os.path.normcase(handle.read().strip()), os.path.normcase(os.path.abspath(self.project)))
        self.assertEqual(request_local(project_url + "api/health")["ok"], True)

    def test_without_project_only_prints_the_app_url(self):
        _, app_url = self.start_fake_app()
        result = self.run_harness("serve")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("MONITOR_URL=" + app_url, result.stdout)

    def test_wrong_token_in_app_json_fails_without_starting_another(self):
        self.start_fake_app()
        path = os.path.join(self.data, "app.json")
        registry = read_json(path)
        self.assertEqual(registry["token"], FAKE_APP_TOKEN)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(dict(registry, token="wrong"), handle)
        result = self.run_harness("serve", "--project", self.project)
        self.assertEqual(result.returncode, 1)
        self.assertIn("did not register the project", result.stderr)

    def test_missing_project_is_a_usage_error(self):
        self.assertEqual(self.run_harness("serve", "--project", os.path.join(self.root, "does-not-exist")).returncode, 2)


@unittest.skipUnless(app_modules_import(), "wave-2 modules (services/repositories/controllers) not translated yet: harness_backend.composition does not import")
class TestServeRealApp(HarnessCliCase):
    def start_real_app(self):
        log_path = os.path.join(self.root, "app.log")
        log = open(log_path, "w", encoding="utf-8")
        self.addCleanup(log.close)
        process = subprocess.Popen([sys.executable, HARNESS, "serve", "--project", self.project, "--no-browser", "--idle-hours", "0",
                                    "--data-dir", self.data], stdout=log, stderr=subprocess.STDOUT, env=self.env())
        self.addCleanup(self.stop_process, process)
        limit = time.time() + APP_START_DEADLINE_SECONDS
        text = ""
        while time.time() < limit:
            with open(log_path, encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            if "APP_URL=" in text or process.poll() is not None:
                break
            time.sleep(0.2)
        self.assertIn("APP_URL=", text, text[-2000:])
        return process, text

    def test_starts_the_app_and_the_second_serve_reuses_it(self):
        process, text = self.start_real_app()
        app_url = re.search(r"^APP_URL=(\S+)$", text, re.M).group(1)
        project_url = re.search(r"^MONITOR_URL=(\S+)$", text, re.M).group(1)
        self.assertTrue(project_url.startswith(app_url + "p/"), (project_url, app_url))
        health = request_local(app_url + "api/app/health")
        self.assertEqual((health["ok"], health["pid"], health["url"]), (True, process.pid, app_url))
        info = request_local(app_url + "api/app/info")
        self.assertNotIn(SECRET_VALUE, json.dumps(info))
        self.assertTrue(info["guard"]["blocks_git_push"])
        second = self.run_harness("serve", "--project", self.project)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("MONITOR_URL=" + project_url, second.stdout, "the same project gets the same url in the live app")
        token = read_json(os.path.join(self.data, "app.json"))["token"]
        self.assertEqual(request_local(app_url + "api/app/shutdown", "POST", {}, token), {"ok": True})
        process.wait(15)
        self.assertFalse(os.path.exists(os.path.join(self.data, "app.json")), "the app deletes app.json on exit")


class TestOtherCommands(HarnessCliCase):
    def test_check_forwards_to_run_loop_with_check(self):
        result = self.run_harness("check", self.project)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is not a git repository", result.stdout + result.stderr)

    def test_loop_forwards_help_to_run_loop(self):
        result = self.run_harness("loop", "--help")
        self.assertEqual(result.returncode, 0)
        self.assertIn("--rewind", result.stdout)

    def test_state_forwards_to_loop_state(self):
        result = self.run_harness("state", "migrate", self.project)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("nothing to migrate", result.stdout)

    def test_install_writes_config_with_the_app_root_and_the_skill(self):
        result = self.run_harness("install", "--data-dir", self.data, "--skill-dir", self.skill)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        config = read_json(os.path.join(self.data, "config.json"))
        self.assertEqual(os.path.normcase(config["app_root_dir"]), os.path.normcase(APP_ROOT_DIR))
        self.assertEqual(os.path.normcase(config["orchestrator_skill_dir"]), os.path.normcase(os.path.abspath(self.skill)))

    def test_doctor_json_never_shows_a_variable_value(self):
        result = self.run_harness("doctor", "--json")
        self.assertNotIn(SECRET_VALUE, result.stdout + result.stderr)
        diagnostics = json.loads(result.stdout)
        self.assertEqual(diagnostics["guard"]["blocks_git_push"], True)
        self.assertEqual(os.path.normcase(diagnostics["skill_dir"]), os.path.normcase(os.path.abspath(self.skill)))
        self.assertIsNone(diagnostics["live_app"])
        self.assertEqual(result.returncode, 1 if diagnostics["problems"] else 0)

    def test_doctor_text(self):
        result = self.run_harness("doctor")
        self.assertIn("guard blocks `git push`", result.stdout)
        self.assertNotIn(SECRET_VALUE, result.stdout)

    def test_no_arguments_and_unknown_command_are_usage_errors(self):
        self.assertEqual(self.run_harness().returncode, 2)
        self.assertEqual(self.run_harness("fly").returncode, 2)
        self.assertEqual(self.run_harness("--help").returncode, 0)


if __name__ == "__main__":
    unittest.main()
