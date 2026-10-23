"""`ClaudeJobCommand.before_start` in the job thread: it may return the final argv/stdin, it may end the job with a coded error (the
record and the public job carry `code`), and the old contract (return None) keeps working."""
import os
import shutil
import tempfile
import time
import unittest

from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.claude_job_service import ClaudeJobCommand, ClaudeJobError, ClaudeJobService


class RecordingClient:
    def __init__(self):
        self.calls = []

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        self.calls.append({"argv": list(argv), "stdin": stdin_text})
        return {"ok": True, "reply": "done", "cost_usd": 0.1, "session_id": "s1", "error": ""}


class BeforeStartTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="job-before-start-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        os.makedirs(self.repo)
        self.client = RecordingClient()
        self.jobs = ClaudeJobService(self.client, SpecsFileRepository(), StructuredLog(os.path.join(self.tmp, "logs")))

    def finished(self, job):
        for _ in range(400):
            if job["id"] not in self.jobs.running_jobs:
                return self.jobs.get_job(self.repo, job["id"])
            time.sleep(0.025)
        self.fail("the job never finished")

    def test_the_returned_argv_and_stdin_replace_the_commands(self):
        command = ClaudeJobCommand(["old"], "old text", before_start=lambda progress: {"argv": ["claude", "-p"], "stdin_text": "built later"})
        job = self.finished(self.jobs.start_job(self.repo, "planning", command))
        self.assertEqual(job["state"], "ok")
        self.assertEqual(self.client.calls, [{"argv": ["claude", "-p"], "stdin": "built later"}])
        self.assertNotIn("code", job)

    def test_a_before_start_that_returns_nothing_keeps_the_command(self):
        command = ClaudeJobCommand(["claude"], "text", before_start=lambda progress: None)
        self.assertEqual(self.finished(self.jobs.start_job(self.repo, "map", command))["state"], "ok")
        self.assertEqual(self.client.calls, [{"argv": ["claude"], "stdin": "text"}])

    def test_a_coded_error_ends_the_job_with_the_code_and_claude_never_starts(self):
        finalized = []

        def refuse(progress):
            raise ClaudeJobError("prerequisites missing before planning: Codebase map", "planning_prerequisites")

        command = ClaudeJobCommand(["claude"], "text", before_start=refuse, finalize=lambda result: finalized.append(result) or result)
        job = self.finished(self.jobs.start_job(self.repo, "planning", command))
        self.assertEqual((job["state"], job["code"]), ("error", "planning_prerequisites"))
        self.assertIn("Codebase map", job["error"])
        self.assertEqual((self.client.calls, finalized), ([], []))

    def test_a_plain_exception_has_no_code(self):
        def fail(progress):
            raise OSError("disk full")

        job = self.finished(self.jobs.start_job(self.repo, "map", ClaudeJobCommand(["claude"], "", before_start=fail)))
        self.assertEqual(job["state"], "error")
        self.assertNotIn("code", job)


if __name__ == "__main__":
    unittest.main()
