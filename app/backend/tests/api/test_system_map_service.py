"""SystemMapService: the root `docs/codebase/SYSTEM.md` of a confirmed monorepo (last step of "map all", own action, refusals,
revert outside SYSTEM.md, no root CLAUDE.md write, older_than_list) and the run_hints rule that the root map folder of a confirmed
monorepo is never "the map". A fake claude writes SYSTEM.md (at the root) or STACK.md (in a subproject) in its cwd."""
from tests import settings_text
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from harness_backend.domain.project_errors import InvalidRequestError, JobInProgressError
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.repositories.specs_file_repository import SpecsFileRepository, run_hints
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.claude_job_service import ClaudeJobService
from harness_backend.services.map_job_runner import MapJobRunner
from harness_backend.services.subproject_map_service import SubprojectMapService
from harness_backend.services.subprojects_service import SubprojectsNotConfirmedError
from harness_backend.services.system_map_service import SystemMapService, attach_system_map

WAIT_SECONDS = 20
ROOT_CLAUDE_TEXT = "# shop\r\n\r\nTeam notes.\r\n"


class SilentLog:
    def __getattr__(self, name):
        return lambda *arguments, **fields: None


def write_file(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


class FakeClaudeClient:
    def __init__(self, real_client, root_name):
        self.real_client = real_client
        self.root_name = root_name
        self.calls = []
        self.write_system = True
        self.stray_paths = []

    def build_command(self, *arguments, **keywords):
        return self.real_client.build_command(*arguments, **keywords)

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        self.calls.append({"argv": argv, "folder": folder})
        target = os.path.join(folder, "docs", "codebase")
        if os.path.basename(folder) == self.root_name:
            if self.write_system:
                write_file(os.path.join(target, "SYSTEM.md"), "# system\n")
            for relative in self.stray_paths:
                write_file(os.path.join(folder, relative), "stray")
        else:
            write_file(os.path.join(target, "STACK.md"), "# stack\n")
        return {"ok": True, "reply": "done", "cost_usd": 0.1, "session_id": "", "error": ""}


class FakeSubprojectsService:
    def resolve_target(self, context, repo_name=None):
        return os.path.basename(context.repo_path), context.work_dir


class SystemMapServiceTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="sysmap-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, "backend", "Program.cs"), "class P {}")
        write_file(os.path.join(self.repo, "frontend", "src", "app.js"), "export default 1")
        write_file(os.path.join(self.repo, "CLAUDE.md"), ROOT_CLAUDE_TEXT)
        guard = os.path.join(self.temporary_dir, "guard.py")
        write_file(guard, "")
        self.fake_client = FakeClaudeClient(ClaudeClient(None, claude_binary="fake_claude.py", guard_path=guard), "shop")
        self.job_service = ClaudeJobService(self.fake_client, SpecsFileRepository(), SilentLog())
        self.runner = MapJobRunner(self.job_service, self.fake_client, WorkspaceFileGuard(), SilentLog())
        self.repository = SubprojectsFileRepository()
        subprojects_service = FakeSubprojectsService()
        self.map_service = SubprojectMapService(self.runner, self.repository, subprojects_service, self.job_service,
                                                self.ensure_free, SilentLog())
        self.service = SystemMapService(self.runner, subprojects_service, self.job_service, self.ensure_free, SilentLog())
        attach_system_map(self.map_service, self.service)
        self.context = SimpleNamespace(project=SimpleNamespace(id="p1"), repo_path=self.repo, work_dir=self.repo)
        self.store_list()

    def tearDown(self):
        self.wait_idle()
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def ensure_free(self, context):
        if self.job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running")

    def store_list(self, confirmed=True, monorepo=True, projects=None, confirmed_at="2000-01-01T00:00:00+00:00"):
        projects = projects if projects is not None else [
            {"path": "backend", "name": "backend", "stack": "dotnet", "skip": False, "has_code": True},
            {"path": "frontend", "name": "frontend", "stack": "node", "skip": False, "has_code": True}]
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": monorepo, "confirmed": confirmed, "projects": projects,
                                                             "confirmed_at": confirmed_at})

    def doc(self, *parts):
        return os.path.join(self.repo, *parts)

    def map_subprojects(self):
        for name in ("backend", "frontend"):
            write_file(self.doc(name, "docs", "codebase", "STACK.md"), "old")

    def wait_idle(self):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            if not self.job_service.is_busy() and not self.map_service.active_folders:
                return
            time.sleep(0.05)

    def wait_for_jobs(self, count):
        deadline = time.time() + WAIT_SECONDS
        while time.time() < deadline:
            if len(self.fake_client.calls) >= count and not self.job_service.is_busy():
                time.sleep(0.2)
                if not self.job_service.is_busy():
                    return
            time.sleep(0.05)
        self.fail("jobs never finished: %s" % len(self.fake_client.calls))

    @staticmethod
    def prompt_of(call):
        return call["argv"][call["argv"].index("-p") + 1]

    def read_root_claude(self):
        with open(self.doc("CLAUDE.md"), "rb") as handle:
            return handle.read()

    # ------------------------------------------------ happy paths
    def test_map_all_ends_with_the_system_map_and_nothing_else_at_the_root(self):
        self.map_service.start(self.context)
        self.wait_for_jobs(3)
        self.wait_idle()
        folders = [os.path.basename(call["folder"]) for call in self.fake_client.calls]  # the maps run in parallel: any order, the system map last
        self.assertEqual((sorted(folders[:2]), folders[2:]), (["backend", "frontend"], ["shop"]))
        self.assertEqual(self.prompt_of(self.fake_client.calls[2]), "/orchestrator map --system")
        self.assertEqual(sorted(os.listdir(self.doc("docs", "codebase"))), ["README.md", "SYSTEM.md"])
        self.assertEqual(os.listdir(self.doc("docs")), ["codebase"])
        with open(self.doc("docs", "codebase", "README.md"), encoding="utf-8") as handle:
            self.assertIn("SYSTEM.md", handle.read())
        self.assertEqual(self.read_root_claude(), ROOT_CLAUDE_TEXT.encode("utf-8"))

    def test_argv_model_profile_and_work_dir(self):
        self.map_subprojects()
        result = self.service.start(self.context, budget_usd=4)
        self.assertEqual(result["job"]["type"], "map")
        self.wait_for_jobs(1)
        call = self.fake_client.calls[0]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertNotIn("--max-budget-usd", argv)  # no spend cap: budget_usd is ignored
        self.assertIn("systemmap", settings_text(argv))
        self.assertEqual(os.path.normcase(call["folder"]), os.path.normcase(self.repo))

    def test_single_path_map_does_not_start_the_system_map(self):
        self.map_service.start(self.context, paths=["backend"])
        self.wait_idle()
        time.sleep(0.3)
        self.assertEqual(len(self.fake_client.calls), 1)
        self.assertFalse(os.path.exists(self.doc("docs")))

    def test_stray_write_outside_system_md_is_reverted(self):
        self.map_subprojects()
        self.fake_client.stray_paths = ["docs/codebase/STACK.md", "docs/other.md", "backend/Program.cs"]
        self.service.start(self.context)
        self.wait_for_jobs(1)
        self.assertFalse(os.path.exists(self.doc("docs", "other.md")))
        self.assertFalse(os.path.exists(self.doc("docs", "codebase", "STACK.md")))
        with open(self.doc("backend", "Program.cs"), encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "class P {}")
        self.assertTrue(os.path.isfile(self.doc("docs", "codebase", "SYSTEM.md")))
        self.assertEqual(self.read_root_claude(), ROOT_CLAUDE_TEXT.encode("utf-8"))

    def test_job_fails_when_system_md_was_not_written(self):
        self.map_subprojects()
        self.fake_client.write_system = False
        job = self.service.start(self.context)["job"]
        self.wait_for_jobs(1)
        finished = self.job_service.get_job(self.repo, job["id"])
        self.assertEqual(finished["state"], "error")
        self.assertIn("SYSTEM.md", finished["error"])
        self.assertFalse(os.path.exists(self.doc("docs", "codebase", "README.md")))

    # ------------------------------------------------ refusals
    def test_refused_with_fewer_than_two_mapped_subprojects(self):
        write_file(self.doc("backend", "docs", "codebase", "STACK.md"), "old")
        with self.assertRaises(InvalidRequestError):
            self.service.start(self.context)
        self.assertEqual(self.fake_client.calls, [])
        status = self.service.status(self.context)
        self.assertFalse(status["enabled"])
        self.assertIn("map at least 2", status["reason"])

    def test_skipped_subprojects_do_not_count(self):
        self.map_subprojects()
        self.store_list(projects=[{"path": "backend", "skip": False}, {"path": "frontend", "skip": True}])
        with self.assertRaises(InvalidRequestError):
            self.service.start(self.context)

    def test_single_project_never_gets_a_system_map(self):
        self.map_subprojects()
        self.store_list(monorepo=False)
        with self.assertRaises(InvalidRequestError):
            self.service.start(self.context)
        self.assertFalse(self.service.status(self.context)["enabled"])
        self.assertEqual(self.fake_client.calls, [])

    def test_unconfirmed_list_is_refused(self):
        self.map_subprojects()
        self.store_list(confirmed=False)
        with self.assertRaises(SubprojectsNotConfirmedError):
            self.service.start(self.context)

    # ------------------------------------------------ status
    def test_status_older_than_the_subproject_list(self):
        self.map_subprojects()
        write_file(self.doc("docs", "codebase", "SYSTEM.md"), "# system\n")
        past = time.time() - 100000
        os.utime(self.doc("docs", "codebase", "SYSTEM.md"), (past, past))
        self.store_list(confirmed_at="2030-01-01T00:00:00+00:00")
        status = self.service.status(self.context)
        self.assertTrue(status["exists"])
        self.assertTrue(status["older_than_list"])
        self.assertTrue(status["enabled"])
        self.assertEqual(status["reason"], "")
        self.assertTrue(status["mapped_at"])
        self.store_list(confirmed_at="2000-01-01T00:00:00+00:00")
        self.assertFalse(self.service.status(self.context)["older_than_list"])

    def test_status_without_a_system_map(self):
        status = self.service.status(self.context)
        self.assertEqual((status["exists"], status["mapped_at"], status["older_than_list"]), (False, None, False))


class RunHintsRootMapTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="hints-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(self.doc("docs", "codebase", "SYSTEM.md"), "# system")
        write_file(self.doc("docs", "codebase", "README.md"), "# index")
        self.repository = SubprojectsFileRepository()

    def tearDown(self):
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def doc(self, *parts):
        return os.path.join(self.repo, *parts)

    def store(self, monorepo, confirmed=True):
        self.repository.write_repo_entry(self.repo, "shop", {"monorepo": monorepo, "confirmed": confirmed, "projects": []})

    def test_root_map_folder_is_not_the_map_of_a_confirmed_monorepo(self):
        self.store(True)
        relative, root = run_hints.find_map_folder(self.repo)
        self.assertEqual(run_hints.list_root_map_documents(root, relative), [])
        self.assertIsNone(run_hints.build_codebase_block(self.repo))
        self.assertFalse(run_hints.write_codebase_block(self.repo))
        self.assertFalse(os.path.exists(self.doc("CLAUDE.md")))

    def test_legacy_folder_is_still_found_for_a_monorepo(self):
        self.store(True)
        write_file(self.doc(".specs", "codebase", "STACK.md"), "legacy")
        self.assertEqual(run_hints.find_map_folder(self.repo), (".specs/codebase", self.repo))

    def test_single_project_behaviour_is_unchanged(self):
        self.store(False)
        self.assertEqual(run_hints.find_map_folder(self.repo), ("docs/codebase", self.repo))
        self.assertEqual(run_hints.list_root_map_documents(self.repo), ["SYSTEM.md"])
        self.assertIn("SYSTEM.md", run_hints.build_codebase_block(self.repo))

    def test_unconfirmed_monorepo_behaves_as_single(self):
        self.store(True, confirmed=False)
        self.assertEqual(run_hints.list_root_map_documents(self.repo), ["SYSTEM.md"])

    def test_index_lists_system_md_with_its_purpose(self):
        self.store(True)
        os.remove(self.doc("docs", "codebase", "README.md"))
        self.assertTrue(run_hints.write_map_index(self.repo, "abc1234"))
        with open(self.doc("docs", "codebase", "README.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn(run_hints.CODEBASE_PURPOSE["SYSTEM.md"], text)


if __name__ == "__main__":
    unittest.main()
