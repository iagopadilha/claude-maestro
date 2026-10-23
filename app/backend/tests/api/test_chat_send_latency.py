"""CHT-01: every chat send route answers within 300 ms on a repository with ~5000 files (the heavy preparation runs in the job thread).

Each case times only the handler (HTTP POST for the routes, the service call for the follow-up), prints `TIMING route=... ms=...` and
asserts the budget. The job is then awaited so a refusal moved into the job (code `planning_prerequisites`) is also checked."""
import os
import shutil
import time
import unittest

from .support_commands import CommandsTestCase
from .support_projects import write_file
from . import test_commands_quarantine as quarantine_tests
from . import test_follow_up_service as follow_up_base
from .test_planning_import import SPEC_TEXT
from .test_projects_planning import PlanningTestCase

FILE_COUNT = 5000
BUDGET_MS = 300
FOLDERS = 50


def fill_with_files(root):
    """FILE_COUNT small text files under `root/data` plus a codebase map document (a repo with code needs a map to plan).

    The files are read once here: on Windows the antivirus scans a freshly written file on its first read (about 50 s for 5000 files),
    which would measure the scanner instead of the app."""
    write_file(os.path.join(root, "docs", "codebase", "STACK.md"), "# Stack\n\nText only.\n")
    for index in range(FILE_COUNT):
        write_file(os.path.join(root, "data", "d%02d" % (index % FOLDERS), "file%04d.txt" % index), "row %d\n" % index)
    for current, _, names in os.walk(os.path.join(root, "data")):
        for name in names:
            with open(os.path.join(current, name), "rb") as handle:
                handle.read()


def report(route, started):
    elapsed = (time.perf_counter() - started) * 1000
    print("TIMING route=%s ms=%.0f" % (route, elapsed))
    return elapsed


class TestPlanningLatency(PlanningTestCase):
    def setUp(self):
        super().setUp()
        self.repo, self.project_id = self.create_planning_project()
        fill_with_files(self.repo)

    def test_planning_send(self):
        started = time.perf_counter()
        status, body = self.send(self.project_id, "Build a to-do app")
        self.assertLess(report("planning/send", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_planning_job(self.project_id, body["job_id"])["state"], "ok")

    def test_planning_send_with_a_warm_gates_cache(self):
        self.get_planning(self.project_id)
        started = time.perf_counter()
        status, body = self.send(self.project_id, "Build a to-do app")
        self.assertLess(report("planning/send (gates cached)", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.wait_for_planning_job(self.project_id, body["job_id"])

    def test_planning_import_send(self):
        write_file(os.path.join(self.temporary_dir, "source", "feat", "spec.md"), SPEC_TEXT)
        status, imported = self.request("POST", "/p/%s/api/planning/import" % self.project_id,
                                        {"path": os.path.join(self.temporary_dir, "source", "feat")})
        self.assertEqual(status, 200, imported)
        started = time.perf_counter()
        status, body = self.request("POST", "/p/%s/api/planning/import/%s/send" % (self.project_id, imported["import_id"]))
        self.assertLess(report("planning/import/send", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.wait_for_planning_job(self.project_id, body["job_id"])

    def test_red_prerequisites_end_the_job_with_the_code_when_the_gates_are_not_cached(self):
        shutil.rmtree(os.path.join(self.repo, "docs"))  # code (the data folder) without a map: the map gate turns red
        status, body = self.send(self.project_id, "plan it")
        self.assertIn(status, (202, 409), body)
        if status == 409:  # the gates were computed within 10 s by an earlier call: the refusal stays synchronous
            self.assertEqual(body["code"], "planning_prerequisites")
            return
        job = self.wait_for_planning_job(self.project_id, body["job_id"])
        self.assertEqual((job["state"], job.get("code")), ("error", "planning_prerequisites"), job)
        self.assertIn("Codebase map", job["error"])
        self.assertEqual(self.read_claude_calls(), [], "claude never started")
        thread = self.get_planning(self.project_id)["thread"]
        self.assertEqual(thread["messages"], [], "the unsent user message is taken out of the thread, as the old 409 never stored it")

    def test_red_prerequisites_do_not_mark_an_import_as_sent(self):
        write_file(os.path.join(self.temporary_dir, "source", "feat", "spec.md"), SPEC_TEXT)
        _, imported = self.request("POST", "/p/%s/api/planning/import" % self.project_id,
                                   {"path": os.path.join(self.temporary_dir, "source", "feat")})
        shutil.rmtree(os.path.join(self.repo, "docs"))
        status, body = self.request("POST", "/p/%s/api/planning/import/%s/send" % (self.project_id, imported["import_id"]))
        if status == 202:
            job = self.wait_for_planning_job(self.project_id, body["job_id"])
            self.assertEqual(job.get("code"), "planning_prerequisites", job)
        else:
            self.assertEqual(status, 409, body)
        _, listed = self.request("GET", "/p/%s/api/planning/imports" % self.project_id)
        self.assertIsNone(listed["imports"][0]["sent_at"])
        self.assertEqual(self.get_planning(self.project_id)["thread"]["messages"], [])


class TestFollowUpLatency(follow_up_base.FollowUpTestCase):
    def test_follow_up_send(self):
        fill_with_files(self.work)
        self.client.behavior = self.append_stage
        started = time.perf_counter()
        job = self.service.send_follow_up(self.context, {"text": "add a report"})["job"]
        self.assertLess(report("follow-up/send", started), BUDGET_MS)
        self.assertEqual(self.wait_for(job["id"])["state"], "ok")


class TestChatAndQuarantineLatency(CommandsTestCase):
    setup_work_repository = quarantine_tests.QuarantineTests.setup_work_repository
    setup_verifier_attempt = quarantine_tests.QuarantineTests.setup_verifier_attempt
    prepare = quarantine_tests.QuarantineTests.prepare
    git = quarantine_tests.QuarantineTests.git
    pause = quarantine_tests.QuarantineTests.pause
    wait_for_job = quarantine_tests.QuarantineTests.wait_for_job

    def setUp(self):
        super().setUp()
        self.setup_work_repository()
        self.setup_verifier_attempt()
        fill_with_files(self.fixture.work)  # untracked: `git status` lists them as `??`, the snapshot would walk them
        self.prepare()

    def test_chat_send(self):
        started = time.perf_counter()
        status, body = self.fixture.call("/api/chat/send", "POST", {"text": "what is going on?"})
        self.assertLess(report("chat/send", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_job(body["job"])["state"], "ok")

    def test_chat_send_in_the_quarantine_scope(self):
        started = time.perf_counter()
        status, body = self.fixture.call("/api/chat/send", "POST", {"text": "why did it fail?", "scope": {"type": "quarantine", "feature": "service-c"}})
        self.assertLess(report("chat/send (quarantine scope)", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_job(body["job"])["state"], "ok")

    def test_quarantine_summary(self):
        started = time.perf_counter()
        status, body = self.fixture.call("/api/quarantine/service-c/summary", "POST", {})
        self.assertLess(report("quarantine/summary", started), BUDGET_MS)
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_job(body["job"])["state"], "ok")


if __name__ == "__main__":
    unittest.main()
