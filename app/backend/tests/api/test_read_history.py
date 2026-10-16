"""Agent history from the Claude Code transcripts (history, history log, cache, real agents in /state and agent clarity)
with the /p/<id>/api prefix."""
import json
import os
import time
import unittest

from harness_backend.repositories.claude_transcript_repository import list_project_slug_variants

from .support_read import (AGENT_TOKENS, SESSION, ReadTestCase, TranscriptFixtureMixin, agent_rows, assistant_row, shifted, tool_result, tool_use,
                           user_row)


class HistoryTests(TranscriptFixtureMixin, ReadTestCase):
    def test_slug_replaces_non_alphanumeric_and_tries_the_lower_case_drive(self):
        if os.name != "nt":
            self.skipTest("drive letter paths only on Windows")
        self.assertEqual(list_project_slug_variants(r"C:\Users\dev\Desktop\sample-api-worktree-loop"),
                         ["C--Users-dev-Desktop-sample-api-worktree-loop", "c--Users-dev-Desktop-sample-api-worktree-loop"])
        self.assertEqual(list_project_slug_variants(r"c:\Users\a b\x_y"), ["c--Users-a-b-x-y", "C--Users-a-b-x-y"])

    def test_history_lists_the_orchestrator_and_two_subagents_with_role_feature_tokens_and_duration(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        data = self.history()
        self.assertEqual([t["n"] for t in data["turns"]], [16])
        turn = data["turns"][0]
        self.assertEqual((turn["session_id"], turn["cost_usd"], turn["duration_s"], turn["transcript"]), (SESSION, 0.96, 13, True))
        orch, first, second = turn["agents"]
        self.assertEqual((orch["id"], orch["role"], orch["feature"], orch["state"], orch["tokens"], orch["duration_s"]),
                         ("v016:orch", "orchestrator", None, "done", 30, 13))
        self.assertEqual(orch["summary"], "turn ok")
        self.assertEqual(orch["started_at"], "2026-10-15T15:00:00.000Z")
        self.assertEqual([a["id"] for a in (first, second)], ["v016:bbb222", "v016:aaa111"])  # sorted by start
        self.assertEqual((first["role"], first["feature"], first["state"], first["tokens"]), ("spec-reviewer", "slug-b", "done", AGENT_TOKENS))
        self.assertEqual((second["role"], second["feature"], second["tokens"]), ("spec-writer", "slug-a", AGENT_TOKENS))
        self.assertEqual(second["duration_s"], 66)
        self.assertEqual(second["summary"], "FEATURE: slug-a")
        self.assertEqual((second["started_at"], second["finished_at"]), ("2026-10-15T15:00:10.000Z", "2026-10-15T15:01:15.500Z"))

    def test_turns_newest_first_and_without_transcript_only_the_orchestrator(self):
        self.setup_projects()
        self.fixture.write_json("turns/003.json", {"session_id": "99999999-0000-0000-0000-000000000000", "total_cost_usd": 0.1, "duration_ms": 5000,
                                                   "usage": {"output_tokens": 9}})
        self.fixture.write_json("turns/004.json", {"total_cost_usd": 0.2, "is_error": True})
        self.fixture.start(projects_dir=self.projects)
        data = self.history()
        self.assertEqual([t["n"] for t in data["turns"]], [16, 4, 3])
        for turn in data["turns"][1:]:
            self.assertFalse(turn["transcript"])
            self.assertEqual([a["role"] for a in turn["agents"]], ["orchestrator"])
        self.assertEqual(data["turns"][2]["agents"][0]["tokens"], 9)
        self.assertIsNone(data["turns"][1]["agents"][0]["tokens"])
        self.assertEqual((data["turns"][1]["status"], data["turns"][1]["agents"][0]["state"]), ("error", "failed"))

    def test_missing_projects_root_or_no_turns_never_gives_500(self):
        self.fixture.start()
        self.assertEqual(self.history(), {"turns": []})
        self.fixture.write_json("turns/001.json", {"session_id": SESSION, "total_cost_usd": 0.3})
        data = self.history()
        self.assertFalse(data["turns"][0]["transcript"])
        self.assertEqual(self.fixture.call("/api/agent/v001:orch/log")[1]["log"], [])

    def test_corrupted_or_binary_transcript_does_not_break_and_unreadable_agent_is_skipped(self):
        subs = self.setup_projects()
        with open(os.path.join(subs, "agent-ccc333.jsonl"), "wb") as handle:
            handle.write(b"\xff\xfe\x00\x00{{{{ not json\n\x80\x81\n")
        self.write_lines(os.path.join(subs, "agent-ddd444.jsonl"), [], extra_lines=['{"type":"assistant","message":"loose text"}',
                                                                                     '{"type":"user","message":{"content":42}}', "null"])
        self.fixture.write(os.path.join(subs, "agent-ddd444.meta.json"), "{broken")
        self.fixture.start(projects_dir=self.projects)
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertIsNone(by_id["v016:ddd444"]["role"])
        self.assertIsNone(by_id["v016:ddd444"]["tokens"])
        self.assertIsNone(by_id["v016:ddd444"]["feature"])
        self.assertEqual(self.log("v016:ccc333")["log"], [])
        self.assertEqual(self.log("v016:ddd444")["log"], [])

    def test_malicious_session_id_does_not_leave_the_projects_folder(self):
        self.setup_projects()
        self.fixture.write_json("turns/017.json", {"session_id": "../../etc/passwd"})
        self.fixture.start(projects_dir=self.projects)
        data = self.history()
        self.assertEqual(data["turns"][0]["n"], 17)
        self.assertFalse(data["turns"][0]["transcript"])
        self.assertEqual(self.log("v016:..%2f..%2fx")["log"], [])


class HistoryLogTests(TranscriptFixtureMixin, ReadTestCase):
    def test_subagent_log_has_prompt_tool_result_and_model_speech(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        data = self.log("v016:aaa111")
        self.assertEqual(data["id"], "v016:aaa111")
        self.assertFalse(data["truncated"])
        self.assertEqual([item["type"] for item in data["log"]], ["sys", "tool", "ai", "tool", "ai"])
        self.assertIn("SCOPE_FEATURE: slug-a", data["log"][0]["text"])
        self.assertEqual(data["log"][1]["text"], "Read C:/x/spec.md")
        self.assertEqual(data["log"][1]["result"], "# spec content")
        self.assertEqual(data["log"][3]["text"], "Bash dotnet build")
        self.assertEqual(data["log"][3]["result"], "Build ok")
        self.assertTrue(data["log"][4]["text"].startswith("FEATURE: slug-a"))
        self.assertEqual([item["t"] for item in data["log"]], ["00:00:00", "00:00:01", "00:00:03", "00:00:03", "00:01:05"])
        for item in data["log"]:
            self.assertEqual(set(item), {"type", "text", "result", "t"})

    def test_orchestrator_log_uses_the_main_transcript(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        log = self.log("v016:orch")["log"]
        self.assertEqual([item["type"] for item in log], ["sys", "ai"])
        self.assertEqual(log[1]["text"], "Dispatching agents")

    def test_orchestrator_log_without_transcript_falls_back_to_the_turn_result(self):
        self.fixture.write_json("turns/005.json", {"session_id": SESSION, "result": "done"})
        self.fixture.start()
        log = self.log("v005:orch")["log"]
        self.assertEqual([(item["type"], item["text"]) for item in log], [("ai", "done")])

    def test_text_and_result_clip_at_2000_and_log_at_400_items_with_truncated(self):
        subs = self.setup_projects()
        rows = [user_row("2026-10-15T15:00:00.000Z", "P " * 2500)]
        for index in range(450):
            rows.append(assistant_row("2026-10-15T15:00:01.000Z", "m%d" % index, [tool_use("t%d" % index, "Bash", command="echo " + "x " * 1500)],
                                      {"output_tokens": 1}))
            rows.append(user_row("2026-10-15T15:00:02.000Z", [tool_result("t%d" % index, "R " * 2000)]))
        self.write_lines(os.path.join(subs, "agent-eee555.jsonl"), rows)
        self.fixture.start(projects_dir=self.projects)
        data = self.log("v016:eee555")
        self.assertTrue(data["truncated"])
        self.assertEqual(len(data["log"]), 400)
        self.assertEqual(len(data["log"][0]["text"]), 2000)
        self.assertEqual(len(data["log"][1]["text"]), 2000)
        self.assertEqual(len(data["log"][1]["result"]), 2000)
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertEqual(by_id["v016:eee555"]["tokens"], 450)

    def test_credentials_are_redacted_in_the_log_and_the_summary(self):
        subs = self.setup_projects()
        secret = "A" * 48
        rows = [user_row("2026-10-15T15:00:00.000Z", "Use password: hunter2 and token=abc123 in the test"),
                assistant_row("2026-10-15T15:00:01.000Z", "m1", [tool_use("t1", "Bash", command="curl -H 'Authorization: Bearer %s' http://x" % secret)],
                              {"output_tokens": 1}),
                user_row("2026-10-15T15:00:02.000Z", [tool_result("t1", "passwd=apassword123 key %s" % secret)]),
                assistant_row("2026-10-15T15:00:03.000Z", "m2", [tool_use("t2", "SubagentHandback", message="secret: leaked123 end")], {"output_tokens": 1})]
        self.write_lines(os.path.join(subs, "agent-fff666.jsonl"), rows)
        self.fixture.start(projects_dir=self.projects)
        text = json.dumps(self.log("v016:fff666"))
        for leaked in ("hunter2", "abc123", secret, "apassword123", "leaked123"):
            self.assertNotIn(leaked, text)
        self.assertIn("[redacted]", text)
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertNotIn("leaked123", by_id["v016:fff666"]["summary"])

    def test_agent_ending_in_a_tool_error_without_final_return_is_failed(self):
        subs = self.setup_projects()
        rows = [user_row("2026-10-15T15:00:00.000Z", "FEATURE: slug-z"),
                assistant_row("2026-10-15T15:00:01.000Z", "m1", [tool_use("t1", "Bash", command="x")], {"output_tokens": 1}),
                user_row("2026-10-15T15:00:02.000Z", [tool_result("t1", "boom", is_error=True)])]
        self.write_lines(os.path.join(subs, "agent-ggg777.jsonl"), rows)
        self.fixture.start(projects_dir=self.projects)
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertEqual((by_id["v016:ggg777"]["state"], by_id["v016:ggg777"]["feature"]), ("failed", "slug-z"))
        self.assertEqual(by_id["v016:aaa111"]["state"], "done")

    def test_feature_comes_from_the_spec_path_when_the_prompt_has_no_FEATURE_line(self):
        subs = self.setup_projects()
        self.write_lines(os.path.join(subs, "agent-hhh888.jsonl"), [user_row("2026-10-15T15:00:00.000Z", "Review the spec .specs/features/refactor-x/spec.md")])
        self.fixture.start(projects_dir=self.projects)
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertEqual(by_id["v016:hhh888"]["feature"], "refactor-x")


class HistoryCacheTests(TranscriptFixtureMixin, ReadTestCase):
    def test_mtime_cache_only_rereads_when_the_file_changes(self):
        subs = self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        self.history()
        parsed = self.fixture.transcripts.parse_count
        self.assertEqual(parsed, 3)  # main + 2 subagents
        self.history()
        self.log("v016:aaa111")
        self.assertEqual(self.fixture.transcripts.parse_count, parsed)
        path = os.path.join(subs, "agent-aaa111.jsonl")
        self.write_lines(path, agent_rows("FEATURE: new-feature"))
        later = time.time() + 5
        os.utime(path, (later, later))
        by_id = {a["id"]: a for a in self.history()["turns"][0]["agents"]}
        self.assertEqual(self.fixture.transcripts.parse_count, parsed + 1)
        self.assertEqual(by_id["v016:aaa111"]["feature"], "new-feature")

    def test_cache_does_not_grow_without_bound(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        self.fixture.transcripts.cache_limit = 2
        self.history()
        self.assertLessEqual(len(self.fixture.transcripts.transcript_cache), 2)


class StateRealAgentsTests(TranscriptFixtureMixin, ReadTestCase):
    def test_state_uses_the_real_agents_of_the_last_turn_when_there_is_a_transcript_and_the_loop_is_stopped(self):
        self.setup_projects()
        self.fixture.set_loop("awaiting_user", turn=16)
        self.fixture.start(projects_dir=self.projects)
        agents = {a["id"]: a for a in self.state()["agents"]}
        self.assertEqual(set(agents), {"orch", "v016:aaa111", "v016:bbb222"})
        self.assertEqual(agents["orch"]["tokens"], 30)
        self.assertEqual(agents["orch"]["cost_usd"], 0.96)
        self.assertEqual((agents["v016:aaa111"]["role"], agents["v016:aaa111"]["feature"], agents["v016:aaa111"]["tokens"]),
                         ("spec-writer", "slug-a", AGENT_TOKENS))
        self.assertEqual(agents["v016:aaa111"]["state"], "done")
        self.assertIsNone(agents["v016:aaa111"]["cost_usd"])
        self.assertLessEqual(len(agents["v016:aaa111"]["log"]), 8)
        self.assertNotIn("service-a:executor-back", agents)

    def test_state_with_the_loop_running_or_without_transcript_keeps_the_inferred_agents(self):
        self.setup_projects()
        self.fixture.set_lock_alive()
        self.fixture.set_loop("continue", turn=17)
        self.fixture.start(projects_dir=self.projects)
        agents = {a["id"] for a in self.state()["agents"]}
        self.assertIn("service-a:executor-back", agents)
        self.assertNotIn("v016:aaa111", agents)
        os.remove(self.fixture.at("LOOP.lock"))
        self.fixture.transcripts.transcripts_dir = os.path.join(self.fixture.tmp, "does-not-exist")
        agents = {a["id"] for a in self.state()["agents"]}
        self.assertIn("service-a:executor-back", agents)


class AgentClarityTests(TranscriptFixtureMixin, ReadTestCase):
    def add_agent(self, name, rows, role="executor-back"):
        subs = os.path.join(self.slug_dir, SESSION, "subagents")
        self.write_lines(os.path.join(subs, "agent-%s.jsonl" % name), rows)
        self.fixture.write(os.path.join(subs, "agent-%s.meta.json" % name), json.dumps({"agentType": role}))

    def with_return(self, final_return, start="2026-10-15T16:00:00.000Z"):
        return [user_row(start, "FEATURE: x"), assistant_row(shifted(start, 1), "m1", [tool_use("t1", "Read", file_path="a.cs")], {"output_tokens": 1}),
                user_row(shifted(start, 2), [tool_result("t1", "ok")]),
                assistant_row(shifted(start, 5), "m2", [tool_use("t2", "SubagentHandback", message=final_return)], {"output_tokens": 1}, stop="tool_use")]

    def history_agents(self):
        return {a["id"]: a for a in self.history()["turns"][0]["agents"]}

    def test_last_action_tool_and_ai_before_the_final_return(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        agents = self.history_agents()
        action = agents["v016:aaa111"]["last_action"]
        self.assertEqual((action["type"], action["text"], action["t"]), ("tool", "Bash dotnet build", "00:00:03"))
        self.assertEqual(agents["v016:orch"]["last_action"]["type"], "ai")
        self.assertEqual(agents["v016:orch"]["last_action"]["text"], "Dispatching agents")

    def test_last_action_ai_text_clipped_and_credential_redacted(self):
        self.setup_projects()
        start = "2026-10-15T16:00:00.000Z"
        text = "token=abc123secretvalue " + "talked " * 60
        self.add_agent("ddd444", [user_row(start, "FEATURE: x"), assistant_row(shifted(start, 7), "m1", [{"type": "text", "text": text}], stop="end_turn")])
        self.fixture.start(projects_dir=self.projects)
        action = self.history_agents()["v016:ddd444"]["last_action"]
        self.assertEqual((action["type"], action["t"]), ("ai", "00:00:07"))
        self.assertLessEqual(len(action["text"]), 160)
        self.assertNotIn("abc123secretvalue", action["text"])

    def test_return_status_and_decision_for_approved_blocked_done_pass_fail(self):
        self.setup_projects()
        cases = {
            "e1": ("FEATURE: x\nVERDICT: APPROVED\nCITED: SC-01\nFINDINGS:\n- SC-05: tight limit, does not block", "APPROVED", "SC-05: tight limit, does not block"),
            "e2": ("FEATURE: x\nVERDICT: BLOCKED\nFINDINGS: the measurable criterion is missing", "BLOCKED", "the measurable criterion is missing"),
            "e3": ("FEATURE: x\nSTATUS: DONE\nCHANGED:\n- a.cs new", "DONE", "a.cs new"),
            "e4": ("FEATURE: x\nVERDICT: PASS\nRAN: dotnet build", "PASS", None),
            "e5": ("FEATURE: x\nVERDICT: FAIL\nSUMMARY: 2 tests broke", "FAIL", "2 tests broke"),
            "e6": ("TOO_BIG: the feature goes over 8 files", "TOO_BIG", "TOO_BIG: the feature goes over 8 files"),
            "e7": ("FEATURE: x\nSTATUS: DONE\nLAYERS: api\nDECISION: kept the old endpoint", "DONE", "kept the old endpoint"),
        }
        for name, (final_return, _, _) in cases.items():
            self.add_agent(name, self.with_return(final_return))
        self.fixture.start(projects_dir=self.projects)
        agents = self.history_agents()
        for name, (_, status, decision) in cases.items():
            self.assertEqual(agents["v016:" + name]["return_status"], status, name)
            if status == "PASS":
                self.assertTrue(agents["v016:" + name]["decision"] is None or len(agents["v016:" + name]["decision"]) <= 200)
            else:
                self.assertEqual(agents["v016:" + name]["decision"], decision, name)

    def test_missing_return_becomes_null_and_decision_is_clipped(self):
        self.setup_projects()
        start = "2026-10-15T16:00:00.000Z"
        self.add_agent("f1", [user_row(start, "FEATURE: x"), assistant_row(shifted(start, 1), "m1", [tool_use("t1", "Read", file_path="a.cs")], stop="tool_use")])
        self.add_agent("f2", self.with_return("Integration done, but the suite did not go green."))
        self.add_agent("f3", self.with_return("STATUS: DONE\nSUMMARY: " + "word " * 60))
        self.fixture.start(projects_dir=self.projects)
        agents = self.history_agents()
        self.assertEqual((agents["v016:f1"]["return_status"], agents["v016:f1"]["decision"]), (None, None))
        self.assertEqual(agents["v016:f1"]["last_action"]["text"], "Read a.cs")
        self.assertEqual((agents["v016:f2"]["return_status"], agents["v016:f2"]["decision"]), (None, "Integration done, but the suite did not go green."))
        self.assertEqual(agents["v016:f3"]["return_status"], "DONE")
        self.assertEqual(len(agents["v016:f3"]["decision"]), 200)

    def test_state_brings_the_fields_on_real_agents_and_null_on_inferred_ones(self):
        self.setup_projects()
        self.fixture.set_loop("awaiting_user", turn=16)
        self.fixture.start(projects_dir=self.projects)
        real = {a["id"]: a for a in self.state()["agents"]}
        self.assertEqual((real["v016:aaa111"]["return_status"], real["v016:aaa111"]["last_action"]["type"]), ("APPROVED", "tool"))
        self.assertEqual(real["v016:aaa111"]["decision"], "more")
        self.fixture.transcripts.transcripts_dir = os.path.join(self.fixture.tmp, "does-not-exist")
        inferred = {a["id"]: a for a in self.state()["agents"]}
        self.assertIn("service-a:executor-back", inferred)
        for agent in (a for a in inferred.values() if a["id"] != "orch"):
            self.assertTrue({"last_action", "return_status", "decision"} <= set(agent))
            self.assertEqual((agent["last_action"], agent["return_status"], agent["decision"]), (None, None, None))

    def test_orchestrator_brings_action_turn_status_and_decision_from_the_result(self):
        self.setup_projects()
        snap = self.fixture.snapshot(16)
        self.fixture.write(os.path.join(snap, "LOOP.json"), json.dumps({"status": "done"}))
        self.fixture.set_loop("done", turn=16)
        self.fixture.start(projects_dir=self.projects)
        orch = self.history_agents()["v016:orch"]
        self.assertEqual((orch["return_status"], orch["decision"]), ("done", "turn ok"))
        state_orch = {a["id"]: a for a in self.state()["agents"]}["orch"]
        self.assertEqual((state_orch["return_status"], state_orch["decision"], state_orch["last_action"]["type"]), ("done", "turn ok", "ai"))
        self.fixture.transcripts.transcripts_dir = os.path.join(self.fixture.tmp, "does-not-exist")
        without = {a["id"]: a for a in self.state()["agents"]}["orch"]
        self.assertEqual(without["last_action"]["text"], "turn ok")

    def test_mtime_cache_covers_the_new_fields(self):
        self.setup_projects()
        self.fixture.start(projects_dir=self.projects)
        self.history()
        parses = self.fixture.transcripts.parse_count
        first = self.history_agents()["v016:aaa111"]["last_action"]
        self.history()
        self.assertEqual(self.fixture.transcripts.parse_count, parses)
        self.assertEqual(first["text"], "Bash dotnet build")


if __name__ == "__main__":
    unittest.main()
