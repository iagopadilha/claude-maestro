"""Reset specs keeps REPOS.json and last-run.json (the repo list and how to re-attach after Close loop); the planning
`open_questions` payload ({id, text, recommended}) built from the ROADMAP `## Open questions` section."""
import json
import os
import unittest

from harness_backend.services.planning_service import describe_open_questions

from .support_projects import ProjectsTestCase, loop_file_path, read_file, write_file


class TestResetKeepsRepoFiles(ProjectsTestCase):
    def test_repos_json_and_last_run_survive_and_are_not_moved(self):
        repo, project_id = self.create_ready_project(name="kept")
        repos_path = os.path.join(repo, ".specs", "project", "REPOS.json")
        last_run_path = loop_file_path(repo, "json", "last-run.json")
        write_file(repos_path, json.dumps({"multi": True, "repos": [{"name": "kept", "path": repo, "primary": True}]}))
        write_file(last_run_path, json.dumps({"closed_at": "2026-10-19T10:00:00", "repos": {}}))
        write_file(loop_file_path(repo, "json", "LOOP.json"), "{}")
        status, body = self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {"confirm": "kept"})
        self.assertEqual(status, 200, body)
        self.assertNotIn(".specs/project/REPOS.json", body["moved"])
        self.assertNotIn(".specs/project/loop/json/last-run.json", body["moved"])
        self.assertIn(".specs/project/loop/json/LOOP.json", body["moved"])  # the rest of the loop state does go to the trash
        self.assertTrue(json.loads(read_file(repos_path))["multi"])
        self.assertIn("closed_at", read_file(last_run_path))


class TestOpenQuestionsPayload(unittest.TestCase):
    def test_questions_with_and_without_a_recommended_answer(self):
        roadmap = ("# Roadmap\n\n## Stage 1\n\n- [a] status=pending\n\n## Open questions\n\n"
                   "- Which database? | recommended: SQLite\n- Who approves the release?\n* Cache it | Recommended : yes, 5 minutes\n")
        self.assertEqual(describe_open_questions(roadmap), [
            {"id": "q1", "text": "Which database?", "recommended": "SQLite"},
            {"id": "q2", "text": "Who approves the release?", "recommended": ""},
            {"id": "q3", "text": "Cache it", "recommended": "yes, 5 minutes"}])

    def test_no_questions(self):
        for text in (None, "", "# Roadmap\n", "## Open questions\n\n- none\n", "## Open questions\n\n<!-- a comment -->\n"):
            with self.subTest(text=text):
                self.assertEqual(describe_open_questions(text), [])


if __name__ == "__main__":
    unittest.main()
