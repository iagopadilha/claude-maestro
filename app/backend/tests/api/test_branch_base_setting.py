"""ROADMAP `**Branch base:**`: the parser, the one resolver (ROADMAP, then env, then developer), `GET loop/settings`, the workspace payload,
the planning status block and the run_loop options built by the loop control."""
import os
import unittest
from unittest import mock

from harness_backend.domain.roadmap_settings import parse_loop_settings, read_roadmap_base_branch
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services import loop_worktree_service, planning_gates
from harness_backend.services.loop_control_service import convert_settings_to_loop_flags
from harness_backend.services.workspace_service import WorkspaceService

from .support_commands import wait_for_process_to_end
from .support_multi_repo import MultiRepoTestCase
from .support_projects import run_git, write_file
from .test_loop_settings import LoopSettingsTests
from .test_workspace_service import FakeEngineAdapter, InlineJobService

NO_ENVIRONMENT = {"LOOP_PR_BASE": "", "LOOP_INTEGRATION_BRANCH": ""}


class IsolatedEnvironment:
    """No branch variable from the machine: neither the process environment nor the app .env."""

    def isolate_environment(self):
        patcher = mock.patch.dict(os.environ, NO_ENVIRONMENT)
        patcher.start()
        self.addCleanup(patcher.stop)
        reader = mock.patch.object(loop_worktree_service, "read_env_file_value", lambda name: "")
        reader.start()
        self.addCleanup(reader.stop)


class ParserTests(unittest.TestCase):
    def test_default_when_the_line_is_absent(self):
        parsed = parse_loop_settings("**Goal:** x\n")
        self.assertEqual(parsed["settings"]["base_branch"], "developer")
        self.assertIn("base_branch", parsed["missing"])
        self.assertEqual(parsed["problems"], [])

    def test_explicit_value_in_every_header_style(self):
        for line in ("**Branch base:** release/1.0", "Branch base: release/1.0", "- Branch base: release/1.0 (the train)"):
            self.assertEqual(parse_loop_settings(line)["settings"]["base_branch"], "release/1.0", line)

    def test_it_is_not_confused_with_the_loop_branch_line(self):
        parsed = parse_loop_settings("**Branch:** loop/mine\n**Branch base:** staging\n")
        self.assertEqual((parsed["settings"]["branch"], parsed["settings"]["base_branch"]), ("loop/mine", "staging"))
        only_base = parse_loop_settings("**Branch base:** staging\n")["settings"]
        self.assertEqual((only_base["branch"], only_base["base_branch"]), ("", "staging"))

    def test_invalid_value_is_the_default_plus_a_problem(self):
        for raw in ("bad branch!", "-x", "a..b"):
            parsed = parse_loop_settings("**Branch base:** %s\n" % raw)
            self.assertEqual(parsed["settings"]["base_branch"], "developer", raw)
            self.assertEqual([item["name"] for item in parsed["problems"]], ["Branch base"], raw)
            self.assertEqual(read_roadmap_base_branch("**Branch base:** %s\n" % raw), "", raw)

    def test_empty_value_is_the_default_plus_a_problem(self):
        parsed = parse_loop_settings("**Branch base:**\n")
        self.assertEqual(parsed["settings"]["base_branch"], "developer")
        self.assertEqual([item["name"] for item in parsed["problems"]], ["Branch base"])

    def test_a_line_inside_an_html_comment_is_ignored(self):
        self.assertEqual(read_roadmap_base_branch("<!--\n**Branch base:** hidden\n-->\n"), "")

    def test_reader_returns_only_an_explicit_valid_value(self):
        self.assertEqual(read_roadmap_base_branch("**Branch base:** staging"), "staging")
        self.assertEqual(read_roadmap_base_branch(""), "")


class ResolverTests(IsolatedEnvironment, unittest.TestCase):
    def setUp(self):
        self.isolate_environment()

    def test_roadmap_then_environment_then_developer(self):
        self.assertEqual(loop_worktree_service.get_integration_branch(""), "developer")
        os.environ["LOOP_INTEGRATION_BRANCH"] = "from-env"
        self.assertEqual(loop_worktree_service.get_integration_branch(""), "from-env")
        self.assertEqual(loop_worktree_service.get_integration_branch("**Branch base:** staging"), "staging")
        self.assertEqual(loop_worktree_service.get_integration_branch("**Branch base:** bad branch!"), "from-env")

    def test_flags_carry_the_base_branch_only_when_there_is_one(self):
        settings = parse_loop_settings("")["settings"]
        self.assertNotIn("--base-branch", convert_settings_to_loop_flags(settings, 1, True))
        flags = convert_settings_to_loop_flags(settings, 1, True, base_branch="staging")
        self.assertEqual(flags[flags.index("--base-branch") + 1], "staging")

    def test_planning_status_block_shows_the_resolved_value(self):
        os.environ["LOOP_PR_BASE"] = "from-env"
        absent = planning_gates.describe_loop_settings_for_ai(parse_loop_settings(""))
        self.assertIn("    * Branch base: from-env", absent)
        explicit = planning_gates.describe_loop_settings_for_ai(parse_loop_settings("**Branch base:** staging\n"))
        self.assertIn("    * Branch base: staging", explicit)
        invalid = planning_gates.describe_loop_settings_for_ai(parse_loop_settings("**Branch base:** bad branch!\n"))
        self.assertIn("    * Branch base: from-env (invalid in the ROADMAP: the default is used)", invalid)
        self.assertTrue(any("[WARNING]" in line and "Branch base" in line for line in invalid))


class BranchBaseSettingsRouteTests(IsolatedEnvironment, LoopSettingsTests):
    """Same fixture as the loop settings tests (their own tests run again here: they must keep passing with the new line)."""

    def test_settings_route_reports_the_roadmap_value(self):
        self.isolate_environment()
        self.prepare_fake("**Branch base:** staging\n")
        _status, data = self.fixture.call("/api/loop/settings")
        self.assertEqual(data["integration_branch"], "staging")
        self.assertEqual(data["settings"]["base_branch"], "staging")
        self.assertNotIn("base_branch", data["missing"])

    def test_settings_route_without_the_line_reports_developer_as_missing(self):
        self.isolate_environment()
        self.prepare_fake()
        _status, data = self.fixture.call("/api/loop/settings")
        self.assertEqual(data["integration_branch"], "developer")
        self.assertIn("base_branch", data["missing"])

    def test_start_passes_the_base_branch_to_run_loop(self):
        self.isolate_environment()
        self.prepare_fake("**Branch base:** staging\n")
        status, data = self.fixture.call("/api/loop/start", "POST", {})
        self.assertEqual(status, 200)
        argv = self.read_record("start_record.json")["argv"]
        self.assertEqual(argv[argv.index("--base-branch") + 1], "staging")
        wait_for_process_to_end(data["pid"])

    def test_start_without_the_line_sends_no_base_branch(self):
        self.isolate_environment()
        self.prepare_fake()
        status, data = self.fixture.call("/api/loop/start", "POST", {})
        self.assertEqual(status, 200)
        self.assertNotIn("--base-branch", self.read_record("start_record.json")["argv"])
        wait_for_process_to_end(data["pid"])


class WorkspacePayloadTests(IsolatedEnvironment, MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.isolate_environment()
        self.primary = self.make_repo("app")
        self.context = self.make_context(self.primary)
        self.workspace = WorkspaceService(self.registry, self.loop_status_service, LoopFileRepository(), self.git_client,
                                          InlineJobService(), FakeEngineAdapter(), self.log)

    def test_payload_reports_the_roadmap_branch_as_integration_and_base(self):
        run_git(self.primary, "branch", "release-train")
        write_file(os.path.join(self.context.specs_dir, "project", "ROADMAP.md"), "# R\n\n**Branch base:** release-train\n")
        payload = self.workspace.get_workspace(self.context)
        self.assertEqual(payload["integration_branch"], "release-train")
        self.assertEqual(payload["repos"][0]["base_branch"], "release-train")

    def test_payload_falls_back_to_the_current_branch_when_the_roadmap_branch_is_missing_locally(self):
        write_file(os.path.join(self.context.specs_dir, "project", "ROADMAP.md"), "# R\n\n**Branch base:** release-train\n")
        payload = self.workspace.get_workspace(self.context)
        row = payload["repos"][0]
        self.assertEqual(payload["integration_branch"], "release-train")
        self.assertEqual(row["base_branch"], row["current_branch"])


if __name__ == "__main__":
    unittest.main()
