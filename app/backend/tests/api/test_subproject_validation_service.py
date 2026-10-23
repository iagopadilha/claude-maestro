"""SubprojectValidationService: argv (model ceiling, guard profile), budget limits, refusals, and the validation and merge of the
AI suggestion (skip, stack change, rejected entries, confirmed stays false, failure keeps the script list). No real claude runs:
a fake job service runs the job synchronously with a fake claude that writes (or breaks) the suggestion file."""
from tests import settings_text
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.domain.project_errors import InvalidRequestError, JobInProgressError, LoopRunningError
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.subproject_validation_service import JOB_TYPE_SUBPROJECTS, SubprojectValidationService


class SilentLog:
    def __getattr__(self, name):
        return lambda *arguments, **fields: None


class FakeJobService:
    """Runs the job at once, in the calling thread: `claude_action(command)` stands for claude, then `finalize` runs."""

    def __init__(self, claude_action):
        self.claude_action = claude_action
        self.started = []
        self.running = None

    def get_running_job(self, repo_path):
        return self.running

    def start_job(self, repo_path, job_type, command):
        self.started.append((repo_path, job_type, command))
        result = self.claude_action(command)
        if command.finalize is not None:
            result = command.finalize(result)
        return {"id": "j-000000000001", "type": job_type, "state": "ok" if result.get("ok") else "error", "error": result.get("error", "")}


class FakeLoopStatus:
    running = False

    def is_loop_running(self, context):
        return self.running


def write_file(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class SubprojectValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary_dir = tempfile.mkdtemp(prefix="validation-")
        self.repo = os.path.join(self.temporary_dir, "shop")
        write_file(os.path.join(self.repo, "backend", "Shop.sln"), "")
        write_file(os.path.join(self.repo, "backend", "Shop.Api", "Shop.Api.csproj"), "<Project/>")
        write_file(os.path.join(self.repo, "backend", "Program.cs"), "class P {}")
        write_file(os.path.join(self.repo, "frontend", "package.json"), json.dumps({"dependencies": {"react": "18"}}))
        write_file(os.path.join(self.repo, "frontend", "src", "app.js"), "export default 1")
        write_file(os.path.join(self.repo, "tools", "seed", "seed.sh"), "echo seed")
        write_file(os.path.join(self.repo, "tools", "package.json"), "{}")
        guard = os.path.join(self.temporary_dir, "guard.py")
        write_file(guard, "")
        self.claude_client = ClaudeClient(None, claude_binary="fake_claude.py", guard_path=guard)
        self.suggestion = None
        self.claude_result = {"ok": True, "reply": "done", "cost_usd": 0.1, "session_id": "", "error": ""}
        self.job_service = FakeJobService(self.fake_claude)
        self.loop_status = FakeLoopStatus()
        self.repository = SubprojectsFileRepository()
        self.service = SubprojectValidationService(self.job_service, self.claude_client, self.repository, self.loop_status, None, SilentLog())
        self.context = SimpleNamespace(repo_path=self.repo, work_dir=self.repo)

    def tearDown(self):
        shutil.rmtree(self.temporary_dir, ignore_errors=True)

    def fake_claude(self, command):
        suggested = self.repository.suggested_path(self.repo)
        if isinstance(self.suggestion, str):
            write_file(suggested, self.suggestion)
        elif self.suggestion is not None:
            write_file(suggested, json.dumps(self.suggestion))
        return dict(self.claude_result)

    def stored_entry(self):
        return self.repository.read_repo_entry(self.repo, "shop")

    def good_suggestion(self):
        return {"projects": [
            {"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev", "skip": False, "reason": ""},
            {"path": "frontend", "name": "web", "stack": "node", "skill": "", "skip": False, "reason": "no react found"},
            {"path": "tools/seed", "name": "seed", "stack": "unknown", "skill": "", "skip": True, "reason": "shell scripts only"},
            {"path": "does/not/exist", "name": "x", "stack": "node", "skill": "", "skip": False, "reason": ""},
        ], "notes": ["tools/seed has no code"]}

    # ------------------------------------------------ argv and limits
    def test_argv_pins_the_model_the_guard_profile_and_no_mcp(self):
        self.service.start(self.context)
        repo_path, job_type, command = self.job_service.started[0]
        argv = command.argv
        self.assertEqual(job_type, JOB_TYPE_SUBPROJECTS)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertIn("--profile subprojects", settings_text(argv))
        self.assertNotIn("--max-budget-usd", argv)
        self.assertIn("--strict-mcp-config", argv)
        prompt = argv[argv.index("-p") + 1]
        self.assertIn(".specs/project/SUBPROJECTS-SUGGESTED.json", prompt)
        self.assertIn('"path": "backend"', prompt)

    def test_budget_is_ignored_and_never_refused(self):
        self.service.start(self.context, budget_usd=-1)
        self.assertNotIn("--max-budget-usd", self.job_service.started[0][2].argv)

    def test_refused_with_the_loop_running_or_another_job_running(self):
        self.loop_status.running = True
        with self.assertRaises(LoopRunningError):
            self.service.start(self.context)
        self.loop_status.running = False
        self.job_service.running = {"id": "j-000000000002"}
        with self.assertRaises(JobInProgressError):
            self.service.start(self.context)
        self.assertEqual(self.job_service.started, [])

    def test_detects_and_stores_the_script_list_when_absent(self):
        self.suggestion = None
        self.service.start(self.context)
        entry = self.stored_entry()
        self.assertEqual(entry["validated_by"], "script")
        self.assertEqual(sorted(project["path"] for project in entry["projects"]), ["backend", "frontend", "tools"])

    # ------------------------------------------------ merge
    def test_suggestion_is_merged_with_the_stack_change_and_the_rejections(self):
        self.suggestion = self.good_suggestion()
        job = self.service.start(self.context)
        self.assertEqual(job["state"], "ok")
        entry = self.stored_entry()
        self.assertIs(entry["confirmed"], False)
        self.assertEqual(entry["validated_by"], "ai")
        self.assertTrue(entry["validated_at"])
        self.assertEqual(entry["notes"], ["tools/seed has no code"])
        by_path = {project["path"]: project for project in entry["projects"]}
        self.assertEqual(by_path["frontend"]["stack"], "node")
        self.assertEqual(by_path["frontend"]["name"], "web")
        self.assertNotIn("tools/seed", by_path)  # nested inside the script project `tools`
        self.assertIn({"path": "tools/seed", "reason": "nested inside the listed project tools"}, entry["rejected"])
        self.assertIn({"path": "does/not/exist", "reason": "the folder does not exist"}, entry["rejected"])
        changes = {(item["path"], item["change"]) for item in entry["differences"]}
        self.assertIn(("frontend", "stack changed: react -> node"), changes)
        self.assertFalse(os.path.exists(self.repository.suggested_path(self.repo)), "the suggestion file is deleted after reading")

    def test_skipping_a_folder_and_adding_a_new_one(self):
        write_file(os.path.join(self.repo, "docs-site", "notes.txt"), "x")
        self.suggestion = {"projects": [
            {"path": "tools", "name": "tools", "stack": "node", "skill": "", "skip": True, "reason": "tooling, no code"},
            {"path": "docs-site", "name": "docs-site", "stack": "node", "skill": "", "skip": False, "reason": ""},
        ], "notes": "one note"}
        self.service.start(self.context)
        entry = self.stored_entry()
        by_path = {project["path"]: project for project in entry["projects"]}
        self.assertTrue(by_path["tools"]["skip"])
        self.assertIn("docs-site", by_path)
        self.assertEqual(entry["notes"], ["one note"])
        changes = {(item["path"], item["change"]) for item in entry["differences"]}
        self.assertIn(("tools", "skipped: tooling, no code"), changes)
        self.assertIn(("docs-site", "added"), changes)
        self.assertEqual(entry["rejected"], [])
        self.assertIs(entry["confirmed"], False)

    def test_each_kind_of_bad_entry_is_rejected_with_its_reason(self):
        self.suggestion = {"projects": [
            {"path": "../outside", "stack": "node"},
            {"path": "/etc", "stack": "node"},
            {"path": "backend/Shop.Api", "stack": "dotnet", "skill": "backend-dev"},
            {"path": "frontend", "stack": "cobol", "skill": ""},
            {"path": "frontend", "stack": "react", "skill": "backend-dev"},
            {"path": "frontend", "stack": "react", "skill": "frontend-dev"},
            {"path": "frontend/", "stack": "react", "skill": "frontend-dev"},
            "not an object",
        ]}
        self.service.start(self.context)
        entry = self.stored_entry()
        reasons = [item["reason"] for item in entry["rejected"]]
        self.assertEqual(len(reasons), 7)
        self.assertEqual(reasons[0], "the path is outside the repository")
        self.assertEqual(reasons[1], "the path is outside the repository")
        self.assertIn("nested inside the listed project backend", reasons[2])
        self.assertIn("cobol", reasons[3])
        self.assertIn("does not match the stack", reasons[4])
        self.assertEqual(reasons[5], "the path is listed twice")
        self.assertEqual(reasons[6], "the entry is not an object")
        frontend = next(project for project in entry["projects"] if project["path"] == "frontend")
        self.assertEqual(frontend["stack"], "react")

    # ------------------------------------------------ failures (VAL-05)
    def assert_script_list_kept_with_error(self, job, expected_text):
        entry = self.stored_entry()
        self.assertEqual(job["state"], "error")
        self.assertIn(expected_text, job["error"])
        self.assertIn(expected_text, entry["validation_error"])
        self.assertEqual(entry["validated_by"], "script")
        self.assertIs(entry["confirmed"], False)
        self.assertEqual(sorted(project["path"] for project in entry["projects"]), ["backend", "frontend", "tools"])

    def test_unreadable_suggestion_keeps_the_script_list(self):
        self.suggestion = "{ this is not json"
        self.assert_script_list_kept_with_error(self.service.start(self.context), "unreadable")
        self.assertFalse(os.path.exists(self.repository.suggested_path(self.repo)))

    def test_missing_suggestion_keeps_the_script_list(self):
        self.suggestion = None
        self.assert_script_list_kept_with_error(self.service.start(self.context), "did not write")

    def test_suggestion_without_a_projects_list_keeps_the_script_list(self):
        self.suggestion = {"notes": ["x"]}
        self.assert_script_list_kept_with_error(self.service.start(self.context), "no `projects` list")

    def test_failed_job_keeps_the_script_list_and_ignores_the_suggestion(self):
        self.suggestion = self.good_suggestion()
        self.claude_result = {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "budget exceeded"}
        self.assert_script_list_kept_with_error(self.service.start(self.context), "budget exceeded")
        self.assertFalse(os.path.exists(self.repository.suggested_path(self.repo)))

    def test_a_stale_suggestion_is_deleted_before_the_job_starts(self):
        write_file(self.repository.suggested_path(self.repo), json.dumps(self.good_suggestion()))
        self.suggestion = None
        self.assert_script_list_kept_with_error(self.service.start(self.context), "did not write")


if __name__ == "__main__":
    unittest.main()
