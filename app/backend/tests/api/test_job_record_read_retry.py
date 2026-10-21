"""A job record that exists but is unreadable for a moment (Windows: being replaced by the job thread) is read again instead of
answering "job not found"."""
import os
import tempfile
import unittest

from harness_backend.services.claude_job_service import ClaudeJobService


class FlakyRepository:
    def __init__(self, failures):
        self.failures = failures

    def read_json(self, path):
        if self.failures:
            self.failures -= 1
            return {}
        return {"id": "j-1", "state": "done"}


class TestJobRecordReadRetry(unittest.TestCase):
    def test_existing_record_is_read_again_after_a_failed_read(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "j-1.json")
            open(path, "w").close()
            service = ClaudeJobService(None, FlakyRepository(failures=2), None)
            self.assertEqual(service.read_corrected_record(path)["id"], "j-1")

    def test_missing_record_is_not_retried(self):
        repository = FlakyRepository(failures=1)
        service = ClaudeJobService(None, repository, None)
        self.assertIsNone(service.read_corrected_record(os.path.join(tempfile.gettempdir(), "no-such-job-file.json")))
        self.assertEqual(repository.failures, 0)


if __name__ == "__main__":
    unittest.main()
