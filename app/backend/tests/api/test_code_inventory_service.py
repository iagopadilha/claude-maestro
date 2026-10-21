"""CodeInventoryService: thresholds from the app config (invalid values fall back with a note), the inventory run in `before_start` of
the root map and of every subproject map job (progress summary, `--inventory` in the prompt, a broken script is a note not an error)
and the giant count / queue state in the subprojects table. A fake claude behind the REAL job service and MapJobRunner."""
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from harness_backend.config.app_config import load_app_config
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.claude_job_service import ClaudeJobService
from harness_backend.services.code_inventory_service import CodeInventoryService, inventory_name
from harness_backend.services.map_job_runner import MapJobRunner, attach_code_inventory
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subproject_map_service import SubprojectMapService
from harness_backend.services.subprojects_service import SubprojectsService

from .test_subproject_map_service import FakeClaudeClient, FakeSubprojectsService, SilentLog, write_file

WAIT_SECONDS = 20
ROOT_INVENTORY_PATH = ".specs/project/loop/internal/inventory/_root.json"


class RecordingLog:
    def __init__(self):
        self.warnings = []

    def warn(self, event, **fields):
        self.warnings.append((event, fields))

    def __getattr__(self, name):
        return lambda *arguments, **fields: None


class SpyInventoryService(CodeInventoryService):
    """Records every progress text the job reports while the inventory runs."""

    def __init__(self, *arguments):
        super().__init__(*arguments)
        self.progress_texts = []

    def run_for_job(self, repo_root, folder_relative, name, update_progress):
        def record(text):
            self.progress_texts.append(text)
            update_progress(text)

        return super().run_for_job(repo_root, folder_relative, name, record)


class ThresholdTests(unittest.TestCase):
    def setUp(self):
        self.data_dir = tempfile.mkdtemp(prefix="inv-config-")

    def tearDown(self):
        shutil.rmtree(self.data_dir, ignore_errors=True)

    def load(self, content):
        with open(os.path.join(self.data_dir, "config.json"), "w", encoding="utf-8") as handle:
            json.dump(content, handle)
        return load_app_config(user_data_dir=self.data_dir)

    def test_defaults_when_absent(self):
        config = self.load({})
        self.assertEqual((config.inventory_giant_lines, config.inventory_giant_kb, config.inventory_threshold_note), (1500, 200, ""))
        self.assertEqual(CodeInventoryService(config, SilentLog()).thresholds(), (1500, 200, ""))

    def test_configured_values_are_used(self):
        config = self.load({"inventory_giant_lines": 800, "inventory_giant_kb": 50})
        self.assertEqual(CodeInventoryService(config, SilentLog()).thresholds(), (800, 50, ""))

    def test_invalid_values_fall_back_and_leave_a_note(self):
        for invalid in (0, -5, "many", None, True, [3]):
            config = self.load({"inventory_giant_lines": invalid, "inventory_giant_kb": 120})
            lines, kilobytes, note = CodeInventoryService(config, SilentLog()).thresholds()
            self.assertEqual((lines, kilobytes), (1500, 120), invalid)
            self.assertIn("inventory_giant_lines", note)
        config = self.load({"inventory_giant_kb": -1})
        self.assertEqual(CodeInventoryService(config, SilentLog()).thresholds()[:2], (1500, 200))
        self.assertIn("inventory_giant_kb", config.inventory_threshold_note)

    def test_service_guards_an_invalid_config_object_on_its_own(self):
        broken = SimpleNamespace(inventory_giant_lines=-1, inventory_giant_kb="x")
        lines, kilobytes, note = CodeInventoryService(broken, SilentLog()).thresholds()
        self.assertEqual((lines, kilobytes), (1500, 200))
        self.assertTrue(note)


class InventoryRunTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="inv-run-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, ".specs", "keep"), "")
        write_file(os.path.join(self.repo, "Big.cs"), "int x;\n" * 30)
        write_file(os.path.join(self.repo, "small.cs"), "int y;\n")
        self.service = CodeInventoryService(SimpleNamespace(inventory_giant_lines=10, inventory_giant_kb=200), SilentLog())

    def tearDown(self):
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def test_run_writes_the_inventory_and_summarizes(self):
        result = self.service.run(self.repo, ".", "_root")
        self.assertTrue(result["ok"])
        self.assertEqual(result["path"], ROOT_INVENTORY_PATH)
        self.assertEqual((result["files"], result["giant"]), (2, 1))
        self.assertEqual(result["giant_list"], [{"path": "Big.cs", "lines": 30}])
        self.assertEqual(result["summary"], "inventory: 2 files, 1 giant")
        self.assertEqual(self.service.giant_count(self.repo, "_root"), 1)
        self.assertEqual(self.service.read(self.repo, "_root")["totals"]["files"], 2)
        self.assertIsNone(self.service.read(self.repo, "missing"))
        self.assertEqual(self.service.giant_count(self.repo, "missing"), 0)

    def test_summary_separates_thousands_with_a_space(self):
        from harness_backend.services.code_inventory_service import build_summary
        self.assertEqual(build_summary(1240, 3), "inventory: 1 240 files, 3 giant")

    def test_names(self):
        self.assertEqual((inventory_name("."), inventory_name(""), inventory_name("backend/api")), ("_root", "_root", "backend-api"))

    def test_failure_is_a_note_and_never_raises(self):
        import harness_backend.services.code_inventory_service as module
        original = module.inventory_engine.build_inventory
        module.inventory_engine.build_inventory = lambda *arguments, **keywords: (_ for _ in ()).throw(RuntimeError("disk on fire"))
        try:
            result = self.service.run(self.repo, ".", "_root")
        finally:
            module.inventory_engine.build_inventory = original
        self.assertFalse(result["ok"])
        self.assertIn("disk on fire", result["note"])
        self.assertEqual(result["summary"], "")


class MapJobsInventoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="inv-jobs-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, ".specs", "keep"), "")
        write_file(os.path.join(self.repo, "backend", "api", "Huge.cs"), "int x;\n" * 40)
        write_file(os.path.join(self.repo, "backend", "api", "Small.cs"), "int y;\n")
        write_file(os.path.join(self.repo, "frontend", "src", "app.js"), "export default 1\n")
        guard = os.path.join(self.temporary_dir, "guard.py")
        write_file(guard, "")
        self.fake_client = FakeClaudeClient(ClaudeClient(None, claude_binary="fake_claude.py", guard_path=guard))
        self.log = RecordingLog()
        self.job_service = ClaudeJobService(self.fake_client, SpecsFileRepository(), SilentLog())
        self.runner = MapJobRunner(self.job_service, self.fake_client, WorkspaceFileGuard(), self.log)
        self.inventory = SpyInventoryService(SimpleNamespace(inventory_giant_lines=10, inventory_giant_kb=200), self.log)
        self.repository = SubprojectsFileRepository()
        self.map_service = SubprojectMapService(self.runner, self.repository, FakeSubprojectsService(), self.job_service,
                                                lambda context: None, SilentLog())
        attach_code_inventory(self.runner, self.map_service, self.inventory)
        self.context = SimpleNamespace(project=SimpleNamespace(id="p1"), repo_path=self.repo, work_dir=self.repo)
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": True, "confirmed": True, "projects": [
            {"path": "backend/api", "name": "api", "stack": "dotnet", "skip": False, "has_code": True},
            {"path": "frontend", "name": "frontend", "stack": "node", "skip": False, "has_code": True}]})

    def tearDown(self):
        self.wait_idle()
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def wait_idle(self):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            if not self.job_service.is_busy() and not self.map_service.active_folders:
                return
            time.sleep(0.05)

    def wait_for_job(self, job_id):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            record = self.job_service.get_job(self.repo, job_id)
            if record.get("state") != "running":
                self.wait_idle()
                return record
            time.sleep(0.05)
        self.fail("job never ended")

    @staticmethod
    def prompt_of(call):
        return call["argv"][call["argv"].index("-p") + 1]

    def test_root_map_runs_the_inventory_and_carries_the_path(self):
        job = self.runner.start(self.repo, "p1", 1)
        self.wait_for_job(job["id"])
        self.assertEqual(self.prompt_of(self.fake_client.calls[0]), "/orchestrator map --inventory " + ROOT_INVENTORY_PATH)
        self.assertRegex(self.inventory.progress_texts[0], r"^inventory: \d+ files, 1 giant$")
        self.assertTrue(os.path.isfile(os.path.join(self.repo, *ROOT_INVENTORY_PATH.split("/"))))
        argv = self.fake_client.calls[0]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_subproject_jobs_carry_their_own_path_and_progress(self):
        self.map_service.start(self.context, paths=["backend/api", "frontend"])
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline and len(self.fake_client.calls) < 2:
            time.sleep(0.05)
        self.wait_idle()
        prompts = sorted(self.prompt_of(call) for call in self.fake_client.calls)  # the two maps run in parallel: any order
        self.assertEqual(prompts[0], "/orchestrator map --subproject backend/api --inventory "
                                     ".specs/project/loop/internal/inventory/backend-api.json")
        self.assertEqual(prompts[1], "/orchestrator map --subproject frontend --inventory "
                                     ".specs/project/loop/internal/inventory/frontend.json")
        self.assertEqual(sorted(self.inventory.progress_texts), ["inventory: 1 files, 0 giant", "inventory: 2 files, 1 giant"])
        self.assertEqual(self.inventory.giant_count(self.repo, "backend-api"), 1)

    def test_broken_inventory_still_lets_the_job_run_with_a_note(self):
        import harness_backend.services.code_inventory_service as module
        original = module.inventory_engine.build_inventory
        module.inventory_engine.build_inventory = lambda *arguments, **keywords: (_ for _ in ()).throw(OSError("denied"))
        try:
            job = self.runner.start(self.repo, "p1", 1)
            record = self.wait_for_job(job["id"])
        finally:
            module.inventory_engine.build_inventory = original
        self.assertEqual(len(self.fake_client.calls), 1)  # claude still ran
        self.assertIn("the code inventory failed", self.inventory.progress_texts[0])
        self.assertIn("denied", self.inventory.progress_texts[0])
        if record:
            self.assertNotEqual(record.get("state"), "error")
        self.assertTrue(any(event == "code_inventory_failed" for event, _ in self.log.warnings))

    def test_runner_without_the_service_keeps_the_plain_prompt(self):
        plain = MapJobRunner(self.job_service, self.fake_client, WorkspaceFileGuard(), SilentLog())
        job = plain.start(self.repo, "p1", 1)
        self.wait_for_job(job["id"])
        self.assertEqual(self.prompt_of(self.fake_client.calls[0]), "/orchestrator map")


class DescribeRowsTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="inv-rows-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, ".specs", "keep"), "")
        write_file(os.path.join(self.repo, "backend", "Huge.cs"), "int x;\n" * 40)
        write_file(os.path.join(self.repo, "frontend", "app.js"), "export default 1\n")
        self.inventory = CodeInventoryService(SimpleNamespace(inventory_giant_lines=10, inventory_giant_kb=200), SilentLog())
        self.repository = SubprojectsFileRepository()
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": True, "confirmed": True, "projects": [
            {"path": "backend", "name": "backend", "stack": "dotnet", "skip": False, "has_code": True},
            {"path": "frontend", "name": "frontend", "stack": "node", "skip": False, "has_code": True}]})
        self.context = SimpleNamespace(project=SimpleNamespace(id="p1"), repo_path=self.repo, work_dir=self.repo)
        self.structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, SilentLog())

    def tearDown(self):
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def build_service(self, **keywords):
        return SubprojectsService(self.repository, self.structure, None, None, SilentLog(), **keywords)

    def test_rows_show_giant_count_and_top_files_from_the_last_inventory(self):
        self.inventory.run(self.repo, "backend", inventory_name("backend"))
        rows = {row["path"]: row for row in self.build_service(code_inventory_service=self.inventory).describe(self.context)["projects"]}
        self.assertEqual(rows["backend"]["giant_files"], 1)
        self.assertEqual(rows["backend"]["giant"], [{"path": "backend/Huge.cs", "lines": 40}])
        self.assertEqual((rows["frontend"]["giant_files"], rows["frontend"]["giant"]), (0, []))

    def test_without_the_collaborator_the_count_is_zero(self):
        rows = self.build_service().describe(self.context)["projects"]
        self.assertEqual([row["giant_files"] for row in rows], [0, 0])

    def test_queue_and_running_row_come_from_the_reader(self):
        queue = {"state": "running", "queue": ["frontend"], "current": "backend", "done": [], "failed": {}}
        description = self.build_service(map_queue_reader=lambda context, repo_name: queue).describe(self.context)
        self.assertEqual(description["queue"], queue)
        rows = {row["path"]: row for row in description["projects"]}
        self.assertTrue(rows["backend"]["map"]["running"])
        self.assertEqual(rows["backend"]["map"]["job"]["path"], "backend")
        self.assertFalse(rows["frontend"]["map"]["running"])
        self.assertIsNone(rows["frontend"]["map"]["job"])

    def test_stopped_queue_marks_no_row_running_and_a_failing_reader_is_tolerated(self):
        stopped = {"state": "stopped", "queue": [], "current": None, "done": ["backend"], "failed": {"frontend": "boom"}}
        description = self.build_service(map_queue_reader=lambda context, repo_name: stopped).describe(self.context)
        self.assertEqual(description["queue"], stopped)
        self.assertFalse(any(row["map"]["running"] for row in description["projects"]))

        def failing(context, repo_name):
            raise RuntimeError("queue unreadable")

        description = self.build_service(map_queue_reader=failing).describe(self.context)
        self.assertIsNone(description["queue"])
        self.assertEqual(len(description["projects"]), 2)


if __name__ == "__main__":
    unittest.main()
