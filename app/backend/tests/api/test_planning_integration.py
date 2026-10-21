"""Wave 2 integration of the planning chat: every reply applies REPOS-SUGGESTED.json, rewrites the capabilities block of PROJECT.md and
schedules the readiness check; the app's refusals reach the AI in the next status block; the approval needs the `ready` gate; the
capabilities block is the app's (the snapshot revert puts it back); the workspace bar reads role, reason, changed_by_feature and rejected.

Fake `claude` (support_projects), fake readiness runner: nothing starts the real claude or `run_loop.py --check`.
"""
import json
import os
import time
import unittest

from harness_backend.infrastructure.git_client import GitClient
from harness_backend.infrastructure.process_runner import ProcessRunner
from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
from harness_backend.services.planning_service import GUARD_IGNORED_PATHS, PLANNING_EDITABLE_PATHS
from harness_backend.services.project_capabilities_service import END_MARKER, START_MARKER
from harness_backend.services.planning_repos_service import PlanningReposService
from harness_backend.services.repo_registry_service import RepoRegistryService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import FAKE_CLAUDE_CODE, TWO_FEATURE_ROADMAP, create_git_repo, read_file, write_file, write_planning_documents
from .test_workspace_service import InlineJobService
from .test_projects_planning import PlanningTestCase, list_flag_values

SUGGEST_VARIABLE = "FAKE_SUGGEST_JSON"
HACK_VARIABLE = "FAKE_HACK_BLOCK"
PLANNED_LINE = '    result = "planned: " + stdin_text.strip().splitlines()[-1]\n'
FAKE_EXTRA = (
    '    if os.environ.get("FAKE_SUGGEST_JSON"):\n'
    '        write(os.path.join(project, "REPOS-SUGGESTED.json"), os.environ["FAKE_SUGGEST_JSON"])\n'
    '    if os.environ.get("FAKE_HACK_BLOCK"):\n'
    '        write(os.path.join(project, "PROJECT.md"), "# Project\\n\\n**Vision:** a to-do app.\\n\\nAI line outside the block.\\n\\n"\n'
    '              "<!-- maestro:capabilities -->\\nHACKED BY THE AI\\n<!-- /maestro:capabilities -->\\n")\n')
READY_OUTPUT = "[check] READY. Command: python run_loop.py x\n"
PROBLEM_OUTPUT = "[check] PROBLEM: credential $ADMIN_TOKEN is not defined\n"


def wait_until(condition, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


class PlanningIntegrationTestCase(PlanningTestCase):
    def setUp(self):
        super().setUp()
        self.assertIn(PLANNED_LINE, FAKE_CLAUDE_CODE)
        write_file(self.fake_claude_path, FAKE_CLAUDE_CODE.replace(PLANNED_LINE, FAKE_EXTRA + PLANNED_LINE))
        self.addCleanup(lambda: [os.environ.pop(name, None) for name in (SUGGEST_VARIABLE, HACK_VARIABLE)])
        self.repo, self.project_id = self.create_planning_project("main")
        self.other = create_git_repo(os.path.join(self.projects_dir, "other"))
        self.project_md = os.path.join(self.repo, ".specs", "project", "PROJECT.md")
        self.repos_json = os.path.join(self.repo, ".specs", "project", "REPOS.json")

    def context(self):
        return self.module.project_service.get_project_context(self.project_id)

    def suggest(self, *entries):
        os.environ[SUGGEST_VARIABLE] = json.dumps({"repos": list(entries)})

    def stored_roles(self):
        return {repo["name"]: repo.get("role") for repo in json.loads(read_file(self.repos_json))["repos"]}


class TestReplyEffects(PlanningIntegrationTestCase):
    def test_a_reply_applies_the_repos_writes_the_capabilities_block_and_schedules_readiness(self):
        self.suggest({"path": self.other, "role": "reference", "reason": "the contract lives there"})
        job = self.send_and_wait(self.project_id, "Plan it")
        self.assertEqual(job["state"], "ok", job)
        self.assertEqual(self.stored_roles().get("other"), "reference")
        text = read_file(self.project_md)
        block = text[text.index(START_MARKER):text.index(END_MARKER)]
        self.assertIn("`other` (reference)", block, "the block was rewritten AFTER the repos were applied")
        self.assertIn("`main` (edit, primary)", block)
        self.assertIn("**Vision:** a to-do app.", text, "what the AI wrote outside the block stays")
        self.assertTrue(wait_until(lambda: self.readiness_runner.calls), "a readiness check was scheduled after the reply")
        self.assertIn("--planning", self.readiness_runner.calls[0][0])

    def test_the_next_reply_gets_the_reference_repo_as_add_dir_and_the_mcp_flags(self):
        self.suggest({"path": self.other, "role": "reference", "reason": "x"})
        self.send_and_wait(self.project_id, "first")
        self.send_and_wait(self.project_id, "second")
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(list_flag_values(argv, "--add-dir"), [os.path.abspath(self.other)])
        self.assertIn("--strict-mcp-config", argv)
        self.assertNotIn("mcp__grafana__query_loki_logs", argv, "no ~/.claude.json server: no MCP tool is allowed")

    def test_mcp_servers_of_the_users_claude_json_get_read_tools_and_denied_writers(self):
        write_file(self.claude_config_path, json.dumps({"mcpServers": {"grafana": {"command": "x"}, "mcp-mysql-dev": {"url": "http://x"}}}))
        self.send_and_wait(self.project_id, "look at the data")
        argv = self.read_claude_calls()[-1]["argv"]
        allowed, denied = list_flag_values(argv, "--allowedTools"), list_flag_values(argv, "--disallowedTools")
        self.assertIn("mcp__grafana__query_loki_logs", allowed)
        self.assertIn("mcp__mcp-mysql-dev__mysql_query", allowed)
        self.assertNotIn("mcp__mcp-mysql-prod__mysql_query", allowed, "a server the user does not have is just absent")
        self.assertIn("mcp__grafana__update_dashboard", denied)
        self.assertIn("--mcp-config", argv)

    def test_rejections_appear_in_the_status_block_of_the_next_message(self):
        missing = os.path.join(self.projects_dir, "does-not-exist")
        self.suggest({"path": missing, "role": "edit", "reason": "x"})
        self.send_and_wait(self.project_id, "first")
        os.environ.pop(SUGGEST_VARIABLE)
        self.send_and_wait(self.project_id, "second")
        stdin = self.read_claude_calls()[-1]["stdin"]
        self.assertIn("repos_rejected", stdin)
        self.assertIn(missing, stdin)
        self.assertIn("folder does not exist", stdin)

    def test_the_status_block_carries_the_readiness_gate(self):
        self.send_and_wait(self.project_id, "first")
        self.send_and_wait(self.project_id, "second")
        self.assertIn("- [OK] ready: Ready to run the loop", self.read_claude_calls()[-1]["stdin"])

    def test_a_reply_that_rewrites_the_capabilities_block_gets_it_back(self):
        os.environ[HACK_VARIABLE] = "1"
        job = self.send_and_wait(self.project_id, "hack")
        self.assertEqual(job["state"], "ok")
        text = read_file(self.project_md)
        self.assertNotIn("HACKED BY THE AI", text)
        self.assertIn("AI line outside the block.", text, "only the marked region is the app's")
        self.assertIn("## What the agents can use (inside the loop)", text)


class TestRestoreCapabilitiesRegion(PlanningIntegrationTestCase):
    def snapshot_and_edit(self, edit):
        service = self.module.planning_service
        guard = WorkspaceFileGuard()
        self.module.project_capabilities_service.write(self.context())
        before = read_file(self.project_md)
        snapshot = guard.take_snapshot(self.repo, GUARD_IGNORED_PATHS)
        write_file(self.project_md, edit(before))
        self.assertTrue(guard.revert_unexpected_changes(snapshot, PLANNING_EDITABLE_PATHS) == {"reverted": [], "removed": [], "unrestorable": []},
                        "PROJECT.md is an editable document: the file guard alone lets the change through")
        return service, snapshot, before

    def test_an_edit_inside_the_markers_is_undone_and_the_rest_is_kept(self):
        service, snapshot, before = self.snapshot_and_edit(
            lambda text: text.replace("## What the agents can use", "## HACKED").replace("**Vision:** FILL_IN", "**Vision:** edited by the AI", 1))
        self.assertTrue(service.restore_capabilities_region(self.context(), snapshot))
        after = read_file(self.project_md)
        self.assertNotIn("HACKED", after)
        self.assertIn("**Vision:** edited by the AI", after)
        self.assertEqual(after.count(START_MARKER), 1)
        self.assertEqual(after[after.index(START_MARKER):after.index(END_MARKER)], before[before.index(START_MARKER):before.index(END_MARKER)])

    def test_a_deleted_marker_brings_the_block_back(self):
        service, snapshot, _ = self.snapshot_and_edit(lambda text: text.replace(END_MARKER, ""))
        self.assertTrue(service.restore_capabilities_region(self.context(), snapshot))
        self.assertEqual(read_file(self.project_md).count(END_MARKER), 1)

    def test_untouched_block_is_left_alone(self):
        service, snapshot, _ = self.snapshot_and_edit(lambda text: text + "\nMore text by the AI.\n")
        self.assertFalse(service.restore_capabilities_region(self.context(), snapshot))
        self.assertIn("More text by the AI.", read_file(self.project_md))


class TestApprovalNeedsReadiness(PlanningIntegrationTestCase):
    use_real_readiness = True

    def prepare(self):
        write_planning_documents(self.repo, TWO_FEATURE_ROADMAP)

    def test_can_approve_needs_the_ready_gate_and_the_refusal_names_the_first_problem(self):
        self.prepare()
        self.readiness_runner.result = (1, PROBLEM_OUTPUT, "")
        self.module.readiness_service.run(self.context(), wait=True)
        planning = self.get_planning(self.project_id)
        gate = [item for item in planning["gates"] if item["id"] == "ready"][0]
        self.assertFalse(gate["ok"])
        self.assertIn("credential $ADMIN_TOKEN is not defined", gate["detail"])
        self.assertFalse(planning["can_approve"])
        status, body = self.request("POST", "/p/%s/api/roadmap/approve" % self.project_id, {"confirm": True})
        self.assertEqual(status, 422, body)
        self.assertIn("credential $ADMIN_TOKEN is not defined", body["error"])
        self.readiness_runner.result = (0, READY_OUTPUT, "")
        self.module.readiness_service.run(self.context(), wait=True)
        self.assertTrue(self.get_planning(self.project_id)["can_approve"])
        self.assertEqual(self.request("POST", "/p/%s/api/roadmap/approve" % self.project_id, {"confirm": True})[0], 200)

    def test_enforcement_is_on_by_default(self):
        self.assertTrue(self.config.planning_readiness_enforced)
        self.assertTrue(self.module.readiness_service.enforced)


class TestRepoAndEnvironmentChangesTriggerTheBlock(PlanningIntegrationTestCase):
    def test_a_repo_added_and_a_role_change_rewrite_the_block_and_schedule_a_check(self):
        class NoLoopRunning:
            @staticmethod
            def is_loop_running(context):
                return False

        registry = RepoRegistryService(GitClient(ProcessRunner()), NoLoopRunning(), self.log)
        registry.after_change = self.module.on_plan_changed  # what the composition wires
        context = self.context()
        registry.add_repo(context, self.other, [])
        self.assertIn("`other` (edit)", read_file(self.project_md))
        self.assertTrue(wait_until(lambda: len(self.readiness_runner.calls) >= 1))
        calls_after_add = len(self.readiness_runner.calls)
        registry.set_role(context, "other", "reference")
        self.assertIn("`other` (reference)", read_file(self.project_md))
        self.assertTrue(wait_until(lambda: len(self.readiness_runner.calls) > calls_after_add))
        registry.remove_repo(context, "other")
        self.assertNotIn("`other`", read_file(self.project_md))

    def test_saving_test_environment_values_schedules_a_check(self):
        write_file(os.path.join(self.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), "# Test environment\n\nUses $ADMIN_TOKEN.\n")
        status, body = self.request("PUT", "/p/%s/api/test-environment/values" % self.project_id,
                                    {"values": {"ADMIN_TOKEN": "secret-value"}, "target": "project"})
        self.assertEqual(status, 200, body)
        self.assertTrue(wait_until(lambda: self.readiness_runner.calls))


class TestWorkspaceChips(MultiRepoTestCase):
    def test_workspace_repos_carry_role_reason_changed_by_feature_and_the_rejections(self):
        from harness_backend.repositories.loop_file_repository import LoopFileRepository
        from harness_backend.services.workspace_service import WorkspaceService
        primary, contract, service = self.make_repo("app"), self.make_repo("contract"), self.make_repo("billing")
        context = self.make_context(primary)
        write_file(os.path.join(primary, ".specs", "project", "ROADMAP.md"),
                   "# Roadmap\n\n## Stage 1\n\n- [a] status=pending scope=Small kind=back paths=src/api,billing:src/pay\n")
        write_file(os.path.join(primary, ".specs", "project", "REPOS-SUGGESTED.json"), json.dumps({"repos": [
            {"path": contract, "role": "reference", "reason": "the API contract"},
            {"path": service, "role": "edit", "reason": "payments"},
            {"path": os.path.join(self.temporary_dir, "nope"), "role": "edit", "reason": "x"}]}))
        applied = PlanningReposService(self.git_client, self.log).apply_suggested_repos(context)
        self.assertEqual(len(applied["applied"]), 2)
        workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client, InlineJobService(), None, self.log)
        data = workspace.get_workspace(context)
        by_name = {repo["name"]: repo for repo in data["repos"]}
        self.assertEqual((by_name["contract"]["role"], by_name["contract"]["reason"]), ("reference", "the API contract"))
        self.assertEqual(by_name["billing"]["role"], "edit")
        self.assertTrue(by_name["billing"]["changed_by_feature"])
        self.assertFalse(by_name["contract"]["changed_by_feature"])
        self.assertEqual(by_name["app"]["role"], "edit")
        self.assertEqual([item["reason"] for item in data["rejected"]], ["folder does not exist"])
        self.assertTrue(data["rejected"][0]["path"].endswith("nope"))


if __name__ == "__main__":
    unittest.main()
