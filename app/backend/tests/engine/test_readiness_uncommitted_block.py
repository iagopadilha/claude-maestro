"""RD-01 / RD-02 of the real `--check --planning`: ONE uncommitted block per repo (setup files carried, other changes not) instead of the
three repeated warnings, and no "generate and APPROVE the roadmap first" problem while an installer step is still open."""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import readiness_checks  # noqa: E402
import setup_files  # noqa: E402
from test_check_planning import PlanningCheckCase, write  # noqa: E402
from test_loop import sh  # noqa: E402


class TestUncommittedBlock(PlanningCheckCase):
    def warnings_of(self, result):
        return [line for line in result.stdout.splitlines() if line.startswith("[check] WARNING:")]

    def test_one_block_per_repo_with_setup_files_and_other_changes(self):
        project = self.make_planning()
        self.servers("grafana")
        sh(["git", "add", "-A"], project.repo)
        sh(["git", "commit", "-q", "-m", "package"], project.repo)
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        write(os.path.join(project.repo, "package.json"), json.dumps({"scripts": {"test": "echo changed"}}))
        result = project.run("--check", "--planning")
        blocks = [line for line in self.warnings_of(result) if "uncommitted" in line.lower() or "not committed" in line]
        self.assertEqual(len(blocks), 1, blocks)
        self.assertIn("1 setup file not committed: they will be carried into the worktree (or commit them from Workspace): docs/codebase/STACK.md", blocks[0])
        self.assertIn("1 other changed file will NOT be carried: package.json", blocks[0])
        self.assertNotIn("These files are not committed", result.stdout)
        self.assertNotIn("born from the last commit", result.stdout)

    def test_a_clean_repo_has_no_block_and_a_reference_repo_never_has_one(self):
        project = self.make_planning()
        self.servers("grafana")
        reference = self.make_repo(project, "docs-repo", role="reference")
        self.save(project, reference)
        write(os.path.join(reference.path, "docs", "codebase", "STACK.md"), "map\n")
        sh(["git", "add", "-A"], project.repo)
        sh(["git", "commit", "-q", "-m", "package"], project.repo)
        result = project.run("--check", "--planning")
        self.assertFalse([line for line in self.warnings_of(result) if "not committed" in line])

    def test_blocks_are_structured_for_the_service(self):
        project = self.make_planning()
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        blocks = setup_files.uncommitted_blocks(self.repos_of(project), project.repo)
        self.assertEqual(blocks[0]["repo"], os.path.basename(project.repo))
        self.assertIn("docs/codebase/STACK.md", blocks[0]["setup_files"])
        self.assertEqual(readiness_checks.uncommitted_blocks(self.repos_of(project), project.repo), blocks)

    @staticmethod
    def repos_of(project):
        import project_repos
        return project_repos.load_repos(project.repo)

    def test_the_service_exposes_a_top_level_uncommitted_list(self):
        from types import SimpleNamespace
        from harness_backend.services.readiness_service import ReadinessService
        project = self.make_planning()
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        blocks = ReadinessService.read_uncommitted_blocks(SimpleNamespace(repo_path=project.repo))
        self.assertEqual(blocks[0]["kind"], "uncommitted")
        self.assertIn("docs/codebase/STACK.md", blocks[0]["setup_files"])
        self.assertEqual(blocks[0]["other_changes"], [])
        self.assertEqual(ReadinessService.read_cache.__name__, "read_cache")

    def test_no_generate_the_roadmap_problem_while_the_installer_is_open(self):
        project = self.make_planning()
        os.remove(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"))
        write(os.path.join(project.repo, "CLAUDE.md"), "no conformance yet\n")
        result = project.run("--check", "--planning")
        problems = [line for line in result.stdout.splitlines() if line.startswith("[check] PROBLEM:")]
        self.assertTrue(problems)
        self.assertTrue(problems[0].startswith("[check] PROBLEM: CLAUDE.md has no `Conformance"), problems)
        self.assertNotIn("APPROVE the roadmap", result.stdout)

    def test_unfinished_monorepo_subprojects_are_the_first_problem(self):
        project = self.make_planning()
        os.remove(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"))
        write(os.path.join(project.repo, "CLAUDE.md"), "Conformance: STANDARD\n")
        write(os.path.join(project.repo, ".specs", "project", "SUBPROJECTS.json"),
              json.dumps({"version": 1, "repos": {os.path.basename(project.repo): {"monorepo": True, "confirmed": True, "projects": [{"path": "backend"}]}}}))
        write(os.path.join(project.repo, "backend", "src", "a.py"), "x\n")
        result = project.run("--check", "--planning")
        problems = [line for line in result.stdout.splitlines() if line.startswith("[check] PROBLEM:")]
        self.assertIn("setup installer not finished", problems[0])
        self.assertNotIn("APPROVE the roadmap", result.stdout)

    def test_the_roadmap_problem_is_still_said_when_nothing_else_is_open(self):
        project = self.make_planning()
        os.remove(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"))
        write(os.path.join(project.repo, "CLAUDE.md"), "Conformance: STANDARD\n")
        result = project.run("--check", "--planning")
        self.assertIn("generate and APPROVE the roadmap first", result.stdout)


if __name__ == "__main__":
    unittest.main()
