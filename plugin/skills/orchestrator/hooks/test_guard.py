#!/usr/bin/env python3
"""Zero-cost tests of the orchestrator guard (stdlib unittest, no Claude, no network).

Run:  python -m unittest discover -s plugin/skills/orchestrator/hooks -p "test_guard.py" -v

Each test runs the real `guard.py` as the hook does (JSON on stdin, exit 2 = blocked) against a temporary project,
with LOOP_DATA_DIR pointing at an empty temporary folder so no real app or notification channel is ever touched.
"""
import importlib.util
import json
import os
import re
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
        for name in ("LOOP_APP_HOME", "LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "GUARD_PROD_HINTS", "LOOP_SPECS_DIR", "LOOP_TURN", "LOOP_HEADLESS"):  # LOOP_SPECS_DIR inherited from a loop turn would send these tests' blocks to the REAL guard.log
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


class TestStraySpecsInsideTheRepo(GuardTestCase):
    """False positive of a real run: a test had left `app/backend/.specs` inside the repo; an executor whose cwd was under app/backend took that
    folder as the root and its edit of `app/docs/API.md` was blocked by `repo-only`. The `.specs` of the git root wins over a nearer one."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.project, ".git"))
        self.subfolder = os.path.join(self.project, "app", "backend", "service")
        os.makedirs(os.path.join(self.project, "app", "backend", ".specs", "project"))
        os.makedirs(self.subfolder)

    def edit_from_subfolder(self, target, profile="orchestrator"):
        payload = {"tool_name": "Edit", "tool_input": {"file_path": target}, "cwd": self.subfolder}
        return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True,
                              env=self.env, cwd=self.subfolder).returncode

    def test_a_repo_file_outside_the_stray_folder_is_allowed(self):
        self.assertEqual(self.edit_from_subfolder(os.path.join(self.project, "app", "docs", "API.md")), PASSED)
        self.assertEqual(self.edit_from_subfolder(os.path.join(self.project, "app", "docs", "API.md").replace("/", "\\")), PASSED)

    def test_a_file_outside_the_repo_is_still_blocked(self):
        # under the home folder: files in the system temp folder (where this test project lives) are exempt on purpose
        self.assertEqual(self.edit_from_subfolder(os.path.join(os.path.expanduser("~"), "not-a-worktree-stray-specs", "x.md")), BLOCKED)

    def test_the_git_root_specs_wins_and_without_git_the_nearest_is_used_as_before(self):
        guard = load_guard_module()
        previous = os.environ.pop("LOOP_SPECS_DIR", None)
        self.addCleanup(lambda: previous is not None and os.environ.__setitem__("LOOP_SPECS_DIR", previous))
        self.assertEqual(os.path.normcase(guard.find_specs_dir(self.subfolder)), os.path.normcase(self.specs()))
        shutil.rmtree(os.path.join(self.project, ".git"))
        self.assertEqual(os.path.normcase(guard.find_specs_dir(self.subfolder)), os.path.normcase(os.path.join(self.project, "app", "backend", ".specs")))


class TestGuardLogTurnAndAttempt(GuardTestCase):
    """Each guard.log line says in which loop turn it happened and what the agent tried; the block message tells the agent it is final."""

    def log_lines(self):
        return [json.loads(line) for line in read_text(self.specs("project", "loop", "internal", "guard.log")).splitlines()]

    def blocked_run(self, command="git push origin loop/x", **env):
        saved = dict(self.env)
        self.env.update(env)
        try:
            payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": self.project}
            return subprocess.run([sys.executable, GUARD, "--profile", "executor"], input=json.dumps(payload), capture_output=True, text=True, env=self.env)
        finally:
            self.env = saved

    def finish_turns(self, *numbers):
        for number in numbers:
            self.write_json(self.specs("project", "loop", "internal", "turns", "%03d.json" % number), {"turn": number})

    def test_the_turn_in_progress_is_the_last_finished_turn_plus_one(self):
        self.finish_turns(1, 2, 7)
        self.blocked_run(LOOP_HEADLESS="1")
        self.assertEqual(self.log_lines()[0]["turn"], 8)

    def test_the_first_turn_has_no_file_yet(self):
        os.makedirs(self.specs("project", "loop", "internal", "turns"))
        self.blocked_run(LOOP_HEADLESS="1")
        self.assertEqual(self.log_lines()[0]["turn"], 1)

    def test_outside_the_headless_loop_the_turn_is_unknown_never_a_guess(self):
        self.finish_turns(1, 2)
        self.env.pop("LOOP_HEADLESS", None)
        self.blocked_run()
        self.assertIsNone(self.log_lines()[0]["turn"])

    def test_an_exported_loop_turn_wins(self):
        self.finish_turns(1, 2)
        self.blocked_run(LOOP_HEADLESS="1", LOOP_TURN="12")
        self.assertEqual(self.log_lines()[0]["turn"], 12)

    def test_the_log_keeps_what_was_tried_clipped(self):
        self.blocked_run("git push origin loop/x   &&   echo " + "z" * 400)
        attempt = self.log_lines()[0]["attempt"]
        self.assertTrue(attempt.startswith("git push origin loop/x && echo z"))
        self.assertLessEqual(len(attempt), 200)

    def test_the_block_message_keeps_its_prefix_and_ends_with_the_final_instruction(self):
        result = self.blocked_run()
        self.assertEqual(result.returncode, BLOCKED)
        self.assertTrue(result.stderr.startswith("BLOCKED by the orchestrator guard [git-push]: "))
        self.assertIn("This block is final: do not retry this command or a variant of it. Continue without it, or report it in your final return.",
                      result.stderr.strip().splitlines()[-1])


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
        self.assertEqual(self.run_guard("Read", {"file_path": "/proj/.specs/project/.env.test"}, "executor"), BLOCKED)
        self.assertEqual(self.run_guard("Read", {"file_path": "/proj/.specs/project/.env.test.example"}, "executor"), PASSED)


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

    def test_grafana_is_read_only_for_every_profile_and_no_permission_releases_it(self):
        profiles = ("executor", "verifier", "spec", "orchestrator", "planning")
        reads = ("query_loki_logs", "list_loki_label_names", "list_loki_label_values", "query_loki_stats", "query_loki_patterns",
                 "find_error_pattern_logs", "list_datasources", "get_dashboard_by_uid", "search_dashboards", "check_datasources_health", "generate_deeplink")
        writes = ("create_incident", "update_dashboard", "delete_snapshot", "create_snapshot", "add_activity_to_incident", "install_plugin",
                  "alerting_manage_rules", "alerting_manage_routing", "create_annotation", "update_datasource", "create_folder")
        for profile in profiles:
            for name in reads:
                self.assertEqual(self.run_guard("mcp__grafana__" + name, {}, profile), PASSED, (name, profile))
            for name in writes:
                self.assertEqual(self.run_guard("mcp__grafana__" + name, {}, profile), BLOCKED, (name, profile))
            self.assertEqual(self.run_guard("mcp__grafana__grafana_api_request", {"url": "/api/health"}, profile), PASSED, profile)
            self.assertEqual(self.run_guard("mcp__grafana__grafana_api_request", {"method": "get", "url": "/api/health"}, profile), PASSED, profile)
            for method in ("POST", "put", "DELETE", "PATCH"):
                self.assertEqual(self.run_guard("mcp__grafana__grafana_api_request", {"method": method, "url": "/api/x"}, profile), BLOCKED, (method, profile))
        self.allow("grafana-write")
        self.assertEqual(self.run_guard("mcp__grafana__create_incident", {}, "executor"), BLOCKED)
        self.assertEqual(self.run_guard("mcp__grafana__query_loki_logs", {}, "executor"), PASSED)


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


class TestSpecsOnly(GuardTestCase):
    """Spec and map agents (profile spec) write only inside .specs/ and .regression/; other profiles are untouched."""

    def test_spec_profile_cannot_edit_code(self):
        self.assertEqual(self.edit("src/Orders/Service.cs", profile="spec"), BLOCKED)

    def test_spec_profile_writes_inside_specs(self):
        self.assertEqual(self.edit(".specs/codebase/STACK.md", profile="spec"), PASSED)
        self.assertEqual(self.edit(".specs/features/orders/spec.md", profile="spec"), PASSED)
        self.assertEqual(self.edit(".regression/orders.md", profile="spec"), PASSED)
        self.assertEqual(self.edit("docs/codebase/STACK.md", profile="spec"), PASSED)
        self.assertEqual(self.edit("docs/other/STACK.md", profile="spec"), BLOCKED)

    def test_spec_profile_shell_text_with_arrows_is_not_mistaken_for_a_write(self):
        # a shell redirect is best effort: markdown text with `>` must never be judged a file write (the map job has its own
        # snapshot that undoes anything outside .specs/)
        self.assertEqual(self.bash("python -c \"print('a -> stack.md > domain')\"", profile="spec"), PASSED)
        self.assertEqual(self.bash("echo x > .specs/codebase/STACK.md", profile="spec"), PASSED)

    def test_map_agent_under_auto_profile_is_confined(self):
        payload = {"file_path": os.path.join(self.project, "src", "Service.cs")}
        self.assertEqual(self.run_guard("Write", payload, "auto", agent_type="loop-engineering-harness:brownfield-mapper"), BLOCKED)
        ok = {"file_path": os.path.join(self.project, ".specs", "codebase", "STACK.md")}
        self.assertEqual(self.run_guard("Write", ok, "auto", agent_type="brownfield-mapper"), PASSED)

    def test_other_profiles_and_the_main_session_are_not_confined(self):
        self.assertEqual(self.edit("src/Orders/Service.cs", profile="executor"), PASSED)
        self.assertEqual(self.edit("src/Orders/Service.cs", profile="orchestrator"), PASSED)
        payload = {"file_path": os.path.join(self.project, "src", "Service.cs")}
        self.assertEqual(self.run_guard("Write", payload, "auto"), PASSED, "main session: nothing enforced")

    def test_a_released_rule_lets_it_through(self):
        self.allow("specs-only")
        self.assertEqual(self.edit("src/Orders/Service.cs", profile="spec"), PASSED)


class TestPlanningProfile(GuardTestCase):
    """Profile planning (the app's planning chat, `claude -p --settings`): only .specs/, but it may write ROADMAP.md / FLOWS.md."""

    def write(self, relative, profile="planning"):
        return self.run_guard("Write", {"file_path": os.path.join(self.project, relative)}, profile)

    def test_writing_outside_specs_is_blocked(self):
        self.assertEqual(self.write("src/Orders/Service.cs"), BLOCKED)
        self.assertEqual(self.write("notes.txt"), BLOCKED)
        self.assertEqual(self.edit("src/Orders/Service.cs", profile="planning"), BLOCKED)
        self.assertEqual(self.run_guard("NotebookEdit", {"notebook_path": os.path.join(self.project, "a.ipynb")}, "planning"), BLOCKED)

    def test_planning_documents_are_writable(self):
        for name in ("PROJECT.md", "ROADMAP.md", "FLOWS.md", "TEST-ENVIRONMENT.md"):
            self.assertEqual(self.write(".specs/project/" + name), PASSED, name)
            self.assertEqual(self.edit(".specs/project/" + name, profile="planning"), PASSED, name)

    def test_planning_cannot_write_the_codebase_map(self):
        self.assertEqual(self.write("docs/codebase/STACK.md"), BLOCKED)
        self.assertEqual(self.write("docs/codebase/STACK.md", profile="spec"), PASSED)

    def test_same_documents_stay_blocked_for_spec_profile(self):
        self.assertEqual(self.write(".specs/project/ROADMAP.md", profile="spec"), BLOCKED)

    def test_roadmap_approval_is_blocked(self):
        self.assertEqual(self.write(".specs/project/ROADMAP-APPROVED.json"), BLOCKED)
        self.assertEqual(self.write(".specs/project/loop/json/ROADMAP-APPROVED.json"), BLOCKED)

    def test_git_push_is_blocked(self):
        self.assertEqual(self.bash("git push origin feature/x", profile="planning"), BLOCKED)

    def test_a_credential_path_is_blocked(self):
        self.assertEqual(self.write(".specs/.secrets/token.txt"), BLOCKED)


class TestMultiRootScope(GuardTestCase):
    """SCOPE.json with `roots`, absolute entries and `copied`: several worktrees of one project."""

    def setUp(self):
        super().setUp()
        self.second = os.path.join(self.tmp, "second-worktree")
        os.makedirs(self.second)
        self.outside = os.path.join(os.path.expanduser("~"), "not-a-worktree-of-this-project")
        self.copied = os.path.join(self.second, ".env.local")
        self.write_json(self.specs("project", "loop", "json", "SCOPE.json"), {
            "roots": [self.project, self.second],
            "features": {"api": [os.path.join(self.second, "src", "orders")], "old": ["apps/a"]},
            "copied": [self.copied]})

    def edit_at(self, path, profile="executor"):
        return self.run_guard("Edit", {"file_path": path}, profile)

    def test_a_path_of_the_feature_in_the_second_worktree_is_allowed(self):
        self.assertEqual(self.edit_at(os.path.join(self.second, "src", "orders", "x.ts")), PASSED)
        self.assertEqual(self.edit("apps/a/x.ts"), PASSED)  # relative entry = primary root

    def test_same_name_elsewhere_or_in_a_repo_not_in_roots_is_blocked(self):
        self.assertEqual(self.edit_at(os.path.join(self.second, "src", "other", "x.ts")), BLOCKED)
        self.assertEqual(self.edit_at(os.path.join(self.outside, "src", "orders", "x.ts")), BLOCKED)

    def test_the_orchestrator_writes_only_inside_the_listed_roots(self):
        self.assertEqual(self.edit_at(os.path.join(self.second, "src", "other", "x.ts"), "orchestrator"), PASSED)
        self.assertEqual(self.edit(".specs/project/ROADMAP.md", "orchestrator"), PASSED)
        self.assertEqual(self.edit_at(os.path.join(self.outside, "x.ts"), "orchestrator"), BLOCKED)

    def test_git_commit_resolves_against_its_folder(self):
        self.assertEqual(self.bash("git -C %s commit -m x" % self.second, "orchestrator"), PASSED)
        self.assertEqual(self.bash("git commit -m x", "orchestrator"), PASSED)
        self.assertEqual(self.bash("git -C %s commit -m x" % self.outside, "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("cd %s && git commit -m x" % self.outside, "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("git -C %s status" % self.outside, "orchestrator"), PASSED)

    def test_push_pr_and_add_all_stay_blocked_for_every_profile(self):
        for profile in ("orchestrator", "executor", "verifier", "spec", "planning"):
            self.assertEqual(self.bash("git -C %s push origin loop/x" % self.second, profile), BLOCKED, profile)
            self.assertEqual(self.bash("gh pr create --fill", profile), BLOCKED, profile)
            self.assertEqual(self.bash("git add -A", profile), BLOCKED, profile)

    def test_copied_config_is_a_secret_inside_the_worktree_but_the_build_is_not(self):
        for profile in ("orchestrator", "executor", "verifier"):
            self.assertEqual(self.run_guard("Read", {"file_path": self.copied}, profile), BLOCKED, profile)
            self.assertEqual(self.bash("cd %s && cat .env.local" % self.second, profile), BLOCKED, profile)
            self.assertEqual(self.bash("cd %s && npm run build" % self.second, profile), PASSED, profile)
        self.assertEqual(self.run_guard("Read", {"file_path": os.path.join(self.second, "src", "a.ts")}, "executor"), PASSED)

    def test_broad_reads_that_would_reach_the_copied_config_are_blocked(self):
        for command in ("cd %s && rg --no-ignore SECRET" % self.second, "cd %s && rg -uu SECRET ." % self.second,
                        "cd %s && rg -g '.env*' SECRET" % self.second, "grep -rn SECRET %s" % self.second,
                        "cd %s && grep -R SECRET ." % self.second, "cd %s && find . -exec cat {} +" % self.second,
                        "cd %s && find . -type f | xargs grep SECRET" % self.second,
                        "cd %s && Get-ChildItem -Recurse | Select-String SECRET" % self.second,
                        "cd %s && cat .env*" % self.second, "cd %s && type *.local" % self.second,
                        "cd %s && Select-String -Path * -Pattern SECRET" % self.second,
                        "cd %s && git grep --no-index SECRET" % self.second, "cd %s && cp -r . %s" % (self.second, self.tmp)):
            for profile in ("orchestrator", "executor", "verifier"):
                self.assertEqual(self.bash(command, profile), BLOCKED, (command, profile))
        self.assertEqual(self.run_guard("PowerShell", {"command": "cd %s; gci -r | sls SECRET" % self.second}, "executor"), BLOCKED)

    def test_normal_source_searches_in_the_worktree_still_pass(self):
        os.makedirs(os.path.join(self.second, "src"))
        open(os.path.join(self.second, "src", "a.ts"), "w").close()
        for command in ("cd %s && rg SECRET" % self.second, "cd %s && rg -n foo src" % self.second, "grep -rn foo %s" % os.path.join(self.second, "src"),
                        "cd %s && grep -r foo src" % self.second, "cd %s && grep -rn foo src/a.ts" % self.second,
                        "cd %s && grep -r --include=*.ts foo ." % self.second, "cd %s && find . -name '*.ts' -exec grep foo {} +" % self.second,
                        "cd %s && cat src/*.ts" % self.second,
                        "cd %s && Get-ChildItem -Recurse -Include *.ts | Select-String foo" % self.second,
                        "cd %s && git grep foo" % self.second, "cd %s && rg -g '*.ts' foo" % self.second, "cd %s && ls -la" % self.second):
            for profile in ("orchestrator", "executor", "verifier"):
                self.assertEqual(self.bash(command, profile), PASSED, (command, profile))

    def test_grep_and_glob_tools_cannot_whitelist_or_list_the_copied_name(self):
        in_second = {"path": self.second}
        self.assertEqual(self.run_guard("Grep", dict(in_second, pattern="SECRET"), "executor"), PASSED)  # rg skips git-ignored files
        self.assertEqual(self.run_guard("Grep", dict(in_second, pattern="SECRET", glob=".env*"), "executor"), BLOCKED)
        self.assertEqual(self.run_guard("Grep", dict(in_second, pattern="SECRET", glob="*.local"), "verifier"), BLOCKED)
        self.assertEqual(self.run_guard("Grep", dict(in_second, pattern="SECRET", glob="*.ts"), "executor"), PASSED)
        self.assertEqual(self.run_guard("Grep", dict(in_second, pattern="SECRET", no_ignore=True), "executor"), BLOCKED)
        self.assertEqual(self.run_guard("Glob", dict(in_second, pattern=".env*"), "orchestrator"), BLOCKED)
        self.assertEqual(self.run_guard("Glob", dict(in_second, pattern="**/*.ts"), "orchestrator"), PASSED)
        self.assertEqual(self.run_guard("Glob", dict(in_second, pattern="*"), "orchestrator"), PASSED)
        self.assertEqual(self.run_guard("Glob", {"path": os.path.join(self.second, "src"), "pattern": ".env*"}, "orchestrator"), PASSED)

    def test_shell_writes_are_confined_like_write_and_edit(self):
        inside = os.path.join(self.second, "src", "orders")
        other = os.path.join(self.second, "src", "other")
        self.assertEqual(self.bash("echo x > %s" % os.path.join(inside, "a.log")), PASSED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(other, "a.log")), BLOCKED)
        self.assertEqual(self.bash("echo x >> %s" % os.path.join(other, "a.log")), BLOCKED)
        self.assertEqual(self.bash("echo x | tee %s" % os.path.join(other, "a.log")), BLOCKED)
        self.assertEqual(self.bash("sed -i s/a/b/ %s" % os.path.join(other, "a.ts")), BLOCKED)
        self.assertEqual(self.bash("cd %s && echo x > ../other/a.log" % inside), BLOCKED)
        self.assertEqual(self.run_guard("PowerShell", {"command": "Set-Content -Path %s -Value x" % os.path.join(other, "a.ts")}), BLOCKED)
        self.assertEqual(self.run_guard("PowerShell", {"command": "'x' | Out-File %s" % os.path.join(other, "a.ts")}), BLOCKED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(self.outside, "a.log"), "orchestrator"), BLOCKED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(other, "a.log"), "orchestrator"), PASSED)  # repo-only: any worktree file
        self.assertEqual(self.bash("echo x > %s" % os.path.join(other, "a.log"), "verifier"), PASSED)  # no confinement for the verifier

    def test_shell_write_exemptions_and_build_outputs(self):
        in_second = "cd %s && " % self.second
        for command in ("dotnet build 2>&1 > %s" % os.path.join(self.tmp, "build.log"), "echo x > /dev/null", "echo x > nul",
                        "echo x > $null", "echo x > $env:TEMP\\a.log", "npm test > %s" % os.path.join(tempfile.gettempdir(), "t.log"),
                        in_second + "dotnet test > bin/Debug/test.log", in_second + "pytest > .pytest_cache/out.txt",
                        in_second + "npm run build 2>&1 > node_modules/.cache/out.log", "python -c \"print('a > b')\""):
            self.assertEqual(self.bash(command), PASSED, command)
        self.assertEqual(self.bash(in_second + "npm test > out.log"), BLOCKED)  # the worktree root is not in the feature PATHS

    def test_spec_and_planning_shell_writes_stay_in_specs(self):
        for profile in ("spec", "planning"):
            self.assertEqual(self.bash("echo x > src/a.ts", profile), BLOCKED, profile)
            self.assertEqual(self.bash("echo x > .specs/features/a/notes.md", profile), PASSED, profile)
            self.assertEqual(self.bash("echo x > %s" % os.path.join(self.tmp, "scratch.txt"), profile), PASSED, profile)


    # ---- LOOP_SPECS_DIR: the hook's cwd is another worktree (no `.specs` above it)
    def run_in(self, tool, tool_input, cwd, profile="executor", specs_env=None):
        env = dict(self.env)
        env.pop("LOOP_SPECS_DIR", None)
        if specs_env:
            env["LOOP_SPECS_DIR"] = specs_env
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": cwd}
        return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload),
                              capture_output=True, text=True, env=env).returncode

    def test_loop_specs_dir_finds_the_primary_scope_from_another_worktree(self):
        env = self.specs()
        in_second = os.path.join(self.second, "apps", "z", "a.ts")
        self.assertEqual(self.run_in("Write", {"file_path": in_second, "content": "x"}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Write", {"file_path": os.path.join("apps", "z", "a.ts"), "content": "x"}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Edit", {"file_path": os.path.join("src", "orders", "x.ts")}, self.second, specs_env=env), PASSED)  # relative = against the hook cwd
        self.assertEqual(self.run_in("Edit", {"file_path": os.path.join(self.second, "src", "orders", "x.ts")}, self.second, specs_env=env), PASSED)
        self.assertEqual(self.run_in("Read", {"file_path": self.copied}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Read", {"file_path": ".env.local"}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Bash", {"command": "cat .env.local"}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Bash", {"command": "rg -uu TOKEN ."}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Bash", {"command": "echo x > apps/z/a.log"}, self.second, specs_env=env), BLOCKED)
        self.assertEqual(self.run_in("Bash", {"command": "npm run build"}, self.second, specs_env=env), PASSED)
        self.assertEqual(self.run_in("Read", {"file_path": "src/a.ts"}, self.second, specs_env=env), PASSED)

    def test_without_the_variable_the_old_walk_up_is_unchanged(self):
        in_second = os.path.join(self.second, "apps", "z", "a.ts")
        self.assertEqual(self.run_in("Write", {"file_path": in_second, "content": "x"}, self.second), PASSED)  # nothing found: the guard does not stall
        self.assertEqual(self.run_in("Write", {"file_path": in_second, "content": "x"}, self.project), BLOCKED)  # `.specs` found walking up

    def test_an_invalid_loop_specs_dir_is_ignored(self):
        in_second = os.path.join(self.second, "apps", "z", "a.ts")
        for bad in (os.path.join(self.tmp, "missing", ".specs"), self.project, os.path.join(".specs"), self.second):
            self.assertEqual(self.run_in("Write", {"file_path": in_second, "content": "x"}, self.second, specs_env=bad), PASSED, bad)
            self.assertEqual(self.run_in("Write", {"file_path": in_second, "content": "x"}, self.project, specs_env=bad), BLOCKED, bad)

    # ---- the copied-secret check compares NORMALIZED paths
    def secret_spellings(self):
        forward = self.second.replace("\\", "/")
        spellings = [forward + "/.env.local", forward + "/./.env.local", forward + "/apps/../.env.local", forward.replace(":/", "://", 1) + "/.env.local",
                     self.second + "\\.env.local", forward.upper() + "/.ENV.LOCAL", "../second-worktree/.env.local", "../second-worktree/./src/../.env.local"]
        match = re.match(r"^([A-Za-z]):/(.*)$", forward)
        if match and os.name == "nt":
            drive, rest = match.group(1), match.group(2)
            spellings += ["/%s/%s/.env.local" % (drive.lower(), rest), "/%s/%s/.env.local" % (drive.upper(), rest), "/mnt/%s/%s/.env.local" % (drive.lower(), rest)]
        return spellings

    def test_every_spelling_of_a_copied_secret_is_blocked(self):
        for spelling in self.secret_spellings():
            self.assertEqual(self.run_in("Read", {"file_path": spelling}, self.project), BLOCKED, spelling)
            self.assertEqual(self.run_in("Bash", {"command": "cat " + spelling}, self.project), BLOCKED, spelling)
            self.assertEqual(self.run_in("Bash", {"command": 'cat "%s"' % spelling}, self.project, "verifier"), BLOCKED, spelling)

    def test_the_tail_inside_another_listed_root_is_a_secret_too(self):
        # the copied file is in `second`; the same tail in the primary root (also a listed root) is the same kind of file
        for spelling in (os.path.join(self.project, ".env.local"), "./.env.local"):
            self.assertEqual(self.run_in("Read", {"file_path": spelling}, self.project), BLOCKED, spelling)

    def test_normalized_paths_that_are_not_the_secret_still_pass(self):
        forward = self.second.replace("\\", "/")
        for spelling in (forward + "/./src/a.ts", forward + "/apps/../src/a.ts", "../second-worktree/src/a.ts", forward + "/.env.local.md".replace(".local.md", ".example")):
            self.assertEqual(self.run_in("Read", {"file_path": spelling}, self.project), PASSED, spelling)
            self.assertEqual(self.run_in("Bash", {"command": "cat " + spelling}, self.project), PASSED, spelling)

    def test_normalize_path_unit(self):
        guard = load_guard_module()
        self.assertEqual(guard.normalize_path("A://b/./c/../d\\e", None), "a:/b/d/e")
        self.assertEqual(guard.normalize_path("../x/y", "C:\\repo\\sub"), "c:/repo/x/y")
        if os.name == "nt":
            self.assertEqual(guard.normalize_path("/c/Users/x"), "c:/users/x")
            self.assertEqual(guard.normalize_path("/mnt/d/x//y"), "d:/x/y")

    # ---- the temp exemption never covers a git working tree
    def git_init(self, path):
        os.makedirs(path, exist_ok=True)
        subprocess.run(["git", "init", "-q", path], capture_output=True, check=True)

    def test_a_repo_under_a_temp_looking_folder_is_not_exempt(self):
        repo = os.path.join(self.tmp, "tmp", "other-repo")
        self.git_init(repo)
        plain = os.path.join(self.tmp, "scratchpad", "plain")
        os.makedirs(plain)
        linked = os.path.join(self.tmp, "temp", "linked-worktree")
        os.makedirs(linked)
        with open(os.path.join(linked, ".git"), "w", encoding="utf-8") as handle:  # a linked worktree has a .git FILE
            handle.write("gitdir: elsewhere\n")
        for profile in ("executor", "orchestrator"):
            self.assertEqual(self.edit_at(os.path.join(repo, "src", "x.ts"), profile), BLOCKED, profile)
            self.assertEqual(self.edit_at(os.path.join(linked, "src", "x.ts"), profile), BLOCKED, profile)
            self.assertEqual(self.edit_at(os.path.join(plain, "x.txt"), profile), PASSED, profile)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(repo, "a.log")), BLOCKED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(linked, "a.log")), BLOCKED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(plain, "a.log")), PASSED)

    def test_a_spec_agent_cannot_write_into_a_repo_under_a_temp_folder(self):
        repo = os.path.join(self.tmp, "temp", "other-repo")
        self.git_init(repo)
        self.assertEqual(self.edit_at(os.path.join(repo, "x.md"), "spec"), BLOCKED)
        self.assertEqual(self.edit_at(os.path.join(self.tmp, "temp", "notes.md"), "spec"), PASSED)


    # ---- case handling is platform-aware: folded only where the filesystem is case-insensitive
    def test_case_is_folded_only_on_a_case_insensitive_filesystem(self):
        guard = load_guard_module()
        guard.CASE_INSENSITIVE = False  # the POSIX branch, simulated (works on Windows too)
        self.assertEqual(guard.norm("/Srv/Repo/./Src/"), "/Srv/Repo/Src")
        self.assertEqual(guard.normalize_path("../Other/File.TS", "/Srv/Repo/Sub"), "/Srv/Repo/Other/File.TS")
        self.assertEqual(guard.normalize_path("A:/B/c", None, True), "a:/b/c")
        guard.CASE_INSENSITIVE = True
        self.assertEqual(guard.norm("/Srv/Repo/./Src/"), "/srv/repo/src")
        self.assertEqual(guard.normalize_path("../Other/File.TS", "/Srv/Repo/Sub"), "/srv/repo/other/file.ts")
        self.assertEqual(guard.normalize_path("A:/B/c", None, False), "A:/B/c")

    def test_the_git_ancestor_walk_uses_the_original_case_path(self):
        guard = load_guard_module()
        repo = os.path.join(self.tmp, "Tmp", "MixedCase-Repo")
        self.git_init(repo)
        target = os.path.join(repo, "Src", "X.ts")
        for insensitive in (False, True):
            guard.CASE_INSENSITIVE = insensitive
            absolute = guard.normalize_path(target)
            self.assertEqual(absolute, guard.normalize_path(target, None, insensitive))
            if not insensitive:
                self.assertIn("MixedCase-Repo", absolute)  # original case reaches the filesystem check
            self.assertTrue(guard.has_git_ancestor(absolute), insensitive)
            self.assertFalse(guard.temp_exempt(absolute), insensitive)
        guard.CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"
        plain = os.path.join(self.tmp, "Scratchpad", "Plain", "x.txt")
        os.makedirs(os.path.dirname(plain))
        self.assertTrue(guard.temp_exempt(guard.normalize_path(plain)))

    def test_a_mixed_case_repo_under_a_temp_folder_is_not_exempt_end_to_end(self):
        repo = os.path.join(self.tmp, "Temp", "MixedCase-Repo")
        self.git_init(repo)
        self.assertEqual(self.edit_at(os.path.join(repo, "Src", "X.ts")), BLOCKED)
        self.assertEqual(self.bash("echo x > %s" % os.path.join(repo, "Src", "A.log")), BLOCKED)
        self.assertEqual(self.bash("cd %s && echo x > Src/A.log" % repo), BLOCKED)  # a cd carries the original case along

    def test_build_output_folders_and_mixed_case_secret_names_keep_working(self):
        in_second = "cd %s && " % self.second
        self.assertEqual(self.bash(in_second + "dotnet test > TestResults/out.log"), PASSED)
        if os.name == "nt":
            self.assertEqual(self.run_in("Read", {"file_path": os.path.join(self.second, ".Env.Local")}, self.project), BLOCKED)


class TestQuotedSedInLoopWorktree(GuardTestCase):
    """False positive of the real run: `cd "<loop worktree>" && sed -i 's/a/b/; s/c/d/' file` was blocked by `repo-only`:
    the `;` inside the quoted sed script split the statement and the script's tail was judged as a file path."""

    def setUp(self):
        super().setUp()
        self.worktree = os.path.join(self.tmp, "shop-worktree-loop")
        self.outside = os.path.join(os.path.expanduser("~"), "not-a-worktree-of-this-project")
        os.makedirs(self.worktree)
        self.write_json(self.specs("project", "loop", "json", "SCOPE.json"), {"roots": [self.project, self.worktree], "features": {}})

    def forward(self, path):
        return path.replace("\\", "/")

    def test_cd_into_the_loop_worktree_then_sed_with_semicolons_passes(self):
        script = "'s/^- \[a\] status=pending/- [a] status=speccing/; s/^- \[b\] status=pending/- [b] status=speccing/'"
        command = 'cd "%s" && sed -i %s .specs/project/ROADMAP.md' % (self.forward(self.worktree), script)
        self.assertEqual(self.bash(command, "orchestrator"), PASSED)

    def test_quoted_forward_slash_cd_and_plain_sed_pass(self):
        command = 'cd "%s" && sed -i s/a/b/ src/a.ts' % self.forward(self.worktree)
        self.assertEqual(self.bash(command, "orchestrator"), PASSED)

    def test_a_target_really_outside_stays_blocked(self):
        script = "'s/a/b/; s/c/d/'"
        outside_file = self.forward(os.path.join(self.outside, "a.ts"))
        self.assertEqual(self.bash('cd "%s" && sed -i %s %s' % (self.forward(self.worktree), script, outside_file), "orchestrator"), BLOCKED)
        self.assertEqual(self.bash('cd "%s" && sed -i %s a.ts' % (self.forward(self.outside), script), "orchestrator"), BLOCKED)

    def test_semicolon_outside_quotes_still_splits_statements(self):
        command = 'cd "%s"; echo x > %s' % (self.forward(self.worktree), self.forward(os.path.join(self.outside, "a.log")))
        self.assertEqual(self.bash(command, "orchestrator"), BLOCKED)



# ======================================================================================================================
# V1 (loop-hardening): headless delivery rules, subagent git writes, per-task scope, secrets layers, rule probes
# ======================================================================================================================
ALL_PROFILES = ("orchestrator", "executor", "verifier", "spec")
SUBAGENTS = {"executor-back": "executor", "executor-front": "executor", "executor-generic": "executor",
             "verifier": "verifier", "verifier-flow": "verifier", "spec-writer": "spec", "spec-reviewer": "spec"}
HOME = os.path.expanduser("~")


class V1GuardCase(GuardTestCase):
    """GuardTestCase plus a runner that also returns stderr and an os.environ patch helper (restored on cleanup)."""

    def run_full(self, tool, tool_input, profile="executor", env=None, **extra):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.project}
        payload.update(extra)
        run_env = dict(self.env)
        run_env.update(env or {})
        result = subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload),
                                capture_output=True, text=True, env=run_env)
        return result.returncode, result.stderr

    def patch_environ(self, **values):
        saved = {name: os.environ.get(name) for name in values}

        def restore():
            for name, old in saved.items():
                if old is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = old
        self.addCleanup(restore)
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


HEADLESS_PUSH_VARIANTS = [
    "git push",
    "git push origin feat/x",
    "git push -u origin HEAD:refs/heads/feat/x",
    "git push origin feat/x:feat/x",
    "git.exe push origin feat/x",
    "git -C repo push origin feat/x",
    "python -c \"import subprocess; subprocess.run(['git','push'])\"",
    "python -c \"import subprocess; subprocess.run(['git', 'push', 'origin', 'feat/x'])\"",
    "echo $(git push origin feat/x)",
    "echo `git push origin feat/x`",
    "bash -c 'git push origin feat/x'",
    "cd repo && git push origin feat/x",
]
HEADLESS_GH_VARIANTS = [
    "gh pr view 12",
    "gh pr create --title x --body y",
    "gh.exe api repos/o/r/pulls",
    "gh api repos/o/r",
    "gh auth status",
    "cd repo && gh pr list",
]


class TestHeadlessDelivery(V1GuardCase):
    """HEADLESS-01/02: with LOOP_HEADLESS=1 push and gh belong to the engine, in every profile and with no agent_type."""

    def assert_headless_block(self, command, profile, rule, **extra):
        code, err = self.run_full("Bash", {"command": command}, profile, env={"LOOP_HEADLESS": "1"}, **extra)
        self.assertEqual(code, BLOCKED, (profile, command, err))
        self.assertIn(rule, err, (profile, command))

    def test_push_variants_blocked_in_every_profile_headless(self):
        for profile in ALL_PROFILES:
            for command in HEADLESS_PUSH_VARIANTS:
                with self.subTest(profile=profile, command=command):
                    self.assert_headless_block(command, profile, "headless-push")

    def test_gh_variants_blocked_in_every_profile_headless(self):
        for profile in ALL_PROFILES:
            for command in HEADLESS_GH_VARIANTS:
                with self.subTest(profile=profile, command=command):
                    self.assert_headless_block(command, profile, "headless-gh")

    def test_quoted_git_executable_push_is_blocked_headless(self):
        for command in ('"git" push origin feat/x', "'git' push origin feat/x", '& "git" push origin feat/x'):
            self.assert_headless_block(command, "executor", "headless-push")

    def test_powershell_tool_is_covered_too(self):
        for command in ("git push origin feat/x", "gh pr view 1"):
            code, err = self.run_full("PowerShell", {"command": command}, "orchestrator", env={"LOOP_HEADLESS": "1"})
            self.assertEqual(code, BLOCKED, command)

    def test_auto_payload_without_agent_type_is_blocked_headless(self):
        for command in HEADLESS_PUSH_VARIANTS[:7]:
            with self.subTest(command=command):
                self.assert_headless_block(command, "auto", "headless-push")
        for command in HEADLESS_GH_VARIANTS:
            with self.subTest(command=command):
                self.assert_headless_block(command, "auto", "headless-gh")

    def test_auto_payload_with_unknown_agent_type_is_blocked_headless(self):
        self.assert_headless_block("git push origin feat/x", "auto", "headless-push", agent_type="some-other-agent")

    def test_headless_rules_cannot_be_released_by_permissions(self):
        self.allow("headless-push", "headless-gh", "git-push", "pr-release")
        for command, rule in (("git push origin feat/x", "headless-push"), ("gh pr view 1", "headless-gh")):
            for profile in ALL_PROFILES:
                with self.subTest(profile=profile, command=command):
                    self.assert_headless_block(command, profile, rule)

    def test_headless_commit_and_reads_are_still_allowed_for_the_orchestrator(self):
        for command in ("git commit -m x", "git status", "git diff", "git log --oneline"):
            code, err = self.run_full("Bash", {"command": command}, "orchestrator", env={"LOOP_HEADLESS": "1"})
            self.assertEqual(code, PASSED, (command, err))
        code, err = self.run_full("Bash", {"command": "git commit -m x"}, "auto", env={"LOOP_HEADLESS": "1"})
        self.assertEqual(code, PASSED, err)

    def test_headless_value_other_than_1_does_not_enable_it(self):
        for value in ("0", "true", ""):
            code, err = self.run_full("Bash", {"command": "git push origin feat/x"}, "auto", env={"LOOP_HEADLESS": value})
            self.assertEqual(code, PASSED, (value, err))

    def test_is_headless_reads_the_environment(self):
        guard = load_guard_module()
        self.patch_environ(LOOP_HEADLESS="1")
        self.assertTrue(guard.is_headless())
        os.environ["LOOP_HEADLESS"] = "0"
        self.assertFalse(guard.is_headless())
        os.environ.pop("LOOP_HEADLESS")
        self.assertFalse(guard.is_headless())

    def test_check_bash_function_applies_only_the_listed_rules(self):
        guard = load_guard_module()
        hits = guard.check_bash("git push origin feat/x", ["headless-push", "headless-gh"], {})
        self.assertIn("headless-push", [rule for rule, _ in hits])
        self.assertEqual(guard.check_bash("git push origin feat/x", [], {}), [])
        gh_hits = guard.check_bash("gh pr view 1", ["headless-gh"], {})
        self.assertEqual([rule for rule, _ in gh_hits], ["headless-gh"])


class TestNotHeadless(V1GuardCase):
    """HEADLESS-03: without LOOP_HEADLESS nothing changes."""

    def test_auto_payload_without_agent_type_passes(self):
        for command in HEADLESS_PUSH_VARIANTS + HEADLESS_GH_VARIANTS + ["git commit -m x"]:
            with self.subTest(command=command):
                code, err = self.run_full("Bash", {"command": command}, "auto")
                self.assertEqual(code, PASSED, err)

    def test_auto_with_unknown_agent_type_passes(self):
        self.assertEqual(self.run_guard("Bash", {"command": "git push origin feat/x"}, "auto", agent_type="Explore"), PASSED)

    def test_orchestrator_feature_push_is_not_a_headless_block(self):
        code, err = self.run_full("Bash", {"command": "git push origin feat/x"}, "orchestrator")
        self.assertEqual(code, BLOCKED)  # the old git-push rule (push only via harness.py deliver)
        self.assertIn("git-push", err)
        self.assertNotIn("headless", err)
        self.allow("git-push")
        code, err = self.run_full("Bash", {"command": "git push origin HEAD:refs/heads/feat/x"}, "orchestrator")
        self.assertEqual(code, PASSED, err)

    def test_orchestrator_gh_pr_view_is_not_blocked(self):
        self.assertEqual(self.bash("gh pr view 12", "orchestrator"), PASSED)
        self.assertEqual(self.bash("gh.exe api repos/o/r", "orchestrator"), PASSED)

    def test_protected_and_forced_push_stay_blocked_even_when_released(self):
        self.allow("git-push")
        for command, rule in (("git push origin developer", "push-protected"), ("git push origin HEAD:main", "push-protected"),
                              ("git push --force origin feat/x", "push-force"), ("git push origin +feat/x", "push-force"),
                              ("git push -f origin feat/x", "push-force")):
            for profile in ("orchestrator", "executor"):
                with self.subTest(command=command, profile=profile):
                    code, err = self.run_full("Bash", {"command": command}, profile)
                    self.assertEqual(code, BLOCKED, err)
                    self.assertIn(rule, err)

    def test_pr_base_env_makes_that_branch_protected(self):
        self.allow("git-push")
        code, err = self.run_full("Bash", {"command": "git push origin release/9"}, "orchestrator", env={"LOOP_PR_BASE": "release/9"})
        self.assertEqual(code, BLOCKED, err)
        self.assertIn("push-protected", err)


class TestSubagentGitWrites(V1GuardCase):
    """Subagents (agent_type under --profile auto) never run write git; reads pass."""

    def test_git_writes_blocked_for_every_subagent_headless_or_not(self):
        for agent in ("executor-back", "verifier", "spec-writer"):
            for command in ("git commit -m x", "git add src/a.ts", "git add -A", "git push origin feat/x", "git checkout -b x",
                            "git stash", "git reset --hard HEAD~1", "git merge feat/y"):
                for env in ({}, {"LOOP_HEADLESS": "1"}):
                    with self.subTest(agent=agent, command=command, env=env):
                        code, err = self.run_full("Bash", {"command": command}, "auto", env=env, agent_type=agent)
                        self.assertEqual(code, BLOCKED, err)

    def test_commit_add_push_blocked_for_every_agent_type(self):
        for agent in SUBAGENTS:
            for command in ("git commit -m x", "git add a.ts", "git push origin feat/x"):
                with self.subTest(agent=agent, command=command):
                    code, err = self.run_full("Bash", {"command": command}, "auto", agent_type=agent)
                    self.assertEqual(code, BLOCKED, err)

    def test_plugin_prefix_is_stripped(self):
        for agent in ("executor-back", "spec-writer", "verifier"):
            code, _ = self.run_full("Bash", {"command": "git commit -m x"}, "auto", agent_type="loop-engineering-harness:" + agent)
            self.assertEqual(code, BLOCKED, agent)

    def test_read_only_git_passes_for_subagents(self):
        for agent in ("executor-generic", "verifier-flow", "spec-reviewer"):
            for command in ("git status", "git diff --stat", "git log --oneline -5", "git show HEAD", "git rev-parse HEAD"):
                with self.subTest(agent=agent, command=command):
                    code, err = self.run_full("Bash", {"command": command}, "auto", agent_type=agent)
                    self.assertEqual(code, PASSED, err)

    def test_explicit_profiles_block_git_writes_too(self):
        for profile in ("executor", "verifier", "spec"):
            for command in ("git commit -m x", "git add a.ts", "git push origin feat/x"):
                with self.subTest(profile=profile, command=command):
                    self.assertEqual(self.bash(command, profile), BLOCKED)


class TestPerTaskScope(V1GuardCase):
    """SCOPE.json `tasks`: an executor whose transcript carries `SCOPE_TASK: <slug>/T<n>` is confined to that task's PATHS."""

    def setUp(self):
        super().setUp()
        self.write_json(self.specs("project", "loop", "json", "SCOPE.json"),
                        {"features": {"f": ["a1", "a2"]}, "tasks": {"f/T1": ["a1"], "f/T2": ["a2"]}})

    def transcript(self, text, name="agent-1.jsonl"):
        path = os.path.join(self.tmp, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
        return path

    def edit_with(self, relative, transcript=None, tool="Edit"):
        extra = {"agent_type": "executor-back", "agent_id": "1"}
        if transcript:
            extra["agent_transcript_path"] = transcript
        return self.run_guard(tool, {"file_path": os.path.join(self.project, relative)}, "auto", **extra)

    def test_task_mark_confines_to_the_task_paths(self):
        path = self.transcript("Implement.\nSCOPE_FEATURE: f\nSCOPE_TASK: f/T2\n")
        self.assertEqual(self.edit_with("a2/x.ts", path), PASSED)
        self.assertEqual(self.edit_with("a1/x.ts", path), BLOCKED)
        self.assertEqual(self.edit_with("a2/x.ts", path, tool="Write"), PASSED)
        self.assertEqual(self.edit_with("a1/x.ts", path, tool="Write"), BLOCKED)

    def test_the_other_task_is_confined_the_other_way(self):
        path = self.transcript("SCOPE_TASK: f/T1")
        self.assertEqual(self.edit_with("a1/x.ts", path), PASSED)
        self.assertEqual(self.edit_with("a2/x.ts", path), BLOCKED)

    def test_without_the_mark_both_paths_are_allowed(self):
        path = self.transcript("Implement the feature.\nSCOPE_FEATURE: f\n")
        self.assertEqual(self.edit_with("a1/x.ts", path), PASSED)
        self.assertEqual(self.edit_with("a2/x.ts", path), PASSED)
        self.assertEqual(self.edit_with("b/x.ts", path), BLOCKED)

    def test_without_any_transcript_the_union_applies(self):
        self.assertEqual(self.edit_with("a1/x.ts"), PASSED)
        self.assertEqual(self.edit_with("a2/x.ts"), PASSED)
        self.assertEqual(self.edit_with("b/x.ts"), BLOCKED)

    def test_unknown_task_falls_back_to_the_feature_scope(self):
        path = self.transcript("SCOPE_FEATURE: f\nSCOPE_TASK: f/T9")
        self.assertEqual(self.edit_with("a1/x.ts", path), PASSED)
        self.assertEqual(self.edit_with("a2/x.ts", path), PASSED)

    def test_transcript_resolved_from_agent_id_and_transcript_path(self):
        session = os.path.join(self.tmp, "session")
        os.makedirs(os.path.join(session, "subagents"))
        with open(os.path.join(session, "subagents", "agent-77.jsonl"), "w", encoding="utf-8") as handle:
            handle.write("SCOPE_TASK: f/T2")
        common = dict(agent_type="executor-back", agent_id="77", transcript_path=session + ".jsonl")
        self.assertEqual(self.run_guard("Edit", {"file_path": os.path.join(self.project, "a1", "x.ts")}, "auto", **common), BLOCKED)
        self.assertEqual(self.run_guard("Edit", {"file_path": os.path.join(self.project, "a2", "x.ts")}, "auto", **common), PASSED)

    def test_shell_redirect_follows_the_task_scope(self):
        path = self.transcript("SCOPE_TASK: f/T2")

        def run(target):
            return self.run_guard("Bash", {"command": "echo x > %s" % target}, "auto", agent_type="executor-back", agent_id="1", agent_transcript_path=path)
        self.assertEqual(run("a2/out.txt"), PASSED)
        self.assertEqual(run("a1/out.txt"), BLOCKED)

    def test_task_scope_functions(self):
        guard = load_guard_module()
        specs_dir = self.specs()
        self.assertEqual(guard.load_scope(specs_dir, "f", "f/T1"), ["a1"])
        self.assertEqual(guard.load_scope(specs_dir, "f", "f/T2"), ["a2"])
        self.assertEqual(guard.load_scope(specs_dir, "f"), ["a1", "a2"])
        self.assertEqual(guard.load_scope(specs_dir, None, None), ["a1", "a2"])
        path = self.transcript("SCOPE_TASK: f/T2")
        self.assertEqual(guard.executor_task({"agent_transcript_path": path}), "f/T2")
        self.assertIsNone(guard.executor_task({"agent_transcript_path": self.transcript("nothing", "other.jsonl")}))
        self.assertIsNone(guard.executor_task({}))


SECRET_PATHS = {
    "skills-shared": ["~/.claude/skills/_shared/.env", "$HOME/.claude/skills/_shared/.env", "%USERPROFILE%\\.claude\\skills\\_shared\\.env",
                      "%USERPROFILE%/.claude/skills/_shared/.env", "C:\\Users\\Iago\\.claude\\skills\\_shared\\.env",
                      "C:/Users/Iago/.claude/skills/_shared/.env", "/home/iago/.claude/skills/_shared/.env"],
    "project-env-test": [".specs/project/.env.test", ".specs\\project\\.env.test", "C:/proj/.specs/project/.env.test", "C:\\proj\\.specs\\project\\.env.test"],
    "app-data-env": ["~/.loop-harness/.env", "$HOME/.loop-harness/.env", "%USERPROFILE%\\.loop-harness\\.env", "C:\\Users\\Iago\\.loop-harness\\.env",
                     "C:/Users/Iago/.loop-harness/.env"],
    "plugin-shared": ["plugin/shared/.env", "plugin\\shared\\.env", "/plugin/shared/.env", "C:\\repo\\plugin\\shared\\.env", "${CLAUDE_PLUGIN_ROOT}/shared/.env"],
}
SECRET_EXAMPLES = ["~/.claude/skills/_shared/.env.example", ".specs/project/.env.test.example", "~/.loop-harness/.env.example",
                   "plugin/shared/.env.example", "C:\\repo\\plugin\\shared\\.env.example", "%USERPROFILE%\\.claude\\skills\\_shared\\.env.example"]


class TestSecretsLayers(V1GuardCase):
    """ENV-01 / ENV-04: every credential layer is blocked for the executor (tools and shell), `.env.example` is not."""

    def all_paths(self):
        for layer, paths in SECRET_PATHS.items():
            for path in paths:
                yield layer, path

    def test_read_write_edit_tools_block_every_spelling(self):
        for layer, path in self.all_paths():
            for tool, tool_input in (("Read", {"file_path": path}), ("Write", {"file_path": path, "content": "x"}),
                                     ("Edit", {"file_path": path, "old_string": "a", "new_string": "b"})):
                with self.subTest(layer=layer, path=path, tool=tool):
                    self.assertEqual(self.run_guard(tool, tool_input, "executor"), BLOCKED)

    def test_grep_on_a_secret_path_is_blocked(self):
        for layer, path in self.all_paths():
            with self.subTest(layer=layer, path=path):
                self.assertEqual(self.run_guard("Grep", {"pattern": "KEY", "path": path}, "executor"), BLOCKED)

    def test_shell_readers_block_every_spelling(self):
        for layer, path in self.all_paths():
            for template in ("cat %s", "type %s", 'cat "%s"', "Get-Content %s", "Get-Content -Path '%s'", "grep KEY %s"):
                with self.subTest(layer=layer, path=path, template=template):
                    self.assertEqual(self.bash(template % path, "executor"), BLOCKED)
            with self.subTest(layer=layer, path=path, tool="PowerShell"):
                self.assertEqual(self.run_guard("PowerShell", {"command": "Get-Content %s" % path}, "executor"), BLOCKED)

    def test_shell_writes_block_every_spelling(self):
        for layer, path in self.all_paths():
            for template in ("echo K=1 > %s", "echo K=1 | tee %s", "Set-Content %s 'K=1'"):
                with self.subTest(layer=layer, path=path, template=template):
                    self.assertEqual(self.bash(template % path, "executor"), BLOCKED)

    def test_every_profile_that_carries_secrets_blocks_them(self):
        for profile in ("executor", "verifier", "spec", "orchestrator", "planning"):
            for layer, paths in SECRET_PATHS.items():
                with self.subTest(profile=profile, layer=layer):
                    self.assertEqual(self.run_guard("Read", {"file_path": paths[0]}, profile), BLOCKED)
                    self.assertEqual(self.bash("cat %s" % paths[0], profile), BLOCKED)

    def test_auto_subagents_block_secrets(self):
        for agent in SUBAGENTS:
            for layer, paths in SECRET_PATHS.items():
                with self.subTest(agent=agent, layer=layer):
                    self.assertEqual(self.run_guard("Read", {"file_path": paths[0]}, "auto", agent_type=agent), BLOCKED)

    def test_env_example_variants_pass(self):
        for path in SECRET_EXAMPLES:
            for tool, tool_input in (("Read", {"file_path": path}), ("Write", {"file_path": path, "content": "x"}),
                                     ("Edit", {"file_path": path, "old_string": "a", "new_string": "b"})):
                with self.subTest(path=path, tool=tool):
                    code, err = self.run_full(tool, tool_input, "orchestrator")
                    self.assertNotIn("secrets", err)
            for template in ("cat %s", "type %s", "Get-Content %s"):
                with self.subTest(path=path, template=template):
                    code, err = self.run_full("Bash", {"command": template % path}, "executor")
                    self.assertNotIn("secrets", err)

    def test_env_example_read_exit_zero_for_executor(self):
        for path in SECRET_EXAMPLES:
            with self.subTest(path=path):
                self.assertEqual(self.run_guard("Read", {"file_path": path}, "executor"), PASSED)
                self.assertEqual(self.bash("cat %s" % path, "executor"), PASSED)

    def test_similar_but_harmless_names_pass(self):
        for path in ("~/.claude/skills/_shared/config.json", "~/.claude/skills/_shared/MCP_TOOLS.md", ".specs/project/TEST-ENVIRONMENT.md",
                     "~/.loop-harness/config.json", "src/environment.ts", "plugin/shared/README.md"):
            with self.subTest(path=path):
                self.assertEqual(self.run_guard("Read", {"file_path": path}, "executor"), PASSED)
                self.assertEqual(self.bash("cat %s" % path, "executor"), PASSED)


# ---- rule probes: for each rule one blocked and one allowed case, asserted against the stderr of the real guard.
# Used by the AUDIT-06 coverage report in test_guard_cookbook.py. Entry: rule -> dict(profile, block=(tool, input), allow=(tool, input),
# optional: scope/perm/perm_allow setup, headless, block_profile/block_rule when the block comes from another profile or rule id).
def _bash(command):
    return ("Bash", {"command": command})


def _file(tool, path):
    return (tool, {"file_path": path})


RULE_PROBES = {
    "git-write": dict(profile="executor", block=_bash("git commit -m x"), allow=_bash("git status")),
    "git-push": dict(profile="executor", block=_bash("git push origin feat/x"), allow=_bash("git log --oneline")),
    "pr-release": dict(profile="executor", block=_bash("gh release create v1"), allow=_bash("gh pr view 1")),
    "prod-write": dict(profile="executor", block=("mcp__prod-api__create_user", {}), allow=("mcp__prod-api__get_user", {})),
    "dev-write": dict(profile="executor", block=("mcp__sqlite__query", {"query": "INSERT INTO t VALUES (1)"}), allow=("mcp__sqlite__query", {"query": "SELECT 1"})),
    "outbound-msg": dict(profile="executor", block=("mcp__jira__create_issue", {}), allow=("mcp__jira__get_issue", {})),
    "secrets": dict(profile="executor", block=_file("Read", "~/.claude/skills/_shared/.env"), allow=_file("Read", "~/.claude/skills/_shared/.env.example")),
    "install": dict(profile="executor", block=_bash("npm install left-pad"), allow=_bash("npm run build")),
    "state-write": dict(profile="executor", block=_file("Edit", "@project/.specs/project/ROADMAP.md"), allow=_file("Edit", "@project/src/a.ts")),
    "project-deny": dict(profile="executor", perm={"deny_bash_regex": [r"\bcurl\b.*tracking"]}, block=_bash("curl https://x/tracking"), allow=_bash("curl https://x/orders")),
    "path-scope": dict(profile="executor", scope={"features": {"a": ["apps/a"]}}, block=_file("Edit", "@project/apps/b/x.ts"), allow=_file("Edit", "@project/apps/a/x.ts")),
    "repo-only": dict(profile="orchestrator", scope={"roots": ["@project"]}, block=_file("Edit", os.path.join(HOME, "not-a-worktree-v1", "x.ts")), allow=_file("Edit", "@project/src/x.ts")),
    "delivery": dict(profile="executor", block=_bash("python harness.py deliver --project x"), allow=_bash("python harness.py doctor")),
    "state-script": dict(profile="executor", block=_bash("python harness.py rules write"), allow=_bash("python harness.py state show")),
    "loop-run": dict(profile="executor", block=_bash("python run_loop.py"), allow=_bash("python harness.py doctor")),
    "approval-write": dict(profile="orchestrator", block=_file("Write", "@project/.specs/project/ROADMAP-APPROVED.json"), allow=_file("Write", "@project/src/a.ts")),
    "panel-trigger": dict(profile="orchestrator", block=_bash("curl -X POST http://localhost:8765/p/x/api/loop/start"), allow=_bash("curl http://localhost:8765/p/x/api/state")),
    "grafana-write": dict(profile="executor", block=("mcp__grafana__update_dashboard", {}), allow=("mcp__grafana__query_prometheus", {})),
    "sql-write": dict(profile="executor", block=("mcp__mcp-mysql-dev__mysql_query", {"query": "DELETE FROM t"}), allow=("mcp__mcp-mysql-dev__mysql_query", {"query": "SELECT 1"})),
    "migration-target": dict(profile="executor", block=_bash("ASPNETCORE_ENVIRONMENT=Production dotnet ef database update"), allow=_bash("ASPNETCORE_ENVIRONMENT=Development dotnet ef database update")),
    "specs-only": dict(profile="spec", block=_file("Write", "@project/src/x.ts"), allow=_file("Write", "@project/.specs/features/f/spec.md")),
    "map-docs": dict(profile="spec", block=_file("Write", "@project/docs/codebase/STACK.md"), allow=_file("Write", "@project/docs/codebase/STACK.md"), block_profile="planning", block_rule="specs-only"),
    "git-add-all": dict(profile="orchestrator", block=_bash("git add ."), allow=_bash("git add src/a.ts")),
    "capabilities-block": dict(profile="planning", block=("Edit", {"file_path": "@project/.specs/project/PROJECT.md", "old_string": "<!-- maestro:capabilities -->", "new_string": "x"}),
                               allow=("Edit", {"file_path": "@project/.specs/project/PROJECT.md", "old_string": "a", "new_string": "b"})),
    "subprojects-only": dict(profile="subprojects", block=_file("Write", "@project/.specs/project/OTHER.md"), allow=_file("Write", "@project/.specs/project/SUBPROJECTS-SUGGESTED.json")),
    "system-only": dict(profile="systemmap", block=_file("Write", "@project/docs/codebase/OTHER.md"), allow=_file("Write", "@project/docs/codebase/SYSTEM.md")),
    "push-protected": dict(profile="orchestrator", perm_allow=["git-push"], block=_bash("git push origin developer"), allow=_bash("git push origin feat/x")),
    "push-force": dict(profile="orchestrator", perm_allow=["git-push"], block=_bash("git push --force origin feat/x"), allow=_bash("git push origin feat/x")),
    "headless-push": dict(profile="orchestrator", headless=True, block=_bash("git push origin feat/x"), allow=_bash("git commit -m x")),
    "permissions-write": dict(profile="orchestrator", headless=True, block=_file("Write", "@project/.specs/project/loop/json/PERMISSIONS.json"), allow=_file("Write", "@project/src/a.ts")),
    "headless-gh": dict(profile="orchestrator", headless=True, block=_bash("gh pr view 1"), allow=_bash("git status")),
}


class TestRuleProbes(V1GuardCase):
    """One blocked and one allowed case per rule (table above), checked on the rule id in the guard's stderr."""

    def prepare(self, probe):
        if probe.get("scope"):
            scope = json.loads(json.dumps(probe["scope"]).replace("@project", self.project.replace("\\", "\\\\")))
            self.write_json(self.specs("project", "loop", "json", "SCOPE.json"), scope)
        if probe.get("perm"):
            self.write_json(self.specs("project", "loop", "json", "PERMISSIONS.json"), probe["perm"])
        if probe.get("perm_allow"):
            self.allow(*probe["perm_allow"])

    def resolve(self, case):
        tool, tool_input = case
        return tool, {key: (value.replace("@project", self.project) if isinstance(value, str) else value) for key, value in tool_input.items()}

    def test_every_probe_blocks_and_allows(self):
        for rule, probe in sorted(RULE_PROBES.items()):
            with self.subTest(rule=rule):
                for stale in ("SCOPE.json", "PERMISSIONS.json"):
                    try:
                        os.remove(self.specs("project", "loop", "json", stale))
                    except OSError:
                        pass
                self.prepare(probe)
                env = {"LOOP_HEADLESS": "1"} if probe.get("headless") else {}
                tool, tool_input = self.resolve(probe["block"])
                code, err = self.run_full(tool, tool_input, probe.get("block_profile", probe["profile"]), env=env)
                self.assertEqual(code, BLOCKED, (rule, err))
                self.assertIn(probe.get("block_rule", rule), err, rule)
                tool, tool_input = self.resolve(probe["allow"])
                code, err = self.run_full(tool, tool_input, probe["profile"], env=env)
                self.assertEqual(code, PASSED, (rule, err))

    def test_probes_cover_the_rules_the_guard_defines(self):
        guard = load_guard_module()
        known = {rule for rules in guard.PROFILES.values() for rule in rules} | set(guard.HARD)
        self.assertEqual(sorted(known - set(RULE_PROBES)), [], "rules without a probe")
        self.assertEqual(sorted(set(RULE_PROBES) - known), [], "probe for a rule the guard no longer has")


class TestPermissionsWrite(V1GuardCase):
    """HARD rule `permissions-write`: headless loop never writes PERMISSIONS.json (the user's releases); nothing changes without LOOP_HEADLESS."""

    PERM = ".specs/project/loop/json/PERMISSIONS.json"

    def headless(self, tool, tool_input, profile):
        return self.run_full(tool, tool_input, profile, env={"LOOP_HEADLESS": "1"})

    def test_tools_blocked_headless_in_every_profile(self):
        path = os.path.join(self.project, *self.PERM.split("/"))
        for profile in ALL_PROFILES + ("planning",):
            for tool, tool_input in (("Write", {"file_path": path, "content": "{}"}), ("Edit", {"file_path": path, "old_string": "a", "new_string": "b"}),
                                     ("NotebookEdit", {"notebook_path": path})):
                with self.subTest(profile=profile, tool=tool):
                    code, err = self.headless(tool, tool_input, profile)
                    self.assertEqual(code, BLOCKED, err)
                    self.assertIn("permissions-write", err)

    def test_shell_writes_and_cli_blocked_headless(self):
        for command in ("echo {} > %s" % self.PERM, "echo {} | tee %s" % self.PERM, "Set-Content %s '{}'" % self.PERM,
                        "python harness.py rules write", "python rules.py write --id x", "python harness.py rules allow x"):
            for profile in ("orchestrator", "executor"):
                with self.subTest(command=command, profile=profile):
                    code, err = self.headless("Bash", {"command": command}, profile)
                    self.assertEqual(code, BLOCKED, err)
                    self.assertIn("permissions-write", err)

    def test_not_releasable_by_permissions(self):
        self.allow("permissions-write")
        code, err = self.headless("Bash", {"command": "echo {} > %s" % self.PERM}, "orchestrator")
        self.assertEqual(code, BLOCKED, err)

    def test_scope_json_and_other_files_stay_allowed_headless(self):
        scope = os.path.join(self.project, ".specs", "project", "loop", "json", "SCOPE.json")
        code, err = self.headless("Write", {"file_path": scope, "content": "{}"}, "orchestrator")
        self.assertEqual(code, PASSED, err)
        code, err = self.headless("Bash", {"command": "echo {} > .specs/project/loop/json/SCOPE.json"}, "orchestrator")
        self.assertEqual(code, PASSED, err)
        code, err = self.headless("Bash", {"command": "cat %s" % self.PERM}, "orchestrator")
        self.assertEqual(code, PASSED, err)

    def test_nothing_changes_without_headless(self):
        path = os.path.join(self.project, *self.PERM.split("/"))
        code, err = self.run_full("Write", {"file_path": path, "content": "{}"}, "orchestrator")
        self.assertEqual(code, PASSED, err)
        code, err = self.run_full("Bash", {"command": "echo {} > %s" % self.PERM}, "orchestrator")
        self.assertNotIn("permissions-write", err)


class TestRoadmapMapProfile(V1GuardCase):
    """Profile `roadmapmap` (roadmap generation with --map-first): planning rules + map-docs."""

    def write(self, relative, **kwargs):
        return self.run_guard("Write", {"file_path": os.path.join(self.project, relative), "content": "x"}, "roadmapmap", **kwargs)

    def test_map_docs_and_roadmap_are_writable(self):
        self.assertEqual(self.write("docs/codebase/STACK.md"), PASSED)
        self.assertEqual(self.write("docs/codebase/SYSTEM.md"), PASSED)
        self.assertEqual(self.write(".specs/project/ROADMAP.md"), PASSED)
        self.assertEqual(self.write(".specs/project/FLOWS.md"), PASSED)

    def test_app_code_and_other_folders_blocked(self):
        for relative in ("src/app.ts", "app/backend/x.py", "docs/other.md", "README.md"):
            with self.subTest(relative=relative):
                code, err = self.run_full("Write", {"file_path": os.path.join(self.project, relative), "content": "x"}, "roadmapmap")
                self.assertEqual(code, BLOCKED, err)
                self.assertIn("specs-only", err)

    def test_git_push_commit_and_approval_blocked(self):
        for command in ("git push origin feat/x", "git commit -m x", "gh pr create"):
            with self.subTest(command=command):
                self.assertEqual(self.bash(command, "roadmapmap"), BLOCKED)
        self.assertEqual(self.write(".specs/project/ROADMAP-APPROVED.json"), BLOCKED)

    def test_secrets_and_reads_follow_planning(self):
        self.assertEqual(self.run_guard("Read", {"file_path": "~/.loop-harness/.env"}, "roadmapmap"), BLOCKED)
        self.assertEqual(self.bash("git status", "roadmapmap"), PASSED)
        self.assertEqual(self.run_guard("Read", {"file_path": os.path.join(self.project, "src", "a.ts")}, "roadmapmap"), PASSED)

    def test_headless_push_is_still_blocked_for_the_profile(self):
        code, err = self.run_full("Bash", {"command": "git push origin feat/x"}, "roadmapmap", env={"LOOP_HEADLESS": "1"})
        self.assertEqual(code, BLOCKED, err)


if __name__ == "__main__":
    unittest.main()
