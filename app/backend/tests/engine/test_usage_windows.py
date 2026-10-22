#!/usr/bin/env python3
"""The 5-hour and weekly usage windows (engine/usage_windows.py): reading them from the parse result, the blocking window and the
phrases of the notices; and the panel client (ClaudeClient) and its call log reading the same windows from a stream-json output.

Run (from app/backend):  python -m unittest tests.engine.test_usage_windows -v
"""
import datetime
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401  (engine on sys.path, temporary LOOP_DATA_DIR)
import usage_windows  # noqa: E402
from harness_backend.infrastructure.claude_client import ClaudeClient  # noqa: E402
from harness_backend.infrastructure.process_runner import ProcessRunner  # noqa: E402
from harness_backend.services.claude_call_log_service import ClaudeCallLogService  # noqa: E402

LIMITED = os.path.join(HERE, "fake_claude_limited.py")
NOW = datetime.datetime(2026, 10, 22, 12, 0, 0)


def epoch(moment):
    return int(moment.timestamp())


def rate_limit(status="allowed", limit_type="five_hour", five=60.0, seven=33.0, five_in=2, seven_in=72):
    """A parse result's `rate_limit`: resets `five_in` hours and `seven_in` hours after NOW."""
    return {"status": status, "limit_type": limit_type, "overage_status": "rejected", "reset_at": None, "windows": {
        "five_hour": {"pct": five, "reset_at": epoch(NOW + datetime.timedelta(hours=five_in))},
        "seven_day": {"pct": seven, "reset_at": epoch(NOW + datetime.timedelta(hours=seven_in))}}}


class TestBlockingWindow(unittest.TestCase):
    def test_nothing_blocks_below_100_and_when_allowed(self):
        self.assertIsNone(usage_windows.blocking_window(rate_limit(), NOW))
        self.assertIsNone(usage_windows.blocking_window({}, NOW))
        self.assertIsNone(usage_windows.blocking_window(None, NOW))

    def test_a_full_five_hour_window_blocks_with_its_reset(self):
        blocking = usage_windows.blocking_window(rate_limit(five=100.0), NOW)
        self.assertEqual(blocking["key"], "five_hour")
        self.assertEqual(usage_windows.reset_moment(blocking), NOW + datetime.timedelta(hours=2))

    def test_when_both_are_full_the_one_that_frees_up_last_blocks(self):
        blocking = usage_windows.blocking_window(rate_limit(five=100.0, seven=100.0), NOW)
        self.assertEqual(blocking["key"], "seven_day")

    def test_a_rejected_status_blocks_through_its_limit_type_even_below_100(self):
        blocking = usage_windows.blocking_window(rate_limit(status="rejected", limit_type="seven_day", seven=97.0), NOW)
        self.assertEqual(blocking["key"], "seven_day")
        self.assertEqual(usage_windows.reset_moment(blocking), NOW + datetime.timedelta(hours=72))

    def test_a_rejected_limit_type_missing_from_the_windows_uses_the_event_reset(self):
        limit = {"status": "rejected", "limit_type": "five_hour", "reset_at": epoch(NOW + datetime.timedelta(hours=1)), "windows": {}}
        blocking = usage_windows.blocking_window(limit, NOW)
        self.assertEqual((blocking["key"], blocking["pct"]), ("five_hour", 100.0))
        self.assertEqual(usage_windows.reset_moment(blocking), NOW + datetime.timedelta(hours=1))

    def test_a_full_window_whose_reset_already_passed_is_stale(self):
        self.assertIsNone(usage_windows.blocking_window(rate_limit(five=100.0, five_in=-1), NOW))

    def test_a_warning_status_is_not_a_block(self):
        self.assertFalse(usage_windows.is_limited(rate_limit(status="allowed_warning", five=90.0), NOW))
        self.assertTrue(usage_windows.is_rejected("rejected"))


class TestPhrases(unittest.TestCase):
    def test_the_five_hour_window_says_the_clock_time(self):
        blocking = usage_windows.blocking_window(rate_limit(status="rejected", five=100.0), NOW)
        self.assertEqual(usage_windows.phrase(blocking), "5-hour window, back at 14:00")

    def test_the_weekly_window_says_weekday_date_and_time(self):
        blocking = usage_windows.blocking_window(rate_limit(status="rejected", limit_type="seven_day", seven=100.0), NOW)
        self.assertEqual(usage_windows.phrase(blocking), "weekly window, back on Sun 25/10 12:00")  # 2026-10-25 is a Sunday

    def test_another_window_key_and_an_unknown_reset(self):
        self.assertEqual(usage_windows.window_name("seven_day_opus"), "seven day opus")
        self.assertEqual(usage_windows.phrase({"key": "seven_day", "pct": 100.0, "reset_at": None}), "weekly window")
        self.assertEqual(usage_windows.phrase(None), "")

    def test_describe_windows_lists_percent_and_reset_or_says_unknown(self):
        text = usage_windows.describe_windows(usage_windows.windows_of(rate_limit()))
        self.assertEqual(text, "5h 60% (back 14:00), weekly 33% (back Sun 25/10 12:00)")
        self.assertEqual(usage_windows.describe_windows({}), "usage unknown")

    def test_the_turn_log_windows_carry_the_local_reset_text(self):
        windows = usage_windows.with_local_reset(usage_windows.windows_of(rate_limit()))
        self.assertEqual(windows["five_hour"]["reset_local"], (NOW + datetime.timedelta(hours=2)).isoformat(timespec="seconds"))
        self.assertEqual(windows["five_hour"]["pct"], 60.0)
        self.assertEqual(usage_windows.with_local_reset({"x": {"pct": 1.0, "reset_at": None}})["x"]["reset_local"], "")


class TestClientReadsTheWindows(unittest.TestCase):
    """ClaudeClient and the call log with a command that is out of usage on its 5-hour window (stream-json output)."""

    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="usage-windows-")
        patcher = mock.patch.dict(os.environ, {"FAKE_LIMITED_WINDOW": "five_hour", "FAKE_LIMITED_RESET_IN": "7200"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_rejected_event_is_a_usage_limit_even_when_the_text_names_no_limit(self):
        client = ClaudeClient(ProcessRunner(), LIMITED)
        argv = client.build_command("sonnet", 1)
        result, switch_cause = client.run_once(argv, self.folder, "", 60, None)
        self.assertFalse(result["ok"])
        self.assertEqual(switch_cause, "usage_limit")
        self.assertEqual(result["raw"]["rate_limit"]["status"], "rejected")

    def test_the_call_log_entry_has_the_windows_and_the_status_and_no_secret(self):
        client = ClaudeClient(ProcessRunner(), LIMITED)
        result = client.run_prompt(client.build_command("sonnet", 1), self.folder, "", 60)
        entry = ClaudeCallLogService(repository=None).build_entry("chat", result)
        self.assertEqual(entry["limit_status"], "rejected")
        self.assertEqual(entry["usage_windows"]["five_hour"]["pct"], 100.0)
        self.assertEqual(entry["usage_windows"]["seven_day"]["pct"], 40.0)
        self.assertTrue(entry["usage_windows"]["five_hour"]["reset_local"])
        self.assertNotIn("raw", entry)

    def test_a_call_without_a_rate_limit_event_logs_empty_windows(self):
        entry = ClaudeCallLogService(repository=None).build_entry("chat", {"ok": True, "binary": "claude", "raw": {"result": "x"}})
        self.assertEqual((entry["usage_windows"], entry["limit_status"]), ({}, ""))


if __name__ == "__main__":
    unittest.main()
