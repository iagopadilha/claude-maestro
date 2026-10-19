"""Roadmap: read, approval (accepted by the engine --check) and jobs of the FAKE claude."""
import json
import os
import time
import unittest

from .support_projects import (FAKE_DELAY_VARIABLE, FAKE_FAIL_VARIABLE, FAKE_MAP_MODE_VARIABLE, FAKE_SESSION, TWO_FEATURE_ROADMAP, ProjectsTestCase,
                               create_git_repo, loop_file_path, mark_loop_already_ran, read_file, run_git, write_file,
                               write_planning_documents)

EXPECTED_ROADMAP_PROMPT = "/orchestrator roadmap --headless --goal-file .specs/project/loop/internal/roadmap-goal.md"
ENGINE_NOT_APPROVED_MESSAGE = "roadmap not approved"


class TestReadAndApproval(ProjectsTestCase):
    def test_get_structured_roadmap(self):
        repo, project_id = self.create_ready_project(approve=False)
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP + "\n## Open questions\n\n- Which database?\n- none\n")
        status, roadmap = self.request("GET", "/p/%s/api/roadmap" % project_id)
        self.assertEqual(status, 200, roadmap)
        self.assertFalse(roadmap["approved"])
        self.assertIsNone(roadmap["approved_at"])
        self.assertEqual(roadmap["not_approved_reason"], "roadmap not approved in the panel yet")
        self.assertEqual(roadmap["stages"], [{"n": "1", "title": "Base", "features": ["api-base", "screen-base"]}])
        self.assertEqual([feature["slug"] for feature in roadmap["features"]], ["api-base", "screen-base"])
        self.assertEqual(roadmap["features"][1]["deps"], "api-base")
        self.assertEqual(roadmap["features"][0]["stage"], "1")
        self.assertEqual(roadmap["open_questions"], ["Which database?"])
        self.assertEqual(roadmap["errors"], [])
        self.assertIn("[api-base]", roadmap["text"])

    def test_approve_requires_confirm_and_token(self):
        repo, project_id = self.create_ready_project(approve=False)
        for body in ({}, {"confirm": "true"}, {"confirm": 1}):
            status, response = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, body)
            self.assertEqual((status, response), (400, {"ok": False, "error": "confirm must be true"}))
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True}, with_token=False)[0], 403)
        self.assertFalse(os.path.exists(loop_file_path(repo, "json", "ROADMAP-APPROVED.json")))

    def test_approve_writes_the_signature_the_engine_check_accepts(self):
        repo, project_id = self.create_ready_project(approve=False)
        self.assertIn(ENGINE_NOT_APPROVED_MESSAGE, self.run_engine_check(repo))
        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual((body["approval"]["approved"], body["approval"]["features"]), (True, 2))
        self.assertEqual(set(body["approval"]), {"approved", "reason", "at", "features"})
        approval = json.loads(read_file(loop_file_path(repo, "json", "ROADMAP-APPROVED.json")))
        self.assertEqual(set(approval), {"signature", "features", "at"})
        output = self.run_engine_check(repo)
        self.assertIn("[check] ROADMAP: 2 features", output)
        self.assertNotIn(ENGINE_NOT_APPROVED_MESSAGE, output)
        self.assertTrue(self.request("GET", "/p/%s/api/roadmap" % project_id)[1]["approved"])

        roadmap = read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"))
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), roadmap.replace("status=pending scope=Medium", "status=specced scope=Medium"))
        self.assertTrue(self.request("GET", "/p/%s/api/roadmap" % project_id)[1]["approved"], "a status change during the loop does not invalidate it")
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), roadmap + "- [new-one] status=pending\n")
        response = self.request("GET", "/p/%s/api/roadmap" % project_id)[1]
        self.assertFalse(response["approved"])
        self.assertIn("changed after the approval", response["not_approved_reason"])

    def test_approve_refuses_invalid_roadmap_and_running_loop(self):
        repo, project_id = self.create_ready_project(approve=False)
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), "# Roadmap\n\nno features\n")
        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual((status, body["code"]), (422, "planning_gates"), body)
        self.assertIn("Roadmap features", body["error"])
        self.assertFalse(next(gate for gate in body["gates"] if gate["id"] == "features")["ok"])
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        self.write_live_loop_lock(repo)
        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})
        self.assertEqual((status, body), (409, {"ok": False, "error": "loop running: the roadmap was already approved"}))

    def test_approve_with_worktree_writes_in_the_worktree(self):
        repo, project_id = self.create_ready_project(approve=False)
        worktree = repo + "-worktree-loop"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/x", worktree)
        write_planning_documents(worktree, TWO_FEATURE_ROADMAP)
        mark_loop_already_ran(worktree)  # the work dir is the worktree only once a loop ran there
        write_file(os.path.join(worktree, "CLAUDE.md"), "# ready\n\nConformance: LEGACY\n")
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})[0], 200)
        self.assertTrue(os.path.isfile(loop_file_path(worktree, "json", "ROADMAP-APPROVED.json")))

    def test_unknown_project_is_404(self):
        for method, path in (("GET", "roadmap"), ("POST", "roadmap/approve"), ("POST", "roadmap/generate"), ("GET", "jobs")):
            status, body = self.request(method, "/p/missing/api/%s" % path, {"confirm": True, "goal": "x"} if method == "POST" else None)
            self.assertEqual((status, body), (404, {"ok": False, "error": "project not found"}), path)


class TestClaudeJobs(ProjectsTestCase):
    def test_generate_roadmap_with_fake_claude_does_not_approve_on_its_own(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "generate"))
        project_id = self.open_project(repo)
        self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": "LEGACY"})
        mark_loop_already_ran(repo)  # out of planning mode: the one-shot jobs are only for projects that already looped
        status, body = self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "A to-do app.", "map_first": False})
        self.assertEqual(status, 202, body)
        self.assertEqual((body["ok"], body["detail"]["id"], body["detail"]["type"]), (True, body["job"], "roadmap"))
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual((job["state"], job["type"], job["reply"], job["cost_usd"]), ("ok", "roadmap", "roadmap generated", 0.42))
        self.assertEqual(set(job), {"id", "type", "state", "started_at", "finished_at", "cost_usd", "reply", "error"})
        self.assertNotIn("session_id", json.dumps(self.request("GET", "/p/%s/api/jobs" % project_id)[1]))

        call = self.read_claude_calls()[-1]
        argv = call["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], EXPECTED_ROADMAP_PROMPT)
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "auto")
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 5.0)
        self.assertNotIn("--strict-mcp-config", argv, "the roadmap runs with the normal MCP")
        self.assertEqual(os.path.normcase(call["cwd"]), os.path.normcase(repo), "the job runs in the repo folder")
        goal = read_file(loop_file_path(repo, "internal", "roadmap-goal.md"))
        self.assertTrue(goal.startswith("# Roadmap goal\n"))
        self.assertIn("A to-do app.", goal)
        self.assertIn("Map first: no", goal)
        self.assertIn("Written by the harness app on", goal)
        stored = json.loads(read_file(loop_file_path(repo, "internal", "jobs", body["job"] + ".json")))
        self.assertEqual(stored["session_id"], FAKE_SESSION)

        progress = self.get_progress(project_id)
        self.assertEqual((progress["next_action"], progress["counts"]["total"]), ("roadmap", 2), "an open question is pending")
        self.assertTrue(read_file(os.path.join(repo, ".specs", "project", "FLOWS.md")).startswith("# Flows"))
        self.assertFalse(os.path.exists(loop_file_path(repo, "json", "ROADMAP-APPROVED.json")))
        self.assertEqual(self.request("GET", "/p/%s/api/roadmap" % project_id)[1]["open_questions"], ["Which database to use?"])

        status, body = self.request("POST", "/p/%s/api/roadmap/refine" % project_id, {"text": "Add an extra feature."})
        self.assertEqual(status, 202, body)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual((job["state"], job["type"]), ("ok", "roadmap_refine"))
        call = self.read_claude_calls()[-1]
        self.assertEqual(call["argv"][call["argv"].index("--resume") + 1], FAKE_SESSION)
        self.assertIn("Add an extra feature.", call["stdin"])
        self.assertIn("never approve", call["stdin"])
        self.assertIn("[extra]", read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md")))
        _, listing = self.request("GET", "/p/%s/api/jobs" % project_id)
        self.assertEqual([item["type"] for item in listing["jobs"]][:2], ["roadmap_refine", "roadmap"])

    def test_map_first_and_budget_go_into_the_command(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "budget"))
        project_id = self.open_project(repo)
        mark_loop_already_ran(repo)
        status, body = self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "x", "map_first": True, "budget_usd": 2})
        self.assertEqual(status, 202, body)
        self.wait_for_job(project_id, body["job"])
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], EXPECTED_ROADMAP_PROMPT + " --map-first")
        self.assertEqual(float(argv[argv.index("--max-budget-usd") + 1]), 2.0)
        self.assertIn("Map first: yes", read_file(loop_file_path(repo, "internal", "roadmap-goal.md")))
        for invalid_body in ({"goal": ""}, {"goal": "x", "budget_usd": 0}, {"goal": "x", "budget_usd": "5"},
                             {"goal": "x" * 20001}):
            self.assertEqual(self.request("POST", "/p/%s/api/roadmap/generate" % project_id, invalid_body)[0], 400, invalid_body)

    def test_map_codebase(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "map"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_job(project_id, body["job"])["type"], "map")
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/orchestrator map")
        self.assertIn("--strict-mcp-config", argv, "mapping runs without MCP")
        hook_command = json.loads(argv[argv.index("--settings") + 1])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertTrue(hook_command.endswith('guard.py" --profile spec'), "the map agents write only inside .specs/ and docs/codebase/ (PreToolUse hook)")
        self.assertTrue(os.path.isfile(os.path.join(repo, "docs", "codebase", "STACK.md")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs", "codebase", "STACK.md")), "the map is no longer under .specs/")
        claude_md = read_file(os.path.join(repo, "CLAUDE.md"))
        self.assertIn("<!-- CODEBASE:BEGIN -->", claude_md, "CLAUDE.md points at the new map")
        self.assertIn("`docs/codebase/STACK.md`: languages, frameworks", claude_md)
        self.assertIn("the code wins", claude_md)
        index = read_file(os.path.join(repo, "docs", "codebase", "README.md"))
        self.assertIn("[STACK.md](STACK.md)", index, "the app writes the index of the map")
        self.assertRegex(index, r"Mapped at commit [0-9a-f]{7,} on \d{4}-\d{2}-\d{2}")
        sha = index.split("Mapped at commit ")[1].split()[0]
        gate = next(g for g in self.request("GET", "/p/%s/api/planning" % project_id)[1]["gates"] if g["id"] == "map")
        self.assertTrue(gate["ok"])
        self.assertIn("mapped at " + sha, gate["detail"], "the checklist shows the staleness marker")
        self.assertIn("docs/codebase", gate["detail"])

    def test_map_that_wrote_nothing_is_an_error_not_a_success(self):
        os.environ[FAKE_MAP_MODE_VARIABLE] = "nothing"
        repo = create_git_repo(os.path.join(self.projects_dir, "map-nothing"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, body)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "error")
        self.assertIn("wrote no document in docs/codebase", job["error"])

    def test_map_job_changes_outside_specs_are_undone(self):
        os.environ[FAKE_MAP_MODE_VARIABLE] = "touch-code"
        repo = create_git_repo(os.path.join(self.projects_dir, "map-touch"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, body)
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "ok")
        self.assertIn("src/app.py", job["reply"], "the reply tells what was undone")
        self.assertEqual(read_file(os.path.join(repo, "src", "app.py")), "print(1)\n")
        self.assertTrue(os.path.isfile(os.path.join(repo, "docs", "codebase", "STACK.md")), "the map itself stays")

    def test_refine_without_generated_roadmap_is_409(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "no-session"))
        project_id = self.open_project(repo)
        mark_loop_already_ran(repo)
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/refine" % project_id, {"text": "more"})[0], 409)
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/refine" % project_id, {"text": ""})[0], 400)

    def test_one_job_at_a_time_and_busy(self):
        os.environ[FAKE_DELAY_VARIABLE] = "1.5"
        repo = create_git_repo(os.path.join(self.projects_dir, "busy"))
        project_id = self.open_project(repo)
        mark_loop_already_ran(repo)
        status, body = self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "first"})
        self.assertEqual(status, 202)
        self.assertTrue(self.module.busy_providers[0].is_busy())
        running_job = self.get_progress(project_id)["running_job"]
        self.assertEqual((running_job["id"], running_job["type"]), (body["job"], "roadmap"))
        self.assertTrue(running_job["started_at"])
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "second"})[0], 409)
        self.assertEqual(self.request("POST", "/p/%s/api/codebase/map" % project_id, {})[0], 409)
        self.assertIn("first", read_file(loop_file_path(repo, "internal", "roadmap-goal.md")))
        self.wait_for_job(project_id, body["job"])
        deadline = time.time() + 5
        while self.module.busy_providers[0].is_busy() and time.time() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.module.busy_providers[0].is_busy())

    def test_job_with_claude_error(self):
        os.environ[FAKE_FAIL_VARIABLE] = "1"
        repo = create_git_repo(os.path.join(self.projects_dir, "failure"))
        project_id = self.open_project(repo)
        mark_loop_already_ran(repo)
        _, body = self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "x"})
        job = self.wait_for_job(project_id, body["job"])
        self.assertEqual(job["state"], "error")
        self.assertIn("simulated claude failure", job["error"])

    def test_refuses_job_with_worktree_or_running_loop_and_unknown_job(self):
        repo, project_id = self.create_ready_project()
        self.write_live_loop_lock(repo)
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "x"})[0], 409)
        os.remove(loop_file_path(repo, "internal", "LOOP.lock"))
        run_git(repo, "worktree", "add", "-q", "-b", "loop/y", repo + "-worktree-loop")
        mark_loop_already_ran(repo + "-worktree-loop")
        status, body = self.request("POST", "/p/%s/api/roadmap/generate" % project_id, {"goal": "x"})
        self.assertEqual(status, 409, body)
        self.assertIn("worktree", body["error"])
        self.assertEqual(self.read_claude_calls(), [])
        for job_id in ("j-000000000000", "..%2F..%2Fsecret", "anything"):
            self.assertEqual(self.request("GET", "/p/%s/api/jobs/%s" % (project_id, job_id))[0], 404, job_id)
        self.assertEqual(self.request("GET", "/p/%s/api/jobs/j-000000000000" % project_id)[1]["error"], "job not found")

    def test_one_shot_jobs_refused_in_planning_mode(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "planning"))
        project_id = self.open_project(repo)
        for path, body in (("roadmap/generate", {"goal": "x"}), ("roadmap/refine", {"text": "more"})):
            status, response = self.request("POST", "/p/%s/api/%s" % (project_id, path), body)
            self.assertEqual((status, response["code"]), (409, "planning_required"), path)
        status, body = self.request("POST", "/p/%s/api/codebase/map" % project_id, {})
        self.assertEqual(status, 202, "mapping stays open")
        self.wait_for_job(project_id, body["job"])
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("-p") + 1], "/orchestrator map")

    def test_interrupted_job_shows_as_error(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "interrupted"))
        project_id = self.open_project(repo)
        write_file(loop_file_path(repo, "internal", "jobs", "j-aaaaaaaaaaaa.json"), json.dumps(
            {"id": "j-aaaaaaaaaaaa", "type": "roadmap", "state": "running", "started_at": "2026-10-15T10:00:00", "session_id": "s"}))
        status, job = self.request("GET", "/p/%s/api/jobs/j-aaaaaaaaaaaa" % project_id)
        self.assertEqual((status, job["state"]), (200, "error"))
        self.assertIn("interrupted", job["error"])
        self.assertIsNone(self.get_progress(project_id)["running_job"])


if __name__ == "__main__":
    unittest.main()
