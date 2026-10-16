"""Support for the loop COMMANDS module tests: a temporary project with a fake `.specs`, a real HTTP app (AppHttpServer +
Router) with only the command controllers, fake `project_service` and `loop_status_service`, fake engine scripts.

Nothing is written outside %TEMP% (temporary `LOOP_DATA_DIR`, `LOOP_ENV_FILE` pointing to a missing file), no network
and no real `claude`.
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from harness_backend.api.http_server import AppHttpServer
from harness_backend.api.router import Router
from harness_backend.config.app_config import AppConfig
from harness_backend.domain.project import Project, ProjectContext, ProjectNotFoundError
from harness_backend.infrastructure.activity_monitor import ActivityMonitor
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.loop_command_file_repository import loop_state
from harness_backend.services.loop_commands_module import build_loop_commands_module

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "harness_backend", "engine"))
PROJECT_ID = "proj-test"
ISOLATED_ENV_VARIABLES = ("LOOP_DATA_DIR", "LOOP_ENV_FILE")
ROADMAP = """# Test roadmap

## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base regression=ok

## Stage 2 - Services

- [service-a] status=building scope=Medium kind=back paths=src/a deps=base,dir:src/common
- [service-b] status=pending scope=Small kind=front paths=src/b deps=service-a
- [service-c] status=blocked tries=2 note=gate-rejected scope=Small kind=back paths=src/c deps=base
- [service-d] status=approved scope=Small kind=generic paths=src/d deps=service-c
"""
# fake run_loop for loop/*: with --check it prints check_output.txt (next to the script) and records the argv in
# check_argv.json; without --check it records argv and LOOP_MONITOR_URL in start_record.json.
FAKE_RUN_LOOP_CHECKABLE = """import json, os, sys
folder = os.path.dirname(os.path.abspath(__file__))
if "--check" in sys.argv:
    json.dump(sys.argv[1:], open(os.path.join(folder, "check_argv.json"), "w"))
    sys.stdout.write(open(os.path.join(folder, "check_output.txt"), encoding="utf-8").read())
    sys.exit(0)
json.dump({"argv": sys.argv[1:], "monitor_url": os.environ.get("LOOP_MONITOR_URL")}, open(os.path.join(folder, "start_record.json"), "w"))
"""
CHECK_READY = "[check] WARNING: no remote\n[check] ROADMAP: 3 features\n[check] READY. Command:\n  python \"run_loop.py\" \"repo\" --wave 2\n"
FAKE_RUN_LOOP = "import sys\nopen(sys.argv[1], 'w', encoding='utf-8').write(chr(10).join(sys.argv[2:]))\n"

RUNNER = ProcessRunner()


def is_alive(pid):
    return RUNNER.is_alive(pid)


def wait_for_process_to_end(pid, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline and is_alive(pid):
        time.sleep(0.1)


class FakeProjectService:
    """Minimal IProjectService: an in-memory registry."""

    def __init__(self, projects):
        self.projects = {project.id: project for project in projects}

    def list_projects(self):
        return list(self.projects.values())

    def get_project(self, project_id):
        if project_id not in self.projects:
            raise ProjectNotFoundError(project_id)
        return self.projects[project_id]

    def get_project_context(self, project_id):
        return ProjectContext(self.get_project(project_id))


class FakeLoopStatusService:
    """Same derivation as the real `get_loop_summary` (owned by the read module), reading the work files."""

    def get_loop_summary(self, context):
        work = context.work_dir
        loop_json = read_json(loop_state.state_file(work, "LOOP.json"))
        meta = read_json(loop_state.loop_path(work, "meta.json"))
        try:
            pid = int(loop_state.read(loop_state.loop_path(work, "LOOP.lock")).strip() or 0)
        except ValueError:
            pid = 0
        running = is_alive(pid)
        status = loop_json.get("status") if loop_json.get("status") in loop_state.LOOP_STATUS else None
        reason = loop_json.get("reason")
        if running:
            status = "running"
        elif status == "continue":
            status = "stopped"
            if meta.get("stopped") == "error" and str(meta.get("finished_at") or "") >= str(loop_json.get("at") or ""):
                status, reason = "error", meta.get("reason")
        elif status is None:
            status = "no_loop"
        return {"status": status, "running": running, "pid": pid if running else None, "json": loop_json, "meta": meta, "reason": reason}

    def is_any_loop_running(self):
        return False


def read_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class Fixture:
    """Temporary repo + loop worktree with the real `.specs/project/loop` layout and the app serving only the commands."""

    def __init__(self, roadmap=ROADMAP):
        self.tmp = tempfile.mkdtemp(prefix="harness-commands-")
        self.repo = os.path.join(self.tmp, "project")
        self.work = self.repo + "-worktree-loop"
        self.project_dir = os.path.join(self.work, ".specs", "project")
        self.loop = os.path.join(self.project_dir, "loop")
        os.makedirs(self.repo)
        os.makedirs(loop_state.turns_dir(self.work))
        self.write(os.path.join(self.project_dir, "ROADMAP.md"), roadmap)
        self.output_dir = os.path.join(self.tmp, "output")
        self.transcripts = os.path.join(self.tmp, "claude-projects-empty")
        self.scripts = {}
        self.claude_binary = ""
        self.server = None
        self.thread = None
        self.module = None
        self.config = None
        self.previous_env = {name: os.environ.get(name) for name in ISOLATED_ENV_VARIABLES}
        os.environ["LOOP_DATA_DIR"] = os.path.join(self.tmp, "data")
        os.environ["LOOP_ENV_FILE"] = os.path.join(self.tmp, "missing.env")

    # ------------------------------------------------ files
    def at(self, name):
        return loop_state.loop_path(self.work, name)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def write_json(self, relative, data):
        if relative.startswith("turns/"):
            path = os.path.join(loop_state.turns_dir(self.work), relative.split("/", 1)[1])
        else:
            path = self.at(relative)
        self.write(path, json.dumps(data))

    def set_loop(self, status, **extra):
        data = {"status": status, "turn": 2, "feature": "service-a", "reason": "text", "next": "next", "at": "2026-10-15T10:00:00"}
        data.update(extra)
        self.write_json("LOOP.json", data)

    def set_lock_alive(self):
        self.write(self.at("LOOP.lock"), str(os.getpid()))

    # ------------------------------------------------ app
    def build_config(self):
        return AppConfig(app_root_dir=self.tmp, user_data_dir=os.environ["LOOP_DATA_DIR"],
                         orchestrator_skill_dir=os.path.join(self.tmp, "skill"), guard_path="",
                         frontend_dir=os.path.join(self.tmp, "frontend"), templates_dir=os.path.join(self.tmp, "templates"),
                         engine_dir=ENGINE_DIR, claude_transcripts_dir=self.transcripts, output_dir=self.output_dir,
                         claude_binary=self.claude_binary, idle_hours=0, open_browser=False,
                         engine_script_overrides=self.scripts)

    def start(self, repo=None, transcripts=None):
        """Starts the app (ephemeral port). `repo` swaps the project path (e.g. the loop worktree itself)."""
        if transcripts:
            self.transcripts = transcripts
        self.config = self.build_config()
        self.log = StructuredLog(os.path.join(os.environ["LOOP_DATA_DIR"], "logs"))
        router = Router()
        self.activity_monitor = ActivityMonitor(0, on_idle=lambda: None)
        self.server = AppHttpServer(router, self.config.frontend_dir, self.activity_monitor, self.log)
        self.project = Project(PROJECT_ID, "project", repo or self.repo)
        self.project_service = FakeProjectService([self.project])
        self.module = build_loop_commands_module(self.config, self.log, self.project_service, FakeLoopStatusService(),
                                                 self.activity_monitor, lambda: self.server.url or "")
        for controller in self.module.controllers:
            controller.register_routes(router)
        for provider in self.module.busy_providers:
            self.activity_monitor.add_busy_provider(provider)
        self.server.open_port((0, 0))
        self.thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        self.thread.start()
        return self.module

    def stop_server(self):
        if self.server and self.thread and self.thread.is_alive():
            self.server.request_shutdown()
            self.thread.join(10)

    def stop(self):
        self.stop_server()
        for name, value in self.previous_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        shutil.rmtree(self.tmp, ignore_errors=True)

    @property
    def project_url(self):
        return "%sp/%s/" % (self.server.url, PROJECT_ID)

    def call(self, path, method="GET", body=None, token="default", project_id=PROJECT_ID, raw_body=None):
        """(status, json) of `/p/<project>/api<path>`; a legacy path `/api/x` becomes `/p/<project>/api/x`."""
        if path.startswith("/api/"):
            path = path[len("/api"):]
        return self.call_raw("/p/%s/api%s" % (project_id, path), method, body, token, raw_body)

    def call_raw(self, path, method="GET", body=None, token="default", raw_body=None):
        headers = {"Content-Type": "application/json"}
        if token == "default":
            token = self.server.token
        if token:
            headers["X-Monitor-Token"] = token
        data = None
        if method != "GET":
            data = raw_body if raw_body is not None else json.dumps(body if body is not None else {}).encode("utf-8")
        request = urllib.request.Request(self.server.url.rstrip("/") + path, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))

    # ------------------------------------------------ waiting
    def wait_for_file(self, path, seconds=15):
        deadline = time.time() + seconds
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, encoding="utf-8") as handle:
                    return handle.read()
            time.sleep(0.1)
        raise AssertionError("the fake script did not run: %s" % path)

    def read_answers(self):
        path = os.path.join(self.loop, "ANSWERS.md")
        if not os.path.exists(path):
            return ""
        with open(path, encoding="utf-8") as handle:
            return handle.read()

    def prepare_relaunch(self):
        fake = os.path.join(self.tmp, "fake_run_loop.py")
        output = os.path.join(self.tmp, "argv.txt")
        self.write(fake, FAKE_RUN_LOOP)
        self.write_json("LOOP-CMD.json", {"argv": [sys.executable, fake, output, "--wave", "2"], "cwd": self.tmp})
        return output


class CommandsTestCase(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        self.addCleanup(self.fixture.stop)
