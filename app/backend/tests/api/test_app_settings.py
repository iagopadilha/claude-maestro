"""Settings screen routes (GET/POST /api/app/settings): the list, validation per type, token and confirm, atomic write that keeps the
other keys, reset, caveman availability and the caveman cost comparison. Temporary folders only: LOOP_CLAUDE_HOME points at a temp
dir, so the real ~/.claude and ~/.loop-harness are never read or written."""
import json
import os
import shutil
import tempfile
import threading
import types
import unittest
import urllib.error
import urllib.request

from harness_backend.api.controllers.app_settings_controller import AppSettingsController
from harness_backend.api.http_server import AppHttpServer
from harness_backend.api.router import Request, Router, normalize_response
from harness_backend.config.app_config import AppConfig
from harness_backend.domain.project import Project
from harness_backend.domain.app_settings import SETTING_CATALOG
from harness_backend.engine import runtime_isolation
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.app_config_repository import AppConfigRepository
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.services.app_settings_service import AppSettingsService

TEST_PORT_RANGE = (48400, 48499)
DEFAULTS = {"loop_isolation": True, "loop_caveman": False, "loop_extra_skills": [], "orchestrator_model": "opus", "claude_binary": "",
            "claude_fallback_binaries": [], "max_parallel_maps": 6, "idle_hours": 2, "inventory_giant_lines": 1500, "inventory_giant_kb": 200}
CAVEMAN_SETTINGS = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": 'cat "C:/x/caveman-sessionstart.json"'}]}]}}


def write_file(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


class SettingsCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="app-settings-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.data = os.path.join(self.root, "data")
        self.claude_home = os.path.join(self.root, "claude-home")
        self.config_path = os.path.join(self.data, "config.json")
        self.user_settings = {}
        self.projects = []
        config = AppConfig(app_root_dir=self.root, user_data_dir=self.data, orchestrator_skill_dir=os.path.join(self.root, "plugin", "skills", "orchestrator"),
                           guard_path=os.path.join(self.root, "guard.py"), frontend_dir=self.root, templates_dir=self.root, engine_dir=self.root,
                           claude_transcripts_dir=self.root, output_dir=self.root)
        self.service = AppSettingsService(config, AppConfigRepository(self.data), types.SimpleNamespace(list_projects=lambda: self.projects),
                                          LoopFileRepository(), read_user_settings=lambda: self.user_settings)
        self.router = Router()
        AppSettingsController(StructuredLog(""), self.service).register_routes(self.router)

    def call(self, method, body=None):
        route, params = self.router.find_route(method, "/api/app/settings")
        response = normalize_response(route.handler(Request(method, "/api/app/settings", params, {}, body or {})))
        return response.status, response.body

    def save(self, key, value, **extra):
        return self.call("POST", dict({"key": key, "value": value, "confirm": True}, **extra))

    def read_config_file(self):
        with open(self.config_path, encoding="utf-8") as handle:
            return json.load(handle)

    def items(self):
        return {item["key"]: item for item in self.call("GET")[1]["settings"]}

    def add_turns(self, name, turns):
        """`turns`: [(caveman flag or None for an old record, cost)] written as turn logs of a project folder."""
        repo = os.path.join(self.root, name)
        for number, (caveman, cost) in enumerate(turns, 1):
            record = {"total_cost_usd": cost}
            if caveman is not None:
                record["caveman"] = caveman
            write_file(os.path.join(repo, ".specs", "project", "loop", "internal", "turns", "%03d.json" % number), json.dumps(record))
        self.projects.append(Project(id=name, name=name, repo_path=repo))


class TestListSettings(SettingsCase):
    def test_lists_every_setting_with_the_defaults_of_today(self):
        status, body = self.call("GET")
        self.assertEqual(status, 200)
        self.assertEqual([item["key"] for item in body["settings"]], [definition.key for definition in SETTING_CATALOG])
        items = self.items()
        for key, default in DEFAULTS.items():
            self.assertEqual((items[key]["value"], items[key]["default"]), (default, default), key)
        self.assertEqual(items["output_dir"]["default"], os.path.join(os.path.expanduser("~"), "Downloads"))
        for item in items.values():
            self.assertTrue(item["label"] and item["description"], item["key"])
            self.assertIn(item["type"], ("bool", "int", "number", "text", "list", "choice"))
            self.assertFalse(item["customized"])
        self.assertTrue(all(items[key]["restart_required"] for key in ("orchestrator_model", "claude_binary", "max_parallel_maps", "idle_hours", "output_dir")))
        self.assertFalse(any(items[key]["restart_required"] for key in ("loop_isolation", "loop_caveman", "loop_extra_skills")))
        self.assertEqual([choice["value"] for choice in items["orchestrator_model"]["choices"]], ["opus", "sonnet", "haiku", ""])

    def test_info_rows_and_no_file_is_created_by_reading(self):
        info = {row["key"]: row["value"] for row in self.call("GET")[1]["info"]}
        self.assertEqual(info["data_dir"], self.data)
        self.assertEqual(info["port_range"], "47800-47899")
        self.assertEqual(set(info), {"data_dir", "plugin_root", "guard_path", "port_range"})
        self.assertFalse(os.path.exists(self.config_path), "a GET never writes: untouched keys stay absent")

    def test_shows_what_the_app_really_uses_when_the_file_holds_garbage(self):
        write_file(self.config_path, json.dumps({"max_parallel_maps": -3, "idle_hours": "x", "loop_isolation": "no", "orchestrator_model": "claude-opus-9"}))
        items = self.items()
        self.assertEqual((items["max_parallel_maps"]["value"], items["idle_hours"]["value"], items["loop_isolation"]["value"]), (6, 2, True))
        self.assertEqual(items["orchestrator_model"]["value"], "claude-opus-9")
        self.assertIn("claude-opus-9", [choice["value"] for choice in items["orchestrator_model"]["choices"]], "a hand-written model stays selectable")
        self.assertTrue(items["max_parallel_maps"]["customized"])

    def test_unreadable_config_shows_defaults(self):
        write_file(self.config_path, "{not json")
        self.assertEqual(self.items()["max_parallel_maps"]["value"], 6)


class TestSaveSettings(SettingsCase):
    def test_post_needs_the_token_and_get_does_not(self):
        requires = {(route.method, route.pattern): route.requires_token for route in self.router.routes}
        self.assertEqual(requires, {("GET", "/api/app/settings"): False, ("POST", "/api/app/settings"): True})

    def test_post_needs_confirm_true(self):
        for body in ({"key": "idle_hours", "value": 3}, {"key": "idle_hours", "value": 3, "confirm": "yes"}, {"key": "idle_hours", "value": 3, "confirm": False}):
            status, answer = self.call("POST", body)
            self.assertEqual((status, answer["ok"]), (400, False), body)
        self.assertFalse(os.path.exists(self.config_path))

    def test_saves_and_returns_the_updated_item(self):
        status, body = self.save("max_parallel_maps", 3)
        self.assertEqual(status, 200, body)
        self.assertEqual((body["ok"], body["setting"]["key"], body["setting"]["value"], body["setting"]["customized"]), (True, "max_parallel_maps", 3, True))
        self.assertEqual(self.read_config_file(), {"max_parallel_maps": 3})
        self.assertEqual(self.items()["max_parallel_maps"]["value"], 3)

    def test_other_keys_unknown_included_are_kept_and_the_file_is_utf8(self):
        write_file(self.config_path, json.dumps({"orchestrator_skill_dir": "C:/skills", "future_key": {"a": [1, 2]}, "app_root_dir": "Pasta ção"}, ensure_ascii=False))
        self.assertEqual(self.save("idle_hours", 0.5)[0], 200)
        self.assertEqual(self.save("loop_extra_skills", ["caveman", " caveman ", "idata-suporte"])[0], 200)
        content = self.read_config_file()
        self.assertEqual(content, {"orchestrator_skill_dir": "C:/skills", "future_key": {"a": [1, 2]}, "app_root_dir": "Pasta ção",
                                   "idle_hours": 0.5, "loop_extra_skills": ["caveman", "idata-suporte"]})
        with open(self.config_path, "rb") as handle:
            self.assertIn("Pasta ção".encode("utf-8"), handle.read())
        self.assertEqual([name for name in os.listdir(self.data) if name != "config.json"], [], "no temp file is left behind")

    def test_unreadable_config_is_not_overwritten(self):
        write_file(self.config_path, "{not json")
        status, body = self.save("idle_hours", 1)
        self.assertEqual((status, body["ok"]), (409, False))
        with open(self.config_path, encoding="utf-8") as handle:
            self.assertEqual(handle.read(), "{not json")

    def test_unknown_key_is_400(self):
        for key in ("nope", "app_root_dir", "projects", None, 5, ""):
            status, body = self.save(key, 1)
            self.assertEqual((status, body["ok"]), (400, False), key)
        self.assertFalse(os.path.exists(self.config_path))

    def test_value_is_required(self):
        self.assertEqual(self.call("POST", {"key": "idle_hours", "confirm": True})[0], 400)

    def test_validation_per_type_and_range(self):
        bad = [("loop_isolation", "false"), ("loop_isolation", 0), ("loop_caveman", None), ("max_parallel_maps", 0), ("max_parallel_maps", 2.5),
               ("max_parallel_maps", True), ("max_parallel_maps", "3"), ("inventory_giant_lines", -1), ("inventory_giant_kb", 0),
               ("idle_hours", -1), ("idle_hours", "2"), ("idle_hours", float("inf")), ("idle_hours", True), ("orchestrator_model", "gpt"),
               ("orchestrator_model", 3), ("claude_binary", 5), ("claude_binary", "a\nb"), ("claude_binary", "x" * 501), ("output_dir", ""),
               ("output_dir", ["a"]), ("loop_extra_skills", "caveman"), ("loop_extra_skills", [1]), ("loop_extra_skills", ["../etc"]),
               ("loop_extra_skills", ["a b"]), ("loop_extra_skills", ["s%d" % number for number in range(51)]), ("claude_fallback_binaries", [None])]
        for key, value in bad:
            status, body = self.save(key, value)
            self.assertEqual(status, 400, (key, value, body))
        self.assertFalse(os.path.exists(self.config_path), "nothing invalid reaches the file")

    def test_good_values_of_every_type(self):
        good = [("loop_isolation", False), ("loop_isolation", True), ("max_parallel_maps", 1), ("idle_hours", 0), ("idle_hours", 1.5),
                ("inventory_giant_lines", 1), ("inventory_giant_kb", 999), ("orchestrator_model", ""), ("orchestrator_model", "sonnet"),
                ("claude_binary", "cc"), ("claude_binary", ""), ("claude_fallback_binaries", ["cc", " cc2 "]), ("output_dir", "D:/out"),
                ("loop_extra_skills", [])]
        for key, value in good:
            status, body = self.save(key, value)
            self.assertEqual(status, 200, (key, value, body))
        content = self.read_config_file()
        self.assertEqual((content["orchestrator_model"], content["claude_fallback_binaries"], content["idle_hours"]), ("sonnet", ["cc", "cc2"], 1.5))

    def test_body_json_types_survive_a_real_json_round_trip(self):
        self.assertEqual(self.save("idle_hours", json.loads("2.0"))[0], 200)
        self.assertEqual(self.save("max_parallel_maps", json.loads("4"))[0], 200)


class TestResetSetting(SettingsCase):
    def test_reset_removes_only_that_key(self):
        write_file(self.config_path, json.dumps({"idle_hours": 5, "max_parallel_maps": 2, "other": 1}))
        status, body = self.call("POST", {"key": "idle_hours", "reset": True, "confirm": True})
        self.assertEqual((status, body["setting"]["value"], body["setting"]["customized"]), (200, 2, False))
        self.assertEqual(self.read_config_file(), {"max_parallel_maps": 2, "other": 1})

    def test_reset_of_an_untouched_key_creates_nothing(self):
        self.assertEqual(self.call("POST", {"key": "idle_hours", "reset": True, "confirm": True})[0], 200)
        self.assertFalse(os.path.exists(self.config_path))

    def test_reset_needs_confirm_and_a_known_key(self):
        self.assertEqual(self.call("POST", {"key": "idle_hours", "reset": True})[0], 400)
        self.assertEqual(self.call("POST", {"key": "nope", "reset": True, "confirm": True})[0], 400)

    def test_saving_the_default_writes_it_and_reset_drops_it(self):
        self.save("max_parallel_maps", 6)
        self.assertEqual(self.read_config_file(), {"max_parallel_maps": 6})
        self.call("POST", {"key": "max_parallel_maps", "reset": True, "confirm": True})
        self.assertEqual(self.read_config_file(), {})


class TestCavemanSetting(SettingsCase):
    def test_not_installed_shows_the_note_and_refuses_to_turn_on(self):
        item = self.items()["loop_caveman"]
        self.assertEqual((item["available"], item["value"]), (False, False))
        self.assertIn("Not installed", item["note"])
        status, body = self.save("loop_caveman", True)
        self.assertEqual((status, body["ok"]), (400, False))
        self.assertIn("Not installed", body["error"])
        self.assertFalse(os.path.exists(self.config_path))

    def test_not_installed_still_allows_turning_off(self):
        write_file(self.config_path, json.dumps({"loop_caveman": True}))
        self.assertEqual(self.save("loop_caveman", False)[0], 200)
        self.assertIs(self.read_config_file()["loop_caveman"], False)

    def test_installed_can_be_turned_on_and_the_engine_reads_it(self):
        self.user_settings = CAVEMAN_SETTINGS
        item = self.items()["loop_caveman"]
        self.assertEqual((item["available"], item["note"]), (True, ""))
        self.assertEqual(self.save("loop_caveman", True)[0], 200)
        self.assertIs(self.read_config_file()["loop_caveman"], True)
        self.assertTrue(runtime_isolation.caveman_hooks_to_carry({"loop_caveman": True}, CAVEMAN_SETTINGS))

    def test_note_when_isolation_is_off(self):
        self.user_settings = CAVEMAN_SETTINGS
        write_file(self.config_path, json.dumps({"loop_isolation": False}))
        self.assertIn("isolation is off", self.items()["loop_caveman"]["note"])


class TestCavemanComparison(SettingsCase):
    def comparison(self):
        return self.call("GET")[1]["caveman"]

    def test_no_turns_is_not_enough_data(self):
        comparison = self.comparison()
        self.assertFalse(comparison["enough_data"])
        self.assertEqual(comparison["message"], "Not enough data yet (0 on / 0 off)")
        self.assertIn("Off by default", comparison["recommendation"])
        self.assertEqual((comparison["on"]["turns"], comparison["on"]["average_cost_usd"]), (0, None))

    def test_fewer_than_three_on_either_side(self):
        self.add_turns("a", [(True, 1.0), (True, 2.0), (False, 1.0), (False, 1.0), (False, 1.0)])
        comparison = self.comparison()
        self.assertEqual((comparison["enough_data"], comparison["message"]), (False, "Not enough data yet (2 on / 3 off)"))

    def test_averages_with_and_without_over_all_projects(self):
        self.add_turns("a", [(True, 1.0), (True, 2.0), (False, 4.0), (False, 2.0), (None, 9.0)])
        self.add_turns("b", [(True, 3.0), (False, 3.0), (None, 9.0)])
        comparison = self.comparison()
        self.assertTrue(comparison["enough_data"])
        self.assertEqual(comparison["message"], "")
        self.assertEqual((comparison["on"]["turns"], comparison["on"]["average_cost_usd"]), (3, 2.0))
        self.assertEqual((comparison["off"]["turns"], comparison["off"]["average_cost_usd"]), (3, 3.0),
                         "a turn log without the flag (before the option existed) is left out")

    def test_turns_without_a_cost_are_ignored(self):
        self.add_turns("a", [(True, 1.0), (True, 1.0), (True, 1.0), (False, 1.0), (False, 1.0), (False, 1.0)])
        write_file(os.path.join(self.root, "a", ".specs", "project", "loop", "internal", "turns", "007.json"), json.dumps({"raw": "x"}))
        self.assertEqual(self.comparison()["off"]["turns"], 3)


class TestHttpTokenAndConfirm(SettingsCase):
    """The real server in front of the controller: no token = 403, wrong token = 403, token without confirm = 400."""

    def setUp(self):
        super().setUp()
        monitor = types.SimpleNamespace(seconds_until_shutdown=lambda: None, record_activity=lambda: None)
        router = Router()
        AppSettingsController(StructuredLog(""), self.service).register_routes(router)
        self.server = AppHttpServer(router, self.root, monitor, StructuredLog(""))
        self.url = self.server.open_port(TEST_PORT_RANGE)
        self.thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop(self):
        self.server.request_shutdown()
        self.thread.join(5)

    def request(self, method, body=None, token=None):
        request = urllib.request.Request(self.url + "api/app/settings", data=json.dumps(body).encode() if body is not None else None, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-Monitor-Token", token)
        try:
            with self.opener.open(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as failure:
            with failure:
                return failure.code, json.loads(failure.read())

    def test_token_confirm_and_write(self):
        body = {"key": "idle_hours", "value": 3, "confirm": True}
        self.assertEqual(self.request("GET")[0], 200)
        self.assertEqual(self.request("POST", body)[0], 403)
        self.assertEqual(self.request("POST", body, token="wrong")[0], 403)
        self.assertEqual(self.request("POST", dict(body, confirm=False), token=self.server.token)[0], 400)
        self.assertFalse(os.path.exists(self.config_path))
        status, answer = self.request("POST", body, token=self.server.token)
        self.assertEqual((status, answer["setting"]["value"]), (200, 3))
        self.assertEqual(self.read_config_file(), {"idle_hours": 3})


if __name__ == "__main__":
    unittest.main()
