"""The loop panel's "current Claude account": command, method, e-mail, cc account, cache that never blocks the poll."""
import threading
import unittest

from harness_backend.services.claude_account_service import ClaudeAccountService, command_name, parse_account_label

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


if __name__ == "__main__":
    unittest.main()
