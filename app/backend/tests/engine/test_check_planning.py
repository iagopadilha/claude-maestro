"""`run_loop.py <repo> --check --planning`: the readiness of a roadmap that is NOT approved yet (C2). Real script, temporary git repos,
fake claude, a fake `~/.claude.json` (LOOP_CLAUDE_CONFIG_FILE). No tokens, no network."""
import json
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import project_repos  # noqa: E402
import run_loop  # noqa: E402
from test_loop import LoopCase, sh  # noqa: E402

ROAD_ONE = "# Roadmap\n## Stage 1 - Base\n- [api] status=pending paths=src\n"
SCENARIO = {"features": {}}
UNLIKELY_NAME = "ZZ_PLANNING_CHECK_TOKEN_9431"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class PlanningCheckCase(LoopCase):
    def make_planning(self, roadmap=ROAD_ONE):
        project = self.make(roadmap, SCENARIO, approve=False)
        self.config_path = os.path.join(project.tmp, "claude.json")
        project.extra_env["LOOP_CLAUDE_CONFIG_FILE"] = self.config_path
        write(os.path.join(project.repo, "package.json"), json.dumps({"scripts": {"test": "echo ok"}}))
        write(os.path.join(project.repo, ".gitignore"), "node_modules\n")
        return project

    def primary_ref(self, project):
        return project_repos.RepoRef(name=project_repos.short_name_of(project.repo), path=project.repo, primary=True)

    def save(self, project, *others):
        project_repos.save_repos(project.repo, True, [self.primary_ref(project)] + list(others))

    def make_repo(self, project, name, role="edit", git_repo=True):
        path = os.path.join(project.tmp, name)
        os.makedirs(path)
        if git_repo:
            sh(["git", "init", "-q", "-b", "main"], path)
            for key, value in (("user.name", "tester"), ("user.email", "t@t.t"), ("commit.gpgsign", "false")):
                sh(["git", "config", key, value], path)
            write(os.path.join(path, "README.md"), "x\n")
            write(os.path.join(path, "CLAUDE.md"), "Conformance: STANDARD\n")
            write(os.path.join(path, "package.json"), json.dumps({"scripts": {"test": "echo ok"}}))
            sh(["git", "add", "-A"], path)
            sh(["git", "commit", "-q", "-m", "base"], path)
        return project_repos.RepoRef(name=name, path=path, role=role)

    def servers(self, *names):
        write(self.config_path, json.dumps({"mcpServers": {name: {"type": "http", "url": "https://%s.example/mcp" % name} for name in names}}))


class TestPlanningCheck(PlanningCheckCase):
    def test_unapproved_roadmap_is_ready_with_planning_and_a_problem_without(self):
        project = self.make_planning()
        self.servers("grafana")
        plain = project.run("--check")
        self.done(plain, 1)
        self.assertIn("[check] PROBLEM: roadmap not approved", plain.stdout)
        planning = project.run("--check", "--planning")
        self.done(planning, 0)
        self.assertNotIn("roadmap not approved", planning.stdout)
        self.assertIn("[check] READY", planning.stdout)
        self.assertFalse(os.path.exists(project.work), "--check creates nothing")

    def test_planning_keeps_the_other_checks(self):
        project = self.make_planning(roadmap="# Roadmap\n")
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("[check] PROBLEM: ROADMAP.md has no feature line", result.stdout)

    def test_a_path_in_a_reference_repo_is_a_problem_that_says_promote_it(self):
        project = self.make_planning(roadmap="# Roadmap\n## Stage 1 - Base\n- [api] status=pending paths=proj:src,docs:guide\n")
        self.servers("grafana")
        self.save(project, self.make_repo(project, "docs", role="reference"))
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("feature api: path `docs:guide` is in the reference repo `docs` (read only): promote it to edit or change the feature", result.stdout)

    def test_a_path_in_an_unlisted_repo_is_a_problem(self):
        project = self.make_planning(roadmap="# Roadmap\n## Stage 1 - Base\n- [api] status=pending paths=ghost:src\n")
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("path `ghost:src` is in repo `ghost`, which is not listed in the project", result.stdout)

    def test_an_edit_repo_a_feature_changes_must_be_a_git_repo_but_an_untouched_one_is_not_checked(self):
        project = self.make_planning(roadmap="# Roadmap\n## Stage 1 - Base\n- [api] status=pending paths=src,broken:src\n")
        self.servers("grafana")
        broken = self.make_repo(project, "broken", git_repo=False)
        idle = self.make_repo(project, "idle", git_repo=False)
        self.save(project, broken, idle)
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("repo broken: %s is not a git repository" % broken.path, result.stdout)
        self.assertNotIn("repo idle", result.stdout)

    def test_a_loop_branch_name_that_already_exists_is_a_problem(self):
        project = self.make_planning()
        self.servers("grafana")
        sh(["git", "branch", "loop/taken"], project.repo)
        result = project.run("--check", "--planning", "--branch", "loop/taken")
        self.done(result, 1)
        self.assertIn("branch `loop/taken` already exists", result.stdout)

    def test_a_server_named_by_the_test_environment_must_be_configured(self):
        project = self.make_planning()
        write(os.path.join(project.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), "Reads the database through mcp-mysql-dev (read only).\n")
        self.servers("grafana")
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("MCP server(s) named by TEST-ENVIRONMENT.md or a feature are not configured", result.stdout)
        self.assertIn("mcp-mysql-dev", result.stdout)
        self.servers("grafana", "mcp-mysql-dev")
        self.done(project.run("--check", "--planning"), 0)

    def test_a_server_named_by_the_roadmap_counts_too(self):
        project = self.make_planning()
        write(os.path.join(project.repo, ".specs", "project", "ROADMAP.md"), "# Roadmap\n## Stage 1 - Base\n<!-- needs grafana logs -->\n- [api] status=pending paths=src\n")
        self.servers("mcp-mysql-dev")
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("not configured", result.stdout)

    def test_unreadable_claude_config_is_only_a_warning_when_nothing_needs_a_server(self):
        project = self.make_planning()
        result = project.run("--check", "--planning")  # no fake ~/.claude.json written
        self.done(result, 0)
        self.assertIn("[check] WARNING: %s could not be read" % self.config_path, result.stdout)

    def test_credentials_named_by_the_test_environment_resolve_through_the_layers(self):
        project = self.make_planning()
        self.servers("grafana")
        write(os.path.join(project.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), "Login uses $%s.\n" % UNLIKELY_NAME)
        result = project.run("--check", "--planning")
        self.done(result, 1)
        self.assertIn("mentions names no .env defines: $%s" % UNLIKELY_NAME, result.stdout)
        write(os.path.join(project.repo, ".specs", "project", ".env.test"), "%s=abc123\n" % UNLIKELY_NAME)
        self.done(project.run("--check", "--planning"), 0)

    def test_a_repo_with_no_known_test_command_is_a_warning(self):
        project = self.make_planning()
        self.servers("grafana")
        os.remove(os.path.join(project.repo, "package.json"))
        result = project.run("--check", "--planning")
        self.done(result, 0)
        self.assertIn("[check] WARNING: repo proj: no build/test command known", result.stdout)
        write(os.path.join(project.repo, "docs", "codebase", "TESTING.md"), "# Testing\nrun it\n")
        self.assertNotIn("no build/test command known", project.run("--check", "--planning").stdout)

    def test_the_shared_guard_check_still_runs(self):
        project = self.make_planning()
        self.servers("grafana")
        result = project.run("--check", "--planning")
        self.done(result, 0)  # blocking `git push` is part of the shared preflight, which --planning keeps
        self.assertNotIn("guard.py did not block", result.stdout)


class TestPlanningHelpers(unittest.TestCase):
    def test_mentioned_servers_match_whole_names_only(self):
        self.assertEqual(run_loop.mentioned_mcp_servers("use mcp__mcp-mysql-dev__mysql_query and the grafana MCP"), ["grafana", "mcp-mysql-dev"])
        self.assertEqual(run_loop.mentioned_mcp_servers("grafanalite, mcp-mysql-devel"), [])

    def test_known_test_command_reads_the_repo(self):
        folder = tempfile.mkdtemp(prefix="known-test-")
        self.addCleanup(shutil.rmtree, folder, True)
        self.assertEqual(run_loop.known_test_command(folder), "")
        write(os.path.join(folder, "go.mod"), "module x\n")
        self.assertEqual(run_loop.known_test_command(folder), "go test ./...")


if __name__ == "__main__":
    unittest.main()
