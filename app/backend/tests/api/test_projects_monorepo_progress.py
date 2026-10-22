"""Checklist and planning gates of a monorepo: gate `subprojects`, Conformance and map per subproject, the `progress.monorepo` flag,
the root map skipped, an unreadable SUBPROJECTS.json and the subproject list in the planning status block. Real app, fake claude."""
import json
import os
import time

from .support_projects import JOB_WAIT_TIMEOUT_SECONDS, ProjectsTestCase, create_git_repo, read_file, write_file

MONOREPO_FILES = {
    "backend/App.sln": "",
    "backend/Api/Program.cs": "class P {}\n",
    "backend/Data/Repo.cs": "class R {}\n",
    "frontend/package.json": json.dumps({"dependencies": {"react": "18"}}),
    "frontend/src/main.tsx": "export {}\n",
}
SINGLE_FILES = {"src/app.py": "print(1)\n"}
SINGLE_GATE_IDS = ["git", "structure", "conformance", "map", "goal", "features", "fields", "open_questions", "flows", "test_environment", "ready"]
MONOREPO_GATE_IDS = ["git", "structure", "subprojects", "conformance", "map", "goal", "features", "fields", "open_questions", "flows",
                     "test_environment", "ready"]
SUBPROJECTS_FILE_PARTS = (".specs", "project", "SUBPROJECTS.json")


def gates_by_id(gates):
    return {gate["id"]: gate for gate in gates}


class TestMonorepoProgress(ProjectsTestCase):
    def open_scratch(self, name, files):
        repo = create_git_repo(os.path.join(self.projects_dir, name), files)
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        self.assertEqual(status, 200, body)
        return repo, project_id

    def get_planning_gates(self, project_id):
        status, body = self.request("GET", "/p/%s/api/planning" % project_id)
        self.assertEqual(status, 200, body)
        return body["gates"]

    def confirm_list(self, project_id):
        context = self.module.project_service.get_project_context(project_id)
        return self.module.subprojects_service.update_list(context, {"confirmed": True})

    def set_every_conformance(self, project_id):
        context = self.module.project_service.get_project_context(project_id)
        return self.module.subprojects_service.set_conformance_all(context)

    def write_maps(self, repo, paths):
        for path in paths:
            write_file(os.path.join(repo, path, "docs", "codebase", "STACK.md"), "# Stack\n")

    def test_unconfirmed_monorepo_shows_the_subprojects_gate_and_blocks_the_rest(self):
        _, project_id = self.open_scratch("mono", MONOREPO_FILES)
        gates = self.get_planning_gates(project_id)
        self.assertEqual([gate["id"] for gate in gates], MONOREPO_GATE_IDS)
        by_id = gates_by_id(gates)
        self.assertEqual((by_id["subprojects"]["ok"], by_id["subprojects"]["label"]), (False, "Subprojects"))
        self.assertTrue(any(item.startswith("backend") for item in by_id["subprojects"]["items"]), by_id["subprojects"])
        self.assertTrue(any(item.startswith("frontend") for item in by_id["subprojects"]["items"]), by_id["subprojects"])
        self.assertFalse(by_id["conformance"]["ok"])
        self.assertIn("confirm the list", by_id["conformance"]["detail"])
        self.assertFalse(by_id["map"]["ok"])
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "subprojects")
        self.assertNotIn("monorepo", progress, "the flag is only true for a confirmed monorepo")
        self.assertEqual([step["id"] for step in progress["steps"]][:5], ["folder", "git", "structure", "subprojects", "conformance"])

    def test_confirmed_monorepo_gates_pass_with_conformance_and_maps_per_subproject(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        self.confirm_list(project_id)
        by_id = gates_by_id(self.get_planning_gates(project_id))
        self.assertTrue(by_id["subprojects"]["ok"])
        self.assertEqual(by_id["conformance"]["label"], "Conformance per subproject")
        self.assertEqual(by_id["map"]["label"], "Codebase map per subproject")
        self.assertEqual((by_id["conformance"]["ok"], by_id["map"]["ok"]), (False, False))
        self.assertEqual(len(by_id["conformance"]["items"]), 2, "one line per subproject")
        self.assertEqual(self.get_progress(project_id)["next_action"], "conformance")

        self.set_every_conformance(project_id)
        by_id = gates_by_id(self.get_planning_gates(project_id))
        self.assertTrue(by_id["conformance"]["ok"], by_id["conformance"])
        self.assertFalse(by_id["map"]["ok"])
        self.assertEqual(self.get_progress(project_id)["next_action"], "map")

        self.write_maps(repo, ["backend"])
        self.assertFalse(gates_by_id(self.get_planning_gates(project_id))["map"]["ok"], "frontend still has no map")
        self.write_maps(repo, ["frontend"])
        by_id = gates_by_id(self.get_planning_gates(project_id))
        self.assertFalse(by_id["map"]["ok"], "two mapped subprojects: the system map is due")
        self.assertIn("system map", by_id["map"]["detail"])
        write_file(os.path.join(repo, "docs", "codebase", "SYSTEM.md"), "# system"+chr(10))
        by_id = gates_by_id(self.get_planning_gates(project_id))
        self.assertTrue(by_id["map"]["ok"], by_id["map"])
        progress = self.get_progress(project_id)
        self.assertIs(progress["monorepo"], True)
        self.assertEqual(sorted(item["path"] for item in progress["subprojects"]), ["backend", "frontend"])
        steps = {step["id"]: step for step in progress["steps"]}
        self.assertTrue(steps["subprojects"]["ok"] and steps["conformance"]["ok"] and steps["map"]["ok"])
        self.assertEqual(steps["conformance"]["title"], "Conformance per subproject")
        self.assertEqual(progress["next_action"], "roadmap")

    def test_root_docs_codebase_with_only_system_md_is_not_the_map(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        self.confirm_list(project_id)
        self.set_every_conformance(project_id)
        write_file(os.path.join(repo, "docs", "codebase", "SYSTEM.md"), "# System\n")
        self.write_maps(repo, ["backend"])
        by_id = gates_by_id(self.get_planning_gates(project_id))
        self.assertFalse(by_id["map"]["ok"], "the root SYSTEM.md does not stand in for the frontend map")
        self.write_maps(repo, ["frontend"])
        self.assertTrue(gates_by_id(self.get_planning_gates(project_id))["map"]["ok"])

    def test_not_a_monorepo_keeps_todays_gates(self):
        _, project_id = self.open_scratch("mono", MONOREPO_FILES)
        context = self.module.project_service.get_project_context(project_id)
        self.module.subprojects_service.update_list(context, {"monorepo": False})
        self.assertEqual([gate["id"] for gate in self.get_planning_gates(project_id)], SINGLE_GATE_IDS)
        self.assertNotIn("monorepo", self.get_progress(project_id))

    def test_single_project_is_unchanged(self):
        _, project_id = self.open_scratch("single", SINGLE_FILES)
        self.assertEqual([gate["id"] for gate in self.get_planning_gates(project_id)], SINGLE_GATE_IDS)
        progress = self.get_progress(project_id)
        self.assertEqual([step["id"] for step in progress["steps"]],
                         ["folder", "git", "structure", "conformance", "map", "roadmap", "flows", "approval", "test_environment", "loop"])
        self.assertNotIn("monorepo", progress)
        self.assertNotIn("subprojects", progress)
        self.assertEqual(progress["next_action"], "conformance")

    def test_unreadable_subprojects_file_is_a_single_project_and_the_gate_fails(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        write_file(os.path.join(repo, *SUBPROJECTS_FILE_PARTS), "{ not json")
        gates = self.get_planning_gates(project_id)
        self.assertEqual([gate["id"] for gate in gates], ["git", "structure", "subprojects", "conformance", "map"] + MONOREPO_GATE_IDS[5:])
        by_id = gates_by_id(gates)
        self.assertEqual((by_id["subprojects"]["ok"], by_id["subprojects"]["detail"]), (False, "SUBPROJECTS.json unreadable"))
        self.assertEqual(by_id["conformance"]["label"], "Conformance in CLAUDE.md", "treated as a single project")
        self.assertNotIn("monorepo", self.get_progress(project_id))

    def test_status_block_lists_the_subprojects_only_for_a_monorepo(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        self.confirm_list(project_id)
        self.set_every_conformance(project_id)
        self.write_maps(repo, ["backend", "frontend"])
        write_file(os.path.join(repo, "docs", "codebase", "SYSTEM.md"), "# system"+chr(10))
        status, body = self.request("POST", "/p/%s/api/planning/send" % project_id, {"text": "plan it"})
        self.assertEqual(status, 202, body)
        self.wait_for_planning_job(project_id, body["job_id"])
        stdin = self.read_claude_calls()[-1]["stdin"]
        self.assertIn("[Subprojects of this monorepo]", stdin)
        self.assertIn("- backend (stack dotnet): Conformance ", stdin)
        self.assertIn("- frontend (stack react): Conformance ", stdin)
        self.assertIn("map: 1 document(s)", stdin)
        self.assertIn("`paths=` with the subproject folder as the prefix", stdin)

        single_repo, single_id = self.open_scratch("single", SINGLE_FILES)
        write_file(os.path.join(single_repo, "CLAUDE.md"), "x\n")
        self.request("POST", "/p/%s/api/structure/conformance" % single_id, {"value": "LEGACY"})
        write_file(os.path.join(single_repo, "docs", "codebase", "STACK.md"), "# Stack\n")
        status, body = self.request("POST", "/p/%s/api/planning/send" % single_id, {"text": "plan it"})
        if status == 202:
            self.wait_for_planning_job(single_id, body["job_id"])
            self.assertNotIn("Subprojects of this monorepo", self.read_claude_calls()[-1]["stdin"])

    def wait_for_planning_job(self, project_id, job_id):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status, job = self.request("GET", "/p/%s/api/planning/job/%s" % (project_id, job_id))
            self.assertEqual(status, 200, job)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("planning job %s did not finish" % job_id)
