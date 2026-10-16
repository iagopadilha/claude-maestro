"""Project checklist: mode and next action at each phase, initialize without overwriting, conformance, loop in the middle."""
import json
import os
import unittest

from .support_projects import (TWO_FEATURE_ROADMAP, ProjectsTestCase, create_git_repo, loop_file_path, read_file, run_git, write_file,
                               write_planning_documents)


def steps_by_id(progress):
    return {step["id"]: step for step in progress["steps"]}


class TestProgressByMode(ProjectsTestCase):
    def test_empty_folder_is_new(self):
        folder = os.path.join(self.projects_dir, "empty")
        os.makedirs(folder)
        progress = self.get_progress(self.open_project(folder))
        self.assertEqual((progress["mode"], progress["next_action"]), ("new", "structure"))
        steps = steps_by_id(progress)
        self.assertEqual([step["id"] for step in progress["steps"]],
                         ["folder", "git", "structure", "conformance", "map", "roadmap", "flows", "approval", "test_environment", "loop"])
        self.assertTrue(steps["folder"]["ok"])
        self.assertFalse(steps["git"]["ok"])
        self.assertTrue(steps["map"]["optional"], "a repo without code needs no map")
        self.assertEqual(progress["counts"], {"total": 0, "verified": 0, "blocked": 0})
        self.assertIsNone(progress["running_job"])
        self.assertFalse(progress["loop_worktree"])

    def test_path_from_planning_to_ready_for_loop(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "flow"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        self.assertEqual(self.get_progress(project_id)["next_action"], "structure")

        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertEqual((body["progress"]["mode"], body["progress"]["next_action"]), ("planning", "conformance"))
        self.assertIn("script suggestion", steps_by_id(body["progress"])["conformance"]["detail"])
        self.assertTrue(os.path.isfile(loop_file_path(repo, "json", "MONITOR.json")),
                        "with .specs/ created the MONITOR.json starts to exist")

        status, body = self.request("POST", "/p/%s/api/conformance" % project_id, {"value": "LEGACY"})
        self.assertEqual((status, body), (200, {"ok": True, "conformance": "LEGACY"}))
        self.assertIn("Conformance: LEGACY", read_file(os.path.join(repo, "CLAUDE.md")))
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "map", "repo with code and no map: map it before the roadmap")
        self.assertFalse(steps_by_id(progress)["map"]["optional"])

        write_file(os.path.join(repo, ".specs", "codebase", "STACK.md"), "# Stack\n")
        self.assertEqual(self.get_progress(project_id)["next_action"], "roadmap")

        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), "- [broken] status=invented\n")
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "roadmap")
        self.assertIn("outside the grammar", steps_by_id(progress)["roadmap"]["detail"])

        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"]), ("planning", "roadmap"), "PROJECT.md still has FILL_IN")
        self.assertEqual(progress["counts"]["total"], 2)
        self.assertIn("2 feature(s) in 1 stage(s)", steps_by_id(progress)["roadmap"]["detail"])
        self.assertFalse(steps_by_id(progress)["flows"]["ok"])
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})[0], 422)

        write_planning_documents(repo)
        write_file(os.path.join(repo, ".specs", "project", "FLOWS.md"), "# Flows\n\n## [api-base] API\n1. GET /x\n2. Expected: 200\n")
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "flows")
        self.assertIn("screen-base", steps_by_id(progress)["flows"]["detail"])

        write_planning_documents(repo)
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"]), ("planning", "approve"))
        self.assertTrue(steps_by_id(progress)["flows"]["ok"])

        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})[0], 200)
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"]), ("ready_for_loop", "check"))
        self.assertTrue(steps_by_id(progress)["test_environment"]["optional"])

    def test_regression_yes_requires_test_environment(self):
        repo, project_id = self.create_ready_project(approve=False)
        write_planning_documents(repo, TWO_FEATURE_ROADMAP +
                                 "- [refactor-screen] status=pending scope=Small kind=front paths=src/legacy regression=yes\n")
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})[0], 200)
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "test_environment")
        step = steps_by_id(progress)["test_environment"]
        self.assertFalse(step["optional"])
        self.assertIn("TEST-ENVIRONMENT.md", step["detail"])

        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        self.assertEqual(status, 200, body)
        path = os.path.join(repo, ".specs", "project", "TEST-ENVIRONMENT.md")
        self.assertIn(".specs/project/TEST-ENVIRONMENT.md", body["created"])
        self.assertIn("FILL_IN", read_file(path))
        self.assertIn("FILL_IN", steps_by_id(self.get_progress(project_id))["test_environment"]["detail"])

        write_file(path, "# Test environment\n\nEverything filled in.\n")
        self.assertEqual(self.get_progress(project_id)["next_action"], "check")

    def test_loop_done_and_reopened(self):
        repo, project_id = self.create_ready_project()
        roadmap = TWO_FEATURE_ROADMAP.replace("status=pending", "status=verified")
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), roadmap)
        write_file(loop_file_path(repo, "json", "LOOP.json"), json.dumps({"status": "done", "turn": 4, "at": "2026-10-15T10:00:00"}))
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"], progress["loop_status"]), ("done", "follow", "done"))
        self.assertEqual(progress["counts"], {"total": 2, "verified": 2, "blocked": 0})

        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), roadmap + "- [new-one] status=pending scope=Small kind=back paths=src/new\n")
        progress = self.get_progress(project_id)
        self.assertEqual(progress["next_action"], "flows", "a new feature invalidates the approval and needs its flow")
        self.assertTrue(steps_by_id(progress)["flows"]["optional"] is False)
        write_planning_documents(repo)
        self.assertEqual(self.get_progress(project_id)["next_action"], "approve", "same engine signature: approve again")
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % project_id, {"confirm": True})[0], 200)
        self.assertEqual(self.get_progress(project_id)["next_action"], "start_loop")

    def test_running_loop_is_in_loop_and_blocks_structure(self):
        repo, project_id = self.create_ready_project()
        self.write_live_loop_lock(repo)
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"], progress["loop_status"]), ("in_loop", "follow", "running"))
        self.assertEqual(self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})[0], 409)
        self.assertEqual(self.request("POST", "/p/%s/api/conformance" % project_id, {"value": "STANDARD"})[0], 409)

    def test_stopped_loop_with_error_in_meta_is_error(self):
        repo, project_id = self.create_ready_project()
        write_file(loop_file_path(repo, "json", "LOOP.json"), json.dumps({"status": "continue", "turn": 2, "at": "2026-10-15T10:00:00"}))
        self.assertEqual(self.get_progress(project_id)["loop_status"], "stopped")
        write_file(loop_file_path(repo, "json", "meta.json"), json.dumps(
            {"stopped": "error", "reason": "claude crashed", "finished_at": "2026-10-15T10:05:00"}))
        progress = self.get_progress(project_id)
        self.assertEqual((progress["loop_status"], progress["mode"]), ("error", "in_loop"))
        self.assertIn("claude crashed", steps_by_id(progress)["loop"]["detail"])


class TestProjectWithLoopInTheMiddle(ProjectsTestCase):
    def test_sibling_worktree_becomes_the_work_dir(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "middle"), {"src/app.py": "print(1)\n"})
        worktree = repo + "-worktree-loop"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/2026-10-15", worktree)
        specs_project = os.path.join(worktree, ".specs", "project")
        write_file(os.path.join(specs_project, "ROADMAP.md"), TWO_FEATURE_ROADMAP.replace(
            "- [api-base] status=pending", "- [api-base] status=verified"))
        write_file(os.path.join(worktree, "CLAUDE.md"), "# middle\n\nConformance: LEGACY\n")
        write_file(loop_file_path(worktree, "json", "LOOP.json"), json.dumps(
            {"status": "awaiting_user", "turn": 3, "reason": "Which database to use?", "at": "2026-10-15T10:00:00"}))
        from harness_backend.repositories.specs_file_repository import loop_state
        loop_state.approve_roadmap(worktree, "2026-10-15T09:00:00")

        project_id = self.open_project(repo)
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs")), "the original repo is not touched")
        monitor = json.loads(read_file(loop_file_path(worktree, "json", "MONITOR.json")))
        self.assertEqual((monitor["repo"], monitor["work"], monitor["pid"]), (repo, worktree, os.getpid()))
        self.assertEqual(monitor["url"], self.server.url + "p/%s/" % project_id)
        self.assertEqual(monitor["port"], self.server.port)

        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"]), ("in_loop", "follow"))
        self.assertEqual(progress["loop_status"], "awaiting_user")
        self.assertTrue(progress["loop_worktree"])
        self.assertEqual(progress["work_dir"], worktree)
        self.assertEqual(progress["counts"], {"total": 2, "verified": 1, "blocked": 0})
        self.assertTrue(steps_by_id(progress)["approval"]["ok"])
        self.assertIn("Which database to use?", steps_by_id(progress)["loop"]["detail"])

    def test_worktree_born_later_gets_the_monitor(self):
        repo, project_id = self.create_ready_project(name="later")
        worktree = repo + "-worktree-loop"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/x", worktree)
        os.makedirs(os.path.join(worktree, ".specs", "project"))
        self.get_progress(project_id)
        monitor = json.loads(read_file(loop_file_path(worktree, "json", "MONITOR.json")))
        self.assertEqual(monitor["work"], worktree)


class TestInitializeWithoutOverwriting(ProjectsTestCase):
    def test_existing_files_stay_untouched(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "existing"), {
            ".gitignore": "node_modules/\n", "CLAUDE.md": "# My project\n\nMy own rule.\n"})
        write_file(os.path.join(repo, ".specs", "project", "PROJECT.md"), "# My PROJECT written by hand\n")
        write_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"), TWO_FEATURE_ROADMAP)
        project_id = self.open_project(repo)

        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": "LEGACY"})
        self.assertEqual(status, 200, body)
        self.assertIn(".specs/project/PROJECT.md", body["existing"])
        self.assertIn(".specs/project/ROADMAP.md", body["existing"])
        self.assertIn(".specs/project/STATE.md", body["created"])
        self.assertEqual(body["conformance"], "LEGACY")
        self.assertEqual(read_file(os.path.join(repo, ".specs", "project", "PROJECT.md")), "# My PROJECT written by hand\n")
        self.assertEqual(read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md")), TWO_FEATURE_ROADMAP)
        self.assertEqual(read_file(os.path.join(repo, ".gitignore")), "node_modules/\n.specs/\n")
        claude_md = read_file(os.path.join(repo, "CLAUDE.md"))
        self.assertTrue(claude_md.startswith("# My project\n\nMy own rule.\n"))
        self.assertIn("@.specs/project/RULES.md", claude_md)
        self.assertIn("Conformance: LEGACY", claude_md)

        status, second = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})
        self.assertEqual(status, 200)
        self.assertEqual((second["created"], second["changed"]), ([], []), "the second time creates and changes nothing")
        self.assertEqual(read_file(os.path.join(repo, "CLAUDE.md")), claude_md)
        self.assertEqual(read_file(os.path.join(repo, ".gitignore")), "node_modules/\n.specs/\n")

    def test_new_project_templates_are_filled_in(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "templated"))
        project_id = self.open_project(repo)
        self.assertEqual(self.request("POST", "/p/%s/api/structure/initialize" % project_id, {})[0], 200)
        project_md = read_file(os.path.join(repo, ".specs", "project", "PROJECT.md"))
        state_md = read_file(os.path.join(repo, ".specs", "project", "STATE.md"))
        self.assertTrue(project_md.startswith("# templated\n"))
        self.assertNotIn("{project_name}", project_md + state_md)
        self.assertNotIn("{today}", state_md)
        for heading in ("## Checkpoint mode", "## Current stage", "## Open questions"):
            self.assertIn(heading, state_md)
        roadmap_md = read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"))
        self.assertIn("## Open questions", roadmap_md)
        from harness_backend.repositories.specs_file_repository import loop_state
        self.assertEqual(loop_state.parse_roadmap(roadmap_md), ({}, []), "the ROADMAP template carries no feature or stage")

    def test_invalid_conformance_is_400(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "conf"))
        project_id = self.open_project(repo)
        self.assertEqual(self.request("POST", "/p/%s/api/conformance" % project_id, {"value": "MAYBE"})[0], 400)
        self.assertEqual(self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": "X"})[0], 400)
        status, body = self.request("GET", "/p/missing/api/progress")
        self.assertEqual((status, body), (404, {"ok": False, "error": "project not found"}))


if __name__ == "__main__":
    unittest.main()
