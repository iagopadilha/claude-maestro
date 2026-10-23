#!/usr/bin/env python3
"""`doctor --access` (engine/access_report.py): static part, probe parsing/flags with a fake claude script, guard.log summary.
Temp dirs only; the real claude is never called."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import access_report as report  # noqa: E402
import runtime_isolation as iso  # noqa: E402
from harness_backend.config import skill_locator  # noqa: E402

INIT = {"type": "system", "subtype": "init", "model": "haiku", "tools": ["Read", "Bash", "mcp__mcp-mysql-dev__mysql_query"],
        "mcp_servers": [{"name": "mcp-mysql-dev", "status": "connected"}], "agents": ["executor-back", "general-purpose"],
        "skills": ["backend-dev", "update-config"], "plugins": []}
GUARD_SRC = "MCP_ALLOW = {'executor': {'mcp__a__b'}, 'planning': {'mcp__c__d', 'mcp__a__b'}}\n"
ENV_NAMES = ("LOOP_DATA_DIR", "LOOP_CLAUDE_HOME", "LOOP_CLAUDE_CONFIG_FILE")


def write(path, text="x"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class World(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="access-report-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.saved = {name: os.environ.get(name) for name in ENV_NAMES}
        self.addCleanup(self.restore)
        os.environ["LOOP_DATA_DIR"] = os.path.join(self.root, "data")
        os.environ["LOOP_CLAUDE_HOME"] = os.path.join(self.root, "home")
        os.environ["LOOP_CLAUDE_CONFIG_FILE"] = os.path.join(self.root, "claude.json")
        write(os.environ["LOOP_CLAUDE_CONFIG_FILE"], json.dumps({"mcpServers": {"mcp-mysql-dev": {"command": "x"}}}))
        self.plugin = os.path.join(self.root, "plugin")
        write(os.path.join(self.plugin, "skills", "orchestrator", "hooks", "guard.py"), GUARD_SRC)
        write(os.path.join(self.plugin, "skills", "orchestrator", "SKILL.md"))
        write(os.path.join(self.plugin, "agents", "executor-back.md"))
        write(os.path.join(os.environ["LOOP_CLAUDE_HOME"], "skills", "backend-dev", "SKILL.md"))
        self.toolkit = os.path.join(self.root, "toolkit")  # a fake claude-dev-toolkit named by dev_toolkit_dir; the sibling clone is hidden
        write(os.path.join(self.toolkit, "skills", "tlc-spec-driven", "SKILL.md"))
        write(os.path.join(os.environ["LOOP_DATA_DIR"], "config.json"), json.dumps({"dev_toolkit_dir": self.toolkit}))
        patcher = mock.patch.object(skill_locator, "REPO_ROOT_DIR", os.path.join(self.root, "no-sibling"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.project = os.path.join(self.root, "repo")
        os.makedirs(os.path.join(self.project, ".specs"))
        iso.clear_memo()

    def restore(self):
        for name, value in self.saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        iso.clear_memo()


class StaticTest(World):
    def test_static_part(self):
        static = report.build_static(self.project, self.plugin)
        self.assertTrue(static["isolation"])
        self.assertEqual([row["server"] for row in static["mcp_servers"]], ["mcp-mysql-dev"])
        self.assertEqual(static["mcp_allow_by_profile"]["planning"], ["mcp__a__b", "mcp__c__d"])
        self.assertIn("backend-dev", static["skills_allowlist"]["executor"])
        self.assertIn("backend-dev", static["bundle"]["skills"])
        self.assertIn("orchestrator", static["bundle"]["skills"])  # the plugin's own skill folder
        self.assertIn("frontend-dev", static["bundle"]["skipped"])
        self.assertEqual(static["bundle"]["agents"], ["executor-back"])
        self.assertTrue(os.path.isfile(static["allowlist_file"]))

    def test_toolkit_skills_are_bundled_and_the_root_is_reported(self):
        static = report.build_static(self.project, self.plugin)
        self.assertEqual(os.path.normcase(static["toolkit_root"]), os.path.normcase(os.path.abspath(self.toolkit)))
        self.assertIn("tlc-spec-driven", static["bundle"]["skills"])
        self.assertIn("toolkit root", report.format_report(report.build_report(self.project, probe=False, plugin_root_path=self.plugin)))

    def test_slot_skills_reach_the_allowlist_the_bundle_report_and_are_labelled(self):
        write(os.path.join(self.project, "CLAUDE.md"), "# P\n\n## Standards\nfrontend: acme:ghost-front\nbackend: backend-dev\n")
        static = report.build_static(self.project, self.plugin)
        for profile in ("planning", "spec", "executor", "verifier"):
            self.assertIn("ghost-front", static["skills_allowlist"][profile])
        self.assertNotIn("ghost-front", static["skills_allowlist"]["orchestrator"])
        self.assertEqual(static["slot_skills"], {"ghost-front": "frontend", "backend-dev": "backend"})
        self.assertIn("ghost-front", static["bundle"]["skipped"])  # not installed anywhere: skipped, not an error
        text = report.format_report(report.build_report(self.project, probe=False, plugin_root_path=self.plugin))
        self.assertIn("ghost-front (slot: frontend)", text)
        self.assertIn("backend-dev (slot: backend)", text)

    def test_isolation_off_has_no_bundle_and_no_probe(self):
        result = report.build_report(self.project, probe=True, config={"loop_isolation": False}, plugin_root_path=self.plugin)
        self.assertFalse(result["static"]["isolation"])
        self.assertIsNone(result["static"]["bundle"])
        self.assertIsNone(result["probe"])
        self.assertIn("OFF", report.format_report(result))

    def test_missing_guard_gives_empty_mcp_table(self):
        self.assertEqual(report.load_guard_mcp_allow(os.path.join(self.root, "nope.py")), {})


class ProbeTest(World):
    def fake(self, body):
        path = os.path.join(self.root, "fake.py")
        write(path, body)
        return [sys.executable, path]

    def test_parse_init_line(self):
        self.assertEqual(report.parse_init_line(json.dumps(INIT))["model"], "haiku")
        self.assertIsNone(report.parse_init_line("garbage"))
        self.assertIsNone(report.parse_init_line(json.dumps({"type": "result"})))

    def test_analyze_flags_everything_outside_allowlists(self):
        static = report.build_static(self.project, self.plugin)
        clean = report.analyze_init(INIT, static)
        self.assertEqual(clean["flags"], [])
        self.assertEqual(clean["mcp_tools"], ["mcp__mcp-mysql-dev__mysql_query"])
        dirty = dict(INIT, mcp_servers=[{"name": "zendesk"}], agents=["user-agent"], skills=["idata-suporte"],
                     plugins=[{"name": "atlassian", "path": "/p/atlassian"}, {"name": "x", "source": "builtin"}])
        text = " | ".join(report.analyze_init(dirty, static)["flags"])
        for needle in ("zendesk", "user-agent", "idata-suporte", "atlassian"):
            self.assertIn(needle, text)
        self.assertNotIn("not builtin: x", text)

    def test_reads_until_init_then_kills(self):
        argv = self.fake("import json,time\nprint(json.dumps(%r),flush=True)\ntime.sleep(60)\n" % INIT)
        init, error = report.read_until_init(argv, "", dict(os.environ), self.root, timeout=20)
        self.assertEqual(error, "")
        self.assertEqual(init["model"], "haiku")

    def test_no_init_is_reported_not_raised(self):
        argv = self.fake("print('{}')\n")
        init, error = report.read_until_init(argv, "", dict(os.environ), self.root, timeout=20)
        self.assertIsNone(init)
        self.assertIn("without an init", error)
        init, error = report.read_until_init([os.path.join(self.root, "missing-binary")], "", dict(os.environ), self.root, timeout=5)
        self.assertIn("could not start", error)

    def test_probe_argv_is_the_isolated_one(self):
        static = report.build_static(self.project, self.plugin)
        argv, _, env = report.build_probe_argv(static, {"claude_binary": "claude"})
        for flag in ("--setting-sources", "--strict-mcp-config", "--mcp-config", "--max-budget-usd", "--settings"):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertEqual(env["LOOP_ISOLATED"], "1")
        self.assertEqual(env["LOOP_SKILLS_FILE"], static["allowlist_file"])


class GuardLogTest(World):
    def test_summary(self):
        lines = [{"t": "t1", "profile": "executor", "rule": "mcp-allowlist", "decision": "allow", "detail": "ok", "agent": "executor-back"},
                 {"t": "t2", "profile": "executor", "rule": "git-push", "decision": "blocked", "detail": "no push"},
                 {"t": "t3", "profile": "planning", "rule": "sql-write", "decision": "blocked", "detail": "write", "agent": "spec-writer"}]
        write(os.path.join(self.project, report.GUARD_LOG_RELATIVE), "\n".join(json.dumps(line) for line in lines) + "\nnot json\n")
        summary = report.summarize_guard_log(self.project)
        self.assertEqual(summary["lines"], 3)
        self.assertEqual(summary["by_decision"], {"allow": 1, "blocked": 2})
        self.assertEqual(summary["by_agent"]["-"], 1)
        self.assertEqual([item["rule"] for item in summary["last_denials"]], ["git-push", "sql-write"])
        text = report.format_report(report.build_report(self.project, probe=False, plugin_root_path=self.plugin))
        self.assertIn("sql-write", text)

    def test_only_last_200_lines_and_10_denials(self):
        lines = [json.dumps({"t": str(n), "rule": "r", "decision": "blocked", "detail": "d"}) for n in range(300)]
        write(os.path.join(self.project, report.GUARD_LOG_RELATIVE), "\n".join(lines))
        summary = report.summarize_guard_log(self.project)
        self.assertEqual(summary["lines"], 200)
        self.assertEqual(len(summary["last_denials"]), 10)
        self.assertEqual(summary["last_denials"][-1]["t"], "299")

    def test_no_log(self):
        self.assertEqual(report.summarize_guard_log(self.project)["lines"], 0)


if __name__ == "__main__":
    unittest.main()
