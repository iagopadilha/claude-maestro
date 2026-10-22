"""SubprojectMapService: one map job per subproject, in parallel, work_dir per subproject, model ceiling, no budget, per-item
failure and retry, refusals and restart recovery. A fake claude (a ClaudeClient replacement behind the REAL job service and MapJobRunner)
writes `docs/codebase/STACK.md` in its cwd and can fail for a path."""
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from harness_backend.domain.project_errors import InvalidPathError, InvalidRequestError, JobInProgressError, LoopRunningError
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.claude_job_service import ClaudeJobCommand, ClaudeJobService
from harness_backend.services.map_job_runner import MapJobRunner
from harness_backend.services.subproject_map_service import INTERRUPTED_REASON, SubprojectMapService
from harness_backend.services.subprojects_service import SubprojectsNotConfirmedError

WAIT_SECONDS = 20


class SilentLog:
    def __getattr__(self, name):
        return lambda *arguments, **fields: None


class FakeClaudeClient:
    """build_command is the real one; run_prompt records the call and writes a map document in the cwd (or fails)."""

    def __init__(self, real_client):
        self.real_client = real_client
        self.calls = []
        self.failing_folders = set()
        self.write_root_docs = False
        self.delay_seconds = 0.0
        self.running_now = 0
        self.max_running = 0
        self.gate = None
        self.lock = threading.Lock()

    def build_command(self, *arguments, **keywords):
        return self.real_client.build_command(*arguments, **keywords)

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        with self.lock:
            self.calls.append({"argv": argv, "folder": folder})
            self.running_now += 1
            self.max_running = max(self.max_running, self.running_now)
        try:
            if self.gate is not None:
                self.gate.wait(WAIT_SECONDS)
            time.sleep(self.delay_seconds)
            name = os.path.basename(folder)
            if name in self.failing_folders:
                return {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "boom in %s" % name}
            target = os.path.join(folder, "docs", "codebase")
            os.makedirs(target, exist_ok=True)
            with open(os.path.join(target, "STACK.md"), "w", encoding="utf-8") as handle:
                handle.write("# stack of %s\n" % name)
            if self.write_root_docs:
                stray_folder = os.path.join(os.path.dirname(folder), "docs")
                os.makedirs(stray_folder, exist_ok=True)
                with open(os.path.join(stray_folder, "stray.md"), "w", encoding="utf-8") as handle:
                    handle.write("stray")
            return {"ok": True, "reply": "mapped", "cost_usd": 0.1, "session_id": "", "error": ""}
        finally:
            with self.lock:
                self.running_now -= 1


class FakeSubprojectsService:
    def resolve_target(self, context, repo_name=None):
        return os.path.basename(context.repo_path), context.work_dir


def write_file(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class SubprojectMapServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="submap-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, "backend", "Program.cs"), "class P {}")
        write_file(os.path.join(self.repo, "frontend", "src", "app.js"), "export default 1")
        write_file(os.path.join(self.repo, "tools", "seed.sh"), "echo")
        guard = os.path.join(self.temporary_dir, "guard.py")
        write_file(guard, "")
        self.fake_client = FakeClaudeClient(ClaudeClient(None, claude_binary="fake_claude.py", guard_path=guard))
        self.job_service = ClaudeJobService(self.fake_client, SpecsFileRepository(), SilentLog())
        runner = MapJobRunner(self.job_service, self.fake_client, WorkspaceFileGuard(), SilentLog())
        self.repository = SubprojectsFileRepository()
        self.loop_running = False
        self.service = SubprojectMapService(runner, self.repository, FakeSubprojectsService(), self.job_service,
                                            self.ensure_free, SilentLog())
        self.context = SimpleNamespace(project=SimpleNamespace(id="p1"), repo_path=self.repo, work_dir=self.repo)
        self.store_list(confirmed=True)

    def tearDown(self):
        if self.fake_client.gate is not None:
            self.fake_client.gate.set()
        self.wait_idle()
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def ensure_free(self, context):
        if self.loop_running:
            raise LoopRunningError("loop running")
        if os.path.normcase(context.work_dir) != os.path.normcase(context.repo_path):
            raise InvalidRequestError("worktree exists")
        if self.job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running")

    def store_list(self, confirmed, projects=None):
        projects = projects if projects is not None else [
            {"path": "backend", "name": "backend", "stack": "dotnet", "skip": False, "has_code": True},
            {"path": "frontend", "name": "frontend", "stack": "node", "skip": False, "has_code": True},
            {"path": "tools", "name": "tools", "stack": "unknown", "skip": True, "has_code": False}]
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": True, "confirmed": confirmed, "projects": projects})

    def wait_idle(self):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            if not self.job_service.is_busy() and not self.service.active_folders:
                return
            time.sleep(0.05)

    def wait_for_queue_state(self, wanted):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            queue = self.service.read_queue(self.context)
            if queue and queue["state"] == wanted and not self.job_service.is_busy():
                return queue
            time.sleep(0.05)
        self.fail("queue never reached %s: %s" % (wanted, self.service.read_queue(self.context)))

    def doc(self, *parts):
        return os.path.join(self.repo, *parts)

    @staticmethod
    def prompt_of(call):
        return call["argv"][call["argv"].index("-p") + 1]


    # ------------------------------------------------ happy path
    def test_map_all_writes_each_subproject_folder_and_nothing_at_the_root(self):
        self.fake_client.write_root_docs = True  # a stray write outside the allowed folders is undone by the runner
        self.fake_client.delay_seconds = 0.1
        result = self.service.start(self.context)
        self.assertTrue(result["ok"])
        self.assertEqual(sorted(result["queue"]["items"]), ["backend", "frontend"])
        self.assertEqual(result["queue"]["items"]["backend"]["state"], "running")
        self.assertEqual(result["queue"]["items"]["frontend"]["state"], "running")
        self.assertEqual(result["job"]["type"], "map")
        queue = self.wait_for_queue_state("done")
        self.assertEqual(sorted(queue["done"]), ["backend", "frontend"])
        self.assertEqual(queue["failed"], {})
        self.assertEqual(queue["items"]["backend"]["documents"], 1)
        self.assertTrue(os.path.isfile(self.doc("backend", "docs", "codebase", "STACK.md")))
        self.assertTrue(os.path.isfile(self.doc("backend", "docs", "codebase", "README.md")))  # the index, written by the app
        self.assertTrue(os.path.isfile(self.doc("frontend", "docs", "codebase", "STACK.md")))
        with open(self.doc("backend", "CLAUDE.md"), encoding="utf-8") as handle:
            self.assertIn("CODEBASE", handle.read())
        self.assertFalse(os.path.exists(self.doc("docs", "codebase")))
        self.assertFalse(os.path.exists(self.doc("docs", "stray.md")))

    def test_argv_model_prompt_work_dir_and_no_budget_flag(self):
        self.service.start(self.context, budget_usd=7)  # ignored: no spend cap
        self.wait_for_queue_state("done")
        calls = {os.path.basename(call["folder"]): call for call in self.fake_client.calls}
        argv = calls["backend"]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(self.prompt_of(calls["backend"]), "/orchestrator map --subproject backend")
        self.assertEqual(self.prompt_of(calls["frontend"]), "/orchestrator map --subproject frontend")
        for call in calls.values():
            self.assertNotIn("--max-budget-usd", call["argv"])
        self.assertEqual(os.path.normcase(calls["backend"]["folder"]), os.path.normcase(self.doc("backend")))

    def test_default_paths_leave_out_skipped_and_already_mapped_projects(self):
        write_file(self.doc("backend", "docs", "codebase", "STACK.md"), "old")
        result = self.service.start(self.context)
        self.assertEqual(list(result["queue"]["items"]), ["frontend"])
        self.wait_for_queue_state("done")
        self.assertEqual(len(self.fake_client.calls), 1)

    def test_two_maps_run_at_the_same_time(self):
        self.fake_client.gate = threading.Event()
        self.service.start(self.context)
        deadline = time.time() + WAIT_SECONDS
        while len(self.fake_client.calls) < 2 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(len(self.fake_client.calls), 2)  # both claude calls before either finished
        self.assertEqual(self.fake_client.max_running, 2)
        self.fake_client.gate.set()
        self.wait_for_queue_state("done")

    def test_max_parallel_maps_caps_the_running_jobs_and_the_rest_starts_later(self):
        self.service.max_parallel_maps = 1
        self.fake_client.delay_seconds = 0.1
        result = self.service.start(self.context)
        self.assertEqual(sorted(item["state"] for item in result["queue"]["items"].values()), ["queued", "running"])
        queue = self.wait_for_queue_state("done")
        self.assertEqual(sorted(queue["done"]), ["backend", "frontend"])
        self.assertEqual(self.fake_client.max_running, 1)

    def test_queue_file_shape(self):
        self.fake_client.gate = threading.Event()
        self.service.start(self.context, paths=["frontend"], budget_usd=3)
        stored = self.repository.read_map_queue(self.repo)
        self.assertEqual(stored["state"], "running")
        self.assertEqual(list(stored["items"]), ["frontend"])
        item = stored["items"]["frontend"]
        self.assertEqual(item["state"], "running")
        self.assertTrue(item["job_id"].startswith("j-"))
        self.assertIsNone(item["finished_at"])
        self.assertEqual(stored["system"], {"state": "idle", "error": ""})  # explicit paths: no system map
        self.assertIn("started_at", stored)
        self.fake_client.gate.set()
        queue = self.wait_for_queue_state("done")
        self.assertEqual(queue["items"]["frontend"]["state"], "done")
        self.assertIsNotNone(queue["items"]["frontend"]["finished_at"])

    def test_running_documents_are_counted_live_on_each_read(self):
        self.fake_client.gate = threading.Event()
        self.service.start(self.context, paths=["backend"])
        write_file(self.doc("backend", "docs", "codebase", "STACK.md"), "x")
        write_file(self.doc("backend", "docs", "codebase", "ARCHITECTURE.md"), "x")
        self.assertEqual(self.service.read_queue(self.context)["items"]["backend"]["documents"], 2)
        self.fake_client.gate.set()
        self.wait_for_queue_state("done")

    # ------------------------------------------------ failure and retry
    def test_failing_item_does_not_stop_the_others_and_a_new_start_retries_only_it(self):
        self.fake_client.failing_folders = {"backend"}
        self.service.start(self.context)
        queue = self.wait_for_queue_state("stopped")
        self.assertEqual(queue["done"], ["frontend"])
        self.assertIn("boom in backend", queue["failed"]["backend"])
        self.assertIn("boom in backend", queue["items"]["backend"]["error"])
        self.assertEqual(len(self.fake_client.calls), 2)
        self.fake_client.failing_folders = set()
        result = self.service.start(self.context)
        self.assertEqual(sorted(result["queue"]["items"]), ["backend", "frontend"])  # the finished one is kept
        self.assertEqual(result["queue"]["items"]["frontend"]["state"], "done")
        queue = self.wait_for_queue_state("done")
        self.assertEqual(sorted(queue["done"]), ["backend", "frontend"])
        self.assertEqual(queue["failed"], {})
        self.assertEqual(len(self.fake_client.calls), 3)

    def test_queue_finished_callback_runs_only_without_failure(self):
        finished = []
        self.service.add_on_queue_finished(lambda context, key, folder, state: finished.append((key, sorted(state["done"]))))
        self.fake_client.failing_folders = {"backend"}
        self.service.start(self.context)
        queue = self.wait_for_queue_state("stopped")
        self.assertEqual(finished, [])
        self.assertEqual(queue["system"]["state"], "idle")
        self.fake_client.failing_folders = set()
        self.service.start(self.context)
        self.wait_for_queue_state("done")
        self.assertEqual(finished, [("shop", ["backend", "frontend"])])

    def test_system_step_job_is_followed_and_a_refusal_is_recorded(self):
        def start_system(context, key, folder, state):
            return self.job_service.start_job(folder, "map", ClaudeJobCommand(["fake"], work_dir=folder))
        self.service.add_on_queue_finished(start_system)
        self.service.start(self.context)
        queue = self.wait_for_queue_state("done")
        self.assertEqual(queue["system"]["state"], "done")
        self.assertEqual(len(self.fake_client.calls), 3)
        self.service.queue_finished_callbacks.clear()

        def refuse(context, key, folder, state):
            raise InvalidRequestError("map at least 2 subprojects first")
        self.service.add_on_queue_finished(refuse)
        shutil.rmtree(self.doc("backend", "docs"))
        shutil.rmtree(self.doc("frontend", "docs"))
        self.service.start(self.context)
        queue = self.wait_for_queue_state("done")
        self.assertEqual(queue["system"], {"state": "idle", "error": "map at least 2 subprojects first"})

    def test_extra_before_start_and_prompt_extension_hooks(self):
        seen = []
        self.service.add_extra_before_start(lambda context, key, folder, path, update_progress: seen.append(path))
        self.service.add_prompt_extension(lambda context, key, folder, path: " --inventory inv/%s.json" % path)
        self.service.start(self.context, paths=["backend"])
        self.wait_for_queue_state("done")
        self.assertEqual(seen, ["backend"])
        self.assertEqual(self.prompt_of(self.fake_client.calls[0]), "/orchestrator map --subproject backend --inventory inv/backend.json")

    # ------------------------------------------------ refusals
    def test_unconfirmed_list_is_refused(self):
        self.store_list(confirmed=False)
        with self.assertRaises(SubprojectsNotConfirmedError):
            self.service.start(self.context)
        self.assertEqual(self.fake_client.calls, [])

    def test_refused_when_loop_runs_or_worktree_exists(self):
        self.loop_running = True
        with self.assertRaises(LoopRunningError):
            self.service.start(self.context)
        self.loop_running = False
        worktree_context = SimpleNamespace(project=self.context.project, repo_path=self.repo, work_dir=self.repo + "-worktree-loop")
        with self.assertRaises(InvalidRequestError):
            self.service.start(worktree_context)

    def test_second_start_while_maps_run_is_refused(self):
        self.fake_client.gate = threading.Event()
        self.service.start(self.context, paths=["backend"])
        with self.assertRaises(JobInProgressError):
            self.service.start(self.context, paths=["frontend"])
        self.fake_client.gate.set()
        self.wait_for_queue_state("done")
        self.assertEqual(len(self.fake_client.calls), 1)

    def test_map_is_refused_while_another_kind_of_job_runs_and_that_job_is_refused_while_maps_run(self):
        self.fake_client.gate = threading.Event()
        self.job_service.start_job(self.repo, "planning", ClaudeJobCommand(["fake"], work_dir=self.repo))
        with self.assertRaises(JobInProgressError):
            self.service.start(self.context, paths=["backend"])
        self.fake_client.gate.set()
        self.wait_idle()
        self.fake_client.gate = threading.Event()
        self.service.start(self.context, paths=["backend", "frontend"])
        with self.assertRaises(JobInProgressError):  # a planning chat / validate / system map style job
            self.job_service.start_job(self.repo, "planning", ClaudeJobCommand(["fake"], work_dir=self.repo))
        self.assertIsNotNone(self.job_service.get_running_job(self.repo))
        self.fake_client.gate.set()
        self.wait_for_queue_state("done")

    def test_bad_paths_and_budget_is_ignored(self):
        for invalid_paths in ([], ["nope"], ["tools"], "backend", [3]):
            with self.assertRaises(InvalidRequestError):
                self.service.start(self.context, paths=invalid_paths)
        self.assertEqual(self.fake_client.calls, [])
        self.service.start(self.context, paths=["backend"], budget_usd=-5)  # never a 400
        self.wait_for_queue_state("done")

    def test_missing_folder_is_refused_for_that_path_and_left_out_of_the_default(self):
        shutil.rmtree(self.doc("frontend"))
        with self.assertRaises(InvalidPathError):
            self.service.start(self.context, paths=["frontend"])
        result = self.service.start(self.context)
        self.assertEqual(list(result["queue"]["items"]), ["backend"])
        self.wait_for_queue_state("done")

    # ------------------------------------------------ restart recovery
    def test_orphan_running_items_are_marked_interrupted_per_item(self):
        item = {"started_at": "2026-10-20T10:00:00", "finished_at": None, "error": "", "job_id": "", "documents": 0}
        self.repository.write_map_queue(self.repo, {
            "state": "running", "started_at": "2026-10-20T10:00:00", "include_system": True,
            "items": {"backend": dict(item, state="running"), "frontend": dict(item, state="done")},
            "system": {"state": "waiting", "error": ""}})
        queue = self.service.read_queue(self.context)
        self.assertEqual(queue["state"], "stopped")
        self.assertEqual(queue["items"]["backend"]["state"], "failed")
        self.assertEqual(queue["items"]["backend"]["error"], INTERRUPTED_REASON)
        self.assertEqual(queue["items"]["frontend"]["state"], "done")
        self.assertEqual(queue["system"]["state"], "idle")
        result = self.service.start(self.context)  # Retry: only the interrupted item, the finished one is kept
        self.assertEqual(result["queue"]["items"]["frontend"]["state"], "done")
        self.assertEqual(result["queue"]["items"]["backend"]["state"], "running")
        self.wait_for_queue_state("done")
        self.assertEqual(len(self.fake_client.calls), 1)

    def test_old_sequential_queue_file_is_converted(self):
        self.repository.write_map_queue(self.repo, {"queue": ["frontend"], "current": "backend", "done": [], "failed": {},
                                                    "budget_usd": 5.0, "started_at": "2026-10-20T10:00:00", "state": "running"})
        queue = self.service.read_queue(self.context)
        self.assertEqual(queue["state"], "stopped")
        self.assertEqual(queue["failed"], {"backend": INTERRUPTED_REASON})
        self.assertEqual(queue["queue"], ["frontend"])

    def test_read_queue_is_idle_without_a_queue(self):
        queue = self.service.read_queue(self.context)
        self.assertEqual((queue["state"], queue["items"], queue["system"]["state"]), ("idle", {}, "idle"))


if __name__ == "__main__":
    unittest.main()
