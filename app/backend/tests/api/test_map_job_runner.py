"""Root map job (MapJobRunner behind POST codebase/map): answers at once, snapshot in before_start, model ceiling, log lines."""
import json
import os
import time
import unittest

from .support_projects import FAKE_DELAY_VARIABLE, FAKE_MAP_MODE_VARIABLE, ProjectsTestCase, create_git_repo, read_file

TINY_FILE_FOLDERS = 40
TINY_FILES_PER_FOLDER = 500  # 20 000 tiny files in total
FAST_RESPONSE_SECONDS = 1.0
SNAPSHOT_PROGRESS = "preparing the safety snapshot"


class MapJobRunnerTests(ProjectsTestCase):
    def read_log_events(self):
        try:
            with open(os.path.join(self.config.logs_dir, "app.jsonl"), encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()]
        except OSError:
            return []

    def post_map(self, project_id, body=None):
        return self.request("POST", "/p/%s/api/codebase/map" % project_id, body or {})

    def get_job(self, project_id, job_id):
        return self.request("GET", "/p/%s/api/jobs/%s" % (project_id, job_id))[1]

    def get_guard(self):
        return self.module.roadmap_service.map_job_runner.workspace_file_guard

    def test_huge_node_modules_does_not_delay_the_response_and_events_are_ordered(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "huge"), {"src/app.py": "print(1)\n"})
        modules = os.path.join(repo, "frontend", "node_modules")
        for folder_index in range(TINY_FILE_FOLDERS):
            folder = os.path.join(modules, "pkg%d" % folder_index)
            os.makedirs(folder)
            for file_index in range(TINY_FILES_PER_FOLDER):
                with open(os.path.join(folder, "f%d.js" % file_index), "w") as handle:
                    handle.write("x")
        project_id = self.open_project(repo)
        started = time.time()
        status, body = self.post_map(project_id)
        elapsed = time.time() - started
        self.assertEqual(status, 202, body)
        self.assertLess(elapsed, FAST_RESPONSE_SECONDS)
        self.assertEqual(self.wait_for_job(project_id, body["job"])["state"], "ok")
        events = [entry["event"] for entry in self.read_log_events()]
        self.assertIn("map_requested", events)
        self.assertLess(events.index("map_requested"), events.index("claude_job_started"))

    def test_argv_pins_the_model_ceiling(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "model"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.post_map(project_id)
        self.assertEqual(status, 202, body)
        self.wait_for_job(project_id, body["job"])
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_job_record_says_preparing_snapshot_while_before_start_runs(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "progress"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        guard = self.get_guard()
        real_take_snapshot = guard.take_snapshot

        def slow_snapshot(*args, **kwargs):
            time.sleep(1.0)
            return real_take_snapshot(*args, **kwargs)

        guard.take_snapshot = slow_snapshot
        status, body = self.post_map(project_id)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.get_job(project_id, body["job"])["progress"], SNAPSHOT_PROGRESS)
        self.assertEqual(self.wait_for_job(project_id, body["job"])["state"], "ok")

    def test_before_start_failure_ends_the_job_as_error_without_running_claude(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "failure"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)

        def failing_snapshot(*args, **kwargs):
            raise OSError("disk exploded")

        self.get_guard().take_snapshot = failing_snapshot
        status, body = self.post_map(project_id)
        self.assertEqual(status, 202, body)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "error")
        self.assertIn("disk exploded", job["error"])
        self.assertEqual(self.read_claude_calls(), [])

    def test_refusal_is_logged_with_reason(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "refused"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        os.environ[FAKE_DELAY_VARIABLE] = "1.5"
        first_status, first = self.post_map(project_id)
        self.assertEqual(first_status, 202)  # INS-11: no budget field, so the first request is accepted; the second one finds a job running
        status, _ = self.post_map(project_id)
        self.assertEqual(status, 409)
        self.wait_for_job(project_id, first["job"])
        events = {entry["event"]: entry for entry in self.read_log_events()}
        self.assertIn("map_requested", events)
        self.assertIn("map_refused", events)

    def test_existing_behavior_prompt_and_index(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "same"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.post_map(project_id)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_job(project_id, body["job"])["state"], "ok")
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/orchestrator map --inventory .specs/project/loop/internal/inventory/_root.json")  # INV-06
        claude_md = read_file(os.path.join(repo, "CLAUDE.md"))
        self.assertIn("<!-- maestro:context -->", claude_md, "the one block about the map")
        self.assertIn("`docs/codebase/STACK.md`", claude_md)
        self.assertNotIn("<!-- CODEBASE:BEGIN -->", claude_md, "a file with the context block has no second map block")
        self.assertIn("[STACK.md](STACK.md)", read_file(os.path.join(repo, "docs", "codebase", "README.md")))

    def test_changes_outside_allowed_folders_are_undone(self):
        os.environ[FAKE_MAP_MODE_VARIABLE] = "touch-code"
        repo = create_git_repo(os.path.join(self.projects_dir, "touch"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.post_map(project_id)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "ok")
        self.assertIn("src/app.py", job["reply"])
        self.assertEqual(read_file(os.path.join(repo, "src", "app.py")), "print(1)\n")

    def test_no_document_failure_message(self):
        os.environ[FAKE_MAP_MODE_VARIABLE] = "nothing"
        repo = create_git_repo(os.path.join(self.projects_dir, "nothing"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.post_map(project_id)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "error")
        self.assertIn("wrote no document in docs/codebase", job["error"])


if __name__ == "__main__":
    unittest.main()
