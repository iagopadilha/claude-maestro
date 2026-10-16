"""Support for the projects module tests: real app (AppHttpServer + Router + the module's controllers) on a local port,
temporary data folder (LOOP_DATA_DIR), fake skill (LOOP_SKILL_DIR), real git and a FAKE `claude`.

Nothing here touches ~/.claude or ~/.loop-harness.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from harness_backend.api.router import Router
from harness_backend.api.http_server import AppHttpServer
from harness_backend.config.app_config import load_app_config
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.infrastructure.activity_monitor import ActivityMonitor
from harness_backend.services.projects_module import build_projects_module

TEST_PORT_RANGE = (48100, 48399)  # outside the real app range (47800..47899)
TEST_JOB_TIMEOUT_SECONDS = 60
JOB_WAIT_TIMEOUT_SECONDS = 30
FAKE_LOG_VARIABLE = "FAKE_CLAUDE_LOG"
FAKE_FAIL_VARIABLE = "FAKE_CLAUDE_FAIL"
FAKE_DELAY_VARIABLE = "FAKE_CLAUDE_DELAY"
FAKE_SESSION = "fake-roadmap-session-1"
LOOP_DIR_PARTS = (".specs", "project", "loop")

# Simulated claude -p: writes argv/stdin/cwd to a JSONL file and plays the role of /orchestrator (roadmap/map/refine) in the cwd.
FAKE_CLAUDE_CODE = r'''
import json, os, sys, time
argv = sys.argv[1:]
prompt = argv[argv.index("-p") + 1] if "-p" in argv and argv.index("-p") + 1 < len(argv) and not argv[argv.index("-p") + 1].startswith("--") else ""
stdin_text = sys.stdin.read() if not sys.stdin.isatty() else ""
log_path = os.environ.get("FAKE_CLAUDE_LOG")
if log_path:
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": argv, "stdin": stdin_text, "cwd": os.getcwd()}) + "\n")
time.sleep(float(os.environ.get("FAKE_CLAUDE_DELAY") or 0))
if os.environ.get("FAKE_CLAUDE_FAIL"):
    sys.stderr.write("simulated claude failure\n")
    sys.exit(1)
project = os.path.join(".specs", "project")
def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
result = "nothing"
if prompt.startswith("/orchestrator roadmap"):
    tokens = prompt.split()
    goal = open(tokens[tokens.index("--goal-file") + 1], encoding="utf-8").read()
    write(os.path.join(project, "PROJECT.md"), "# Project\n\n" + goal)
    write(os.path.join(project, "ROADMAP.md"), "# Roadmap\n\n## Stage 1 — Base\n\n"
          "- [api-base] status=pending scope=Medium kind=back paths=src/api\n"
          "- [screen-base] status=pending scope=Small kind=front paths=src/web deps=api-base\n\n"
          "## Open questions\n\n- Which database to use?\n")
    result = "roadmap generated"
elif prompt.startswith("/orchestrator map"):
    write(os.path.join(".specs", "codebase", "STACK.md"), "# Stack\n\nPython.\n")
    result = "mapped"
elif "--resume" in argv:
    path = os.path.join(project, "ROADMAP.md")
    text = open(path, encoding="utf-8").read()
    write(path, text.replace("## Open questions", "- [extra] status=pending scope=Small kind=back paths=src/extra\n\n## Open questions"))
    result = "roadmap refined"
print(json.dumps({"type": "result", "is_error": False, "result": result, "session_id": "fake-roadmap-session-1", "total_cost_usd": 0.42}))
'''

TWO_FEATURE_ROADMAP = ("# Roadmap\n\n## Stage 1 — Base\n\n"
                       "- [api-base] status=pending scope=Medium kind=back paths=src/api\n"
                       "- [screen-base] status=pending scope=Small kind=front paths=src/web deps=api-base\n")


def delete_folder(path):
    def clear_read_only(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    for _ in range(5):
        try:
            shutil.rmtree(path, onerror=clear_read_only)
            return
        except OSError:
            time.sleep(0.2)


def run_git(folder, *args):
    return subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace")


def create_git_repo(folder, files=None):
    """Repo with one commit (files = {relative: text})."""
    os.makedirs(folder, exist_ok=True)
    for relative, text in (files or {"README.md": "# test\n"}).items():
        write_file(os.path.join(folder, relative), text)
    run_git(folder, "init", "-q")
    run_git(folder, "add", "-A")
    run_git(folder, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-q", "-m", "start")
    return folder


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read_file(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def loop_file_path(work_dir, *parts):
    """Path inside `.specs/project/loop/` (e.g. `loop_file_path(repo, "json", "MONITOR.json")`)."""
    return os.path.join(work_dir, *(LOOP_DIR_PARTS + parts))


class ProjectsTestCase(unittest.TestCase):
    """Starts the app with the projects module on a local port; every test gets new folders."""

    folder_dialog_argv = None

    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="harness-projects-")
        self.data_dir = os.path.join(self.temporary_dir, "data")
        self.projects_dir = os.path.join(self.temporary_dir, "projects")
        os.makedirs(self.projects_dir)
        self.original_env = {name: os.environ.get(name) for name in
                             ("LOOP_DATA_DIR", "LOOP_SKILL_DIR", FAKE_LOG_VARIABLE, FAKE_FAIL_VARIABLE, FAKE_DELAY_VARIABLE)}
        skill_dir = os.path.join(self.temporary_dir, "skill")
        write_file(os.path.join(skill_dir, "SKILL.md"), "# fake skill\n")
        write_file(os.path.join(skill_dir, "hooks", "guard.py"), "import sys\nsys.exit(2)\n")
        self.fake_claude_path = os.path.join(self.temporary_dir, "fake_claude.py")
        write_file(self.fake_claude_path, FAKE_CLAUDE_CODE)
        self.claude_calls_path = os.path.join(self.temporary_dir, "claude_calls.jsonl")
        os.environ.update({"LOOP_DATA_DIR": self.data_dir, "LOOP_SKILL_DIR": skill_dir, FAKE_LOG_VARIABLE: self.claude_calls_path})
        for name in (FAKE_FAIL_VARIABLE, FAKE_DELAY_VARIABLE):
            os.environ.pop(name, None)
        self.config = load_app_config(user_data_dir=self.data_dir, claude_binary=self.fake_claude_path, open_browser=False)
        self.log = StructuredLog(self.config.logs_dir)
        self.router = Router()
        self.server = AppHttpServer(self.router, self.config.frontend_dir, ActivityMonitor(0, lambda: None), self.log)
        self.server.open_port(TEST_PORT_RANGE)
        self.module = build_projects_module(self.config, self.log, lambda: self.server.url or "",
                                            folder_dialog_argv=self.folder_dialog_argv, job_timeout_seconds=TEST_JOB_TIMEOUT_SECONDS)
        for controller in self.module.controllers:
            controller.register_routes(self.router)
        self.server_thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        self.server_thread.start()

    def tearDown(self):
        self.wait_for_jobs_to_finish()
        self.server.request_shutdown()
        self.server_thread.join(5)
        self.module.project_service.release_projects_on_shutdown()
        for name, value in self.original_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        delete_folder(self.temporary_dir)

    # ------------------------------------------------ HTTP
    def request(self, method, path, body=None, with_token=True):
        data = json.dumps(body).encode("utf-8") if body is not None else None
        http_request = urllib.request.Request(self.server.url.rstrip("/") + path, data=data, method=method)
        http_request.add_header("Content-Type", "application/json")
        if with_token:
            http_request.add_header("X-Monitor-Token", self.server.token)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(http_request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.loads(error.read().decode("utf-8") or "{}")
            finally:
                error.close()

    def open_project(self, path):
        status, body = self.request("POST", "/api/app/projects/open", {"path": path})
        self.assertEqual(status, 200, body)
        return body["project"]["id"]

    def get_progress(self, project_id):
        status, body = self.request("GET", "/p/%s/api/progress" % project_id)
        self.assertEqual(status, 200, body)
        return body

    def wait_for_job(self, project_id, job_id):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status, job = self.request("GET", "/p/%s/api/jobs/%s" % (project_id, job_id))
            self.assertEqual(status, 200, job)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("job %s did not finish" % job_id)

    def wait_for_jobs_to_finish(self):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while self.module.claude_job_service.is_busy() and time.time() < deadline:
            time.sleep(0.1)

    def read_claude_calls(self):
        try:
            with open(self.claude_calls_path, encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except OSError:
            return []

    # ------------------------------------------------ project scenarios
    def create_ready_project(self, name="ready", approve=True, conformance="LEGACY"):
        """Git repo with .specs/ initialized, Conformance, ROADMAP with 2 features (and approved, when asked)."""
        repo = create_git_repo(os.path.join(self.projects_dir, name))
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": conformance})
        self.assertEqual(status, 200, body)
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        if approve:
            status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
            self.assertEqual(status, 200, body)
        return repo, project_id

    @staticmethod
    def write_live_loop_lock(work_dir):
        """LOOP.lock with the PID of this test process (alive): the app sees `loop running`."""
        write_file(loop_file_path(work_dir, "internal", "LOOP.lock"), str(os.getpid()))

    @staticmethod
    def engine_script_path(script_name):
        return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "harness_backend", "engine", script_name)

    def run_engine_check(self, repo):
        """Engine `run_loop.py <repo> --check`, with the fake claude and no required channels."""
        result = subprocess.run([sys.executable, self.engine_script_path("run_loop.py"), repo, "--check", "--allow-silent",
                                 "--claude", self.fake_claude_path], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", timeout=120)
        return result.stdout + result.stderr
