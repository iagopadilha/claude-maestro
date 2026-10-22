"""Installer, parallel subproject maps (INS-04..06, INS-09): two map jobs at once on different subproject folders, every other kind of job
exclusive both ways, no `--max-budget-usd` on map / system map / validate jobs, the system map starting by itself after every map
succeeded (and not after a failure), retry of a failed item, the map gate waiting for the system map, `max_parallel_maps` in the config.
A fake claude behind the REAL job service, runner and services; the real claude is never called."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from harness_backend.config.app_config import DEFAULT_MAX_PARALLEL_MAPS, load_app_config
from harness_backend.domain.project_errors import InvalidRequestError, JobInProgressError
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.claude_job_service import ClaudeJobCommand, ClaudeJobService
from harness_backend.services.map_job_runner import MapJobRunner
from harness_backend.services.planning_gates import build_subproject_map_gate
from harness_backend.services.subproject_map_service import SubprojectMapService
from harness_backend.services.system_map_service import SystemMapService, attach_system_map
from tests.api.test_system_map_service import FakeClaudeClient, FakeSubprojectsService, SilentLog, write_file

WAIT_SECONDS = 20


class GatedFakeClaudeClient(FakeClaudeClient):
    """Same fake as the system map tests; a gate holds every subproject job until released, and a folder can fail."""

    def __init__(self, real_client, root_name):
        super().__init__(real_client, root_name)
        self.gate = None
        self.failing_names = set()

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        if self.gate is not None and os.path.basename(folder) != self.root_name:
            self.calls.append({"argv": argv, "folder": folder})
            self.gate.wait(WAIT_SECONDS)
            if os.path.basename(folder) in self.failing_names:
                return {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "boom in %s" % os.path.basename(folder)}
            write_file(os.path.join(folder, "docs", "codebase", "STACK.md"), "# stack\n")
            return {"ok": True, "reply": "done", "cost_usd": 0.1, "session_id": "", "error": ""}
        if os.path.basename(folder) in self.failing_names:
            self.calls.append({"argv": argv, "folder": folder})
            return {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "boom in %s" % os.path.basename(folder)}
        return super().run_prompt(argv, folder, stdin_text, timeout_seconds, extra_env)


class ParallelSubprojectMapTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="parmap-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, "backend", "Program.cs"), "class P {}")
        write_file(os.path.join(self.repo, "frontend", "src", "app.js"), "export default 1")
        guard = os.path.join(self.temporary_dir, "guard.py")
        write_file(guard, "")
        self.fake_client = GatedFakeClaudeClient(ClaudeClient(None, claude_binary="fake_claude.py", guard_path=guard), "shop")
        self.job_service = ClaudeJobService(self.fake_client, SpecsFileRepository(), SilentLog())
        runner = MapJobRunner(self.job_service, self.fake_client, WorkspaceFileGuard(), SilentLog())
        self.repository = SubprojectsFileRepository()
        subprojects_service = FakeSubprojectsService()
        self.map_service = SubprojectMapService(runner, self.repository, subprojects_service, self.job_service, self.ensure_free,
                                                SilentLog())
        self.system_service = SystemMapService(runner, subprojects_service, self.job_service, self.ensure_free, SilentLog())
        attach_system_map(self.map_service, self.system_service)
        self.context = SimpleNamespace(project=SimpleNamespace(id="p1"), repo_path=self.repo, work_dir=self.repo)
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": True, "confirmed": True, "confirmed_at": "2000-01-01T00:00:00+00:00",
                                                             "projects": [
            {"path": "backend", "name": "backend", "stack": "dotnet", "skip": False, "has_code": True},
            {"path": "frontend", "name": "frontend", "stack": "node", "skip": False, "has_code": True}]})

    def tearDown(self):
        if self.fake_client.gate is not None:
            self.fake_client.gate.set()
        self.wait_idle()
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def ensure_free(self, context):
        if self.job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running")

    def wait_idle(self):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            if not self.job_service.is_busy() and not self.map_service.active_folders:
                return
            time.sleep(0.05)

    def wait_for_queue(self, wanted):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            queue = self.map_service.read_queue(self.context)
            if queue["state"] == wanted and not self.job_service.is_busy():
                return queue
            time.sleep(0.05)
        self.fail("queue never reached %s: %s" % (wanted, self.map_service.read_queue(self.context)))

    def wait_for_calls(self, count):
        deadline = time.time() + WAIT_SECONDS
        while len(self.fake_client.calls) < count and time.time() < deadline:
            time.sleep(0.02)
        self.assertGreaterEqual(len(self.fake_client.calls), count)

    # ------------------------------------------------ INS-04 / INS-05
    def test_two_claude_calls_start_before_either_finishes(self):
        self.fake_client.gate = threading.Event()
        self.map_service.start(self.context)
        self.wait_for_calls(2)
        self.assertEqual(sorted(os.path.basename(call["folder"]) for call in self.fake_client.calls), ["backend", "frontend"])
        self.assertEqual(len([job for job in self.job_service.running_jobs]), 2)
        self.fake_client.gate.set()
        self.wait_for_queue("done")

    def test_other_jobs_are_refused_while_maps_run_and_a_map_is_refused_while_another_job_runs(self):
        self.fake_client.gate = threading.Event()
        self.map_service.start(self.context, paths=["backend", "frontend"])
        self.wait_for_calls(2)
        for folder in ("backend", "frontend"):  # the system map needs two mapped subprojects: give it to the gate, then the job refusal shows
            write_file(os.path.join(self.repo, folder, "docs", "codebase", "STACK.md"), "x")
        with self.assertRaises(JobInProgressError):
            self.system_service.start(self.context)
        with self.assertRaises(JobInProgressError):  # planning chat / follow-up / validate style job on the repository root
            self.job_service.start_job(self.repo, "planning", ClaudeJobCommand(["fake"], work_dir=self.repo))
        with self.assertRaises(JobInProgressError):  # the same subproject folder twice
            self.job_service.start_job(self.repo, "map", ClaudeJobCommand(["fake"], parallel_key=os.path.normcase(
                os.path.abspath(os.path.join(self.repo, "backend")))))
        self.fake_client.gate.set()
        self.wait_for_queue("done")
        self.fake_client.gate = threading.Event()
        planning = self.job_service.start_job(self.repo, "planning", ClaudeJobCommand(  # held by the gate (a non-root folder)
            ["fake"], work_dir=os.path.join(self.repo, "frontend")))
        self.assertTrue(planning["id"])
        for folder in ("backend", "frontend"):
            shutil.rmtree(os.path.join(self.repo, folder, "docs"))
        with self.assertRaises(JobInProgressError):
            self.map_service.start(self.context)

    def test_job_service_parallel_rules(self):
        service = ClaudeJobService(self.fake_client, SpecsFileRepository(), SilentLog())
        self.fake_client.gate = threading.Event()
        first = service.start_job(self.repo, "map", ClaudeJobCommand(["a"], work_dir=self.repo, parallel_key="one"))
        second = service.start_job(self.repo, "map", ClaudeJobCommand(["b"], work_dir=self.repo, parallel_key="two"))
        self.assertNotEqual(first["id"], second["id"])
        with self.assertRaises(JobInProgressError):
            service.start_job(self.repo, "map", ClaudeJobCommand(["c"], work_dir=self.repo, parallel_key="one"))
        with self.assertRaises(JobInProgressError):
            service.start_job(self.repo, "planning", ClaudeJobCommand(["d"], work_dir=self.repo))
        other_repo = os.path.join(self.temporary_dir, "other")
        os.makedirs(other_repo)
        self.assertTrue(service.start_job(other_repo, "planning", ClaudeJobCommand(["e"], work_dir=other_repo))["id"])
        self.fake_client.gate.set()

    # ------------------------------------------------ INS-04 system map after all, INS-06 no budget
    def test_system_map_starts_by_itself_after_both_succeeded_and_no_job_has_a_budget_flag(self):
        self.map_service.start(self.context, budget_usd=9)
        queue = self.wait_for_queue("done")
        self.assertEqual(queue["system"], {"state": "done", "error": ""})
        names = [os.path.basename(call["folder"]) for call in self.fake_client.calls]
        self.assertEqual(sorted(names[:2]), ["backend", "frontend"])
        self.assertEqual(names[2], "shop")  # the system map came last, from the repo root
        self.assertTrue(os.path.isfile(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md")))
        for call in self.fake_client.calls:
            self.assertNotIn("--max-budget-usd", call["argv"])
            self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "sonnet")

    def test_system_map_does_not_start_after_a_failure_and_starts_after_the_retry(self):
        self.fake_client.failing_names = {"backend"}
        self.map_service.start(self.context)
        queue = self.wait_for_queue("stopped")
        self.assertEqual(queue["items"]["backend"]["state"], "failed")
        self.assertEqual(queue["items"]["frontend"]["state"], "done")
        self.assertEqual(queue["system"]["state"], "idle")
        self.assertNotIn("shop", [os.path.basename(call["folder"]) for call in self.fake_client.calls])
        self.fake_client.failing_names = set()
        self.map_service.start(self.context)  # Retry
        queue = self.wait_for_queue("done")
        self.assertEqual(queue["system"]["state"], "done")
        self.assertEqual([os.path.basename(call["folder"]) for call in self.fake_client.calls].count("frontend"), 1)  # not mapped twice

    def test_explicit_single_path_does_not_start_the_system_map(self):
        self.map_service.start(self.context, paths=["backend"])
        queue = self.wait_for_queue("done")
        self.assertEqual(queue["system"]["state"], "idle")
        self.assertEqual(len(self.fake_client.calls), 1)

    def test_system_map_route_budget_is_ignored(self):
        for folder in ("backend", "frontend"):
            write_file(os.path.join(self.repo, folder, "docs", "codebase", "STACK.md"), "x")
        job = self.system_service.start(self.context, budget_usd=-3)["job"]
        self.assertEqual(job["type"], "map")
        self.wait_idle()
        self.assertNotIn("--max-budget-usd", self.fake_client.calls[0]["argv"])

    # ------------------------------------------------ the map gate waits for the system map
    def test_map_gate_is_not_ok_until_the_system_map_is_done_when_it_is_due(self):
        def projects():
            return [{"path": name, "skip": False, "has_code": True, "map": {"files": ["STACK.md"]}} for name in ("backend", "frontend")]
        facts = {"subprojects": {"monorepo": True, "confirmed": True, "projects": projects(), "system": {"exists": False, "enabled": True}}}
        gate = build_subproject_map_gate(facts)
        self.assertFalse(gate["ok"])
        self.assertIn("system map", gate["detail"])
        facts["subprojects"]["map_queue"] = {"state": "done", "system": {"state": "done", "error": ""}}
        self.assertTrue(build_subproject_map_gate(facts)["ok"])
        facts["subprojects"]["map_queue"] = None
        facts["subprojects"]["system"]["exists"] = True
        self.assertTrue(build_subproject_map_gate(facts)["ok"])
        facts["subprojects"]["projects"] = projects()[:1]  # one mapped subproject: no system map is due
        facts["subprojects"]["system"]["exists"] = False
        self.assertTrue(build_subproject_map_gate(facts)["ok"])
        del facts["subprojects"]["system"]  # no system status attached: the old behaviour
        facts["subprojects"]["projects"] = projects()
        self.assertTrue(build_subproject_map_gate(facts)["ok"])


class MaxParallelMapsConfigTests(unittest.TestCase):
    def test_default_and_override(self):
        data_dir = tempfile.mkdtemp(prefix="parcfg-")
        try:
            self.assertEqual(load_app_config(user_data_dir=data_dir).max_parallel_maps, DEFAULT_MAX_PARALLEL_MAPS)
            self.assertEqual(DEFAULT_MAX_PARALLEL_MAPS, 6)
            with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as handle:
                json.dump({"max_parallel_maps": 3}, handle)
            self.assertEqual(load_app_config(user_data_dir=data_dir).max_parallel_maps, 3)
            with open(os.path.join(data_dir, "config.json"), "w", encoding="utf-8") as handle:
                json.dump({"max_parallel_maps": 0}, handle)
            self.assertEqual(load_app_config(user_data_dir=data_dir).max_parallel_maps, 6)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
