"""Routes of the monorepo subprojects (real app, fake claude): detect and read, confirm, Conformance per row and all, the 422 of an
unconfirmed list, validation (job type and `--model sonnet`), the map queue and the system map, the 409 refusals."""
import json
import os
import time

from .support_projects import FAKE_CLAUDE_CODE, JOB_WAIT_TIMEOUT_SECONDS, ProjectsTestCase, create_git_repo, read_file, write_file

MONOREPO_FILES = {
    "backend/App.sln": "",
    "backend/Api/Program.cs": "class P {}\n",
    "backend/Data/Repo.cs": "class R {}\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}),
    "frontend/src/main.tsx": "export {}\n",
}
SUGGESTION = {"projects": [{"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev", "skip": False, "reason": ""}],
              "notes": ["looks right"]}
EXTRA_FAKE_BEHAVIOUR = '''if prompt.startswith("You validate the list"):
    write(os.path.join(project, "SUBPROJECTS-SUGGESTED.json"), %r)
    print(json.dumps({"type": "result", "is_error": False, "result": "validated", "session_id": "s", "total_cost_usd": 0.1}))
    sys.exit(0)
if prompt.startswith("/orchestrator map --system"):
    write(os.path.join("docs", "codebase", "SYSTEM.md"), "# System\\n")
    print(json.dumps({"type": "result", "is_error": False, "result": "system mapped", "session_id": "s", "total_cost_usd": 0.1}))
    sys.exit(0)
result = "nothing"
''' % json.dumps(SUGGESTION)


class TestSubprojectsRoutes(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        write_file(self.fake_claude_path, FAKE_CLAUDE_CODE.replace('result = "nothing"\n', EXTRA_FAKE_BEHAVIOUR, 1))

    def open_scratch(self, name="mono", files=None):
        repo = create_git_repo(os.path.join(self.projects_dir, name), files or MONOREPO_FILES)
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        self.assertEqual(status, 200, body)
        return repo, project_id

    @staticmethod
    def route(project_id, suffix=""):
        return "/p/%s/api/subprojects%s" % (project_id, suffix)

    def confirm(self, project_id):
        status, body = self.request("PUT", self.route(project_id), {"confirmed": True})
        self.assertEqual(status, 200, body)
        return body

    def wait_for_queue(self, project_id):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status, body = self.request("GET", self.route(project_id))
            queue = body.get("queue")
            if status == 200 and queue and queue["state"] != "running":
                return queue
            time.sleep(0.1)
        self.fail("the map queue did not finish")

    def test_get_detects_the_monorepo_and_reports_system_and_no_validation_job(self):
        _, project_id = self.open_scratch()
        status, body = self.request("GET", self.route(project_id))
        self.assertEqual(status, 200, body)
        self.assertTrue(body["monorepo"])
        self.assertFalse(body["confirmed"])
        self.assertEqual(sorted(row["path"] for row in body["projects"]), ["backend", "frontend"])
        self.assertFalse(body["system"]["enabled"])
        self.assertIsNone(body["validation_job"])

    def test_update_list_confirms_and_detect_is_refused_afterwards(self):
        _, project_id = self.open_scratch()
        self.assertTrue(self.confirm(project_id)["confirmed"])
        self.assertEqual(self.request("POST", self.route(project_id, "/detect"), {})[0], 409)
        self.assertEqual(self.request("POST", self.route(project_id, "/detect"), {"force": True})[0], 200)

    def test_conformance_is_422_until_the_list_is_confirmed_then_per_row_and_all(self):
        repo, project_id = self.open_scratch()
        status, body = self.request("POST", self.route(project_id, "/conformance"), {"path": "backend", "value": "STANDARD"})
        self.assertEqual((status, body.get("code")), (422, "subprojects_not_confirmed"), body)
        self.confirm(project_id)
        status, body = self.request("POST", self.route(project_id, "/conformance"), {"path": "backend", "value": "STANDARD"})
        self.assertEqual(status, 200, body)
        self.assertIn("STANDARD", read_file(os.path.join(repo, "backend", "CLAUDE.md")))
        status, body = self.request("POST", self.route(project_id, "/conformance"), {"all": True})
        self.assertEqual(status, 200, body)
        self.assertEqual([item["path"] for item in body["written"]], ["frontend"])
        self.assertEqual([item["path"] for item in body["kept"]], ["backend"])
        self.assertEqual(self.request("POST", self.route(project_id, "/conformance"), {"path": "backend", "value": "BOGUS"})[0], 400)

    def test_root_conformance_is_409_for_a_confirmed_monorepo(self):
        _, project_id = self.open_scratch()
        self.confirm(project_id)
        self.assertEqual(self.request("POST", "/p/%s/api/conformance" % project_id, {"value": "STANDARD"})[0], 409)

    def test_validate_starts_a_subprojects_job_on_sonnet_and_applies_the_suggestion(self):
        _, project_id = self.open_scratch()
        status, body = self.request("POST", self.route(project_id, "/validate"), {})
        self.assertEqual(status, 202, body)
        self.assertTrue(body["ok"])
        self.assertEqual(body["job"]["type"], "subprojects")
        job = self.wait_for_job(project_id, body["job"]["id"])
        self.assertEqual(job["state"], "ok", job)
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        status, body = self.request("GET", self.route(project_id))
        self.assertEqual(body["validated_by"], "ai")
        self.assertEqual(body["validation_job"]["id"], job["id"])
        self.assertFalse(body["confirmed"])
        self.assertEqual(self.request("POST", self.route(project_id, "/validate"), {"budget_usd": 99})[0], 202)  # ignored, never a 400
        self.wait_for_jobs_to_finish()
        for call in self.read_claude_calls():
            self.assertNotIn("--max-budget-usd", call["argv"])

    def test_map_queue_writes_each_subproject_map_then_the_system_map_and_a_running_job_is_409(self):
        repo, project_id = self.open_scratch()
        status, body = self.request("POST", self.route(project_id, "/map"), {})
        self.assertEqual((status, body.get("code")), (422, "subprojects_not_confirmed"), body)
        self.confirm(project_id)
        os.environ["FAKE_CLAUDE_DELAY"] = "0.4"
        status, body = self.request("POST", self.route(project_id, "/map"), {"budget_usd": 1})  # ignored
        self.assertEqual(status, 202, body)
        self.assertTrue(body["ok"])
        self.assertIn("id", body["job"])
        self.assertEqual(body["queue"]["state"], "running")
        self.assertEqual(sorted(body["queue"]["items"]), ["backend", "frontend"])
        self.assertEqual(self.request("POST", self.route(project_id, "/map"), {})[0], 409)
        self.assertEqual(self.request("POST", self.route(project_id, "/validate"), {})[0], 409)
        queue = self.wait_for_queue(project_id)
        self.assertEqual(queue["state"], "done", queue)
        self.assertEqual(queue["system"]["state"], "done", queue)
        self.assertEqual(sorted(queue["done"]), ["backend", "frontend"])
        for path in ("backend", "frontend"):
            self.assertTrue(os.path.isfile(os.path.join(repo, path, "docs", "codebase", "STACK.md")), path)
        # "map all" ends with the system map (SYS-01)
        system_path = os.path.join(repo, "docs", "codebase", "SYSTEM.md")
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while not os.path.isfile(system_path) and time.time() < deadline:
            time.sleep(0.1)
        self.wait_for_jobs_to_finish()
        self.assertTrue(os.path.isfile(system_path))
        for call in self.read_claude_calls():
            self.assertEqual(call["argv"][call["argv"].index("--model") + 1], "sonnet")
            self.assertNotIn("--max-budget-usd", call["argv"])
        status, body = self.request("GET", self.route(project_id))
        self.assertTrue(body["system"]["exists"])
        self.assertIn("enabled", body["system"])
        self.assertEqual(body["map_queue"]["state"], "done")
        self.assertEqual(body["queue"], body["map_queue"])

    def test_system_map_route_is_refused_before_the_subprojects_are_mapped_and_runs_afterwards(self):
        repo, project_id = self.open_scratch()
        self.confirm(project_id)
        status, body = self.request("POST", self.route(project_id, "/system-map"), {})
        self.assertEqual(status, 400, body)
        self.assertIn("map at least", body["error"])
        for path in ("backend", "frontend"):
            write_file(os.path.join(repo, path, "docs", "codebase", "STACK.md"), "# Stack\n")
        status, body = self.request("POST", self.route(project_id, "/system-map"), {})
        self.assertEqual(status, 202, body)
        job = self.wait_for_job(project_id, body["job"]["id"])
        self.assertEqual(job["state"], "ok", job)
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_root_map_route_answers_202_at_once_with_the_job(self):
        _, project_id = self.open_scratch("single", {"src/app.py": "print(1)\n"})
        os.environ["FAKE_CLAUDE_DELAY"] = "0.5"
        started = time.time()
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, body)
        self.assertLess(time.time() - started, 0.45)
        self.assertEqual(body["detail"]["state"], "running")
