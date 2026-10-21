"""INS-11: the root map job (POST codebase/map) runs without `--max-budget-usd`; a budget in the body is ignored, never a 400.
Other job types keep their budget (covered by test_projects_roadmap). The conformance step of GET progress carries the script suggestion (INS-10)."""
import os
import unittest

from .support_projects import ProjectsTestCase, create_git_repo


class RootMapNoBudgetTests(ProjectsTestCase):
    def start_map(self, repo_name, body):
        repo = create_git_repo(os.path.join(self.projects_dir, repo_name), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        status, response = self.request("POST", "/p/%s/api/codebase/map" % project_id, body)
        self.assertEqual(status, 202, response)
        self.assertEqual(self.wait_for_job(project_id, response["job"])["state"], "ok")
        return repo, project_id, self.read_claude_calls()[-1]["argv"]

    def test_argv_has_the_model_and_no_budget(self):
        _, _, argv = self.start_map("plain", {})
        self.assertNotIn("--max-budget-usd", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")

    def test_budget_in_the_body_is_ignored(self):
        for index, value in enumerate((5, 0, -1, "abc", 9999, None)):
            _, _, argv = self.start_map("ignored%d" % index, {"budget_usd": value})
            self.assertNotIn("--max-budget-usd", argv, value)

    def test_conformance_step_carries_the_suggestion(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "suggest"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        progress = self.request("GET", "/p/%s/api/progress" % project_id)[1]
        step = [item for item in progress["steps"] if item["id"] == "conformance"][0]
        self.assertIn(step["suggestion"]["suggestion"], ("STANDARD", "LEGACY"))


if __name__ == "__main__":
    unittest.main()
