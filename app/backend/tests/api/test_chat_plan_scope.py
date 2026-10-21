"""Chat scope `plan` (the plan & files dialog): same read-only job as the others (sonnet, US$ 1, Read/Grep/Glob, no MCP) with a prompt
prefix that limits the answers to the plan, and the files read from the work folder."""
import os
import unittest

from tests.api import test_commands_chat as chat_module


class ChatPlanScopeTests(chat_module.ChatTests):
    def test_plan_scope_argv_is_read_only_sonnet_and_prompt_is_limited(self):
        self.prepare()
        status, sent = self.send("which features are done?", scope={"type": "plan"})
        self.assertEqual(status, 202)
        self.wait_for_job(sent["job"])
        record, = self.records()
        argv = record["argv"]
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1")
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        denied = argv[argv.index("--disallowedTools") + 1:argv.index("--strict-mcp-config")]
        self.assertEqual(allowed, ["Read", "Grep", "Glob"])
        for write_tool in ("Bash", "Edit", "Write", "NotebookEdit"):
            self.assertNotIn(write_tool, allowed)
            self.assertIn(write_tool, denied)
        self.assertEqual(argv[argv.index("--strict-mcp-config") + 1:], ["--mcp-config", '{"mcpServers":{}}'])
        self.assertIn("only answers questions about the project plan", record["stdin"])
        self.assertIn(os.path.join(self.fixture.work, ".specs", "features"), record["stdin"])
        self.assertIn("which features are done?", record["stdin"])
        self.assertEqual(os.path.normcase(os.path.realpath(record["cwd"])), os.path.normcase(os.path.realpath(self.fixture.work)))

    def test_plan_scope_is_stored_without_agent_or_turn(self):
        self.prepare()
        status, sent = self.send("hi", scope={"type": "plan", "agent_id": "x", "turn": 3})
        self.assertEqual(status, 202)
        self.wait_for_job(sent["job"])
        status, thread = self.fixture.call("/api/chat/thread/" + sent["thread"])
        self.assertEqual(thread["scope"], {"type": "plan", "agent_id": None, "turn": None})


# the helpers (fake claude, send, wait_for_job) are inherited; the inherited tests already run in their own module
for inherited_name in [name for name in vars(chat_module.ChatTests) if name.startswith("test_")]:
    setattr(ChatPlanScopeTests, inherited_name, None)


if __name__ == "__main__":
    unittest.main()
