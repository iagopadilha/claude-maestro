"""Test environment tab (`/p/<id>/api/test-environment*`): the credentials table (set or missing, never a value), saving values to
the project `.env.test` or the user-level shared `.env`, the computed `access` card (credential layers + MCP servers, no toggles), the
names file the planning AI reads and the read-only Grafana access of the planning reply. Hermetic: temporary data dir, fake user shared folder, fake ~/.claude.json, fake claude."""
import json
import os
import unittest

from .support_projects import loop_file_path, read_file, write_file
from .test_projects_planning import PlanningTestCase, list_flag_values

DOCUMENT = ("# Test environment (DEV)\n\n| Item | Name |\n|---|---|\n| Admin | `$QA_ADMIN_TOKEN` |\n| User | `$TEST_USER_1_EMAIL` |\n"
            "<!-- later: $QA_ONLY_IN_COMMENT -->\n| App | `$QA_LOCAL_URL` |\n")
SECRET = "hunter2-must-never-leak"
GRAFANA_TOKEN = "grafana-jwt-must-never-reach-argv"
READ_TOOLS = ["mcp__grafana__query_loki_logs", "mcp__grafana__list_loki_label_names", "mcp__grafana__list_loki_label_values",
              "mcp__grafana__query_loki_stats", "mcp__grafana__query_loki_patterns", "mcp__grafana__find_error_pattern_logs",
              "mcp__grafana__list_datasources"]


class TestEnvironmentTestCase(PlanningTestCase):
    def setUp(self):
        super().setUp()
        for name in ("QA_ADMIN_TOKEN", "QA_ONLY_IN_COMMENT", "QA_LOCAL_URL", "TEST_USER_EMAILS", "TEST_USER_PASSWORDS"):
            self.addCleanup(os.environ.pop, name, None)
            os.environ.pop(name, None)
        self.repo, self.project_id = self.create_planning_project("env")
        write_file(os.path.join(self.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), DOCUMENT)
        self.base = "/p/%s/api/test-environment" % self.project_id
        self.project_env_path = os.path.join(self.repo, ".specs", "project", ".env.test")
        self.shared_env_path = os.path.join(self.user_shared_dir, ".env")

    def get_environment(self):
        status, body = self.request("GET", self.base)
        self.assertEqual(status, 200, body)
        return body

    def sources(self):
        return {row["name"]: row["source"] for row in self.get_environment()["names"]}

    def put(self, suffix, body):
        return self.request("PUT", self.base + suffix, body)


class TestCredentialsTable(TestEnvironmentTestCase):
    def test_every_mentioned_name_with_its_source_and_no_value(self):
        write_file(self.project_env_path, "# template\nQA_ADMIN_TOKEN=%s\n" % SECRET)
        body = self.get_environment()
        self.assertEqual(set(body), {"ok", "ready", "names", "fill_in", "access", "shared_paths"})
        rows = {row["name"]: row for row in body["names"]}
        self.assertEqual(sorted(rows), ["QA_ADMIN_TOKEN", "QA_LOCAL_URL", "QA_ONLY_IN_COMMENT", "TEST_USER_1_EMAIL"])
        self.assertEqual((rows["QA_ADMIN_TOKEN"]["source"], rows["QA_ADMIN_TOKEN"]["set"], rows["QA_ADMIN_TOKEN"]["secret"]), ("project", True, True))
        self.assertEqual(rows["QA_ADMIN_TOKEN"]["path_label"], "project .env.test")
        self.assertEqual((rows["QA_LOCAL_URL"]["source"], rows["QA_LOCAL_URL"]["set"], rows["QA_LOCAL_URL"]["secret"]), ("missing", False, False))
        self.assertFalse(body["ready"])
        self.assertEqual(set(body["access"]), {"credential_layers", "mcp"})
        self.assertNotIn(SECRET, json.dumps(body))

    def test_fill_in_lines_keep_it_not_ready_even_with_every_name_set(self):
        write_file(os.path.join(self.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), DOCUMENT + "| Header | FILL_IN |\n")
        write_file(self.project_env_path, "QA_ADMIN_TOKEN=a1b2\nTEST_USER_EMAILS=a@x.com\nQA_ONLY_IN_COMMENT=x\nQA_LOCAL_URL=http://x\n")
        body = self.get_environment()
        self.assertTrue(all(row["set"] for row in body["names"]))
        self.assertEqual(body["fill_in"], ["| Header | FILL_IN |"])
        self.assertFalse(body["ready"])

    def test_ready_when_all_set_and_no_fill_in(self):
        write_file(self.project_env_path, "QA_ADMIN_TOKEN=a1b2\nTEST_USER_EMAILS=a@x.com\nQA_ONLY_IN_COMMENT=x\nQA_LOCAL_URL=http://x\n")
        self.assertTrue(self.get_environment()["ready"])

    def test_a_name_only_in_the_shared_env_resolves_with_source_shared(self):
        write_file(self.shared_env_path, "QA_ADMIN_TOKEN=%s\nTEST_USER_EMAILS=generic@x.com\n" % SECRET)
        body = self.get_environment()
        self.assertEqual(self.sources()["QA_ADMIN_TOKEN"], "shared")
        self.assertEqual(self.sources()["TEST_USER_1_EMAIL"], "shared")
        self.assertEqual(body["names"][0]["path_label"], "~/.claude/skills/_shared/.env")
        self.assertEqual(body["shared_paths"], ["~/.claude/skills/_shared/.env"])
        self.assertNotIn(SECRET, json.dumps(body))

    def test_access_card_lists_the_credential_layers_with_the_winning_source_and_the_mcp_servers(self):
        write_file(self.project_env_path, "QA_ADMIN_TOKEN=%s\n" % SECRET)
        write_file(self.shared_env_path, "QA_ADMIN_TOKEN=other\nQA_LOCAL_URL=http://shared\n")
        write_file(self.claude_config_path, json.dumps({"mcpServers": {
            "mcp-mysql-dev": {"command": "mysql-mcp", "env": {"PASSWORD": GRAFANA_TOKEN}}, "zendesk": {"command": "zd"}}}))
        access = self.get_environment()["access"]
        layers = {layer["label"]: layer for layer in access["credential_layers"]}
        self.assertEqual(layers["project .env.test"]["name"], "project")
        self.assertTrue(layers["project .env.test"]["exists"])
        self.assertEqual(layers["project .env.test"]["names"], [{"name": "QA_ADMIN_TOKEN", "source": "project", "winning": True}])
        shared = layers["~/.claude/skills/_shared/.env"]
        self.assertEqual(shared["name"], "shared")
        self.assertEqual(shared["names"], [{"name": "QA_ADMIN_TOKEN", "source": "project", "winning": False},
                                           {"name": "QA_LOCAL_URL", "source": "shared", "winning": True}])
        self.assertEqual([layer["name"] for layer in access["credential_layers"]][:2], ["project", "machine"])
        servers = {row["server"]: row for row in access["mcp"]}
        self.assertEqual(sorted(servers), ["grafana", "mcp-mysql-dev", "mcp-mysql-prod"])
        self.assertEqual((servers["mcp-mysql-dev"]["configured"], servers["mcp-mysql-prod"]["configured"], servers["grafana"]["configured"]),
                         (True, False, False))
        self.assertEqual(servers["mcp-mysql-dev"]["tools"], ["mcp__mcp-mysql-dev__mysql_query"])
        self.assertNotIn(SECRET, json.dumps(access))
        self.assertNotIn(GRAFANA_TOKEN, json.dumps(access))

    def test_legacy_access_json_keys_are_dropped_and_ignored(self):
        path = os.path.join(self.repo, ".specs", "project", "ACCESS.json")
        write_file(path, json.dumps({"grafana_read": False, "shared_fallback": False}))
        write_file(self.shared_env_path, "QA_ADMIN_TOKEN=%s\n" % SECRET)
        self.assertEqual(self.sources()["QA_ADMIN_TOKEN"], "shared")
        self.assertFalse(os.path.exists(path))

    def test_unknown_project_is_404(self):
        status, body = self.request("GET", "/p/nope/api/test-environment")
        self.assertEqual((status, body["ok"]), (404, False))


class TestSavingValues(TestEnvironmentTestCase):
    def test_project_target_writes_env_test_keeps_other_lines_and_never_echoes(self):
        write_file(self.project_env_path, "# my comment\nOTHER=1\nQA_ADMIN_TOKEN=old\n\n# trailing\n")
        status, body = self.put("/values", {"values": {"QA_ADMIN_TOKEN": SECRET, "QA_LOCAL_URL": "http://localhost:5000"}, "target": "project"})
        self.assertEqual((status, body), (200, {"ok": True, "saved": ["QA_ADMIN_TOKEN", "QA_LOCAL_URL"]}))
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertEqual(read_file(self.project_env_path),
                         "# my comment\nOTHER=1\nQA_ADMIN_TOKEN=%s\n\n# trailing\nQA_LOCAL_URL=http://localhost:5000\n" % SECRET)
        rows = {row["name"]: row["source"] for row in self.get_environment()["names"]}
        self.assertEqual((rows["QA_ADMIN_TOKEN"], rows["QA_LOCAL_URL"]), ("project", "project"))
        gitignore = read_file(os.path.join(self.repo, ".gitignore"))
        self.assertIn(".specs/project/.env.test", gitignore)

    def test_project_target_creates_the_file_and_quotes_values_that_need_it(self):
        os.remove(self.project_env_path) if os.path.exists(self.project_env_path) else None
        status, body = self.put("/values", {"values": {"QA_ADMIN_TOKEN": "with space #1"}})
        self.assertEqual(status, 200, body)
        self.assertEqual(read_file(self.project_env_path), "QA_ADMIN_TOKEN='with space #1'\n")
        self.assertEqual(self.sources()["QA_ADMIN_TOKEN"], "project")

    def test_shared_target_keeps_every_other_byte_and_the_line_endings(self):
        original = b"\xef\xbb\xbf# generic DEV account\r\nTEST_USER_EMAILS=desenv@x.com\r\n\r\nQA_ADMIN_TOKEN=old\r\n# end"
        os.makedirs(self.user_shared_dir, exist_ok=True)
        with open(self.shared_env_path, "wb") as handle:
            handle.write(original)
        status, body = self.put("/values", {"values": {"QA_ADMIN_TOKEN": SECRET, "QA_LOCAL_URL": "http://x"}, "target": "shared"})
        self.assertEqual((status, body), (200, {"ok": True, "saved": ["QA_ADMIN_TOKEN", "QA_LOCAL_URL"]}))
        with open(self.shared_env_path, "rb") as handle:
            written = handle.read()
        self.assertEqual(written, ("﻿# generic DEV account\r\nTEST_USER_EMAILS=desenv@x.com\r\n\r\nQA_ADMIN_TOKEN=%s\r\n# end\r\n"
                                   "QA_LOCAL_URL=http://x\r\n" % SECRET).encode("utf-8"))
        self.assertFalse(os.path.exists(os.path.join(self.repo, ".specs", "project", ".env.test")) and SECRET in read_file(self.project_env_path))
        self.assertEqual([name for name in os.listdir(self.user_shared_dir) if name.endswith(".tmp")], [])

    def test_shared_target_creates_the_file_when_missing(self):
        self.assertFalse(os.path.exists(self.shared_env_path))
        status, _ = self.put("/values", {"values": {"QA_LOCAL_URL": "http://x"}, "target": "shared"})
        self.assertEqual(status, 200)
        self.assertEqual(read_file(self.shared_env_path), "QA_LOCAL_URL=http://x\n")

    def test_unknown_name_is_a_400_and_nothing_is_written(self):
        status, body = self.put("/values", {"values": {"QA_ADMIN_TOKEN": SECRET, "NOT_IN_THE_DOCUMENT": "x"}, "target": "project"})
        self.assertEqual((status, body["ok"], body["code"]), (400, False, "unknown_name"))
        self.assertIn("NOT_IN_THE_DOCUMENT", body["error"])
        self.assertNotIn(SECRET, json.dumps(body))
        self.assertNotIn(SECRET, read_file(self.project_env_path) if os.path.exists(self.project_env_path) else "")

    def test_refused_values_never_appear_in_the_error(self):
        for bad in ("line one\nQA_LOCAL_URL=injected", "carriage\rreturn", "x" * 501, "", 12, None):
            with self.subTest(bad=str(bad)[:20]):
                status, body = self.put("/values", {"values": {"QA_ADMIN_TOKEN": bad}, "target": "project"})
                self.assertEqual((status, body["ok"]), (400, False))
                self.assertNotIn("injected", json.dumps(body))
        self.assertFalse(os.path.exists(self.project_env_path) and "QA_ADMIN_TOKEN" in read_file(self.project_env_path))

    def test_bad_target_and_bad_bodies(self):
        for body in ({"values": {"QA_ADMIN_TOKEN": "a1"}, "target": "elsewhere"}, {"values": {}}, {"values": ["x"]}, {}):
            with self.subTest(body=body):
                status, answer = self.put("/values", body)
                self.assertEqual((status, answer["ok"]), (400, False))

    def test_values_need_the_token(self):
        status, _ = self.request("PUT", self.base + "/values", {"values": {"QA_ADMIN_TOKEN": "a1b2"}}, with_token=False)
        self.assertIn(status, (401, 403))


class TestAccess(TestEnvironmentTestCase):
    def test_the_access_route_is_gone(self):
        status, _ = self.put("/access", {"grafana_read": True})
        self.assertIn(status, (404, 405))

    def test_reset_specs_does_not_resurrect_access(self):
        status, body = self.request("POST", "/api/app/projects/%s/reset-specs" % self.project_id, {"confirm": os.path.basename(self.repo)})
        self.assertEqual(status, 200, body)
        self.assertEqual(set(self.get_environment()["access"]), {"credential_layers", "mcp"})


class TestPlanningSeesNamesOnly(TestEnvironmentTestCase):
    def write_grafana_definition(self):
        write_file(self.claude_config_path, json.dumps({"mcpServers": {
            "grafana": {"type": "http", "url": "https://g.example/mcp", "headers": {"Authorization": GRAFANA_TOKEN}},
            "zendesk": {"command": "zd"}}}))

    def test_credential_names_file_has_name_source_and_set_without_values(self):
        write_file(self.project_env_path, "QA_ADMIN_TOKEN=%s\n" % SECRET)
        write_file(self.shared_env_path, "QA_LOCAL_URL=http://shared\n")
        job = self.send_and_wait(self.project_id, "plan it")
        self.assertEqual(job["state"], "ok", job)
        path = loop_file_path(self.repo, "internal", "credential-names.json")
        content = read_file(path)
        self.assertNotIn(SECRET, content)
        self.assertNotIn("http://shared", content)
        rows = {row["name"]: row for row in json.loads(content)}
        self.assertEqual(sorted(rows), ["QA_ADMIN_TOKEN", "QA_LOCAL_URL", "QA_ONLY_IN_COMMENT", "TEST_USER_1_EMAIL"])
        for row in rows.values():
            self.assertEqual(set(row), {"name", "source", "set"})
        self.assertEqual((rows["QA_ADMIN_TOKEN"]["source"], rows["QA_ADMIN_TOKEN"]["set"]), ("project", True))
        self.assertEqual((rows["QA_LOCAL_URL"]["source"], rows["QA_LOCAL_URL"]["set"]), ("shared", True))
        stdin = self.read_claude_calls()[-1]["stdin"]
        self.assertIn("credential-names.json", stdin)
        self.assertIn("Never ask the user for a password", stdin)
        self.assertNotIn(SECRET, stdin)

    def test_the_names_file_is_not_an_editable_path_and_the_file_guard_ignores_it(self):
        from harness_backend.services.planning_service import GUARD_IGNORED_PATHS, PLANNING_EDITABLE_PATHS
        self.assertFalse(any("credential-names" in path for path in PLANNING_EDITABLE_PATHS))
        self.assertIn(".specs/project/loop/internal", GUARD_IGNORED_PATHS)

    def test_with_a_grafana_definition_the_reply_gets_only_the_read_tools(self):
        self.write_grafana_definition()
        self.send_and_wait(self.project_id, "plan it")
        call = self.read_claude_calls()[-1]
        argv = call["argv"]
        self.assertIn("--strict-mcp-config", argv)
        config_path = argv[argv.index("--mcp-config") + 1]
        self.assertEqual(list(json.loads(read_file(config_path))["mcpServers"]), ["grafana"])
        allowed = list_flag_values(argv, "--allowedTools")
        self.assertEqual([tool for tool in allowed if tool.startswith("mcp__")], READ_TOOLS)
        denied = list_flag_values(argv, "--disallowedTools")
        self.assertIn("mcp__grafana__create_annotation", denied)
        self.assertIn("mcp__grafana__update_dashboard", denied)
        self.assertNotIn(GRAFANA_TOKEN, " ".join(argv) + call["stdin"])

    def test_without_a_server_definition_the_reply_has_no_mcp(self):
        self.send_and_wait(self.project_id, "plan it")
        argv = self.read_claude_calls()[-1]["argv"]
        self.assertEqual(argv[argv.index("--mcp-config") + 1], '{"mcpServers":{}}')
        self.assertFalse(any(tool.startswith("mcp__") for tool in list_flag_values(argv, "--allowedTools")))


if __name__ == "__main__":
    unittest.main()
