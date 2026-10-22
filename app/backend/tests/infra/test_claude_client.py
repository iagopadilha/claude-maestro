"""ClaudeClient: `claude -p` argv, run with stdin, JSON parsing and environment without secrets. Uses a FAKE claude (.py)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from harness_backend.infrastructure.claude_client import (DISABLED_PUSH_URL, EMPTY_MCP_CONFIG, ClaudeClient, build_push_disabled_variables,
                                                          redact_secrets)
from harness_backend.infrastructure.process_runner import ProcessRunner

FAKE_CLAUDE = r'''
import json, os, sys
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
stdin_text = sys.stdin.read()
if mode == "ok":
    print(json.dumps({"type": "result", "is_error": False, "result": "echo: " + stdin_text, "total_cost_usd": 0.0123,
                      "session_id": "session-1", "argv": sys.argv[1:], "variables": sorted(os.environ)}))
elif mode == "error_json":
    print(json.dumps({"type": "result", "is_error": True, "result": "limit reached token=abc123"}))
elif mode == "text":
    print("this is not json")
    print("last error line", file=sys.stderr)
    sys.exit(1)
elif mode == "slow":
    import time
    time.sleep(60)
'''


class ClaudeClientCase(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="claude-client-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.binary = os.path.join(self.folder, "fake_claude.py")
        with open(self.binary, "w", encoding="utf-8") as handle:
            handle.write(FAKE_CLAUDE)
        self.client = ClaudeClient(ProcessRunner(), self.binary)

    def set_variable(self, name, value):
        previous = os.environ.get(name)
        os.environ[name] = value
        self.addCleanup(lambda: os.environ.pop(name, None) if previous is None else os.environ.__setitem__(name, previous))


class TestBuildCommand(ClaudeClientCase):
    def test_py_binary_runs_with_the_current_python_and_flags_in_order(self):
        argv = self.client.build_command("sonnet", 1, ["Read", "Grep"], ["Bash"], no_mcp=True, resume_session="s-9",
                                         fork_session=True, prompt_argument="/orchestrator roadmap", permission_mode="acceptEdits")
        self.assertEqual(argv[:4], [sys.executable, self.binary, "-p", "/orchestrator roadmap"])
        self.assertEqual(argv[argv.index("--output-format") + 1:argv.index("--output-format") + 3], ["stream-json", "--verbose"])
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1")
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "acceptEdits")
        self.assertEqual(argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")], ["Read", "Grep"])
        self.assertEqual(argv[argv.index("--mcp-config") + 1], EMPTY_MCP_CONFIG)
        self.assertIn("--strict-mcp-config", argv)
        self.assertEqual(argv[-3:], ["--resume", "s-9", "--fork-session"])

    def test_a_given_mcp_config_replaces_the_empty_one_and_stays_strict(self):
        argv = self.client.build_command("", 1, no_mcp=True, mcp_config_path="/x/grafana-mcp.json")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], "/x/grafana-mcp.json")
        self.assertIn("--strict-mcp-config", argv)

    def test_without_optionals_adds_no_empty_flags(self):
        argv = self.client.build_command("", 0, no_mcp=False)
        self.assertEqual(argv, [sys.executable, self.binary, "-p", "--output-format", "stream-json", "--verbose"])

    def test_fork_without_session_is_ignored(self):
        self.assertNotIn("--fork-session", self.client.build_command("sonnet", 1, fork_session=True))

    def test_empty_binary_uses_claude_from_path(self):
        argv = ClaudeClient(ProcessRunner()).build_command("sonnet", 1)
        self.assertIn("claude", os.path.basename(argv[0]).lower())


class TestRunPrompt(ClaudeClientCase):
    def test_success_returns_reply_cost_and_session(self):
        result = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "question", 60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "echo: question")
        self.assertEqual(result["cost_usd"], 0.0123)
        self.assertEqual(result["session_id"], "session-1")
        self.assertEqual(result["error"], "")
        self.assertEqual(result["raw"]["argv"][:2], ["-p", "--output-format"])

    def test_claude_environment_carries_no_secret_but_carries_claudes_own(self):
        self.set_variable("LOOP_TEST_API_TOKEN", "secret")
        self.set_variable("LOOP_SMTP_PASSWORD", "secret")
        self.set_variable("LOOP_MONITOR_URL", "http://127.0.0.1:1/")
        self.set_variable("ANTHROPIC_API_KEY", "claude-key")
        self.set_variable("LOOP_TEST_NORMAL", "1")
        variables = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "", 60)["raw"]["variables"]
        for name in ("LOOP_TEST_API_TOKEN", "LOOP_SMTP_PASSWORD", "LOOP_MONITOR_URL"):
            self.assertNotIn(name, variables)
        self.assertIn("ANTHROPIC_API_KEY", variables)
        self.assertIn("LOOP_TEST_NORMAL", variables)

    def test_is_error_becomes_a_redacted_error(self):
        self.set_variable("FAKE_CLAUDE_MODE", "error_json")
        result = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertFalse(result["ok"])
        self.assertIn("limit reached", result["error"])
        self.assertNotIn("abc123", result["error"])

    def test_output_that_is_not_json_uses_the_last_stderr_line(self):
        self.set_variable("FAKE_CLAUDE_MODE", "text")
        result = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertEqual((result["ok"], result["error"], result["raw"]), (False, "last error line", {}))

    def test_timeout(self):
        self.set_variable("FAKE_CLAUDE_MODE", "slow")
        result = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "", 1)
        self.assertEqual((result["ok"], result["error"]), (False, "timed out"))

    def test_missing_binary_does_not_raise(self):
        client = ClaudeClient(ProcessRunner(), os.path.join(self.folder, "does-not-exist.exe"))
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 10)
        self.assertFalse(result["ok"])
        self.assertTrue(result["error"])
        self.assertFalse(client.is_available())


LIMITED_CLAUDE = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "result": "Claude AI usage limit reached|1767225600"}))
sys.exit(1)
'''


UNAUTHORIZED_CLAUDE = r'''
import json, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": True, "result": "API Error: 403 permission_error (error type oauth_org_not_allowed, HTTP 403)"}))
sys.exit(1)
'''


class TestFallbackBinaries(ClaudeClientCase):
    def setUp(self):
        super().setUp()
        self.limited = os.path.join(self.folder, "limited_claude.py")
        with open(self.limited, "w", encoding="utf-8") as handle:
            handle.write(LIMITED_CLAUDE)
        self.unauthorized = os.path.join(self.folder, "unauthorized_claude.py")
        with open(self.unauthorized, "w", encoding="utf-8") as handle:
            handle.write(UNAUTHORIZED_CLAUDE)

    def test_an_unusable_account_runs_the_same_argv_with_the_fallback(self):
        client = ClaudeClient(ProcessRunner(), self.unauthorized, fallback_binaries=[self.binary])
        with self.assertLogs("harness_backend.infrastructure.claude_client", level="WARNING") as logged:
            result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "question", 60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["binary"], self.binary)
        self.assertIn("unusable", logged.output[0])

    def test_every_command_unusable_returns_the_last_error_with_its_binary(self):
        client = ClaudeClient(ProcessRunner(), self.unauthorized, fallback_binaries=[self.limited])
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertFalse(result["ok"])
        self.assertEqual(result["binary"], self.limited)
        client = ClaudeClient(ProcessRunner(), self.unauthorized, fallback_binaries=[self.unauthorized])
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertEqual(result["binary"], self.unauthorized)
        self.assertIn("oauth_org_not_allowed", result["error"])

    def test_usage_limit_runs_the_same_argv_with_the_fallback(self):
        client = ClaudeClient(ProcessRunner(), self.limited, fallback_binaries=[self.binary])
        result = client.run_prompt(client.build_command("sonnet", 1, prompt_argument="/x"), self.folder, "question", 60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["binary"], self.binary)
        self.assertEqual(result["raw"]["argv"][:2], ["-p", "/x"])
        self.assertEqual(result["raw"]["argv"][result["raw"]["argv"].index("--model") + 1], "sonnet")

    def test_other_errors_never_switch(self):
        self.set_variable("FAKE_CLAUDE_MODE", "text")
        client = ClaudeClient(ProcessRunner(), self.binary, fallback_binaries=[self.limited])
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertEqual((result["ok"], result["binary"]), (False, self.binary))

    def test_every_command_limited_returns_the_last_error(self):
        client = ClaudeClient(ProcessRunner(), self.limited, fallback_binaries=[self.limited])
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 60)
        self.assertFalse(result["ok"])
        self.assertIn("usage limit", result["error"])

    @unittest.skipUnless(os.name == "nt", "cmd.exe wrappers exist only on Windows")
    def test_cmd_wrapper_gets_a_multi_line_prompt_whole_through_stdin(self):
        wrapper = os.path.join(self.folder, "cc.cmd")
        with open(wrapper, "w", newline="\r\n") as handle:
            handle.write('@echo off\n"%s" "%s" %%*\n' % (sys.executable, self.binary))
        client = ClaudeClient(ProcessRunner(), wrapper)
        result = client.run_prompt(client.build_command("sonnet", 1, prompt_argument="line 1\nline 2"), self.folder, "", 60)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["reply"], "echo: line 1\nline 2")


class TestExtraEnvAndPushDisabled(ClaudeClientCase):
    def test_extra_env_reaches_claude(self):
        variables = self.client.run_prompt(self.client.build_command("sonnet", 1), self.folder, "", 60,
                                           extra_env={"LOOP_TEST_EXTRA": "1"})["raw"]["variables"]
        self.assertIn("LOOP_TEST_EXTRA", variables)

    @unittest.skipUnless(shutil.which("git"), "git is not on the PATH")
    def test_push_disabled_swaps_the_pushurl_of_every_remote_without_writing_to_the_repo(self):
        repo = os.path.join(self.folder, "repo")
        os.makedirs(repo)
        for args in (["init", "-q"], ["remote", "add", "origin", "https://example.invalid/a.git"], ["remote", "add", "other", "https://example.invalid/b.git"]):
            subprocess.run(["git", "-C", repo] + args, check=True, capture_output=True)
        variables = self.client.build_push_disabled_env(repo)
        env = dict(os.environ, **variables)
        for remote in ("origin", "other"):
            pushurl = subprocess.run(["git", "-C", repo, "config", "remote.%s.pushurl" % remote], capture_output=True, text=True, env=env).stdout.strip()
            self.assertEqual(pushurl, DISABLED_PUSH_URL)
        without_variables = subprocess.run(["git", "-C", repo, "config", "remote.origin.pushurl"], capture_output=True, text=True)
        self.assertEqual(without_variables.stdout.strip(), "", "nothing written to the repo config")

    def test_variables_add_to_the_existing_git_config_count(self):
        variables = build_push_disabled_variables(["origin"], {"GIT_CONFIG_COUNT": "2"})
        self.assertEqual(variables, {"GIT_CONFIG_KEY_2": "remote.origin.pushurl", "GIT_CONFIG_VALUE_2": DISABLED_PUSH_URL, "GIT_CONFIG_COUNT": "3"})
        self.assertEqual(build_push_disabled_variables([], {}), {"GIT_CONFIG_COUNT": "0"})


class TestHelpers(unittest.TestCase):
    def test_parse_json_output_tolerates_lines_before(self):
        parsed = ClaudeClient.parse_json_output('some warning\n{"result": "x"}')
        self.assertEqual((parsed["ok"], parsed["result"], parsed["is_error"], parsed["total_cost_usd"]), (True, "x", False, None))
        self.assertEqual(ClaudeClient.parse_json_output("[1, 2]"), {})
        self.assertEqual(ClaudeClient.parse_json_output(""), {})

    def test_redact_secrets(self):
        text = redact_secrets("token=abc password: xyz " + "A" * 50, 200)
        self.assertNotIn("abc", text)
        self.assertNotIn("xyz", text)
        self.assertNotIn("A" * 50, text)
        self.assertEqual(len(redact_secrets("x" * 30, 10)), 10)

    def test_build_env_without_secrets_is_a_new_dict(self):
        env = ClaudeClient(ProcessRunner()).build_env_without_secrets()
        self.assertIsNot(env, os.environ)
        self.assertTrue(all(isinstance(value, str) for value in env.values()))
        json.dumps(env)


if __name__ == "__main__":
    unittest.main()
