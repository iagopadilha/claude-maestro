"""Test credential layers (engine/credential_check.py), the legacy ACCESS.json migration and the read-only Grafana access of the loop turn.

Same order as the toolkit's testenv.py: real env > project .env.test > ~/.loop-harness/.env > toolkit shared/.env > user-level shared .env
(only when LOOP_USER_SHARED_DIR or config.json `user_shared_dir` names a folder: no default folder).
The last two are the shared layers: always read, lowest priority. Everything lives in temporary folders (fake data dir, fake
plugin, fake toolkit named by `dev_toolkit_dir` in the fake config.json, fake user shared folder, fake ~/.claude.json); no real home file is read.
"""
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import access_settings  # noqa: E402
import credential_check  # noqa: E402
from app_paths import envload  # noqa: E402
import grafana_access  # noqa: E402
import run_loop  # noqa: E402
from harness_backend.config import skill_locator  # noqa: E402

OVERRIDDEN = ("LOOP_DATA_DIR", "LOOP_SKILL_DIR", "LOOP_USER_SHARED_DIR", "LOOP_CLAUDE_CONFIG_FILE", "LOOP_ENV_FILE",
              "QA_LAYER_NAME", "QA_SECRET_TOKEN", "TEST_USER_EMAILS", "TEST_USER_PASSWORDS", "TEST_USER_ROLES")
SECRET_FRAGMENT = "s3cr3t-value-must-never-leak"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class LayersTestCase(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.get(name) for name in OVERRIDDEN}
        self.tmp = tempfile.mkdtemp(prefix="credlayers-")
        self.root = os.path.join(self.tmp, "project")
        self.data_dir = os.path.join(self.tmp, "data")
        self.user_shared = os.path.join(self.tmp, "user-shared")
        skill = os.path.join(self.tmp, "plugin", "skills", "orchestrator")
        engine_env.create_fake_skill(skill, copy_guard=False)
        self.toolkit = os.path.join(self.tmp, "toolkit")
        os.makedirs(os.path.join(self.toolkit, "skills"))
        self.plugin_shared_env = os.path.join(self.toolkit, "shared", ".env")  # the toolkit's shared/.env is the "plugin" layer
        for name in OVERRIDDEN:
            os.environ.pop(name, None)
        os.environ.update({"LOOP_DATA_DIR": self.data_dir, "LOOP_SKILL_DIR": skill, "LOOP_USER_SHARED_DIR": self.user_shared,
                           "LOOP_CLAUDE_CONFIG_FILE": os.path.join(self.tmp, "claude.json")})
        os.makedirs(os.path.join(self.root, ".specs", "project"))
        self.write_config()

    def write_config(self, **extra):
        write(os.path.join(self.data_dir, "config.json"), json.dumps(dict({"dev_toolkit_dir": self.toolkit}, **extra)))

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def project_env(self, text):
        write(os.path.join(self.root, ".specs", "project", ".env.test"), text)

    def machine_env(self, text):
        write(os.path.join(self.data_dir, ".env"), text)

    def plugin_env(self, text):
        write(self.plugin_shared_env, text)

    def user_env(self, text):
        write(os.path.join(self.user_shared, ".env"), text)

    def lookup(self, name="QA_LAYER_NAME"):
        return credential_check.Resolution(self.root).lookup(name)


class TestLayerOrder(LayersTestCase):
    def test_each_layer_wins_over_the_ones_below_it(self):
        self.user_env("QA_LAYER_NAME=from-user-shared\n")
        self.assertEqual(self.lookup(), ("shared", credential_check.user_shared_label()))
        self.plugin_env("QA_LAYER_NAME=from-plugin\n")
        self.assertEqual(self.lookup(), ("shared", credential_check.PLUGIN_SHARED_LABEL))
        self.machine_env("QA_LAYER_NAME=from-machine\n")
        self.assertEqual(self.lookup(), ("machine", credential_check.MACHINE_LABEL))
        self.project_env("QA_LAYER_NAME=from-project\n")
        self.assertEqual(self.lookup(), ("project", credential_check.PROJECT_LABEL))
        os.environ["QA_LAYER_NAME"] = "from-the-real-environment"
        self.assertEqual(self.lookup(), ("env", credential_check.ENVIRONMENT_LABEL))

    def test_missing_name_and_blank_value(self):
        self.project_env("QA_LAYER_NAME=\n")
        self.assertEqual(self.lookup(), ("missing", ""))

    def test_a_blank_project_line_does_not_hide_a_lower_layer(self):
        self.project_env("QA_LAYER_NAME=\n")
        self.machine_env("QA_LAYER_NAME=from-machine\n")
        self.assertEqual(self.lookup()[0], "machine")


class TestSharedLayers(LayersTestCase):
    def test_a_name_only_a_shared_layer_defines_resolves_with_source_shared(self):
        self.plugin_env("QA_LAYER_NAME=plugin-value\n")
        self.assertEqual(self.lookup(), ("shared", credential_check.PLUGIN_SHARED_LABEL))
        self.user_env("QA_SECRET_TOKEN=user-value\n")
        self.assertEqual(self.lookup("QA_SECRET_TOKEN"), ("shared", credential_check.user_shared_label()))

    def test_a_legacy_false_in_access_json_changes_nothing(self):
        write(access_settings.access_file_path(self.root), json.dumps({"grafana_read": False, "shared_fallback": False}))
        self.user_env("QA_LAYER_NAME=user-value\n")
        self.assertEqual(self.lookup()[0], "shared")
        self.assertEqual(credential_check.Resolution(self.root, accept_shared=False).lookup("QA_LAYER_NAME")[0], "shared")

    def test_the_machine_file_is_read(self):
        self.machine_env("QA_LAYER_NAME=machine-value\n")
        self.assertEqual(self.lookup()[0], "machine")

    def test_a_project_value_beats_a_shared_one(self):
        self.user_env("QA_LAYER_NAME=user-value\n")
        self.project_env("QA_LAYER_NAME=project-value\n")
        self.assertEqual(self.lookup()[0], "project")

    def test_test_users_of_the_shared_account_resolve_as_shared(self):
        self.user_env("TEST_USER_EMAILS=generic@x.com\nTEST_USER_PASSWORDS=%s\n" % SECRET_FRAGMENT)
        self.assertEqual(self.lookup("TEST_USER_1_EMAIL")[0], "shared")
        self.assertEqual(self.lookup("TEST_USER_1_PASSWORD")[0], "shared")

    def test_envload_copying_a_plugin_value_into_the_environment_keeps_the_shared_source(self):
        self.plugin_env("QA_LAYER_NAME=plugin-value\n")
        os.environ["QA_LAYER_NAME"] = "plugin-value"  # what envload.load() does to os.environ
        self.assertEqual(self.lookup()[0], "shared")

    def test_a_value_envload_copied_from_an_app_env_is_not_the_real_environment(self):
        write(os.path.join(self.tmp, "plugin", "shared", ".env"), "QA_LAYER_NAME=from-the-app-env\n")  # the app's own settings file
        self.addCleanup(envload.LOADED_VALUES.pop, "QA_LAYER_NAME", None)
        envload.load_all()
        self.assertEqual(os.environ["QA_LAYER_NAME"], "from-the-app-env")
        self.assertEqual(self.lookup(), ("missing", ""))
        os.environ["QA_LAYER_NAME"] = "set-by-the-user-later"  # same name, another value: now it is a real variable
        self.assertEqual(self.lookup(), ("env", credential_check.ENVIRONMENT_LABEL))

    def test_the_user_shared_label_names_the_configured_folder(self):
        self.assertEqual(credential_check.user_shared_label(), "user shared .env (%s)" % self.user_shared)
        del os.environ["LOOP_USER_SHARED_DIR"]
        self.assertEqual(credential_check.user_shared_label(), credential_check.USER_SHARED_LABEL)
        self.assertEqual(credential_check.USER_SHARED_LABEL, "user shared .env")

    def test_layer_report_names_what_each_layer_defines_without_values(self):
        self.project_env("QA_LAYER_NAME=a\n")
        self.user_env("QA_LAYER_NAME=b\nQA_SECRET_TOKEN=%s\n" % SECRET_FRAGMENT)
        report = credential_check.Resolution(self.root).layer_report(["QA_LAYER_NAME", "QA_SECRET_TOKEN", "OTHER"])
        by_label = {layer["label"]: layer for layer in report}
        self.assertEqual(by_label[credential_check.PROJECT_LABEL]["names"], ["QA_LAYER_NAME"])
        self.assertEqual(by_label[credential_check.user_shared_label()]["names"], ["QA_LAYER_NAME", "QA_SECRET_TOKEN"])
        self.assertFalse(by_label[credential_check.MACHINE_LABEL]["exists"])
        self.assertNotIn(SECRET_FRAGMENT, json.dumps(report))

    def test_shared_files_found_are_listed_by_label_only(self):
        self.assertEqual(credential_check.Resolution(self.root).shared_files_found(), [])
        self.plugin_env("A=1\n")
        self.user_env("B=2\n")
        self.assertEqual(credential_check.Resolution(self.root).shared_files_found(),
                         [credential_check.PLUGIN_SHARED_LABEL, credential_check.user_shared_label()])

    def test_the_user_shared_folder_comes_from_config_json_when_no_override(self):
        del os.environ["LOOP_USER_SHARED_DIR"]
        self.write_config(user_shared_dir=os.path.join(self.tmp, "configured"))
        self.assertEqual(credential_check.user_shared_env_path(), os.path.join(self.tmp, "configured", ".env"))

    def test_there_is_no_default_user_shared_folder(self):
        del os.environ["LOOP_USER_SHARED_DIR"]
        self.assertEqual(credential_check.user_shared_dir(), "")
        self.assertEqual(credential_check.user_shared_env_path(), "")
        labels = [layer.label for layer in credential_check.Resolution(self.root).layers]
        self.assertNotIn(credential_check.user_shared_label(), labels)
        self.assertEqual(labels, [credential_check.PROJECT_LABEL, credential_check.MACHINE_LABEL, credential_check.PLUGIN_SHARED_LABEL])

    def test_the_plugin_layer_is_the_toolkit_shared_env_with_its_label(self):
        self.assertEqual(credential_check.PLUGIN_SHARED_LABEL, "toolkit shared/.env")
        self.assertEqual(credential_check.toolkit_shared_env_path(), self.plugin_shared_env)
        layers = {layer.label: layer for layer in credential_check.Resolution(self.root).layers}
        self.assertEqual(layers[credential_check.PLUGIN_SHARED_LABEL].path, self.plugin_shared_env)

    def test_without_a_toolkit_there_is_no_plugin_layer(self):
        home = os.path.join(self.tmp, "empty-home")
        os.makedirs(home)
        os.remove(os.path.join(self.data_dir, "config.json"))
        with mock.patch.dict(os.environ, {"HOME": home, "USERPROFILE": home}), \
                mock.patch.object(skill_locator, "REPO_ROOT_DIR", os.path.join(self.tmp, "repo")):
            self.assertEqual(credential_check.toolkit_shared_env_path(), "")
            labels = [layer.label for layer in credential_check.Resolution(self.root).layers]
        self.assertNotIn(credential_check.PLUGIN_SHARED_LABEL, labels)


class TestPreflightMessages(LayersTestCase):
    TEXT = "| User | `$QA_LAYER_NAME` |\n| Token | `$QA_SECRET_TOKEN` |\n"

    def test_only_missing_names_are_problems_and_a_shared_name_is_fine(self):
        self.user_env("QA_SECRET_TOKEN=%s\n" % SECRET_FRAGMENT)
        problems = credential_check.credential_problems(self.root, self.TEXT)
        joined = " | ".join(problems)
        self.assertEqual(len(problems), 1)
        self.assertIn("$QA_LAYER_NAME", joined)
        self.assertIn(".specs/project/.env.test", joined)
        self.assertNotIn("$QA_SECRET_TOKEN", joined)
        self.assertNotIn(SECRET_FRAGMENT, joined)

    def test_a_shared_definition_clears_the_problem(self):
        self.user_env("QA_SECRET_TOKEN=%s\nQA_LAYER_NAME=generic\n" % SECRET_FRAGMENT)
        self.assertEqual(credential_check.credential_problems(self.root, self.TEXT), [])

    def test_the_loop_stops_before_the_first_turn_naming_each_name(self):
        write(os.path.join(self.root, ".specs", "project", "TEST-ENVIRONMENT.md"), self.TEXT)
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"), "# R\n\n- [a] status=pending regression=yes\n")
        self.user_env("QA_SECRET_TOKEN=%s\n" % SECRET_FRAGMENT)
        opts = types.SimpleNamespace(e2e=False)
        problems = run_loop.test_environment_problems(self.root, opts)
        reason = run_loop.test_environment_stop_reason(self.root, opts, problems)
        self.assertIn("$QA_LAYER_NAME", reason)
        self.assertNotIn("$QA_SECRET_TOKEN", reason)
        self.assertNotIn(SECRET_FRAGMENT, reason)


class TestDocumentationPlaceholders(LayersTestCase):
    OLD_HEADER = ("# Test environment (DEV)\n\n> Only `$NAMES`; the values are in `.specs/project/.env.test`. Write `$NAME` for each one.\n\n"
                  "| Admin | `$QA_SECRET_TOKEN` |\n| Lower | `$names` |\n")

    def test_name_and_names_are_never_credential_names(self):
        self.assertEqual(credential_check.mentioned_names(self.OLD_HEADER), ["QA_SECRET_TOKEN"])
        self.assertEqual(credential_check.undefined_names(self.root, self.OLD_HEADER), ["QA_SECRET_TOKEN"])
        self.assertEqual(credential_check.mentioned_names("`$NAMES_EXTRA` `${NAME}` `$NAMES`"), ["NAMES_EXTRA"])


class TestAccessFile(LayersTestCase):
    def test_the_file_is_ignored_and_the_effective_access_is_always_on(self):
        path = access_settings.access_file_path(self.root)
        for content in (None, "", "not json", "[1]", '{"grafana_read": false, "shared_fallback": false}'):
            if content is not None:
                write(path, content)
            self.assertEqual(access_settings.read_access(self.root), {"grafana_read": True, "shared_fallback": True}, content)

    def test_migration_drops_the_legacy_keys_and_keeps_the_rest(self):
        path = access_settings.access_file_path(self.root)
        write(path, json.dumps({"grafana_read": False, "shared_fallback": False, "note": "keep"}))
        self.assertTrue(access_settings.migrate_access_file(self.root))
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle), {"note": "keep"})
        self.assertFalse(access_settings.migrate_access_file(self.root))
        write(path, json.dumps({"shared_fallback": False}))
        self.assertTrue(access_settings.migrate_access_file(self.root))
        self.assertFalse(os.path.exists(path))
        self.assertEqual([name for name in os.listdir(os.path.dirname(path)) if name.endswith(".tmp")], [])

    def test_migration_leaves_a_broken_file_alone(self):
        path = access_settings.access_file_path(self.root)
        write(path, "not json")
        self.assertFalse(access_settings.migrate_access_file(self.root))
        self.assertTrue(os.path.exists(path))


class TestGrafanaReadOnly(LayersTestCase):
    TOKEN = "Bearer-token-that-must-not-reach-argv"

    def write_claude_config(self, servers):
        write(os.environ["LOOP_CLAUDE_CONFIG_FILE"], json.dumps({"mcpServers": servers, "projects": {}}))

    def opts(self, **extra):
        values = {"claude": "claude", "permission_mode": "acceptEdits", "budget": 3, "access_root": self.root, "extra_worktrees": []}
        values.update(extra)
        return types.SimpleNamespace(**values)

    def test_the_config_holds_only_the_grafana_server_and_lives_under_secrets(self):
        self.write_claude_config({"grafana": {"type": "http", "url": "https://g.example/mcp", "headers": {"Authorization": self.TOKEN}},
                                  "zendesk": {"command": "zd"}})
        path = grafana_access.write_mcp_config(self.data_dir)
        self.assertEqual(path, os.path.join(self.data_dir, ".secrets", "grafana-mcp.json"))
        with open(path, encoding="utf-8") as handle:
            content = json.load(handle)
        self.assertEqual(list(content["mcpServers"]), ["grafana"])
        self.assertEqual(content["mcpServers"]["grafana"]["url"], "https://g.example/mcp")

    def test_no_usable_definition_means_the_feature_stays_off(self):
        self.assertEqual(grafana_access.write_mcp_config(self.data_dir), "")  # no ~/.claude.json
        self.write_claude_config({"zendesk": {"command": "zd"}})
        self.assertEqual(grafana_access.write_mcp_config(self.data_dir), "")
        self.write_claude_config({"grafana": {"type": "http"}})
        self.assertEqual(grafana_access.write_mcp_config(self.data_dir), "")

    def test_the_seven_read_tools_and_no_writer(self):
        self.assertEqual(len(grafana_access.READ_TOOLS), 7)
        for tool in grafana_access.READ_TOOLS:
            self.assertTrue(tool.startswith("mcp__grafana__"))
            self.assertNotIn(tool, grafana_access.DENIED_WRITE_TOOLS)
        for writer in ("create_annotation", "update_dashboard", "create_incident"):
            self.assertIn("mcp__grafana__" + writer, grafana_access.DENIED_WRITE_TOOLS)

    def test_the_loop_command_gets_the_grafana_server_and_only_the_read_tools_without_any_toggle(self):
        self.write_claude_config({"grafana": {"type": "http", "url": "https://g.example/mcp", "headers": {"Authorization": self.TOKEN}},
                                  "zendesk": {"command": "zd"}})
        argv = run_loop.claude_command(self.opts(), "go")
        self.assertIn("--strict-mcp-config", argv)
        config = argv[argv.index("--mcp-config") + 1]
        with open(config, encoding="utf-8") as handle:
            self.assertEqual(list(json.load(handle)["mcpServers"]), ["grafana"])
        start = argv.index("--allowedTools") + 1
        self.assertEqual(argv[start:start + 7], list(grafana_access.READ_TOOLS))
        denied = argv[argv.index("--disallowedTools") + 1]
        self.assertIn("mcp__grafana__create_annotation", denied)
        self.assertIn("WebFetch", denied)
        self.assertNotIn(self.TOKEN, " ".join(argv))  # the credentials stay in the file, never in the command line

    def test_without_a_server_definition_the_empty_config_is_used(self):
        argv = run_loop.claude_command(self.opts(), "go")
        self.assertEqual(argv[argv.index("--mcp-config") + 1], run_loop.EMPTY_MCP_CONFIG)
        self.assertNotIn("--allowedTools", argv)


if __name__ == "__main__":
    unittest.main()
