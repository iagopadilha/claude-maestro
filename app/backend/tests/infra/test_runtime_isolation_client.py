"""ClaudeClient with runtime isolation: argv and environment of app jobs, on and off (temp data dir, fake home, fake claude)."""
import json
import os
import shutil
import sys
import tempfile
import unittest

from harness_backend.engine import runtime_isolation as iso
from harness_backend.infrastructure.claude_client import ClaudeClient
from harness_backend.infrastructure.process_runner import ProcessRunner

FAKE_CLAUDE = r'''
import json, os, sys
sys.stdin.read()
print(json.dumps({"type": "result", "is_error": False, "result": "ok", "total_cost_usd": 0, "session_id": "s",
                  "isolated": os.environ.get("LOOP_ISOLATED"), "skills_file": os.environ.get("LOOP_SKILLS_FILE")}))
'''
ISOLATION_FLAGS = ("--setting-sources", "--settings")


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


class IsolatedClientCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="isolation-client-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = os.path.join(self.root, "data")
        self.home = os.path.join(self.root, "home", ".claude")
        self.plugin = os.path.join(self.root, "plugin")
        self.guard = os.path.join(self.plugin, "skills", "orchestrator", "hooks", "guard.py")
        write(self.guard, "# guard")
        write(os.path.join(self.plugin, "skills", "orchestrator", "SKILL.md"), "orchestrator")
        write(os.path.join(self.plugin, "agents", "verifier.md"), "agent")
        write(os.path.join(self.home, "skills", "backend-dev", "SKILL.md"), "user skill")
        self.binary = os.path.join(self.root, "fake_claude.py")
        write(self.binary, FAKE_CLAUDE)
        for name, value in (("LOOP_DATA_DIR", self.data), ("LOOP_CLAUDE_HOME", self.home)):
            previous = os.environ.get(name)
            os.environ[name] = value
            self.addCleanup(lambda n=name, p=previous: os.environ.pop(n, None) if p is None else os.environ.__setitem__(n, p))
        iso.clear_memo()
        self.addCleanup(iso.clear_memo)
        self.client = ClaudeClient(ProcessRunner(), self.binary, guard_path=self.guard)

    def set_isolation(self, enabled):
        write(os.path.join(self.data, "config.json"), json.dumps({"loop_isolation": enabled}))

    def settings_of(self, argv):
        with open(argv[argv.index("--settings") + 1], encoding="utf-8") as handle:  # a file: inline JSON breaks under a cmd.exe wrapper
            return json.load(handle)

    def command_of(self, argv):
        return self.settings_of(argv)["hooks"]["PreToolUse"][0]["hooks"][0]["command"]


class TestBuildCommandIsolation(IsolatedClientCase):
    def test_on_adds_the_flags_and_the_job_profile_guard(self):
        argv = self.client.build_command("sonnet", 1, hook_profile="planning", prompt_argument="/x")
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")
        self.assertEqual(argv[argv.index("--add-dir=%s" % os.path.join(self.data, "runtime", "bundle"))], "--add-dir=%s" % os.path.join(self.data, "runtime", "bundle"))
        self.assertTrue(self.command_of(argv).endswith("--profile planning"))
        self.assertLess(argv.index("--settings"), argv.index("--strict-mcp-config"))

    def test_job_without_a_hook_profile_gets_the_guard_with_profile_auto(self):
        argv = self.client.build_command("sonnet", 1)
        self.assertTrue(self.command_of(argv).endswith("--profile auto"))

    def test_user_auto_mode_is_carried(self):
        write(os.path.join(self.home, "settings.json"), json.dumps({"autoMode": {"allow": ["a"]}, "permissions": {"allow": ["Bash"]}}))
        settings = self.settings_of(self.client.build_command("sonnet", 1, hook_profile="spec"))
        self.assertEqual(settings["autoMode"], {"allow": ["a"]})
        self.assertNotIn("permissions", settings)

    def test_build_command_does_not_build_the_bundle(self):
        self.client.build_command("sonnet", 1, hook_profile="spec")
        self.assertFalse(os.path.exists(os.path.join(self.data, "runtime", "bundle")), "the request path stays cheap: the job thread builds it")

    def test_off_is_the_old_argv_byte_for_byte(self):
        self.set_isolation(False)
        argv = self.client.build_command("sonnet", 1, ["Read"], ["Bash"], hook_profile="planning", permission_mode="acceptEdits",
                                         prompt_argument="/x", resume_session="s1")
        guard = os.path.abspath(self.guard).replace("\\", "/")
        old_settings = json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": 'python "%s" --profile planning' % guard}]}]}})
        self.assertEqual(argv, [sys.executable, self.binary, "-p", "/x", "--output-format", "stream-json", "--verbose", "--model", "sonnet",
                                "--max-budget-usd", "1", "--permission-mode", "acceptEdits", "--allowedTools", "Read", "--disallowedTools", "Bash",
                                "--settings", old_settings, "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--resume", "s1"])

    def test_off_without_a_hook_profile_adds_no_settings(self):
        self.set_isolation(False)
        argv = self.client.build_command("sonnet", 1)
        self.assertFalse([item for item in argv if item in ISOLATION_FLAGS or item.startswith("--add-dir")])


class TestRunPromptIsolation(IsolatedClientCase):
    def test_on_builds_the_bundle_and_exports_the_variables(self):
        result = self.client.run_prompt(self.client.build_command("", 0), self.root, "hi", 30)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["raw"]["isolated"], "1")
        skills_file = result["raw"]["skills_file"]
        self.assertTrue(skills_file.startswith(os.path.join(self.data, "runtime")))
        self.assertIn("executor", read_json(skills_file))
        bundle = os.path.join(self.data, "runtime", "bundle", ".claude")
        self.assertEqual(os.listdir(os.path.join(bundle, "agents")), ["verifier.md"])
        self.assertIn("backend-dev", os.listdir(os.path.join(bundle, "skills")))

    def test_off_exports_nothing_and_builds_nothing(self):
        self.set_isolation(False)
        result = self.client.run_prompt(self.client.build_command("", 0), self.root, "hi", 30)
        self.assertTrue(result["ok"], result)
        self.assertIsNone(result["raw"]["isolated"])
        self.assertIsNone(result["raw"]["skills_file"])
        self.assertFalse(os.path.exists(os.path.join(self.data, "runtime")))

    def test_caller_extra_env_is_kept(self):
        env = self.client.isolation_variables(self.root, {"GIT_CONFIG_COUNT": "1"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "1")
        self.assertEqual(env["LOOP_ISOLATED"], "1")

    def test_subproject_skills_of_the_job_folder_reach_its_allowlist_file(self):
        repo = os.path.join(self.root, "repo")
        write(os.path.join(repo, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"subprojects": [{"path": "w", "skill": "idata-guia-estilo"}]}))
        os.makedirs(os.path.join(repo, "sub"))
        env = self.client.isolation_variables(os.path.join(repo, "sub"), None)
        self.assertIn("idata-guia-estilo", read_json(env["LOOP_SKILLS_FILE"])["executor"])


if __name__ == "__main__":
    unittest.main()
