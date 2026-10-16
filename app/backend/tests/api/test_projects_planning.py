"""Planning mode (`/p/<id>/api/planning*`) with the FAKE claude: gates, the live chat that writes the three documents, the
file guard, busy/reset, the approval refusal and the CLI permission flags of the reply job."""
import json
import os
import time
import unittest

from harness_backend.domain.flows_document import check_flow_coverage, parse_flows
from harness_backend.services.planning_gates import are_gates_ok, are_prerequisites_ok, build_planning_gates

from .support_projects import (FAKE_DELAY_VARIABLE, FAKE_FAIL_VARIABLE, JOB_WAIT_TIMEOUT_SECONDS, ProjectsTestCase, create_git_repo,
                               loop_file_path, read_file, write_file)

GATE_IDS = ["git", "structure", "conformance", "map", "goal", "features", "fields", "open_questions", "flows"]
FAKE_PLANNING_SESSION = "fake-planning-session-1"
EXPECTED_EDIT_RULES = ["Edit(/.specs/project/PROJECT.md)", "Edit(/.specs/project/ROADMAP.md)", "Edit(/.specs/project/FLOWS.md)"]


def gates_by_id(planning):
    return {gate["id"]: gate for gate in planning["gates"]}


def list_flag_values(argv, flag):
    """Values after `flag` up to the next `--option`."""
    start = argv.index(flag) + 1
    values = []
    for item in argv[start:]:
        if item.startswith("--"):
            break
        values.append(item)
    return values


class PlanningTestCase(ProjectsTestCase):
    def create_planning_project(self, name="plan", initialize=True, files=None):
        repo = create_git_repo(os.path.join(self.projects_dir, name), files)
        project_id = self.open_project(repo)
        if initialize:
            status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": "LEGACY"})
            self.assertEqual(status, 200, body)
        return repo, project_id

    def get_planning(self, project_id):
        status, body = self.request("GET", "/p/%s/api/planning" % project_id)
        self.assertEqual(status, 200, body)
        return body

    def send(self, project_id, text):
        return self.request("POST", "/p/%s/api/planning/send" % project_id, {"text": text})

    def wait_for_planning_job(self, project_id, job_id):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status, job = self.request("GET", "/p/%s/api/planning/job/%s" % (project_id, job_id))
            self.assertEqual(status, 200, job)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("planning job %s did not finish" % job_id)

    def send_and_wait(self, project_id, text):
        status, body = self.send(project_id, text)
        self.assertEqual(status, 202, body)
        return self.wait_for_planning_job(project_id, body["job_id"])


class TestPlanningRead(PlanningTestCase):
    def test_new_project_is_in_planning_mode_with_every_gate(self):
        _, project_id = self.create_planning_project(initialize=False)
        planning = self.get_planning(project_id)
        self.assertTrue(planning["required"])
        self.assertEqual([gate["id"] for gate in planning["gates"]], GATE_IDS)
        self.assertEqual(set(planning["gates"][0]), {"id", "label", "ok", "optional", "detail", "items"})
        gates = gates_by_id(planning)
        self.assertTrue(gates["git"]["ok"])
        self.assertFalse(gates["structure"]["ok"])
        self.assertTrue(gates["map"]["optional"], "a repo without code needs no map")
        self.assertEqual((planning["can_approve"], planning["approved"]), (False, False))
        self.assertEqual(planning["thread"], {"messages": [], "busy": False, "job_id": None, "session": False})
        self.assertEqual(planning["docs"], {"project": None, "roadmap": None, "flows": None})

    def test_initialized_project_lists_what_is_missing(self):
        repo, project_id = self.create_planning_project()
        self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "project", "FLOWS.md")), "initialize creates FLOWS.md")
        planning = self.get_planning(project_id)
        gates = gates_by_id(planning)
        for gate_id in ("git", "structure", "conformance"):
            self.assertTrue(gates[gate_id]["ok"], gate_id)
        self.assertFalse(gates["goal"]["ok"])
        self.assertIn("FILL_IN", gates["goal"]["items"][0])
        self.assertFalse(gates["features"]["ok"])
        self.assertTrue(gates["open_questions"]["ok"], "the template says `- none`")
        self.assertFalse(gates["flows"]["ok"])
        self.assertTrue(planning["docs"]["flows"].startswith("# Flows"))

    def test_repo_with_code_requires_the_map(self):
        _, project_id = self.create_planning_project(files={"src/app.py": "print(1)\n"})
        gate = gates_by_id(self.get_planning(project_id))["map"]
        self.assertEqual((gate["ok"], gate["optional"]), (False, False))
        status, body = self.send(project_id, "plan it")
        self.assertEqual((status, body["code"]), (409, "planning_prerequisites"), body)
        self.assertIn("Codebase map", body["error"])
        self.assertEqual([gate["id"] for gate in body["gates"]], GATE_IDS)

    def test_flow_orphans_are_a_warning_only(self):
        repo, project_id = self.create_planning_project()
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), "## Stage 1\n- [a] status=pending\n")
        write_file(os.path.join(repo, ".specs", "project", "FLOWS.md"),
                   "# Flows\n\n## [a] A\n1. do\n2) Expected: ok\n\n## [ghost] G\n1. x\n2. y\n")
        gate = gates_by_id(self.get_planning(project_id))["flows"]
        self.assertTrue(gate["ok"], gate)
        self.assertIn("ghost", gate["detail"])


class TestPlanningConversation(PlanningTestCase):
    def test_conversation_writes_the_documents_and_resumes_the_session(self):
        repo, project_id = self.create_planning_project()
        job = self.send_and_wait(project_id, "Build a to-do app")
        self.assertEqual((job["state"], job["reply"], job["cost_usd"]), ("ok", "planned: Build a to-do app", 0.42))

        call = self.read_claude_calls()[-1]
        argv = call["argv"]
        self.assertEqual(os.path.normcase(call["cwd"]), os.path.normcase(repo))
        self.assertEqual(call["stdin"], "/orchestrator plan --headless\nBuild a to-do app")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(list_flag_values(argv, "--allowedTools"), ["Read", "Grep", "Glob", "Skill"] + EXPECTED_EDIT_RULES)
        self.assertEqual(list_flag_values(argv, "--disallowedTools"), ["Bash", "NotebookEdit", "Agent"])
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 2.0)
        self.assertIn("--strict-mcp-config", argv, "planning runs without MCP")
        self.assertNotIn("--model", argv, "the account's default model")
        self.assertNotIn("--resume", argv)

        planning = self.get_planning(project_id)
        self.assertEqual([(message["role"], message["text"]) for message in planning["thread"]["messages"]],
                         [("user", "Build a to-do app"), ("ai", "planned: Build a to-do app")])
        self.assertEqual(set(planning["thread"]["messages"][1]), {"role", "text", "at", "cost_usd"})
        self.assertEqual(planning["thread"]["messages"][1]["cost_usd"], 0.42)
        self.assertEqual((planning["thread"]["busy"], planning["thread"]["session"]), (False, True))
        self.assertIn("[api-base]", planning["docs"]["roadmap"])
        self.assertIn("## [screen-base]", planning["docs"]["flows"])
        self.assertTrue(all(gate["ok"] or gate["optional"] for gate in planning["gates"]), planning["gates"])
        self.assertEqual((planning["can_approve"], planning["approved"], planning["required"]), (True, False, True))
        self.assertNotIn(FAKE_PLANNING_SESSION, json.dumps(planning), "the session never leaves through the API")
        thread_file = json.loads(read_file(loop_file_path(repo, "internal", "planning", "thread.json")))
        self.assertEqual(thread_file["session_id"], FAKE_PLANNING_SESSION)

        self.send_and_wait(project_id, "Add a due date")
        call = self.read_claude_calls()[-1]
        self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], FAKE_PLANNING_SESSION)
        self.assertEqual(call["stdin"], "Add a due date", "a resumed message carries only the user text")
        self.assertEqual(len(self.get_planning(project_id)["thread"]["messages"]), 4)

        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual(status, 200, body)
        planning = self.get_planning(project_id)
        self.assertEqual((planning["required"], planning["approved"]), (False, True))
        self.assertTrue(os.path.isfile(loop_file_path(repo, "json", "ROADMAP-APPROVED.json")))

    def test_open_question_blocks_the_approval(self):
        _, project_id = self.create_planning_project()
        self.send_and_wait(project_id, "ASK me what you need")
        planning = self.get_planning(project_id)
        gate = gates_by_id(planning)["open_questions"]
        self.assertEqual((gate["ok"], gate["items"]), (False, ["Which database? | recommended: SQLite"]))
        self.assertFalse(planning["can_approve"])
        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual((status, body["ok"], body["code"]), (422, False, "planning_gates"), body)
        self.assertEqual([gate["id"] for gate in body["gates"]], GATE_IDS)
        self.assertIn("Open questions", body["error"])

    def test_guard_undoes_changes_outside_the_three_documents(self):
        repo, project_id = self.create_planning_project()
        state_before = read_file(os.path.join(repo, ".specs", "project", "STATE.md"))
        job = self.send_and_wait(project_id, "TOUCH-OUTSIDE please")
        self.assertEqual(job["state"], "ok")
        self.assertIn("undid changes outside the planning documents", job["reply"])
        self.assertFalse(os.path.exists(os.path.join(repo, "src", "evil.txt")))
        self.assertFalse(os.path.exists(os.path.join(repo, "src")), "a folder born only for the removed file goes too")
        self.assertEqual(read_file(os.path.join(repo, "README.md")), "# test\n")
        self.assertEqual(read_file(os.path.join(repo, ".specs", "project", "STATE.md")), state_before)
        self.assertFalse(os.path.exists(loop_file_path(repo, "json", "ROADMAP-APPROVED.json")), "the chat never approves")
        self.assertIn("[api-base]", read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md")), "the documents stay")
        self.assertIn("undid changes", self.get_planning(project_id)["thread"]["messages"][-1]["text"])
        log = read_file(os.path.join(self.config.logs_dir, "app.jsonl"))
        self.assertIn("planning_guard_reverted", log)

    def test_busy_reset_and_job_route(self):
        os.environ[FAKE_DELAY_VARIABLE] = "1.5"
        repo, project_id = self.create_planning_project()
        status, body = self.send(project_id, "first")
        self.assertEqual(status, 202, body)
        job_id = body["job_id"]
        planning = self.get_planning(project_id)
        self.assertEqual((planning["thread"]["busy"], planning["thread"]["job_id"]), (True, job_id))
        status, body = self.send(project_id, "second")
        self.assertEqual((status, body["code"]), (409, "planning_busy"), body)
        status, body = self.request("POST", "/p/%s/api/planning/reset" % project_id, {"confirm": True})
        self.assertEqual((status, body["code"]), (409, "planning_busy"), body)
        self.assertEqual(self.request("POST", "/p/%s/api/codebase/map" % project_id, {})[0], 409, "one claude job per project")
        self.assertEqual(self.wait_for_planning_job(project_id, job_id)["state"], "ok")
        os.environ.pop(FAKE_DELAY_VARIABLE)

        self.assertEqual(self.request("POST", "/p/%s/api/planning/reset" % project_id, {})[0], 400)
        status, body = self.request("POST", "/p/%s/api/planning/reset" % project_id, {"confirm": True})
        self.assertEqual((status, body), (200, {"ok": True}))
        planning = self.get_planning(project_id)
        self.assertEqual(planning["thread"], {"messages": [], "busy": False, "job_id": None, "session": False})
        self.assertIn("[api-base]", planning["docs"]["roadmap"], "the documents stay after a reset")
        self.send_and_wait(project_id, "again")
        self.assertNotIn("--resume", self.read_claude_calls()[-1]["argv"], "a new conversation starts a new session")

        for unknown in ("j-000000000000", "anything"):
            self.assertEqual(self.request("GET", "/p/%s/api/planning/job/%s" % (project_id, unknown))[0], 404)
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, body)
        self.wait_for_job(project_id, body["job"])
        self.assertEqual(self.request("GET", "/p/%s/api/planning/job/%s" % (project_id, body["job"]))[0], 404,
                         "another job type is not a planning reply")

    def test_validation_loop_running_and_claude_failure(self):
        repo, project_id = self.create_planning_project()
        for text in ("", "   ", None, "x" * 8001):
            status, body = self.send(project_id, text)
            self.assertEqual(status, 400, (text, body))
        self.write_live_loop_lock(repo)
        status, body = self.send(project_id, "plan")
        self.assertEqual((status, body["code"]), (409, "loop_running"), body)
        os.remove(loop_file_path(repo, "internal", "LOOP.lock"))

        os.environ[FAKE_FAIL_VARIABLE] = "1"
        job = self.send_and_wait(project_id, "plan")
        self.assertEqual(job["state"], "error")
        self.assertIn("simulated claude failure", job["error"])
        self.assertNotIn("reply", job)
        self.assertEqual([message["role"] for message in self.get_planning(project_id)["thread"]["messages"]], ["user"],
                         "a failed reply records no AI message")

    def test_unknown_project_is_404(self):
        for method, path in (("GET", "planning"), ("POST", "planning/send"), ("GET", "planning/job/j-000000000000"),
                             ("POST", "planning/reset")):
            status, body = self.request(method, "/p/missing/api/%s" % path, {"text": "x", "confirm": True} if method == "POST" else None)
            self.assertEqual((status, body), (404, {"ok": False, "error": "project not found"}), path)

    def test_send_requires_the_token(self):
        _, project_id = self.create_planning_project()
        self.assertEqual(self.request("POST", "/p/%s/api/planning/send" % project_id, {"text": "x"}, with_token=False)[0], 403)


class TestFlowsParser(unittest.TestCase):
    def test_sections_steps_and_comments(self):
        text = ("# Flows\n\n<!-- example:\n## [ghost] Not a flow\n1. ignored\n2. ignored\n-->\n\n"
                "## [customer-api] Register a customer\n1. POST /customers\n2. Expected: 201\n   3) Expected: still a step\n"
                "not a step\n10. Expected: 409\n\n## Notes\n1. after another heading: not a step\n\n##[bare]\n1. only one\n")
        flows = parse_flows(text)
        self.assertEqual(list(flows), ["customer-api", "bare"])
        self.assertEqual(flows["customer-api"]["title"], "Register a customer")
        self.assertEqual(flows["customer-api"]["steps"], ["1. POST /customers", "2. Expected: 201", "3) Expected: still a step",
                                                          "10. Expected: 409"])
        self.assertEqual((flows["bare"]["title"], len(flows["bare"]["steps"])), ("", 1))

    def test_coverage_missing_and_orphans(self):
        text = "## [a] A\n1. x\n2. y\n## [b] B\n1. only one\n## [old] Old\n1. x\n2. y\n"
        self.assertEqual(check_flow_coverage(["a", "b", "c"], text), {"missing": ["b", "c"], "orphans": ["old"]})
        self.assertEqual(check_flow_coverage([], ""), {"missing": [], "orphans": []})

    def test_template_has_no_flow(self):
        template = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "specs", "FLOWS.md")
        self.assertEqual(parse_flows(read_file(template)), {}, "the template example is inside a comment")


class TestPlanningGatesUnit(unittest.TestCase):
    def facts(self, **overrides):
        facts = {"is_git": True, "structure": {"ok": True, "missing": []}, "conformance": "LEGACY", "map_files": [], "has_code": False,
                 "project_text": "# P\n\n**Vision:** x\n", "roadmap_exists": True, "features": {"a": {"status": "pending"}},
                 "roadmap_parse_errors": [], "roadmap_field_errors": [], "open_questions": [], "flows_exists": True,
                 "flow_coverage": {"missing": [], "orphans": []}}
        facts.update(overrides)
        return facts

    def test_all_ok_and_each_failure(self):
        gates = build_planning_gates(self.facts())
        self.assertEqual([gate["id"] for gate in gates], GATE_IDS)
        self.assertTrue(are_gates_ok(gates) and are_prerequisites_ok(gates))
        cases = {"git": {"is_git": False}, "structure": {"structure": {"ok": False, "missing": ["project/"]}},
                 "conformance": {"conformance": None}, "map": {"has_code": True}, "goal": {"project_text": "**Vision:** FILL_IN"},
                 "features": {"features": {}}, "fields": {"roadmap_field_errors": ["[a] invalid kind"]},
                 "open_questions": {"open_questions": ["Which db?"]}, "flows": {"flow_coverage": {"missing": ["a"], "orphans": []}}}
        for gate_id, override in cases.items():
            gates = {gate["id"]: gate for gate in build_planning_gates(self.facts(**override))}
            self.assertFalse(gates[gate_id]["ok"], gate_id)
            self.assertFalse(are_gates_ok(list(gates.values())), gate_id)
            self.assertEqual(are_prerequisites_ok(list(gates.values())), gate_id not in ("git", "structure", "conformance", "map"),
                             gate_id)
        self.assertFalse(build_planning_gates(self.facts(project_text=None))[4]["ok"], "PROJECT.md missing")
        self.assertFalse(build_planning_gates(self.facts(flows_exists=False))[8]["ok"], "FLOWS.md missing")


if __name__ == "__main__":
    unittest.main()
