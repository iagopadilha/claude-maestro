"""AppController (/api/app/health, info, notify/test, shutdown) and AppDiagnosticsService. No external network, no real claude,
never touching ~/.claude nor the real ~/.loop-harness (fake guard and claude in a temporary folder)."""
import json
import os
import shutil
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

from harness_backend.api.controllers.app_controller import APP_VERSION, AppController
from harness_backend.api.http_server import AppHttpServer
from harness_backend.api.router import Request, Router, normalize_response
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.services.app_diagnostics_service import AppDiagnosticsService

from unittest import mock

from harness_backend.config import skill_locator

ENGINE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "harness_backend", "engine")
TEST_PORT_RANGE = (47900, 47999)
GUARD_FAKE = "import json, sys\nrequest = json.load(sys.stdin)\nsys.exit(2 if 'git push' in request.get('tool_input', {}).get('command', '') else 0)\n"
SECRET_VALUE = "secret-value-that-never-leaves-xyz"
NOTIFY_VARIABLES = ("LOOP_NOTIFY_CHANNELS", "LOOP_NOTIFY_REQUIRED", "LOOP_NOTIFY_TO", "LOOP_SMTP_HOST", "LOOP_SMTP_PORT", "LOOP_SMTP_USER",
                    "LOOP_SMTP_PASSWORD", "LOOP_SMTP_FROM", "LOOP_SMTP_SECURITY", "LOOP_WEBHOOK_URL", "LOOP_ENV_FILE")


class FakeServer:
    def __init__(self, url="http://127.0.0.1:47999/"):
        self.url = url
        self.shutdown_requested = False

    def request_shutdown(self):
        self.shutdown_requested = True


class FakeDiagnostics:
    def __init__(self):
        self.requested_channels = []

    def build_diagnostics(self):
        return {"skill_dir": "x", "problems": []}

    def send_test_notification(self, channels=None):
        self.requested_channels.append(channels)
        if channels == ["does-not-exist"]:
            return {"ok": False, "results": {}, "error": "unknown channel: does-not-exist"}
        return {"ok": True, "results": {"console": "ok: ok"}, "error": ""}


def call_route(router, method, path, body=None):
    route, params = router.find_route(method, path)
    assert route is not None, (method, path)
    return normalize_response(route.handler(Request(method, path, params, {}, body or {})))


class EnvironmentIsolation(unittest.TestCase):
    def isolate_notify_variables(self, env_content=""):
        """The test's own notify environment: explicit temporary .env (the real one is never read), variables restored at the end."""
        saved = {name: os.environ.get(name) for name in NOTIFY_VARIABLES}

        def restore():
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        self.addCleanup(restore)
        for name in NOTIFY_VARIABLES:
            os.environ.pop(name, None)
        folder = tempfile.mkdtemp(prefix="app-controller-env-")
        self.addCleanup(shutil.rmtree, folder, True)
        path = os.path.join(folder, "test.env")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(env_content)
        os.environ["LOOP_ENV_FILE"] = path


class TestRoutesWithFakes(unittest.TestCase):
    def setUp(self):
        self.server = FakeServer()
        self.diagnostics = FakeDiagnostics()
        monitor = types.SimpleNamespace(seconds_until_shutdown=lambda: 120)
        projects = types.SimpleNamespace(list_projects=lambda: ["a", "b"])
        self.router = Router()
        AppController(None, StructuredLog(""), self.server, monitor, projects, app_diagnostics_service=self.diagnostics).register_routes(self.router)

    def test_health(self):
        response = call_route(self.router, "GET", "/api/app/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body, {"ok": True, "pid": os.getpid(), "version": APP_VERSION, "url": self.server.url})

    def test_info_adds_the_process_data(self):
        body = call_route(self.router, "GET", "/api/app/info").body
        self.assertEqual((body["ok"], body["seconds_until_shutdown"], body["open_projects"], body["skill_dir"]), (True, 120, 2, "x"))

    def test_notify_test_validates_channels(self):
        self.assertEqual(call_route(self.router, "POST", "/api/app/notify/test", {"channels": "console"}).status, 400)
        self.assertEqual(call_route(self.router, "POST", "/api/app/notify/test", {"channels": ["does-not-exist"]}).status, 400)
        response = call_route(self.router, "POST", "/api/app/notify/test", {"channels": ["console"]})
        self.assertEqual((response.status, response.body["ok"]), (200, True))
        self.assertEqual(call_route(self.router, "POST", "/api/app/notify/test", {}).status, 200)
        self.assertEqual(self.diagnostics.requested_channels[-1], None)

    def test_shutdown_requests_the_shutdown(self):
        response = call_route(self.router, "POST", "/api/app/shutdown")
        self.assertEqual(response.body, {"ok": True})
        self.assertTrue(self.server.shutdown_requested)

    def test_posts_require_a_token_and_gets_do_not(self):
        requires = {(route.method, route.pattern): route.requires_token for route in self.router.routes}
        self.assertEqual(requires, {("GET", "/api/app/health"): False, ("GET", "/api/app/info"): False,
                                    ("POST", "/api/app/notify/test"): True, ("POST", "/api/app/shutdown"): True})


class TestRealDiagnostics(EnvironmentIsolation):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="app-diagnostics-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.guard = os.path.join(self.folder, "skill", "hooks", "guard.py")
        os.makedirs(os.path.dirname(self.guard))
        with open(self.guard, "w", encoding="utf-8") as handle:
            handle.write(GUARD_FAKE)
        self.claude = os.path.join(self.folder, "fake_claude.py")
        open(self.claude, "w").close()
        self.toolkit = os.path.join(self.folder, "toolkit")  # a fake claude-dev-toolkit named by dev_toolkit_dir (the real one is never looked up)
        os.makedirs(os.path.join(self.toolkit, "skills", "backend-dev"))
        with open(os.path.join(self.toolkit, "skills", "backend-dev", "SKILL.md"), "w", encoding="utf-8") as handle:
            handle.write("toolkit backend-dev")
        self.write_config({"dev_toolkit_dir": self.toolkit})
        self.config = types.SimpleNamespace(
            app_root_dir=self.folder, user_data_dir=os.path.join(self.folder, "data"), orchestrator_skill_dir=os.path.dirname(os.path.dirname(self.guard)),
            guard_path=self.guard, engine_dir=ENGINE_DIR, claude_binary=self.claude,
            engine_script_path=lambda name: os.path.join(ENGINE_DIR, name + ".py"))
        runner = ProcessRunner()
        self.service = AppDiagnosticsService(self.config, runner, ClaudeClient(runner, self.claude))

    def write_config(self, content):
        os.makedirs(os.path.join(self.folder, "data"), exist_ok=True)
        with open(os.path.join(self.folder, "data", "config.json"), "w", encoding="utf-8") as handle:
            json.dump(content, handle)

    def write_claude_md(self, folder, text):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "CLAUDE.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_info_never_returns_a_variable_value(self):
        self.isolate_notify_variables("LOOP_SMTP_PASSWORD=%s\nLOOP_SMTP_HOST=smtp.example.invalid\nLOOP_SMTP_FROM=you@example.com\n"
                                      "LOOP_NOTIFY_TO=someone@example.com\nLOOP_NOTIFY_CHANNELS=console,email\n"
                                      "LOOP_NOTIFY_REQUIRED=email,webhook\n" % SECRET_VALUE)
        diagnostics = self.service.build_diagnostics()
        text = json.dumps(diagnostics)
        for value in (SECRET_VALUE, "someone@example.com", "smtp.example.invalid"):
            self.assertNotIn(value, text)
        channels = {channel["name"]: channel for channel in diagnostics["notify_channels"]}
        self.assertEqual((channels["email"]["enabled"], channels["email"]["configured"], channels["email"]["required"]), (True, True, True))
        self.assertEqual((channels["webhook"]["enabled"], channels["webhook"]["configured"], channels["webhook"]["required"]), (False, False, True))
        self.assertEqual(diagnostics["guard"], {"path": self.guard, "found": True, "blocks_git_push": True})
        self.assertTrue(diagnostics["claude"]["on_path"])
        self.assertEqual(diagnostics["problems"], ["required notify channel webhook is not configured"])

    def test_without_required_variable_no_channel_is_required(self):
        self.isolate_notify_variables("LOOP_WEBHOOK_URL=http://127.0.0.1:1/hook\n")
        diagnostics = self.service.build_diagnostics()
        channels = {channel["name"]: channel for channel in diagnostics["notify_channels"]}
        self.assertFalse(any(channel["required"] for channel in channels.values()))
        self.assertTrue(channels["webhook"]["configured"])
        self.assertEqual(diagnostics["problems"], [])

    def test_a_guard_that_does_not_block_and_a_missing_guard_become_problems(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        with open(self.guard, "w", encoding="utf-8") as handle:
            handle.write("import sys\nsys.exit(0)\n")
        self.assertIn("guard.py did not block `git push` (expected code 2): do not run the loop unsupervised", self.service.build_diagnostics()["problems"])
        self.config.guard_path = ""
        diagnostics = self.service.build_diagnostics()
        self.assertEqual(diagnostics["guard"]["found"], False)
        self.assertIn("the skill's hooks/guard.py was not found", diagnostics["problems"])

    def test_toolkit_found_is_reported_without_a_problem(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        diagnostics = self.service.build_diagnostics()
        self.assertEqual(diagnostics["toolkit"], {"path": os.path.abspath(self.toolkit), "found": True})
        self.assertEqual(diagnostics["problems"], [])
        self.assertEqual(diagnostics["standards"], [])

    def test_a_missing_toolkit_is_a_problem_with_the_two_plugin_lines(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        self.write_config({})
        with mock.patch.object(skill_locator, "REPO_ROOT_DIR", os.path.join(self.folder, "no-sibling")):
            diagnostics = self.service.build_diagnostics()
        self.assertEqual(diagnostics["toolkit"], {"path": "", "found": False})
        problem = [item for item in diagnostics["problems"] if item.startswith("claude-dev-toolkit not found")]
        self.assertEqual(len(problem), 1, diagnostics["problems"])
        self.assertIn("/plugin marketplace add iagopadilha/claude-dev-toolkit", problem[0])
        self.assertIn("/plugin install claude-dev-toolkit@claude-dev-toolkit", problem[0])

    def test_standards_check_warns_for_each_binding_whose_skill_does_not_resolve(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        project = os.path.join(self.folder, "project")
        self.write_claude_md(project, "# P\n\n## Standards\nbackend: backend-dev\nfrontend: acme:ghost-front-skill\n")
        diagnostics = self.service.build_diagnostics([project])
        rows = {row["slot"]: row for row in diagnostics["standards"]}
        self.assertEqual((rows["backend"]["resolved"], rows["frontend"]["resolved"]), (True, False))
        self.assertEqual(diagnostics["problems"], [])
        self.assertEqual(len([item for item in diagnostics["warnings"] if item.startswith("standards:")]), 1)
        warning = [item for item in diagnostics["warnings"] if item.startswith("standards:")][0]
        self.assertIn("frontend", warning)
        self.assertIn("acme:ghost-front-skill", warning)

    def test_standards_check_reads_each_subproject_and_not_the_monorepo_root(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        project = os.path.join(self.folder, "mono")
        self.write_claude_md(project, "## Standards\nbackend: root-skill-ignored\n")
        self.write_claude_md(os.path.join(project, "api"), "## Standards\nbackend: backend-dev\n")
        specs = os.path.join(project, ".specs", "project")
        os.makedirs(specs)
        with open(os.path.join(specs, "SUBPROJECTS.json"), "w", encoding="utf-8") as handle:
            json.dump({"repos": {"mono": {"monorepo": True, "confirmed": True, "projects": [{"path": "api"}]}}}, handle)
        diagnostics = self.service.build_diagnostics([project])
        self.assertEqual([(row["slot"], row["skill"], row["resolved"]) for row in diagnostics["standards"]], [("backend", "backend-dev", True)])

    def test_no_project_means_no_standards_check(self):
        self.isolate_notify_variables("LOOP_NOTIFY_REQUIRED=\n")
        self.assertEqual(self.service.build_diagnostics()["standards"], [])

    def test_test_notification_through_the_console_and_unknown_channel(self):
        self.isolate_notify_variables("")
        result = self.service.send_test_notification(["console"])
        self.assertEqual((result["ok"], list(result["results"])), (True, ["console"]), result)
        self.assertTrue(result["results"]["console"].startswith("ok"))
        self.assertIn("unknown", self.service.send_test_notification(["smoke-signal"])["error"])


class TestHttpServerWithAppControllerOnly(unittest.TestCase):
    """The hub starts with the AppController alone (without the other agents' modules): token and Host apply."""

    def setUp(self):
        router = Router()
        monitor = types.SimpleNamespace(seconds_until_shutdown=lambda: None, record_activity=lambda: None)
        frontend_dir = tempfile.mkdtemp(prefix="app-front-")
        self.addCleanup(shutil.rmtree, frontend_dir, True)
        self.server = AppHttpServer(router, frontend_dir, monitor, StructuredLog(""))
        AppController(None, StructuredLog(""), self.server, monitor, types.SimpleNamespace(list_projects=lambda: []),
                      app_diagnostics_service=FakeDiagnostics()).register_routes(router)
        self.url = self.server.open_port(TEST_PORT_RANGE)
        self.thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop(self):
        self.server.request_shutdown()
        self.thread.join(5)

    def request(self, method, path, token=None):
        request = urllib.request.Request(self.url + path, data=b"{}" if method == "POST" else None, method=method)
        if token:
            request.add_header("X-Monitor-Token", token)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as failure:
            with failure:
                return failure.code, json.loads(failure.read())

    def test_health_without_token_and_shutdown_only_with_token(self):
        status, body = self.request("GET", "api/app/health")
        self.assertEqual((status, body["ok"], body["url"]), (200, True, self.url))
        self.assertEqual(self.request("POST", "api/app/shutdown")[0], 403)
        self.assertEqual(self.request("POST", "api/app/shutdown", token="wrong")[0], 403)
        self.assertEqual(self.request("POST", "api/app/shutdown", token=self.server.token), (200, {"ok": True}))
        self.thread.join(5)
        self.assertFalse(self.thread.is_alive(), "the server stopped after the shutdown")


if __name__ == "__main__":
    unittest.main()
