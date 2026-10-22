"""engine/mcp_access.py: MCP read access computed from a FAKE `~/.claude.json` (never the real one); strict config file with only the
chosen servers, credentials only in the file, an unreadable file = none found."""
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import grafana_access  # noqa: E402
import mcp_access  # noqa: E402

TOKEN = "token-that-must-not-reach-argv"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class McpAccessTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mcpaccess-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.claude_json = os.path.join(self.tmp, "claude.json")
        self.data_dir = os.path.join(self.tmp, "data")
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop(mcp_access.CLAUDE_CONFIG_VARIABLE, None)

    def servers(self, definitions):
        write(self.claude_json, json.dumps({"mcpServers": definitions, "projects": {}}))


class TestAvailableServers(McpAccessTestCase):
    FULL = {"grafana": {"type": "http", "url": "https://g.example/mcp", "headers": {"Authorization": TOKEN}},
            "mcp-mysql-dev": {"command": "node", "args": ["dev.js"], "env": {"DB_PASSWORD": TOKEN}},
            "mcp-mysql-prod": {"command": "node", "args": ["prod.js"]},
            "zendesk": {"command": "zd"}}

    def test_the_three_known_servers_with_read_tools_only(self):
        self.servers(self.FULL)
        rows = {row["server"]: row for row in mcp_access.available_servers(self.claude_json)}
        self.assertEqual(sorted(rows), ["grafana", "mcp-mysql-dev", "mcp-mysql-prod"])
        self.assertEqual(rows["mcp-mysql-dev"]["tools"], ["mcp__mcp-mysql-dev__mysql_query"])
        self.assertEqual(rows["mcp-mysql-prod"]["tools"], ["mcp__mcp-mysql-prod__mysql_query"])
        self.assertEqual(len(rows["grafana"]["tools"]), 7)
        self.assertTrue(all(row["configured"] for row in rows.values()))
        self.assertEqual(rows["grafana"]["tools"], list(grafana_access.READ_TOOLS))
        self.assertNotIn(TOKEN, json.dumps(rows))

    def test_a_missing_server_is_just_absent_and_describe_lists_it_as_not_configured(self):
        self.servers({"mcp-mysql-dev": {"command": "node"}, "grafana": {"type": "http"}})  # grafana without url/command: unusable
        self.assertEqual([row["server"] for row in mcp_access.available_servers(self.claude_json)], ["mcp-mysql-dev"])
        described = {row["server"]: row["configured"] for row in mcp_access.describe_servers(self.claude_json)}
        self.assertEqual(described, {"grafana": False, "mcp-mysql-dev": True, "mcp-mysql-prod": False})

    def test_an_unreadable_claude_json_is_none_found_without_an_exception(self):
        self.assertEqual(mcp_access.available_servers(os.path.join(self.tmp, "missing.json")), [])
        for broken in ("", "not json", "[1]", '{"mcpServers": []}', '{"mcpServers": {"grafana": "x"}}'):
            write(self.claude_json, broken)
            self.assertEqual(mcp_access.available_servers(self.claude_json), [], broken)

    def test_the_environment_variable_points_at_the_file(self):
        self.servers(self.FULL)
        os.environ[mcp_access.CLAUDE_CONFIG_VARIABLE] = self.claude_json
        self.assertEqual(len(mcp_access.available_servers()), 3)

    def test_allowed_tool_names_follow_the_given_servers(self):
        self.servers(self.FULL)
        rows = mcp_access.available_servers(self.claude_json)
        self.assertEqual(mcp_access.allowed_tool_names(["mcp-mysql-dev"]), ["mcp__mcp-mysql-dev__mysql_query"])
        self.assertEqual(len(mcp_access.allowed_tool_names(rows)), 9)
        self.assertEqual(mcp_access.allowed_tool_names(["zendesk", "nope"]), [])
        self.assertEqual(mcp_access.allowed_tool_names([]), [])

    def test_grafana_writers_are_denied_by_name_and_mysql_has_no_denied_list(self):
        denied = mcp_access.denied_write_tools(["grafana", "mcp-mysql-dev"])
        self.assertIn("mcp__grafana__update_dashboard", denied)
        self.assertFalse(set(denied) & set(mcp_access.read_tools("grafana")))
        self.assertEqual(mcp_access.denied_write_tools(["mcp-mysql-dev"]), [])


class TestStrictConfig(McpAccessTestCase):
    def test_the_file_holds_only_the_chosen_servers_with_their_credentials(self):
        self.servers(TestAvailableServers.FULL)
        path = os.path.join(self.tmp, "out", "mcp.json")
        written = mcp_access.write_strict_config(path, ["grafana", "mcp-mysql-prod", "zendesk"], self.claude_json)
        self.assertEqual(written, path)
        with open(path, encoding="utf-8") as handle:
            content = json.load(handle)
        self.assertEqual(sorted(content["mcpServers"]), ["grafana", "mcp-mysql-prod"])
        self.assertEqual(content["mcpServers"]["grafana"]["headers"]["Authorization"], TOKEN)
        self.assertEqual([name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")], [])

    def test_nothing_to_write_returns_an_empty_path(self):
        path = os.path.join(self.tmp, "mcp.json")
        self.assertEqual(mcp_access.write_strict_config(path, [], self.claude_json), "")
        self.servers({"zendesk": {"command": "zd"}})
        self.assertEqual(mcp_access.write_strict_config(path, ["grafana"], self.claude_json), "")
        self.assertEqual(mcp_access.write_strict_config("", ["grafana"], self.claude_json), "")
        self.assertFalse(os.path.exists(path))

    def test_build_access_writes_under_secrets_and_returns_names_never_the_content(self):
        self.servers(TestAvailableServers.FULL)
        access = mcp_access.build_access(self.data_dir, self.claude_json)
        self.assertEqual(access["config_path"], os.path.join(self.data_dir, ".secrets", "mcp-access.json"))
        self.assertEqual(len(access["allowed_tools"]), 9)
        self.assertIn("mcp__grafana__create_annotation", access["denied_tools"])
        self.assertNotIn(TOKEN, json.dumps({key: value for key, value in access.items()}))
        empty = mcp_access.build_access(self.data_dir, os.path.join(self.tmp, "none.json"))
        self.assertEqual(empty, {"servers": [], "allowed_tools": [], "denied_tools": [], "config_path": ""})


class TestGrafanaWrapper(McpAccessTestCase):
    def test_the_wrapper_keeps_its_public_functions(self):
        self.servers(TestAvailableServers.FULL)
        definition = grafana_access.read_server_definition(self.claude_json)
        self.assertEqual(definition["url"], "https://g.example/mcp")
        path = grafana_access.write_mcp_config(self.data_dir, self.claude_json)
        self.assertEqual(path, os.path.join(self.data_dir, ".secrets", "grafana-mcp.json"))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(list(json.load(handle)["mcpServers"]), ["grafana"])
        self.assertEqual(grafana_access.write_mcp_config("", self.claude_json), "")
        self.assertEqual(grafana_access.READ_TOOL_NAMES, mcp_access.GRAFANA_READ_TOOL_NAMES)


if __name__ == "__main__":
    unittest.main()
