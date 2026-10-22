"""The loop panel's "current Claude account": command, method, e-mail, cc account, cache that never blocks the poll."""
import threading
import unittest

from harness_backend.services.claude_account_service import (ClaudeAccountService, build_usage_view, command_name, format_usage_line,
                                                             parse_account_label)

MAIN_ROW = {"name": "claude", "role": "main", "found": True, "label": "login main@x.com (iData, team)",
            "auth": {"method": "login", "email": "main@x.com", "org": "iData", "plan": "team"}}
CC_ROW = {"name": "cc", "role": "fallback", "found": True, "label": "token",
          "auth": {"method": "token", "email": "", "org": "", "plan": ""}}


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def service_with(rows, clock=None, calls=None):
    def describe():
        if calls is not None:
            calls.append(1)
        return rows
    return ClaudeAccountService(describe, ttl_seconds=300, clock=clock or Clock())


class ParsingTests(unittest.TestCase):
    def test_command_name_drops_folder_and_extension(self):
        self.assertEqual(command_name("C:/Users/a/bin/cc.cmd"), "cc")
        self.assertEqual(command_name("C:\\Users\\a\\bin\\cc.cmd"), "cc")
        self.assertEqual(command_name("/usr/bin/claude"), "claude")
        self.assertEqual(command_name(None), "")

    def test_parse_label_with_email_org_plan(self):
        fields = parse_account_label("login desenv10@idata.com.br (iData, team)")
        self.assertEqual((fields["method"], fields["email"], fields["org"], fields["plan"]), ("login", "desenv10@idata.com.br", "iData", "team"))

    def test_parse_label_with_cc_account_and_api_key(self):
        self.assertEqual(parse_account_label("token, cc account claude2")["cc_account"], "claude2")
        self.assertEqual(parse_account_label("API key, cc account a")["method"], "API key")
        self.assertEqual(parse_account_label("")["method"], "")


class CurrentAccountTests(unittest.TestCase):
    def test_before_any_turn_uses_the_main_row_after_the_cache_is_filled(self):
        service = service_with([MAIN_ROW, CC_ROW])
        service.get_rows(wait=True)
        current = service.build_current_account({})
        self.assertEqual((current["command"], current["role"], current["method"], current["email"]), ("claude", "main", "login", "main@x.com"))

    def test_unknown_cache_returns_only_what_meta_has_and_does_not_block(self):
        release = threading.Event()
        service = ClaudeAccountService(lambda: (release.wait(5), [MAIN_ROW])[1])
        current = service.build_current_account({"claude": "C:/bin/cc.cmd", "claude_account": "token, cc account claude2", "claude_switches": 2})
        self.assertEqual((current["command"], current["method"], current["cc_account"], current["switches"]), ("cc", "token", "claude2", 2))
        self.assertEqual(current["email"], "")
        release.set()

    def test_meta_label_wins_and_fallback_role_comes_from_the_row(self):
        service = service_with([MAIN_ROW, CC_ROW])
        service.get_rows(wait=True)
        current = service.build_current_account({"claude": "cc", "claude_account": "login other@y.com, cc account claude2", "claude_switches": 1})
        self.assertEqual((current["role"], current["email"], current["cc_account"]), ("fallback", "other@y.com", "claude2"))
        self.assertEqual(current["org"], "")  # the main row's org is not borrowed for another e-mail

    def test_cache_is_reused_until_the_ttl_and_a_failure_keeps_the_rows(self):
        clock, calls = Clock(), []
        service = service_with([MAIN_ROW], clock, calls)
        service.get_rows(wait=True)
        service.get_rows(wait=True)
        self.assertEqual(len(calls), 1)
        clock.now += 301
        service.describe_commands = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        self.assertEqual(service.get_rows(wait=True), [MAIN_ROW])


NOW = 1792700000.0
FIVE_HOUR_OK = {"pct": 60.0, "reset_at": 1792702800, "reset_local": "2026-10-22T14:30:00"}
WEEKLY_OK = {"pct": 33.0, "reset_at": 1792767600, "reset_local": "2026-10-23T09:00:00"}
WEEKLY_EXHAUSTED = {"pct": 100.0, "reset_at": 1793000000, "reset_local": "2026-10-26T09:00:00"}
CONFIGURED = [{"name": "claude", "role": "main"}, {"name": "cc.cmd", "role": "fallback"}]


class UsageViewTests(unittest.TestCase):
    def test_meta_usage_gives_both_windows_with_label_and_age(self):
        meta = {"claude_usage": {"claude": {"windows": {"seven_day": WEEKLY_OK, "five_hour": FIVE_HOUR_OK}, "status": "allowed",
                                            "read_at": "2026-10-22T10:00:00"}}}
        row = build_usage_view(meta, CONFIGURED[:1], now=NOW)[0]
        self.assertEqual((row["key"], row["command"], row["cc_account"], row["status"]), ("claude", "claude", "", "allowed"))
        self.assertEqual([(w["key"], w["label"], w["pct"], w["exhausted"]) for w in row["windows"]],
                         [("five_hour", "5-hour", 60.0, False), ("seven_day", "weekly", 33.0, False)])
        self.assertIsInstance(row["age_seconds"], int)

    def test_exhausted_weekly_window_is_flagged(self):
        meta = {"claude_usage": {"claude": {"windows": {"five_hour": FIVE_HOUR_OK, "seven_day": WEEKLY_EXHAUSTED}, "status": "rejected", "read_at": ""}}}
        row = build_usage_view(meta, CONFIGURED[:1], now=NOW)[0]
        self.assertEqual([w["exhausted"] for w in row["windows"]], [False, True])
        self.assertIn("weekly 100 %", format_usage_line(row))
        self.assertIn("EXHAUSTED", format_usage_line(row))
        self.assertIsNone(row["age_seconds"])

    def test_configured_command_never_seen_is_usage_unknown(self):
        rows = build_usage_view({}, CONFIGURED, now=NOW)
        self.assertEqual([(row["key"], row["windows"]) for row in rows], [("claude", []), ("cc", [])])
        self.assertEqual(format_usage_line(rows[0]), "usage unknown")

    def test_cc_account_key_is_kept_apart_and_replaces_the_bare_cc_row(self):
        meta = {"claude_usage": {"claude": {"windows": {"five_hour": FIVE_HOUR_OK}, "status": "allowed", "read_at": ""},
                                 "cc:claudep": {"windows": {"five_hour": FIVE_HOUR_OK, "seven_day": WEEKLY_OK}, "status": "allowed", "read_at": ""}}}
        rows = build_usage_view(meta, CONFIGURED, now=NOW)
        self.assertEqual([(row["key"], row["command"], row["cc_account"]) for row in rows], [("claude", "claude", ""), ("cc:claudep", "cc", "claudep")])

    def test_falls_back_to_the_newest_turn_then_to_calls(self):
        turns = [{"claude": "claude", "usage_windows": {"five_hour": dict(FIVE_HOUR_OK, pct=10.0)}, "limit_status": "allowed"},
                 {"claude": "claude", "usage_windows": {"five_hour": FIVE_HOUR_OK}, "limit_status": "allowed_warning"},
                 {"claude": "claude", "usage_windows": {}}]
        calls = [{"claude": "cc", "cc_account": "claudep", "usage_windows": {"seven_day": WEEKLY_OK}, "limit_status": "allowed", "at": "2026-10-22T10:00:00"}]
        rows = {row["key"]: row for row in build_usage_view({}, CONFIGURED[:1], turns, calls, now=NOW)}
        self.assertEqual((rows["claude"]["windows"][0]["pct"], rows["claude"]["status"]), (60.0, "allowed_warning"))
        self.assertEqual(rows["cc:claudep"]["windows"][0]["key"], "seven_day")
        self.assertIsNotNone(rows["cc:claudep"]["age_seconds"])

    def test_garbage_in_meta_never_breaks_the_view(self):
        meta = {"claude_usage": {"claude": {"windows": {"five_hour": {"pct": "x"}, "bad": 3}}, "cc": "oops"}}
        rows = build_usage_view(meta, [], now=NOW)
        self.assertTrue(all(row["windows"] == [] for row in rows))

    def test_service_view_never_waits_for_auth_status(self):
        release = threading.Event()
        service = ClaudeAccountService(lambda: (release.wait(5), [MAIN_ROW])[1])
        rows = service.build_usage_view({})
        self.assertEqual(rows, [])  # cache not read yet: nothing to list, and it returned at once
        release.set()
        service.get_rows(wait=True)
        self.assertEqual(service.build_usage_view({})[0]["windows"], [])


if __name__ == "__main__":
    unittest.main()
