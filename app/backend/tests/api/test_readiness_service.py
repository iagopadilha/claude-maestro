"""Planning readiness: the cached `run_loop.py --check --planning` (fake runner), the `ready` planning gate, the routes and the
server side re-check of the approval."""
import json
import os
import shutil
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace

from harness_backend.infrastructure.process_runner import START_FAILURE_CODE, TIMEOUT_CODE
from harness_backend.services.planning_gates import build_planning_gates, create_readiness_gate, describe_gates_for_ai
from harness_backend.services.readiness_service import NOT_RUN_MESSAGE, ReadinessService

from .support_projects import TWO_FEATURE_ROADMAP, ProjectsTestCase, create_git_repo, read_file, write_file, write_planning_documents

READY_OUTPUT = "[check] WARNING: no test command known for repo-b\n[check] READY. Command: python run_loop.py x\n"
PROBLEM_OUTPUT = "[check] PROBLEM: credential $ADMIN_TOKEN is not defined\n[check] PROBLEM: second one\n"


class FakeLog:
    def info(self, *args, **fields):
        pass

    warn = info


class FakeRunner:
    def __init__(self, result=(0, READY_OUTPUT, ""), delay=0.0):
        self.result, self.delay, self.calls, self.running, self.max_running = result, delay, [], 0, 0
        self.guard = threading.Lock()

    def run_and_wait(self, argv, folder, timeout_seconds, input_text=None, extra_env=None, env=None):
        with self.guard:
            self.calls.append((list(argv), folder, timeout_seconds))
            self.running += 1
            self.max_running = max(self.max_running, self.running)
        time.sleep(self.delay)
        with self.guard:
            self.running -= 1
        return self.result


class ReadinessServiceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="harness-readiness-")
        self.addCleanup(lambda: shutil.rmtree(self.folder, ignore_errors=True))
        self.context = SimpleNamespace(work_dir=self.folder, repo_path=self.folder, project=SimpleNamespace(id="p1"))
        self.roadmap = os.path.join(self.folder, ".specs", "project", "ROADMAP.md")
        self.machine_file = os.path.join(self.folder, "machine.env")
        write_file(self.roadmap, "## Stage 1\n")
        write_file(self.machine_file, "A=1\n")
        self.runner = FakeRunner()
        self.service = self.build(self.runner)

    def build(self, runner, **options):
        return ReadinessService(runner, lambda: "run_loop.py", FakeLog(), lambda: [self.machine_file], python="python", **options)

    @staticmethod
    def touch(path):
        stamp = os.stat(path).st_mtime + 5
        os.utime(path, (stamp, stamp))

    def test_never_run_is_stale_and_starts_no_process(self):
        state = self.service.get(self.context)
        self.assertEqual((state["state"], state["at"], state["problems"]), ("stale", None, []))
        self.assertEqual(self.runner.calls, [])

    def test_run_caches_ready_with_warnings_and_the_planning_command(self):
        state = self.service.run(self.context, wait=True)
        self.assertEqual((state["state"], state["problems"], state["warnings"]), ("ready", [], ["no test command known for repo-b"]))
        self.assertTrue(state["at"] and state["fingerprint"])
        argv, folder, timeout = self.runner.calls[0]
        self.assertEqual((argv, folder, timeout), (["python", "run_loop.py", self.folder, "--check", "--planning"], self.folder, 120))
        cache = json.loads(read_file(os.path.join(self.folder, ".specs", "project", "loop", "internal", "planning", "readiness.json")))
        self.assertEqual(cache["state"], "ready")
        self.assertEqual(self.service.get(self.context)["state"], "ready", "read from the cache, no new process")
        self.assertEqual(len(self.runner.calls), 1)

    def test_problems_are_listed_in_order(self):
        service = self.build(FakeRunner((1, PROBLEM_OUTPUT, "")))
        state = service.run(self.context, wait=True)
        self.assertEqual((state["state"], state["problems"]),
                         ("problems", ["credential $ADMIN_TOKEN is not defined", "second one"]))

    def test_a_change_of_a_fingerprint_file_makes_it_stale_until_it_runs_again(self):
        self.service.run(self.context, wait=True)
        self.touch(self.roadmap)
        self.assertEqual(self.service.get(self.context)["state"], "stale")
        self.assertEqual(self.service.run(self.context, wait=True)["state"], "ready")
        self.touch(self.machine_file)
        self.assertEqual(self.service.get(self.context)["state"], "stale", "machine files count too")

    def test_a_new_file_changes_the_fingerprint(self):
        self.service.run(self.context, wait=True)
        write_file(os.path.join(self.folder, ".specs", "project", "REPOS.json"), "{}")
        self.assertEqual(self.service.get(self.context)["state"], "stale")

    def test_check_that_cannot_run_is_an_error(self):
        cases = ((TIMEOUT_CODE, "", "timed out"), (START_FAILURE_CODE, "", "could not start python"),
                 (2, "", "usage: unknown flag --planning"))
        for result in cases:
            state = self.build(FakeRunner(result)).run(self.context, wait=True)
            self.assertEqual(state["state"], "error", result)
            self.assertTrue(state["error"], result)
        self.assertIn("unknown flag", state["error"])

    def test_one_run_at_a_time_and_the_second_waits(self):
        runner = FakeRunner(delay=0.3)
        service = self.build(runner)
        first, second = service.schedule(self.context), service.schedule(self.context)
        self.assertEqual(service.get(self.context)["state"], "running")
        first.join(10)
        second.join(10)
        self.assertEqual((len(runner.calls), runner.max_running), (2, 1))
        self.assertEqual(service.get(self.context)["state"], "ready")

    def test_a_crashing_runner_never_leaves_it_running(self):
        class Boom:
            def run_and_wait(self, *args, **kwargs):
                raise RuntimeError("boom")

        service = self.build(Boom())
        service.schedule(self.context).join(10)
        self.assertNotEqual(service.get(self.context)["state"], "running")


class ReadinessGateTests(unittest.TestCase):
    def test_gate_reasons(self):
        ready = create_readiness_gate({"state": "ready", "at": "2026-10-20T10:00:00", "problems": [], "warnings": ["w1"]})
        self.assertEqual((ready["id"], ready["ok"], ready["optional"]), ("ready", True, False))
        self.assertIn("2026-10-20T10:00:00", ready["detail"])
        problems = create_readiness_gate({"state": "problems", "problems": ["first", "second"], "warnings": ["w1"]})
        self.assertEqual((problems["ok"], problems["detail"], problems["items"]),
                         (False, "first (+1 more)", ["first", "second", "warning: w1"]))
        error = create_readiness_gate({"state": "error", "error": "unknown flag", "problems": []})
        self.assertEqual((error["ok"], error["detail"]), (False, NOT_RUN_MESSAGE + ": unknown flag"))
        self.assertTrue(create_readiness_gate({"state": "error"})["detail"].startswith(NOT_RUN_MESSAGE))
        stale = create_readiness_gate({"state": "stale", "at": "x"})
        self.assertFalse(stale["ok"])
        self.assertIn("stale", stale["detail"])
        self.assertIn("running", create_readiness_gate({"state": "running"})["detail"])

    def test_gate_is_appended_last_only_when_the_facts_carry_readiness(self):
        facts = {"is_git": True, "structure": {"ok": True, "missing": []}, "conformance": "LEGACY", "map_files": [], "has_code": False,
                 "project_text": "# P\n\n**Vision:** x\n", "roadmap_exists": True, "features": {"a": {"status": "pending"}},
                 "roadmap_parse_errors": [], "roadmap_field_errors": [], "open_questions": [], "flows_exists": True,
                 "flow_coverage": {"missing": [], "orphans": []}}
        self.assertNotIn("ready", [gate["id"] for gate in build_planning_gates(facts)])
        gates = build_planning_gates(dict(facts, readiness={"state": "problems", "problems": ["no push guard"], "warnings": []}))
        self.assertEqual(gates[-1]["id"], "ready")
        self.assertFalse(gates[-1]["ok"] or gates[-1]["optional"])
        block = describe_gates_for_ai(gates)
        self.assertIn("[MISSING] ready: Ready to run the loop - no push guard", block)
        self.assertIn("* no push guard", block)

    def test_status_block_lists_the_last_rejected_repository_suggestions(self):
        folder = tempfile.mkdtemp(prefix="harness-rejected-")
        self.addCleanup(lambda: shutil.rmtree(folder, ignore_errors=True))
        self.assertNotIn("repos_rejected", describe_gates_for_ai([], work_dir=folder), "no file = nothing to say")
        path = os.path.join(folder, ".specs", "project", "loop", "internal", "planning", "repos_rejected.json")
        write_file(path, json.dumps([{"path": "C:/x/missing", "reason": "folder does not exist"}, "junk", {"reason": "no path"}]))
        block = describe_gates_for_ai([], work_dir=folder)
        self.assertIn("repos_rejected", block)
        self.assertIn("C:/x/missing: folder does not exist", block)
        write_file(path, "{broken")
        self.assertNotIn("repos_rejected", describe_gates_for_ai([], work_dir=folder), "a broken file is tolerated")


class ReadinessRoutesTests(ProjectsTestCase):
    use_real_readiness = True

    def setUp(self):
        super().setUp()
        self.runner = FakeRunner((0, READY_OUTPUT, ""))
        self.readiness = self.module.readiness_service
        self.readiness.process_runner = self.runner
        self.readiness.enforced = True
        self.repo = create_git_repo(os.path.join(self.projects_dir, "r"))
        self.project_id = self.open_project(self.repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % self.project_id, {"conformance": "LEGACY"})
        self.assertEqual(status, 200, body)
        write_planning_documents(self.repo, TWO_FEATURE_ROADMAP)

    def context(self):
        return self.module.project_service.get_project_context(self.project_id)

    def wait_for_state(self, expected):
        deadline = time.time() + 15
        while time.time() < deadline:
            status, body = self.request("GET", "/p/%s/api/planning/readiness" % self.project_id)
            self.assertEqual(status, 200, body)
            if body["state"] == expected:
                return body
            time.sleep(0.1)
        self.fail("readiness never became %s" % expected)

    def approve(self):
        return self.request("POST", "/p/%s/api/roadmap/approve" % self.project_id, {"confirm": True})

    def test_routes_run_in_background_and_report_the_cache(self):
        status, body = self.request("GET", "/p/%s/api/planning/readiness" % self.project_id)
        self.assertEqual((status, body["state"]), (200, "stale"))
        status, body = self.request("POST", "/p/%s/api/planning/readiness/run" % self.project_id, {})
        self.assertEqual(status, 202, body)
        self.assertEqual(set(self.wait_for_state("ready")), {"state", "at", "problems", "warnings", "uncommitted", "fingerprint", "error"})

    def test_approval_is_refused_with_the_first_problem_and_allowed_when_ready(self):
        status, body = self.approve()
        self.assertEqual((status, body["code"]), (422, "planning_gates"), "never run: stale")
        self.assertIn("Ready to run the loop", body["error"])
        self.assertEqual(body["gates"][-1]["id"], "ready")
        self.runner.result = (1, PROBLEM_OUTPUT, "")
        self.readiness.run(self.context(), wait=True)
        status, body = self.approve()
        self.assertEqual(status, 422, body)
        self.assertIn("credential $ADMIN_TOKEN is not defined", body["error"])
        self.runner.result = (0, READY_OUTPUT, "")
        self.readiness.run(self.context(), wait=True)
        status, body = self.approve()
        self.assertEqual(status, 200, body)

    def test_a_check_that_did_not_run_blocks_with_its_reason(self):
        self.runner.result = (2, "", "unknown flag")
        self.readiness.run(self.context(), wait=True)
        status, body = self.approve()
        self.assertEqual(status, 422, body)
        self.assertIn("readiness check did not run", body["error"])

    def test_approval_writes_the_capabilities_block_of_project_md(self):
        self.readiness.run(self.context(), wait=True)
        project_md = os.path.join(self.repo, ".specs", "project", "PROJECT.md")
        before = read_file(project_md)
        self.assertEqual(self.approve()[0], 200)
        after = read_file(project_md)
        self.assertIn("## What the agents can use (inside the loop)", after)
        self.assertTrue(after.startswith(before), "the block is appended; nothing before it changes")

    def test_not_enforced_keeps_the_approval_free(self):
        self.readiness.enforced = False
        self.assertEqual(self.approve()[0], 200)


if __name__ == "__main__":
    unittest.main()
