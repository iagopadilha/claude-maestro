#!/usr/bin/env python3
"""Contract of the `claude -p --output-format stream-json --verbose` output (the older `json` shape stays accepted), as the engine and the app read it (engine/cli_output.py).

Run (from app/):  python -m unittest discover -s backend/tests -t backend -p "test_cli_contract.py" -v

The fixtures in fixtures/cli/ are recorded shapes of the CLI output: success, the error subtypes, permission denials, the
`--verbose` array, stream-json, noise before and after the JSON, renamed or extra fields, missing cost, empty output, HTML.
`run_loop.parse_result` (engine) and `ClaudeClient.parse_json_output` (app) must agree on every one of them.

Live test (OPT-IN, skipped by default): LOOP_LIVE_CLI_TEST=1 runs ONE real
    claude -p "Reply with the single word OK" --output-format stream-json --verbose --max-budget-usd 0.05
in a temporary folder and checks the normalised schema. It uses your Claude Code login: it counts toward your plan's usage
limits (or bills your API key, if that is how your CLI is signed in). Run it after updating Claude Code, then bump
`cli_output.MIN_TESTED_CLAUDE_VERSION`.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401  (engine on sys.path, isolated LOOP_DATA_DIR)
import cli_output  # noqa: E402
import run_loop  # noqa: E402
from harness_backend.infrastructure.claude_client import ClaudeClient  # noqa: E402

FIXTURES = os.path.join(HERE, "fixtures", "cli")
LIVE_FLAG = "LOOP_LIVE_CLI_TEST"
LIVE_PROMPT = "Reply with the single word OK"
LIVE_BUDGET_USD = "0.05"
LIVE_TIMEOUT_SECONDS = 180
NORMALISED_TYPES = {"result": str, "is_error": bool, "subtype": str, "session_id": str, "usage": dict, "permission_denials": list, "rate_limit": dict}
OPTIONAL_NUMBERS = {"total_cost_usd": float, "duration_ms": int, "num_turns": int, "api_error_status": int}


def fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as handle:
        return handle.read()


class ContractCase(unittest.TestCase):
    def parse(self, name):
        """Parses a fixture through the engine AND the app, asserts they agree, returns the engine result."""
        text = fixture(name)
        engine = run_loop.parse_result(text)
        app = ClaudeClient.parse_json_output(text)
        self.assertEqual(app, engine if engine.get("ok") else {}, "engine and app disagree on %s" % name)
        return engine

    def assert_schema(self, data):
        self.assertIs(data["ok"], True, data)
        self.assertEqual(data["type"], "result")
        for key, kind in NORMALISED_TYPES.items():
            self.assertIsInstance(data[key], kind, "%s should be %s: %r" % (key, kind.__name__, data[key]))
        for key, kind in OPTIONAL_NUMBERS.items():
            self.assertTrue(data[key] is None or (isinstance(data[key], kind) and not isinstance(data[key], bool)),
                            "%s should be %s or None: %r" % (key, kind.__name__, data[key]))
        for denial in data["permission_denials"]:
            self.assertIsInstance(denial, dict)
        json.dumps(data)  # the engine saves it as the turn log

    def assert_unrecognized(self, data):
        self.assertEqual((data["ok"], data["reason"]), (False, cli_output.UNRECOGNIZED), data)
        self.assertLessEqual(len(data["sample"]), cli_output.SAMPLE_LIMIT)
        self.assertTrue(data["detail"])


class TestRecognisedShapes(ContractCase):
    def test_success(self):
        data = self.parse("success.json")
        self.assert_schema(data)
        self.assertEqual((data["result"], data["is_error"], data["subtype"]), ("OK", False, "success"))
        self.assertEqual((data["total_cost_usd"], data["duration_ms"], data["num_turns"]), (0.0123, 2310, 1))
        self.assertEqual(data["session_id"], "00000000-0000-4000-8000-000000000001")
        self.assertEqual(data["usage"]["output_tokens"], 5)
        self.assertEqual(data["uuid"], "00000000-0000-4000-8000-0000000000aa", "unknown fields are kept")

    def test_error_max_turns_has_no_result_text(self):
        data = self.parse("error_max_turns.json")
        self.assert_schema(data)
        self.assertEqual((data["is_error"], data["subtype"], data["result"]), (True, "error_max_turns", ""))
        self.assertEqual(data["num_turns"], 31)

    def test_error_during_execution(self):
        data = self.parse("error_during_execution.json")
        self.assert_schema(data)
        self.assertEqual((data["is_error"], data["subtype"], data["total_cost_usd"]), (True, "error_during_execution", 0.0))

    def test_budget_exceeded(self):
        data = self.parse("budget_exceeded.json")
        self.assert_schema(data)
        self.assertTrue(data["is_error"])
        self.assertEqual(data["subtype"], "error_max_budget_usd")
        self.assertAlmostEqual(data["total_cost_usd"], 0.0612)

    def test_permission_denials_reach_the_loop_as_dicts(self):
        data = self.parse("permission_denials.json")
        self.assert_schema(data)
        self.assertEqual([d["tool_name"] for d in data["permission_denials"]], ["Bash", "Write"])
        self.assertTrue(run_loop.is_risky_denial(data["permission_denials"][0]), "git push to main is a risky denial")
        self.assertIn("git push origin main", run_loop.denial_text(data["permission_denials"][0]))

    def test_verbose_array_takes_the_last_result_event(self):
        data = self.parse("array_output.json")
        self.assert_schema(data)
        self.assertEqual(data["result"], "OK from the array")

    def test_stream_json_takes_the_last_result_event(self):
        data = self.parse("stream_json.jsonl")
        self.assert_schema(data)
        self.assertEqual(data["result"], "OK from the stream")

    def test_the_old_json_format_has_no_rate_limit_and_no_api_error(self):
        data = self.parse("success.json")
        self.assertEqual(data["rate_limit"], {})
        self.assertIsNone(data["api_error_status"])

    def test_the_last_rate_limit_event_gives_the_windows_in_percent(self):
        data = self.parse("stream_json_rate_limit.jsonl")
        self.assert_schema(data)
        self.assertEqual(data["result"], "OK with usage")
        rate_limit = data["rate_limit"]
        self.assertEqual((rate_limit["status"], rate_limit["limit_type"], rate_limit["overage_status"]), ("allowed", "five_hour", "rejected"))
        self.assertEqual(rate_limit["windows"], {"five_hour": {"pct": 60.0, "reset_at": 1792702800}, "seven_day": {"pct": 33.0, "reset_at": 1792767600}})
        self.assertEqual(rate_limit["reset_at"], 1792702800)

    def test_a_rejected_event_and_the_api_error_status_of_the_result(self):
        data = self.parse("stream_json_rejected_403.jsonl")
        self.assert_schema(data)
        self.assertEqual(data["api_error_status"], 403)
        self.assertEqual((data["rate_limit"]["status"], data["rate_limit"]["limit_type"]), ("rejected", "seven_day"))
        self.assertEqual(data["rate_limit"]["windows"]["seven_day"]["pct"], 100.0)

    def test_utilization_already_in_percent_is_kept_and_bad_values_are_dropped(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {
            "five_hour": {"utilization": 60, "resetsAt": 1792702800}, "seven_day": {"utilization": "x", "resetsAt": 1}, "other": "nope"}}}
        windows = cli_output.normalize_rate_limit(event)["windows"]
        self.assertEqual(windows, {"five_hour": {"pct": 60.0, "reset_at": 1792702800}})

    def test_an_unreadable_event_is_an_empty_rate_limit(self):
        for event in ({"type": "rate_limit_event"}, {"type": "rate_limit_event", "rate_limit_info": "x"},
                      {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": []}}):
            self.assertEqual(cli_output.normalize_rate_limit(event), {})
        result = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok"})
        self.assertEqual(cli_output.parse('{"type": "rate_limit_event", "rate_limit_info": 7}\n' + result)["rate_limit"], {})

    def test_a_future_window_key_is_kept(self):
        event = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": {"seven_day_opus": {"utilization": 0.1, "resetsAt": 5}}}}
        self.assertEqual(list(cli_output.normalize_rate_limit(event)["windows"]), ["seven_day_opus"])

    def test_the_rate_limit_event_lines_can_be_taken_out_of_a_text_search(self):
        text = fixture("stream_json_rate_limit.jsonl")
        self.assertIn("rate_limit_event", text)
        self.assertNotIn("rate_limit_event", cli_output.strip_rate_limit_events(text))
        self.assertIn("OK with usage", cli_output.strip_rate_limit_events(text))
        self.assertEqual(cli_output.strip_rate_limit_events(None), "")

    def test_warning_line_before_the_json(self):
        data = self.parse("warning_before_json.txt")
        self.assert_schema(data)
        self.assertEqual(data["result"], "OK")

    def test_trailing_text_after_the_json(self):
        data = self.parse("trailing_text.txt")
        self.assert_schema(data)
        self.assertEqual(data["total_cost_usd"], 0.0123)

    def test_unknown_extra_fields_and_retyped_fields_do_not_crash(self):
        data = self.parse("unknown_extra_fields.json")
        self.assert_schema(data)
        self.assertEqual((data["num_turns"], data["duration_ms"], data["is_error"]), (3, 1500, False))
        self.assertEqual(json.loads(data["result"]), ["a", "list", "now"])
        self.assertEqual(data["usage"], {})
        self.assertEqual(data["permission_denials"], [{"tool_name": "", "tool_input": {"command": "Bash(rm -rf build)"}}])
        self.assertTrue(run_loop.is_risky_denial(data["permission_denials"][0]))
        self.assertEqual(data["brand_new_field"], {"nested": True})

    def test_missing_cost_is_none_and_the_loop_counts_zero(self):
        data = self.parse("missing_cost.json")
        self.assert_schema(data)
        self.assertIsNone(data["total_cost_usd"])
        self.assertEqual(float(data.get("total_cost_usd") or 0), 0.0)
        self.assertIsNone(ClaudeClient.extract_cost(ClaudeClient.parse_json_output(fixture("missing_cost.json"))))

    def test_legacy_cost_usd_is_the_fallback(self):
        data = self.parse("legacy_cost_usd.json")
        self.assertEqual(data["total_cost_usd"], 0.5)


class TestUnrecognisedOutput(ContractCase):
    def test_empty_output(self):
        data = self.parse("empty_output.txt")
        self.assert_unrecognized(data)
        self.assertEqual(data["sample"], "")

    def test_html_garbage_is_reported_with_a_redacted_sample(self):
        data = self.parse("html_garbage.txt")
        self.assert_unrecognized(data)
        self.assertIn("502 Bad Gateway", data["sample"])
        self.assertNotIn("abc123secret", data["sample"])

    def test_json_without_a_result_event(self):
        data = cli_output.parse('{"type": "system", "subtype": "init"}\n{"type": "assistant", "message": {}}')
        self.assert_unrecognized(data)
        self.assertIn("no result event", data["detail"])

    def test_scalars_and_bytes(self):
        self.assert_unrecognized(cli_output.parse("42"))
        self.assert_unrecognized(cli_output.parse(None))
        self.assertEqual(cli_output.parse(fixture("success.json").encode("utf-8"))["result"], "OK")

    def test_sample_is_capped(self):
        self.assertEqual(len(cli_output.parse("x " * 1000)["sample"]), cli_output.SAMPLE_LIMIT)


class TestVersion(unittest.TestCase):
    def test_parse_and_compare(self):
        self.assertEqual(cli_output.parse_version("2.1.301 (Claude Code)"), (2, 1, 301))
        self.assertIsNone(cli_output.parse_version("command not found"))
        self.assertIsNotNone(cli_output.parse_version(cli_output.MIN_TESTED_CLAUDE_VERSION))

    def test_older_version_only_warns(self):
        old = cli_output.describe_version("0.1.0 (Claude Code)")
        self.assertTrue(old["older_than_tested"])
        self.assertIn("older", old["warning"])
        current = cli_output.describe_version(cli_output.MIN_TESTED_CLAUDE_VERSION + " (Claude Code)")
        self.assertEqual((current["older_than_tested"], current["warning"]), (False, ""))
        newer = cli_output.describe_version("999.0.0")
        self.assertEqual((newer["older_than_tested"], newer["warning"]), (False, ""))
        self.assertTrue(cli_output.describe_version("")["warning"])


class TestLoopStopsOnUnreadableOutput(unittest.TestCase):
    """The real run_loop.py with the simulated claude: noise around the JSON is tolerated, no JSON stops with a clear error."""

    def run_scenario(self, output):
        import test_loop  # noqa: E402  (Project helper: temp git repo + fake claude)
        project = test_loop.Project(test_loop.ROAD_BASIC, dict(test_loop.SCEN_BASIC, output=output))
        self.addCleanup(project.cleanup)
        return project, project.run()

    def test_noisy_output_still_finishes(self):
        project, result = self.run_scenario("noisy")
        self.assertEqual(result.returncode, 0, result.stdout[-2000:] + result.stderr[-1000:])
        self.assertEqual(project.loop_json()["status"], "done")

    def test_garbage_output_stops_with_unrecognized_output(self):
        project, result = self.run_scenario("garbage")
        self.assertEqual(result.returncode, run_loop.EXIT["error"], result.stdout[-2000:] + result.stderr[-1000:])
        reason = project.meta()["reason"]
        self.assertIn("not recognized", reason)
        self.assertIn("502 Bad Gateway", reason)
        with open(os.path.join(project.work, ".specs", "project", "loop", "internal", "turns", "001.json"), encoding="utf-8") as handle:
            log = json.load(handle)
        self.assertEqual(log["reason"], cli_output.UNRECOGNIZED)
        self.assertIn("502 Bad Gateway", log["raw"])


@unittest.skipUnless(os.environ.get(LIVE_FLAG) == "1", "live CLI contract test: set %s=1 (uses your Claude plan)" % LIVE_FLAG)
class TestLiveCliContract(ContractCase):
    def test_real_claude_reply_matches_the_normalised_schema(self):
        binary = shutil.which("claude")
        self.assertTrue(binary, "`claude` is not on the PATH")
        folder = tempfile.mkdtemp(prefix="cli-contract-")
        self.addCleanup(shutil.rmtree, folder, True)
        version = subprocess.run([binary, "--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        done = subprocess.run([binary, "-p", LIVE_PROMPT, "--output-format", "stream-json", "--verbose", "--max-budget-usd", LIVE_BUDGET_USD],
                              cwd=folder, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              stdin=subprocess.DEVNULL, timeout=LIVE_TIMEOUT_SECONDS)
        data = cli_output.parse(done.stdout)
        context = "claude %s, exit %s, stderr: %s" % (version.stdout.strip(), done.returncode, done.stderr[-300:])
        self.assertTrue(data.get("ok"), "%s; parsed: %s" % (context, data))
        self.assert_schema(data)
        self.assertEqual(ClaudeClient.parse_json_output(done.stdout), data)
        self.assertTrue(data["session_id"], context)
        if not data["is_error"]:
            self.assertIn("OK", data["result"].upper(), context)
            self.assertEqual(done.returncode, 0, context)


if __name__ == "__main__":
    unittest.main()
