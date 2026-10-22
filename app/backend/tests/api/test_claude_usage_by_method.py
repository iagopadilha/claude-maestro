"""Command and sign-in per turn and per panel call: structured auth fields (with the fallback for old turns), the panel call log, the
totals per sign-in method and per command, and what `/state` and `/history` expose."""
import os
import shutil
import sys
import tempfile
import unittest

from harness_backend.infrastructure.claude_client import ClaudeClient, call_kind
from harness_backend.repositories.claude_call_log_repository import MAX_LINES, TRIM_AT_LINES, ClaudeCallLogRepository
from harness_backend.services.claude_account_service import (ClaudeAccountService, build_turn_auth, build_turn_claude, build_usage_totals,
                                                             method_code)
from harness_backend.services.claude_call_log_service import ClaudeCallLogService

from .support_read import ReadTestCase

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "harness_backend", "engine"))
import run_loop  # noqa: E402

CC_TURN = {"claude": "C:/bin/cc.cmd", "claude_account": "token, cc account claude2", "total_cost_usd": 0.5}
LOGIN_TURN = {"claude": "claude", "claude_account": "login, dev@x.com (iData, team)", "total_cost_usd": 1.0}


class EngineAuthFieldsTests(unittest.TestCase):
    def test_login_label_gives_method_email_and_plan(self):
        self.assertEqual(run_loop.claude_auth_fields("login, dev@x.com (iData, team)"),
                         {"auth_method": "login", "auth_email": "dev@x.com", "auth_plan": "team", "cc_account": ""})

    def test_cc_token_label_gives_the_account_name_and_no_email(self):
        self.assertEqual(run_loop.claude_auth_fields("token, cc account claude2"),
                         {"auth_method": "token", "auth_email": "", "auth_plan": "", "cc_account": "claude2"})

    def test_api_key_and_unknown_labels(self):
        self.assertEqual(run_loop.claude_auth_fields("API key")["auth_method"], "api_key")
        self.assertEqual(run_loop.claude_auth_fields("")["auth_method"], "")


class TurnAuthReadTests(unittest.TestCase):
    def test_new_turn_uses_its_structured_fields(self):
        record = dict(LOGIN_TURN, auth_method="login", auth_email="dev@x.com", auth_plan="team", cc_account="")
        self.assertEqual(build_turn_auth(record), {"auth_method": "login", "auth_email": "dev@x.com", "auth_plan": "team", "cc_account": ""})

    def test_old_turn_is_read_from_the_account_label(self):
        self.assertEqual(build_turn_auth(LOGIN_TURN), {"auth_method": "login", "auth_email": "dev@x.com", "auth_plan": "team", "cc_account": ""})
        self.assertEqual(build_turn_auth(CC_TURN)["cc_account"], "claude2")
        self.assertEqual(build_turn_auth(CC_TURN)["auth_method"], "token")
        self.assertEqual(build_turn_auth({"claude_account": "API key"})["auth_method"], "api_key")

    def test_turn_without_any_account_is_unknown_not_an_error(self):
        self.assertEqual(build_turn_claude({})["auth_method"], "")
        self.assertEqual(build_turn_claude(None)["claude"], "")

    def test_method_codes(self):
        self.assertEqual([method_code(word) for word in ("login", "token", "API key", "api_key", "?", None)],
                         ["login", "token", "api_key", "api_key", "", ""])


class UsageTotalsTests(unittest.TestCase):
    def test_turns_and_calls_are_summed_per_method_and_per_command(self):
        turns = [dict(build_turn_claude(LOGIN_TURN), cost_usd=1.0), dict(build_turn_claude(CC_TURN), cost_usd=0.5),
                 dict(build_turn_claude(CC_TURN), cost_usd=0.25)]
        calls = [{"claude": "cc", "auth_method": "token", "cost_usd": 0.1}, {"claude": "claude", "auth_method": "", "cost_usd": None}]
        totals = build_usage_totals(turns, calls)
        by_method = {row["method"]: row for row in totals["by_method"]}
        self.assertEqual((by_method["login"]["turns"], by_method["login"]["cost_usd"]), (1, 1.0))
        self.assertEqual((by_method["token"]["turns"], by_method["token"]["calls"], by_method["token"]["cost_usd"]), (2, 1, 0.85))
        self.assertEqual((by_method[""]["turns"], by_method[""]["calls"]), (0, 1))
        by_command = {row["command"]: row for row in totals["by_command"]}
        self.assertEqual((by_command["cc"]["turns"], by_command["cc"]["calls"], by_command["cc"]["cost_usd"]), (2, 1, 0.85))
        self.assertEqual((by_command["claude"]["turns"], by_command["claude"]["calls"]), (1, 1))
        self.assertEqual((totals["turns"], totals["calls"], totals["cost_usd"]), (3, 2, 1.85))

    def test_empty_input_gives_empty_totals(self):
        self.assertEqual(build_usage_totals([]), {"by_method": [], "by_command": [], "turns": 0, "calls": 0, "cost_usd": 0.0})


class FakeRunner:
    def __init__(self, output='{"result": "hi", "total_cost_usd": 0.02, "session_id": "s1"}'):
        self.output = output

    def run_and_wait(self, argv, folder, timeout_seconds, input_text=None, env=None, extra_env=None):
        return 0, self.output, ""


class CallLogTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="call-log-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        rows = [{"name": "claude", "role": "main", "auth": {"method": "login", "email": "dev@x.com", "plan": "team"}}]
        account_service = ClaudeAccountService(lambda: rows)
        account_service.get_rows(wait=True)
        self.service = ClaudeCallLogService(ClaudeCallLogRepository(), account_service)

    def test_run_prompt_records_one_line_with_kind_command_method_and_cost(self):
        client = ClaudeClient(FakeRunner(), claude_binary="claude", call_recorder=self.service.record_call)
        with call_kind("roadmap"):
            client.run_prompt(["claude", "-p"], self.folder, "hello", 5)
        calls = self.service.list_calls([self.folder])
        self.assertEqual(len(calls), 1)
        self.assertEqual({key: calls[0][key] for key in ("kind", "claude", "auth_method", "auth_email", "cost_usd", "ok")},
                         {"kind": "roadmap", "claude": "claude", "auth_method": "login", "auth_email": "dev@x.com", "cost_usd": 0.02, "ok": True})
        self.assertNotIn("reply", calls[0])

    def test_a_failed_call_is_recorded_as_not_ok_and_the_kind_defaults(self):
        client = ClaudeClient(FakeRunner("not json"), claude_binary="claude", call_recorder=self.service.record_call)
        client.run_prompt(["claude", "-p"], self.folder, "hello", 5)
        call = self.service.list_calls([self.folder])[0]
        self.assertEqual((call["ok"], call["kind"], call["cost_usd"]), (False, "panel", None))

    def test_a_recorder_that_fails_never_breaks_the_call(self):
        service = ClaudeCallLogService(ClaudeCallLogRepository())

        def broken(folder, entry):
            raise OSError("disk full")
        service.repository.append_call = broken
        service.record_call(self.folder, "chat", {"ok": True})  # must not raise

    def test_the_log_stays_bounded(self):
        repository = ClaudeCallLogRepository()
        for index in range(TRIM_AT_LINES + 20):
            repository.append_call(self.folder, {"at": "2026-10-22T10:%02d:%02d" % (index // 60 % 60, index % 60), "n": index})
        calls = repository.read_calls(self.folder, limit=10 ** 6)
        self.assertLessEqual(len(calls), TRIM_AT_LINES)
        self.assertGreaterEqual(len(calls), MAX_LINES)
        self.assertEqual(calls[-1]["n"], TRIM_AT_LINES + 19)

    def test_unreadable_lines_are_skipped_and_two_folders_do_not_repeat_a_call(self):
        repository = ClaudeCallLogRepository()
        repository.append_call(self.folder, {"at": "2026-10-22T10:00:00", "kind": "chat", "claude": "claude", "cost_usd": 1})
        with open(repository.get_path(self.folder), "a", encoding="utf-8") as handle:
            handle.write("{broken\n")
        self.assertEqual(len(self.service.list_calls([self.folder, self.folder])), 1)


class StateAndHistoryPayloadTests(ReadTestCase):
    def test_state_turns_agents_and_totals_carry_the_sign_in(self):
        self.fixture.start()
        self.fixture.write_json("turns/001.json", dict(LOGIN_TURN))  # an old turn: no structured fields
        self.fixture.write_json("turns/002.json", dict(CC_TURN, auth_method="token", auth_email="", auth_plan="", cc_account="claude2"))
        self.fixture.write_json("meta.json", dict(claude="C:/bin/cc.cmd", claude_account="token, cc account claude2", turns=2,
                                                  auth_method="token", auth_email="", auth_plan="", cc_account="claude2"))
        data = self.state()
        self.assertEqual([(row["n"], row["auth_method"], row["cc_account"]) for row in data["turns"]], [(1, "login", ""), (2, "token", "claude2")])
        self.assertEqual(data["turns"][0]["auth_email"], "dev@x.com")
        self.assertEqual(data["meta"]["auth_method"], "token")
        self.assertEqual({row["method"]: row["turns"] for row in data["meta"]["claude_totals"]["by_method"]}, {"login": 1, "token": 1})
        self.assertTrue(all(agent["auth_method"] == "token" for agent in data["agents"]))
        self.assertEqual(data["meta"]["max_parallel_tasks"], 3)
        self.assertFalse(data["meta"]["max_parallel_tasks_in_header"])

    def test_history_turns_and_their_agents_carry_the_sign_in(self):
        self.fixture.start()
        self.fixture.write_json("turns/001.json", dict(LOGIN_TURN))
        status, history = self.fixture.call("/api/history")
        self.assertEqual(status, 200)
        turn = history["turns"][0]
        self.assertEqual(turn["auth_method"], "login")
        self.assertTrue(all(agent["auth_method"] == "login" for agent in turn["agents"]))


if __name__ == "__main__":
    unittest.main()
