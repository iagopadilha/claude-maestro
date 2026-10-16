#!/usr/bin/env python3
"""Zero-cost tests of the orchestrator guard (stdlib unittest, no Claude, no network).

Run:  python -m unittest discover -s plugin/skills/orchestrator/hooks -p "test_guard.py" -v

Each test runs the real `guard.py` as the hook does (JSON on stdin, exit 2 = blocked) against a temporary project,
with LOOP_DATA_DIR pointing at an empty temporary folder so no real app or notification channel is ever touched.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
BLOCKED = 2
PASSED = 0


def read_text(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def load_guard_module():
    spec = importlib.util.spec_from_file_location("guard_under_test", GUARD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class GuardTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "project")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.project, ".specs", "project"))
        os.makedirs(self.data_dir)
        self.env = dict(os.environ)
        for name in ("LOOP_APP_HOME", "LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "GUARD_PROD_HINTS"):
            self.env.pop(name, None)
        self.env["LOOP_DATA_DIR"] = self.data_dir

    # ---- helpers
    def specs(self, *parts):
        return os.path.join(self.project, ".specs", *parts)

    def write_json(self, path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle)

    def allow(self, *rules, flat=False):
        folder = self.specs("project") if flat else self.specs("project", "loop", "json")
        self.write_json(os.path.join(folder, "PERMISSIONS.json"),
                        {"allow": [{"rule": rule, "quote": "the user said so", "at": "2026-10-15"} for rule in rules]})

    def run_guard(self, tool, tool_input, profile="executor", **extra):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project}
        payload.update(extra)
        result = subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload),
                                capture_output=True, text=True, env=self.env)
        return result.returncode

    def bash(self, command, profile="executor"):
        return self.run_guard("Bash", {"command": command}, profile)

    def edit(self, relative, profile="executor"):
        return self.run_guard("Edit", {"file_path": os.path.join(self.project, relative)}, profile)


class TestLayoutResolution(GuardTestCase):
    """PERMISSIONS.json, SCOPE.json and guard.log: new layout first, old flat plugin layout as fallback."""

    def test_permissions_in_new_layout_release_a_rule(self):
        self.assertEqual(self.bash("git push origin feat/x"), BLOCKED)
        self.allow("git-push")
        self.assertEqual(self.bash("git push origin feat/x"), PASSED)

    def test_permissions_in_flat_layout_still_work_until_migrated(self):
        self.allow("git-push", flat=True)
        self.assertEqual(self.bash("git push origin feat/x"), PASSED)

    def test_new_layout_wins_over_flat_layout(self):
        self.allow("git-push", flat=True)
        self.write_json(self.specs("project", "loop", "json", "PERMISSIONS.json"), {"allow": []})
        self.assertEqual(self.bash("git push origin feat/x"), BLOCKED)

    def test_project_deny_rule_read_from_new_layout(self):
        self.write_json(self.specs("project", "loop", "json", "PERMISSIONS.json"), {"deny_bash_regex": [r"\bcurl\b.*tracking"]})
        self.assertEqual(self.bash("curl https://api.example.com/tracking/new"), BLOCKED)
        self.assertEqual(self.bash("curl https://api.example.com/orders"), PASSED)
        self.assertEqual(self.bash("curl https://api.example.com/tracking/new", "orchestrator"), BLOCKED)

    def test_scope_in_new_layout(self):
        self.write_json(self.specs("project", "loop", "json", "SCOPE.json"), {"features": {"a": ["apps/a"]}})
        self.assertEqual(self.edit("apps/a/x.ts"), PASSED)
        self.assertEqual(self.edit("apps/b/x.ts"), BLOCKED)

    def test_scope_in_flat_layout_still_limits_the_executor(self):
        self.write_json(self.specs("project", "SCOPE.json"), {"features": {"a": ["apps/a"]}})
        self.assertEqual(self.edit("apps/a/x.ts"), PASSED)
        self.assertEqual(self.edit("apps/b/x.ts"), BLOCKED)

    def test_guard_log_goes_to_loop_internal_in_a_fresh_project(self):
        self.assertEqual(self.bash("git commit -m x"), BLOCKED)
        self.assertTrue(os.path.isfile(self.specs("project", "loop", "internal", "guard.log")))
        self.assertFalse(os.path.exists(self.specs("project", "guard.log")))

    def test_guard_log_keeps_the_flat_file_when_the_project_was_not_migrated(self):
        flat_log = self.specs("project", "guard.log")
        open(flat_log, "w").close()
        self.assertEqual(self.bash("git commit -m x"), BLOCKED)
        self.assertIn("git-write", read_text(flat_log))
        self.assertFalse(os.path.exists(self.specs("project", "loop", "internal", "guard.log")))


class TestStateWrite(GuardTestCase):
    """The whole .specs/project/loop/ folder is the orchestrator's; TEST-ENVIRONMENT.md stays editable."""

    def test_subagents_cannot_write_anything_in_the_loop_folder(self):
        for relative in (".specs/project/loop/ANSWERS.md", ".specs/project/loop/VALIDATE.md", ".specs/project/loop/NOTICE.txt",
                         ".specs/project/loop/json/LOOP.json", ".specs/project/loop/json/SCOPE.json",
                         ".specs/project/loop/internal/turns/001.json", ".specs/project/RULES.md", ".specs/project/ROADMAP.md",
                         ".specs/project/FLOWS.md"):
            for profile in ("executor", "spec", "verifier"):
                self.assertEqual(self.edit(relative, profile), BLOCKED, (relative, profile))

    def test_old_flat_state_files_stay_protected(self):
        for relative in (".specs/project/PERMISSIONS.json", ".specs/project/SCOPE.json", ".specs/project/ANSWERS.md",
                         ".specs/project/LOOP.json", ".specs/project/rules.tests.json"):
            self.assertEqual(self.edit(relative), BLOCKED, relative)

    def test_shell_writes_into_the_loop_folder_are_blocked(self):
        self.assertEqual(self.bash("echo x > .specs/project/loop/VALIDATE.md"), BLOCKED)
        self.assertEqual(self.bash("echo x | tee .specs/project/loop/json/PERMISSIONS.json"), BLOCKED)
        self.assertEqual(self.run_guard("PowerShell", {"command": "Set-Content -Path .specs/project/loop/ANSWERS.md -Value x"}), BLOCKED)
        self.assertEqual(self.bash("sed -i 's/a/b/' .specs/project/loop/DEPENDENCIES.md"), BLOCKED)

    def test_what_stays_writable(self):
        self.assertEqual(self.edit(".specs/project/TEST-ENVIRONMENT.md"), PASSED)
        self.assertEqual(self.edit(".specs/features/coupon/spec.md", "spec"), PASSED)
        self.assertEqual(self.edit(".specs/project/loopback-notes.md"), PASSED)  # a similar name is not the loop folder
        self.assertEqual(self.edit(".specs/project/loop/ANSWERS.md", "orchestrator"), PASSED)
        self.assertEqual(self.edit(".specs/project/loop/json/SCOPE.json", "orchestrator"), PASSED)
        self.assertEqual(self.bash("echo x > .specs/project/loop/VALIDATE.md", "orchestrator"), PASSED)
        self.assertEqual(self.bash("dotnet build 2>&1 > build.log"), PASSED)


class TestRoadmapApproval(GuardTestCase):
    """ROADMAP-APPROVED.json is the user's click in the panel: hard rule, even for the orchestrator."""

    def test_nobody_writes_the_approval_file(self):
        for relative in (".specs/project/loop/json/ROADMAP-APPROVED.json", ".specs/project/ROADMAP-APPROVED.json"):
            for profile in ("orchestrator", "executor", "spec", "verifier"):
                self.assertEqual(self.edit(relative, profile), BLOCKED, (relative, profile))
        self.assertEqual(self.run_guard("Write", {"file_path": self.specs("project", "loop", "json", "ROADMAP-APPROVED.json"),
                                                  "content": "{}"}, "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("echo {} > .specs/project/loop/json/ROADMAP-APPROVED.json", "orchestrator"), BLOCKED)

    def test_not_even_a_user_permission_releases_it(self):
        self.allow("approval-write", "state-write")
        self.assertEqual(self.edit(".specs/project/loop/json/ROADMAP-APPROVED.json", "orchestrator"), BLOCKED)

    def test_reading_it_and_editing_the_roadmap_pass(self):
        self.assertEqual(self.run_guard("Read", {"file_path": self.specs("project", "loop", "json", "ROADMAP-APPROVED.json")}, "orchestrator"), PASSED)
        self.assertEqual(self.edit(".specs/project/ROADMAP.md", "orchestrator"), PASSED)
        self.assertEqual(self.edit(".specs/project/FLOWS.md", "orchestrator"), PASSED)


class TestStateScripts(GuardTestCase):
    """Scripts that write project state: only the orchestrator, in the legacy script form and the `harness.py` form."""

    WRITERS = (
        'python "C:/tools/loop-harness/app/harness.py" rules write . --id R-04 --phrase "never" --path Taxes',
        "python /opt/app/harness.py audit /repo --write",
        "python /opt/app/harness.py audit /repo --apply",
        "python /opt/app/harness.py graph /repo --write",
        'python "/opt/app/harness.py" state loop /repo --status continue --turn 3',
        "python /opt/app/harness.py state migrate /repo",
        "python rules.py write . --id R-04",
        "python audit_deps.py . --apply",
        "python graph_view.py . --write",
        "python loop_state.py loop . --status done --turn 2",
    )
    READERS = (
        "python /opt/app/harness.py graph /repo",
        "python /opt/app/harness.py graph /repo --short",
        "python /opt/app/harness.py audit /repo",
        "python /opt/app/harness.py rules test /repo",
        "python /opt/app/harness.py rules list /repo",
        "python /opt/app/harness.py state validate /repo",
        "python /opt/app/harness.py state next /repo --wave 2",
    )

    def test_subagents_cannot_write_state_through_scripts(self):
        for command in self.WRITERS:
            for profile in ("executor", "spec", "verifier"):
                self.assertEqual(self.bash(command, profile), BLOCKED, (command, profile))

    def test_read_only_forms_pass(self):
        for command in self.READERS:
            self.assertEqual(self.bash(command, "spec"), PASSED, command)

    def test_the_orchestrator_runs_them(self):
        for command in self.WRITERS:
            self.assertEqual(self.bash(command, "orchestrator"), PASSED, command)


class TestDeliveryAndLoopRun(GuardTestCase):
    def test_only_the_orchestrator_delivers(self):
        for command in ("python /opt/app/harness.py deliver /repo", 'python "C:/app/harness.py" deliver . --draft', "python delivery.py ."):
            self.assertEqual(self.bash(command), BLOCKED, command)
            self.assertEqual(self.bash(command, "verifier"), BLOCKED, command)
            self.assertEqual(self.bash(command, "orchestrator"), PASSED, command)
        self.assertEqual(self.bash("python /opt/app/harness.py deliverables"), PASSED)  # similar word, not the subcommand

    def test_subagents_never_start_the_loop_or_the_panel(self):
        for command in ("python /opt/app/harness.py loop /repo --wave 2", "python /opt/app/harness.py check /repo",
                        "python /opt/app/harness.py serve --project /repo", "python run_loop.py /repo --check"):
            self.assertEqual(self.bash(command), BLOCKED, command)
            self.assertEqual(self.bash(command, "orchestrator"), PASSED, command)
        self.assertEqual(self.bash("python /opt/app/harness.py doctor"), PASSED)
        self.assertEqual(self.bash("npm run loop"), PASSED)


class TestPanelTrigger(GuardTestCase):
    """The panel's trigger endpoints are the user's click: hard rule for every profile."""

    TRIGGERS = (
        "curl -X POST http://127.0.0.1:47800/p/a1b2c3/api/loop/start",
        "curl -X POST http://127.0.0.1:47800/p/a1b2c3/api/loop/check -d {}",
        'curl -X POST "http://127.0.0.1:47801/p/a1b2c3/api/roadmap/approve" -d "{\\"confirm\\":true}"',
        "Invoke-RestMethod -Method Post http://localhost:47800/p/x9/api/action/run",
        "curl -X POST http://127.0.0.1:47800/p/a1b2c3/api/answer -d '{\"option\":1}'",
        "curl -X POST http://127.0.0.1:47800/p/a1b2c3/api/resume",
        "curl -X POST http://127.0.0.1:47800/p/a1b2c3/api/instruction -d '{\"text\":\"go\"}'",
    )

    def test_nobody_clicks_for_the_user(self):
        for command in self.TRIGGERS:
            for profile in ("orchestrator", "executor", "verifier"):
                self.assertEqual(self.bash(command, profile), BLOCKED, (command, profile))

    def test_not_even_a_user_permission_releases_it(self):
        self.allow("panel-trigger")
        self.assertEqual(self.bash(self.TRIGGERS[0], "orchestrator"), BLOCKED)

    def test_reading_the_panel_and_the_project_routes_pass(self):
        self.assertEqual(self.bash("curl http://127.0.0.1:47800/p/a1b2c3/api/state", "orchestrator"), PASSED)
        self.assertEqual(self.bash("curl http://127.0.0.1:47800/p/a1b2c3/api/roadmap", "orchestrator"), PASSED)
        self.assertEqual(self.bash("curl -X POST http://localhost:3000/api/loop/start", "verifier"), PASSED)  # the project's own route
        self.assertEqual(self.bash('grep -rn "/p/<id>/api/loop/start" docs', "orchestrator"), PASSED)  # reading the docs is not a call


class TestSecrets(GuardTestCase):
    def test_app_data_env_and_token_are_credentials(self):
        home_env = os.path.join(os.path.expanduser("~"), ".loop-harness", ".env")
        token = os.path.join(os.path.expanduser("~"), ".loop-harness", "app.json")
        for path in (home_env, token):
            for profile in ("orchestrator", "executor"):
                self.assertEqual(self.run_guard("Read", {"file_path": path}, profile), BLOCKED, (path, profile))
        self.assertEqual(self.bash("cat ~/.loop-harness/app.json", "orchestrator"), BLOCKED)

    def test_non_secret_files_pass(self):
        self.assertEqual(self.run_guard("Read", {"file_path": os.path.join("~", ".loop-harness", "config.json")}, "orchestrator"), PASSED)
        self.assertEqual(self.run_guard("Read", {"file_path": "/plugin/shared/.env.example"}, "executor"), PASSED)
        self.assertEqual(self.run_guard("Read", {"file_path": "/plugin/shared/.env"}, "executor"), BLOCKED)


class TestExistingHardRules(GuardTestCase):
    """Rules that already existed keep their behaviour."""

    def test_push_rules(self):
        self.allow("git-push", "push-protected", "push-force")
        self.assertEqual(self.bash("git push origin feat/coupon", "orchestrator"), PASSED)
        for branch in ("developer", "main", "master", "develop", "release", "production", "dev"):
            self.assertEqual(self.bash("git push origin %s" % branch, "orchestrator"), BLOCKED, branch)
        self.assertEqual(self.bash("git push --force origin feat/x", "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("git push origin +feat/x", "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("git push origin feat/developer-tools", "orchestrator"), PASSED)

    def test_git_and_production(self):
        self.assertEqual(self.bash("git commit -m x"), BLOCKED)
        self.assertEqual(self.bash("git commit -m x", "orchestrator"), PASSED)
        self.assertEqual(self.bash("git commit -am x", "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("git status"), PASSED)
        self.assertEqual(self.run_guard("mcp__prod-db__write_rows", {}, "orchestrator"), BLOCKED)
        self.assertEqual(self.run_guard("mcp__prod-db__read_rows", {}, "orchestrator"), PASSED)
        self.assertEqual(self.run_guard("mcp__tickets__update_ticket", {}, "orchestrator"), BLOCKED)

    def test_profile_auto(self):
        payload = {"agent_type": "loop-engineering-harness:executor-back"}
        self.assertEqual(self.run_guard("Bash", {"command": "git commit -m x"}, "auto", **payload), BLOCKED)
        self.assertEqual(self.run_guard("Bash", {"command": "git commit -m x"}, "auto"), PASSED)  # main session: nothing enforced


class TestFailOpenNotifier(GuardTestCase):
    """The fail-open alert logs, marks once an hour and calls `<app>/harness.py notify` when the app is found."""

    def make_fake_app(self):
        app = os.path.join(self.tmp, "app")
        os.makedirs(app)
        record = os.path.join(self.tmp, "notify-called.json")
        with open(os.path.join(app, "harness.py"), "w", encoding="utf-8") as handle:
            handle.write("import json, sys\nopen(%r, 'w').write(json.dumps(sys.argv[1:]))\n" % record)
        return app, record

    def with_env(self, values, action):
        old = dict(os.environ)
        os.environ.clear()
        os.environ.update(self.env)
        os.environ.update(values)
        try:
            return action()
        finally:
            os.environ.clear()
            os.environ.update(old)

    def test_notify_command_lookup_order(self):
        guard = load_guard_module()
        app, _ = self.make_fake_app()
        self.assertEqual(self.with_env({}, guard.notify_command), [])  # no LOOP_APP_HOME and no config.json
        self.write_json(os.path.join(self.data_dir, "config.json"), {"app_root_dir": app})
        command = self.with_env({}, guard.notify_command)
        self.assertEqual(command[1:], [os.path.join(app, "harness.py"), "notify"])
        self.assertEqual(self.with_env({"LOOP_APP_HOME": os.path.join(self.tmp, "nowhere")}, guard.notify_command), [])

    def test_alert_logs_marks_and_calls_the_app(self):
        guard = load_guard_module()
        app, record = self.make_fake_app()
        specs = self.specs()
        self.with_env({"LOOP_APP_HOME": app}, lambda: guard.alert(specs, "executor", "test failure"))
        self.assertIn("guard-failopen", read_text(self.specs("project", "loop", "internal", "guard.log")))
        self.assertTrue(os.path.isfile(self.specs("project", "loop", "internal", ".guard-alert")), "once-an-hour marker")
        deadline = time.time() + 15
        while not os.path.isfile(record) and time.time() < deadline:
            time.sleep(0.1)
        argv = json.loads(read_text(record))
        self.assertEqual(argv[0], "notify")
        self.assertIn("--subject", argv)
        os.remove(record)
        self.with_env({"LOOP_APP_HOME": app}, lambda: guard.alert(specs, "executor", "second failure"))
        time.sleep(1)
        self.assertFalse(os.path.isfile(record), "at most one notification per hour")


class TestInvalidProjectRegex(GuardTestCase):
    """A broken regex in PERMISSIONS.json must not make the guard fail open: hard rules and valid rules keep blocking."""

    def setUp(self):
        super().setUp()
        self.write_json(self.specs("project", "loop", "json", "PERMISSIONS.json"),
                        {"deny_bash_regex": ["(unclosed", r"terraform\s+apply"], "deny_path_regex": ["[bad"]})

    def test_hard_rule_still_blocks(self):
        self.assertEqual(self.bash("git push --force origin main", profile="orchestrator"), 2)

    def test_valid_project_rule_still_blocks(self):
        self.assertEqual(self.bash("terraform apply -auto-approve"), 2)

    def test_ordinary_command_still_passes(self):
        self.assertEqual(self.bash("ls -la"), 0)

    def test_edit_with_bad_path_rule_does_not_crash(self):
        self.assertEqual(self.edit("src/app.py", profile="orchestrator"), 0)


if __name__ == "__main__":
    unittest.main()
