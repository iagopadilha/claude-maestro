"""Support for the loop read tests: fake project in a temporary folder (repo + worktree with a .specs like the real one),
FAKE `project_service` (dict id -> Project) and the real app (AppHttpServer + Router + read module) on an ephemeral port.

Nothing is written outside %TEMP%; LOOP_DATA_DIR points to a temporary folder; no external network and no real claude.
"""
import datetime
import json
import os
import re
import shutil
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

from harness_backend.api.http_server import AppHttpServer
from harness_backend.api.router import Router
from harness_backend.config.app_config import AppConfig
from harness_backend.domain.project import Project, ProjectContext, ProjectNotFoundError
from harness_backend.infrastructure.activity_monitor import ActivityMonitor
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.loop_file_repository import loop_state
from harness_backend.services.loop_read_module import build_loop_read_module

PROJECT_ID = "p-test01"
EPHEMERAL_PORT = (0, 0)
REQUEST_TIMEOUT_SECONDS = 15
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
ROADMAP = """# Test roadmap

## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base regression=ok

## Stage 2 - Services

- [service-a] status=building scope=Medium kind=back paths=src/a deps=base,dir:src/common
- [service-b] status=pending scope=Small kind=front paths=src/b deps=service-a
- [service-c] status=blocked tries=2 note=gate-rejected scope=Small kind=back paths=src/c deps=base
- [service-d] status=approved scope=Small kind=generic paths=src/d deps=service-c
"""


class FakeProjectService:
    """Minimal IProjectService: dict id -> Project."""

    def __init__(self):
        self.projects = {}

    def register_project(self, project):
        self.projects[project.id] = project
        return project

    def list_projects(self):
        return list(self.projects.values())

    def get_project(self, project_id):
        if project_id not in self.projects:
            raise ProjectNotFoundError(project_id)
        return self.projects[project_id]

    def get_project_context(self, project_id):
        return ProjectContext(self.get_project(project_id))


class FakeProject:
    """Temporary repo + worktree with the engine's .specs/project/loop layout."""

    def __init__(self, root, name="project", roadmap=ROADMAP, project_id=PROJECT_ID):
        self.repo = os.path.join(root, name)
        self.work = self.repo + "-worktree-loop"
        self.project_dir = os.path.join(self.work, ".specs", "project")
        os.makedirs(self.repo)
        os.makedirs(self.turns_dir)
        self.write(os.path.join(self.project_dir, "ROADMAP.md"), roadmap)
        self.write(loop_state.loop_path(self.work, "meta.json"), json.dumps({"turns": 1}))  # a worktree where a loop ran (a bare one is not the work dir)
        self.project = Project(id=project_id, name=name, repo_path=self.repo)

    @property
    def turns_dir(self):
        return loop_state.turns_dir(self.work)

    def at(self, name):
        return loop_state.loop_path(self.work, name)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)

    def write_json(self, relative, data):
        if relative.startswith("turns/"):  # turns/NNN.json lives in internal/turns
            path = os.path.join(self.turns_dir, relative.split("/", 1)[1])
        else:
            path = self.at(relative)
        self.write(path, json.dumps(data))

    def set_loop(self, status, **extra):
        data = {"status": status, "turn": 2, "feature": "service-a", "reason": "text", "next": "next step", "at": "2026-10-15T10:00:00"}
        data.update(extra)
        self.write_json("LOOP.json", data)

    def set_lock_alive(self):
        self.write(self.at("LOOP.lock"), str(os.getpid()))

    def snapshot(self, number, roadmap=None, loop=None, head=None):
        folder = loop_state.snap_dir(self.work, number)
        self.write(os.path.join(folder, "snapshot.json"), json.dumps({"turn": number, "head": head}) if head else "{}")
        if loop is not None:
            self.write(os.path.join(folder, "LOOP.json"), json.dumps(loop))
        if roadmap is not None:
            self.write(os.path.join(folder, "ROADMAP.md"), roadmap)
        return folder


class Fixture:
    """Starts the app with the read module and a fake project registered as `PROJECT_ID`."""

    def __init__(self, roadmap=ROADMAP):
        self.tmp = tempfile.mkdtemp(prefix="loop-read-")
        self.data_dir = os.path.join(self.tmp, "harness-data")
        self.previous_env = os.environ.get(DATA_DIR_VARIABLE)
        os.environ[DATA_DIR_VARIABLE] = self.data_dir
        self.fake_project = FakeProject(self.tmp, roadmap=roadmap)
        self.project_service = FakeProjectService()
        self.project_service.register_project(self.fake_project.project)
        self.server = None
        self.thread = None
        self.module = None
        self.activity_monitor = None

    def __getattr__(self, name):  # repo, work, at, write, set_loop... of the main project
        return getattr(self.__dict__["fake_project"], name)

    def add_project(self, name, project_id, roadmap=ROADMAP):
        project = FakeProject(self.tmp, name=name, roadmap=roadmap, project_id=project_id)
        self.project_service.register_project(project.project)
        return project

    def start(self, idle_hours=2.0, projects_dir=None):
        config = AppConfig(
            app_root_dir=self.tmp, user_data_dir=self.data_dir, orchestrator_skill_dir="", guard_path="",
            frontend_dir=os.path.join(self.tmp, "frontend"), templates_dir=os.path.join(self.tmp, "templates"),
            engine_dir=os.path.dirname(loop_state.__file__),
            claude_transcripts_dir=projects_dir or os.path.join(self.tmp, "claude-projects-empty"),
            output_dir=os.path.join(self.tmp, "output"), port_range=EPHEMERAL_PORT, idle_hours=idle_hours, open_browser=False)
        log = StructuredLog(config.logs_dir)
        router = Router()
        self.activity_monitor = ActivityMonitor(config.idle_hours, on_idle=lambda: None)
        self.server = AppHttpServer(router, config.frontend_dir, self.activity_monitor, log)
        self.module = build_loop_read_module(config, log, self.project_service, self.activity_monitor)
        for controller in self.module.controllers:
            controller.register_routes(router)
        for provider in self.module.busy_providers:
            self.activity_monitor.add_busy_provider(provider)
        self.server.open_port(config.port_range)
        self.thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        self.thread.start()
        return self.module

    @property
    def transcripts(self):
        return self.module.claude_transcript_repository

    def stop(self):
        if self.server and self.server.server:
            self.server.request_shutdown()
            self.thread.join(10)
        if self.previous_env is None:
            os.environ.pop(DATA_DIR_VARIABLE, None)
        else:
            os.environ[DATA_DIR_VARIABLE] = self.previous_env
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, path, project_id=PROJECT_ID, host=None):
        """(status, json) of a GET on /p/<project_id>/api<path> (path starts with /api/...)."""
        full_path = "/p/%s%s" % (project_id, path) if path.startswith("/api/") else path
        headers = {"Host": host} if host else {}
        request = urllib.request.Request(self.server.url.rstrip("/") + full_path, method="GET", headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read().decode("utf-8"))


class ReadTestCase(unittest.TestCase):
    INITIAL_ROADMAP = ROADMAP

    def setUp(self):
        self.fixture = Fixture(self.INITIAL_ROADMAP)
        self.addCleanup(self.fixture.stop)

    def state(self, project_id=PROJECT_ID):
        status, data = self.fixture.call("/api/state", project_id)
        self.assertEqual(status, 200, data)
        return data


# ---------------------------------------------------------------- fake transcripts (real Claude Code format)
SESSION = "11111111-2222-3333-4444-555555555555"


def assistant_row(moment, message_id, blocks, usage=None, stop=None, **extra):
    return dict({"type": "assistant", "timestamp": moment, "message": {"id": message_id, "role": "assistant", "content": blocks, "stop_reason": stop,
                                                                       "usage": usage or {}}}, **extra)


def user_row(moment, content, **extra):
    return dict({"type": "user", "timestamp": moment, "message": {"role": "user", "content": content}}, **extra)


def tool_use(identifier, name, **tool_input):
    return {"type": "tool_use", "id": identifier, "name": name, "input": tool_input}


def tool_result(identifier, text, is_error=False):
    return {"type": "tool_result", "tool_use_id": identifier, "content": [{"type": "text", "text": text}], "is_error": is_error}


def shifted(start, seconds):
    moment = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")) + datetime.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def agent_rows(prompt, start="2026-10-15T15:00:00.000Z"):
    """Prompt, streamed message (same id on 2 lines), tool with result, final return through SubagentHandback."""
    at = lambda seconds: shifted(start, seconds)
    return [
        user_row(start, prompt),
        user_row(at(0.1), "<system-reminder>ignore</system-reminder>", isMeta=True),
        assistant_row(at(1), "msg_1", [{"type": "thinking", "thinking": "hmm"}], {"input_tokens": 2, "cache_read_input_tokens": 1000, "output_tokens": 5}),
        assistant_row(at(1.5), "msg_1", [tool_use("tu_1", "Read", file_path="C:/x/spec.md")], {"input_tokens": 2, "cache_read_input_tokens": 1000, "output_tokens": 40}),
        user_row(at(2), [tool_result("tu_1", "# spec content")]),
        assistant_row(at(3), "msg_2", [{"type": "text", "text": "Read the spec."}, tool_use("tu_2", "Bash", command="dotnet build")],
                      {"input_tokens": 3, "cache_read_input_tokens": 2000, "output_tokens": 60}),
        user_row(at(4), [tool_result("tu_2", "Build ok")]),
        assistant_row(at(65), "msg_3", [tool_use("tu_3", "SubagentHandback", message="FEATURE: slug-a\nVERDICT: APPROVED\nmore")],
                      {"input_tokens": 1, "output_tokens": 10}, stop="tool_use"),
        user_row(at(65.5), [tool_result("tu_3", "{\"success\":true}")]),
    ]


AGENT_TOKENS = (2 + 1000 + 40) + (3 + 2000 + 60) + (1 + 10)  # msg_1 counts once (last line of the id)


class TranscriptFixtureMixin:
    """Fake projects folder: <slug>/<session>.jsonl and <slug>/<session>/subagents/agent-*.jsonl + .meta.json."""

    def write_lines(self, path, rows, extra_lines=()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            for line in extra_lines:
                handle.write(line + "\n")

    def setup_projects(self, session=SESSION, number=16):
        fixture = self.fixture
        self.projects = os.path.join(fixture.tmp, "claude-projects")
        slug = re.sub(r"[^A-Za-z0-9]", "-", fixture.work)
        self.slug_dir = os.path.join(self.projects, slug)
        fixture.write_json("turns/%03d.json" % number, {"session_id": session, "total_cost_usd": 0.96, "duration_ms": 13000, "result": "turn ok",
                                                        "usage": {"input_tokens": 7}})
        main = [user_row("2026-10-15T15:00:00.000Z", "<command-name>/orchestrator</command-name>"),
                assistant_row("2026-10-15T15:00:01.000Z", "msg_a", [{"type": "text", "text": "Dispatching agents"}], {"input_tokens": 10, "output_tokens": 20},
                              stop="end_turn")]
        self.write_lines(os.path.join(self.slug_dir, session + ".jsonl"), main, extra_lines=["{corrupted line", "", "[1,2]"])
        subs = os.path.join(self.slug_dir, session, "subagents")
        self.write_lines(os.path.join(subs, "agent-aaa111.jsonl"), agent_rows("Root: x\nSCOPE_FEATURE: slug-a\nWrite the spec.", "2026-10-15T15:00:10.000Z"))
        self.write_lines(os.path.join(subs, "agent-bbb222.jsonl"), agent_rows("Review the spec FEATURE: slug-b", "2026-10-15T15:00:05.000Z"))
        fixture.write(os.path.join(subs, "agent-aaa111.meta.json"), json.dumps({"agentType": "spec-writer"}))
        fixture.write(os.path.join(subs, "agent-bbb222.meta.json"), json.dumps({"agentType": "spec-reviewer"}))
        return subs

    def history(self):
        status, data = self.fixture.call("/api/history")
        self.assertEqual(status, 200, data)
        return data

    def log(self, agent_id):
        status, data = self.fixture.call("/api/agent/%s/log" % agent_id)
        self.assertEqual(status, 200, data)
        return data
