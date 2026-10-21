"""Every route of the loop-panel-focus-and-wrap-up design answers through the REAL assembled app (build_application):
a wired route never answers "route not found" and never 500. Fake claude, temporary data folder, scratch git repo."""
import json
import os
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from harness_backend.composition import build_application
from harness_backend.config.app_config import load_app_config

from .support_projects import TEST_PORT_RANGE, create_git_repo, delete_folder, write_file


class TestFeatureRoutesWiring(unittest.TestCase):
    ENVIRONMENT_NAMES = ("LOOP_DATA_DIR", "LOOP_SKILL_DIR", "LOOP_USER_SHARED_DIR", "LOOP_CLAUDE_CONFIG_FILE")

    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="harness-wiring-")
        self.original_env = {name: os.environ.get(name) for name in self.ENVIRONMENT_NAMES}
        skill_dir = os.path.join(self.temporary_dir, "skill")
        write_file(os.path.join(skill_dir, "SKILL.md"), "# fake skill\n")
        write_file(os.path.join(skill_dir, "hooks", "guard.py"), "import sys\nsys.exit(2)\n")
        data_dir = os.path.join(self.temporary_dir, "data")
        os.environ.update({"LOOP_DATA_DIR": data_dir, "LOOP_SKILL_DIR": skill_dir,
                           "LOOP_USER_SHARED_DIR": os.path.join(self.temporary_dir, "shared"),
                           "LOOP_CLAUDE_CONFIG_FILE": os.path.join(self.temporary_dir, "claude.json")})
        fake_claude = os.path.join(self.temporary_dir, "fake_claude.py")
        write_file(fake_claude, "import sys\nsys.exit(1)\n")
        config = load_app_config(user_data_dir=data_dir, claude_binary=fake_claude, open_browser=False)
        self.application = build_application(config)
        self.application.server.open_port(TEST_PORT_RANGE)
        self.server_thread = threading.Thread(target=self.application.server.serve_until_stopped, daemon=True)
        self.server_thread.start()
        repo = create_git_repo(os.path.join(self.temporary_dir, "repo"))
        status, body = self.request("POST", "/api/app/projects/open", {"path": repo})
        self.assertEqual(status, 200, body)
        self.prefix = "/p/%s/api" % body["project"]["id"]

    def tearDown(self):
        self.application.server.request_shutdown()
        self.server_thread.join(5)
        self.application.projects_module.project_service.release_projects_on_shutdown()
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        delete_folder(self.temporary_dir)

    def request(self, method, path, body=None):
        server = self.application.server
        data = json.dumps(body if body is not None else {}).encode("utf-8") if method == "POST" else None
        http_request = urllib.request.Request(server.url.rstrip("/") + path, data=data, method=method)
        http_request.add_header("Content-Type", "application/json")
        http_request.add_header("X-Monitor-Token", server.token)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(http_request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8") or "{}")
            finally:
                error.close()

    def assert_answers(self, method, path, body=None, expected=None):
        status, payload = self.request(method, self.prefix + path, body)
        self.assertNotEqual(payload.get("error"), "route not found", "%s %s is not wired" % (method, path))
        self.assertLess(status, 500, "%s %s -> %s %s" % (method, path, status, payload))
        if expected is not None:
            self.assertIn(status, expected, "%s %s -> %s %s" % (method, path, status, payload))
        return status, payload

    def test_decisions_routes(self):
        status, payload = self.assert_answers("GET", "/decisions", expected=(200,))
        self.assertIn("items", payload)
        self.assert_answers("POST", "/decisions/unknown-id/review", expected=(404,))
        self.assert_answers("POST", "/decisions/unknown-id/change", expected=(404,))

    def test_codebase_routes(self):
        self.assert_answers("GET", "/codebase/tree", expected=(200,))
        self.assert_answers("GET", "/codebase/file?path=..%2F..%2Fsecret.txt", expected=(400,))

    def test_loop_settings_and_raise_limit(self):
        status, payload = self.assert_answers("GET", "/loop/settings", expected=(200,))
        self.assertIn("settings", payload)
        self.assert_answers("POST", "/loop/raise-limit", {"max_cost_usd": 1}, expected=(400, 409))

    def test_wrap_up_routes(self):
        self.assert_answers("GET", "/wrap-up", expected=(200, 409, 400))
        self.assert_answers("POST", "/wrap-up/finish", {"mode": "unknown"}, expected=(400, 409))

    def test_follow_up_routes(self):
        self.assert_answers("GET", "/follow-up", expected=(200,))
        self.assert_answers("POST", "/follow-up/send", {"text": "one more thing"}, expected=(409,))


if __name__ == "__main__":
    unittest.main()
