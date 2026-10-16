"""Guard visibility in the panel: a tool call blocked by the PreToolUse guard becomes a `guard` log item, and `/state`
brings the latest blocks of `guard.log` (`guard_events`) and the count per rule (`guard_counts`)."""
import json
import os
import unittest

from .support_read import ReadTestCase, TranscriptFixtureMixin, assistant_row, tool_result, tool_use, user_row

HOOK_BLOCK_TEXT = ("PreToolUse:Edit hook error: [python guard.py --profile auto]: BLOCKED by the orchestrator guard [path-scope]: "
                   "outside the feature's PATHS (services/cart-service). Need a file outside the scope? Return NEEDS_CONFIRM. "
                   "Do not try to work around it. Return STATUS: NEEDS_CONFIRM with the reason; the orchestrator takes it to the user.\n")


def guard_line(rule, decision="blocked", detail="reason", profile="executor", moment="2026-10-15T10:00:00"):
    return json.dumps({"t": moment, "profile": profile, "rule": rule, "decision": decision, "detail": detail})


class GuardTranscriptTests(TranscriptFixtureMixin, ReadTestCase):
    def write_blocked_agent(self, is_error=True, text=HOOK_BLOCK_TEXT):
        subs = self.setup_projects()
        rows = [user_row("2026-10-15T15:00:00.000Z", "FEATURE: cart-service\nImplement it."),
                assistant_row("2026-10-15T15:00:02.000Z", "m1", [tool_use("tu_9", "Edit", file_path="infra/prod/terraform.tfvars")]),
                user_row("2026-10-15T15:00:03.000Z", [tool_result("tu_9", text, is_error=is_error)]),
                assistant_row("2026-10-15T15:00:04.000Z", "m2", [tool_use("tu_10", "SubagentHandback", message="STATUS: NEEDS_CONFIRM\nedit outside scope")])]
        self.write_lines(os.path.join(subs, "agent-ccc333.jsonl"), rows)
        self.fixture.write(os.path.join(subs, "agent-ccc333.meta.json"), json.dumps({"agentType": "executor-back"}))

    def test_blocked_tool_call_becomes_a_guard_item_with_rule_and_reason(self):
        self.write_blocked_agent()
        self.fixture.start(projects_dir=self.projects)
        log = self.log("v016:ccc333")["log"]
        blocked = [item for item in log if item["type"] == "guard"]
        self.assertEqual(len(blocked), 1)
        item = blocked[0]
        self.assertEqual(item["text"], "Edit infra/prod/terraform.tfvars")
        self.assertEqual(item["rule"], "path-scope")
        self.assertEqual(item["reason"], "outside the feature's PATHS (services/cart-service). Need a file outside the scope? Return NEEDS_CONFIRM")
        self.assertTrue(item["result"].startswith("exit 2 · [path-scope] outside the feature's PATHS"))
        self.assertNotIn("work around", item["result"])

    def test_marker_in_a_successful_result_stays_a_plain_tool_item(self):
        self.write_blocked_agent(is_error=False)
        self.fixture.start(projects_dir=self.projects)
        log = self.log("v016:ccc333")["log"]
        self.assertFalse([item for item in log if item["type"] == "guard"])
        self.assertTrue([item for item in log if item["type"] == "tool" and item["text"].startswith("Edit ")])

    def test_other_hook_errors_are_not_guard_items(self):
        self.write_blocked_agent(text="PreToolUse:Edit hook error: some other hook failed")
        self.fixture.start(projects_dir=self.projects)
        self.assertFalse([item for item in self.log("v016:ccc333")["log"] if item["type"] == "guard"])

    def test_guard_reason_is_redacted(self):
        self.write_blocked_agent(text="BLOCKED by the orchestrator guard [secrets]: credential path token=abc123secretvalue. Do not try to work around it.")
        self.fixture.start(projects_dir=self.projects)
        item = [item for item in self.log("v016:ccc333")["log"] if item["type"] == "guard"][0]
        self.assertEqual(item["rule"], "secrets")
        self.assertNotIn("abc123secretvalue", item["reason"] + item["result"])
        self.assertIn("[redacted]", item["reason"])

    def test_state_shows_the_guard_item_on_the_real_agents_of_a_stopped_loop(self):
        self.write_blocked_agent()
        self.fixture.set_loop("awaiting_user", turn=16)
        self.fixture.start(projects_dir=self.projects)
        agent = next(agent for agent in self.state()["agents"] if agent["id"] == "v016:ccc333")
        self.assertIn("guard", [item["type"] for item in agent["log"]])


class GuardEventsStateTests(ReadTestCase):
    def write_guard_log(self, lines):
        self.fixture.write(self.fixture.at("guard.log"), "\n".join(lines) + "\n")

    def test_without_guard_log_events_are_empty(self):
        self.fixture.start()
        data = self.state()
        self.assertEqual(data["guard_events"], [])
        self.assertEqual(data["guard_counts"], {})

    def test_only_blocks_newest_first_with_counts_per_rule(self):
        self.write_guard_log([
            guard_line("scope-trace", decision="info", detail="slug=a fields=cwd"),
            guard_line("path-scope", detail="outside the feature's PATHS (src/a)", moment="2026-10-15T10:00:01"),
            "{corrupted",
            guard_line("project-deny", decision="allowed-by-user", detail="released"),
            guard_line("git-push", profile="verifier", detail="git push: only on request", moment="2026-10-15T10:00:02"),
            guard_line("path-scope", detail="outside the feature's PATHS (src/b)", moment="2026-10-15T10:00:03"),
            "[1, 2]",
        ])
        self.fixture.start()
        data = self.state()
        self.assertEqual(data["guard_counts"], {"path-scope": 2, "git-push": 1})
        self.assertEqual([event["t"] for event in data["guard_events"]], ["2026-10-15T10:00:03", "2026-10-15T10:00:02", "2026-10-15T10:00:01"])
        self.assertEqual(data["guard_events"][1], {"t": "2026-10-15T10:00:02", "profile": "verifier", "rule": "git-push",
                                                   "detail": "git push: only on request"})

    def test_last_twenty_blocks_clipped_and_redacted(self):
        lines = [guard_line("install", detail="npm install pkg %02d" % index, moment="2026-10-15T10:00:%02d" % index) for index in range(25)]
        lines.append(guard_line("secrets", detail="cat .env password=hunter2hunter2 " + "x" * 400, moment="2026-10-15T10:01:00"))
        self.write_guard_log(lines)
        self.fixture.start()
        data = self.state()
        self.assertEqual(len(data["guard_events"]), 20)
        self.assertEqual(data["guard_counts"], {"install": 25, "secrets": 1})
        newest = data["guard_events"][0]
        self.assertEqual(newest["rule"], "secrets")
        self.assertNotIn("hunter2", newest["detail"])
        self.assertLessEqual(len(newest["detail"]), 200)
        self.assertEqual(data["guard_events"][-1]["detail"], "npm install pkg 06")
        self.assertNotIn("decision", newest)


class LiveGuardEventTests(ReadTestCase):
    def test_live_event_of_type_guard_keeps_rule_and_reason(self):
        self.fixture.set_lock_alive()
        self.fixture.set_loop("continue")
        self.fixture.write_json("turns/002.json", {"total_cost_usd": 0.5, "duration_ms": 1000, "result": "ok"})
        events = [{"agent": "service-a:executor-back", "type": "guard", "text": "Edit infra/prod/main.tf", "result": "exit 2 · [path-scope] outside",
                   "rule": "path-scope", "reason": "outside the feature's PATHS", "t": "00:03"}]
        self.fixture.write(os.path.join(self.fixture.turns_dir, "003.events.jsonl"), "\n".join(json.dumps(event) for event in events))
        self.fixture.start()
        agent = next(agent for agent in self.state()["agents"] if agent["id"] == "service-a:executor-back")
        self.assertEqual(agent["log"][0]["type"], "guard")
        self.assertEqual(agent["log"][0]["rule"], "path-scope")
        status, data = self.fixture.call("/api/agent/service-a:executor-back/log")
        self.assertEqual(status, 200)
        self.assertEqual(data["log"][0]["reason"], "outside the feature's PATHS")

    def test_events_of_the_turn_in_progress_win_over_the_last_finished_turn(self):
        self.fixture.set_lock_alive()
        self.fixture.set_loop("continue")
        self.fixture.write_json("turns/002.json", {"result": "ok"})
        self.fixture.write(os.path.join(self.fixture.turns_dir, "002.events.jsonl"), json.dumps({"agent": "orch", "type": "ai", "text": "old"}))
        self.fixture.write(os.path.join(self.fixture.turns_dir, "003.events.jsonl"), json.dumps({"agent": "orch", "type": "ai", "text": "live"}))
        self.fixture.start()
        orchestrator = next(agent for agent in self.state()["agents"] if agent["id"] == "orch")
        self.assertIn("live", [item["text"] for item in orchestrator["log"]])
        self.assertNotIn("old", [item["text"] for item in orchestrator["log"]])


if __name__ == "__main__":
    unittest.main()
