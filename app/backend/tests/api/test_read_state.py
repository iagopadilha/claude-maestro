"""Loop reading through the app: health, state, turn, graph, project 404 and multi-project (read side only, with the
/p/<id>/api prefix)."""
import json
import os
import shutil
import time
import unittest

from .support_read import PROJECT_ID, ROADMAP, ReadTestCase


class HealthTests(ReadTestCase):
    def test_health_returns_repo_work_and_pid(self):
        self.fixture.start()
        status, data = self.fixture.call("/api/health")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        self.assertEqual(os.path.normcase(data["repo"]), os.path.normcase(self.fixture.repo))
        self.assertEqual(os.path.normcase(data["work"]), os.path.normcase(self.fixture.work))
        self.assertEqual(data["pid"], os.getpid())

    def test_invalid_host_is_rejected_even_on_get(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/health", host="evil.example.com")[0], 403)
        self.assertEqual(self.fixture.call("/api/health", host="127.0.0.1:1")[0], 403)
        self.assertEqual(self.fixture.call("/api/health", host="localhost:%d" % self.fixture.server.port)[0], 200)

    def test_unknown_project_is_404_on_every_read_route(self):
        self.fixture.start()
        for path in ("/api/health", "/api/state", "/api/history", "/api/agent/orch/log", "/api/turn/1", "/api/graph"):
            status, data = self.fixture.call(path, project_id="does-not-exist")
            self.assertEqual((status, data), (404, {"ok": False, "error": "project not found"}), path)


class StateTests(ReadTestCase):
    def test_no_loop_is_not_an_error_and_brings_the_roadmap_features(self):
        self.fixture.start()
        data = self.state()
        self.assertEqual(data["loop"]["status"], "no_loop")
        self.assertFalse(data["loop"]["running"])
        self.assertEqual(data["turns"], [])
        self.assertEqual(data["answers"], [])
        self.assertEqual(data["validate"], [])
        self.assertEqual(data["recent_actions"], [])
        self.assertEqual(data["counts"], {"pending": 1, "speccing": 0, "specced": 0, "approved": 1, "building": 1, "verified": 1, "blocked": 1, "total": 5})
        self.assertIsNone(data["meta"]["pr"])
        self.assertEqual(data["project"], "project")
        self.assertEqual(data["project_ref"], {"id": PROJECT_ID, "name": "project", "repo_path": self.fixture.repo})
        self.assertFalse(data["roadmap_approval"]["approved"])

    def test_features_stages_graph_and_quarantine_follow_the_roadmap(self):
        self.fixture.start()
        data = self.state()
        self.assertEqual([(s["n"], s["features"]) for s in data["stages"]],
                         [(1, ["base"]), (2, ["service-a", "service-b", "service-c", "service-d"])])
        self.assertEqual(data["stages"][1]["title"], "Stage 2 - Services")
        by_id = {f["id"]: f for f in data["features"]}
        self.assertEqual(by_id["service-a"]["deps"], ["base", "dir:src/common"])
        self.assertEqual(by_id["service-a"]["stage"], 2)
        self.assertEqual(by_id["service-c"]["tries"], 2)
        self.assertEqual(by_id["base"]["regression"], "ok")
        self.assertIsNone(by_id["base"]["e2e"])
        self.assertEqual(by_id["service-b"]["kind"], "front")
        self.assertIn(["service-a", "service-b"], data["graph"]["edges"])
        self.assertNotIn(["dir:src/common", "service-a"], data["graph"]["edges"])
        self.assertEqual(len(data["quarantine"]), 1)
        self.assertEqual(data["quarantine"][0]["id"], "service-c")
        self.assertEqual(data["quarantine"][0]["stuck"], ["service-d"])
        self.assertEqual(data["quarantine"][0]["options"], ["try again", "skip and continue"])
        self.assertEqual(data["quarantine"][0]["reason"], "gate-rejected (2 failures)")

    def test_agents_without_events_do_not_invent_numbers(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        self.fixture.set_loop("continue")
        self.fixture.write_json("turns/002.json", {"total_cost_usd": 0.5, "duration_ms": 12000, "result": "done", "usage": {"input_tokens": 10, "output_tokens": 5}})
        data = self.state()
        agents = {a["id"]: a for a in data["agents"]}
        self.assertEqual(agents["orch"]["tokens"], 15)
        self.assertEqual(agents["orch"]["cost_usd"], 0.5)
        self.assertEqual(agents["orch"]["duration_s"], 12)
        self.assertEqual(agents["orch"]["state"], "running")
        self.assertEqual(agents["orch"]["role"], "orchestrator")
        self.assertEqual(agents["service-a:executor-back"]["state"], "running")
        self.assertEqual(agents["service-b:spec-writer"]["state"], "queued")
        self.assertEqual(agents["service-d:executor-generic"]["state"], "queued")
        for agent_id, agent in agents.items():
            if agent_id != "orch":
                self.assertIsNone(agent["tokens"])
                self.assertIsNone(agent["cost_usd"])
        self.assertNotIn("base:executor-back", agents)
        self.assertNotIn("service-c:executor-back", agents)
        self.assertEqual(data["loop"]["turn"], 3)  # running: the next one after the last one done

    def test_running_status_uses_the_lock_even_with_an_old_loop_json(self):
        self.fixture.start()
        self.fixture.set_loop("done")
        self.fixture.set_lock_alive()
        self.fixture.write_json("LOOP-CMD.json", {"argv": ["python", "run_loop.py", "x", "--max-cost", "50", "--timeout=20"], "cwd": self.fixture.tmp})
        data = self.state()
        self.assertEqual(data["loop"]["status"], "running")
        self.assertTrue(data["loop"]["running"])
        self.assertEqual(data["loop"]["pid"], os.getpid())
        limits = {item["key"]: item for item in data["limits"]}
        self.assertEqual(limits["cost"]["max"], 50.0)
        self.assertEqual(limits["turns_without_progress"]["max"], 10)
        self.assertEqual(limits["turn_minutes"]["max"], 20)
        self.assertEqual([item["label"] for item in data["limits"]], ["cost", "without progress", "current turn"])
        self.assertFalse(data["loop"]["pause_requested"])
        self.assertIsNone(data["monitor"]["shutdown_in_s"], "a running loop keeps the app busy: no shutdown countdown")

    def test_pause_requested_comes_from_the_pause_file(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        self.fixture.write(self.fixture.at("PAUSE"), "")
        self.assertTrue(self.state()["loop"]["pause_requested"])

    def test_limits_without_turn_time_when_not_running(self):
        self.fixture.start()
        self.fixture.set_loop("done")
        keys = [item["key"] for item in self.state()["limits"]]
        self.assertEqual(keys, ["cost", "turns_without_progress"])

    def test_awaiting_user_status_brings_question_and_options(self):
        self.fixture.start()
        self.fixture.set_loop("awaiting_user", reason="Which path?", options=["a (recommended)", "b"], to_check=["x"])
        loop = self.state()["loop"]
        self.assertEqual(loop["status"], "awaiting_user")
        self.assertEqual(loop["reason"], "Which path?")
        self.assertEqual(loop["next"], "next step")
        self.assertEqual(loop["options"], ["a (recommended)", "b"])
        self.assertEqual(loop["to_check"], ["x"])
        self.assertEqual(loop["feature"], ["service-a"])
        self.assertEqual(loop["turn"], 2)

    def test_done_status_shows_the_idle_countdown_and_the_project_url(self):
        self.fixture.start(idle_hours=1)
        self.fixture.set_loop("done")
        data = self.state()
        self.assertEqual(data["loop"]["status"], "done")
        self.assertIsNotNone(data["monitor"]["shutdown_in_s"])
        self.assertLessEqual(data["monitor"]["shutdown_in_s"], 3600)
        self.assertIsNone(data["monitor"]["idle_in_s"])
        self.assertEqual(data["monitor"]["url"], "%sp/%s/" % (self.fixture.server.url, PROJECT_ID))

    def test_no_loop_shows_the_idle_countdown(self):
        self.fixture.start(idle_hours=1)
        data = self.state()
        self.assertEqual(data["loop"]["status"], "no_loop")
        self.assertLessEqual(data["monitor"]["shutdown_in_s"], 3600)

    def test_activity_counts_with_visible_and_not_without_it(self):
        self.fixture.start()
        self.fixture.activity_monitor.last_activity = 0
        self.fixture.call("/api/state")
        self.assertEqual(self.fixture.activity_monitor.last_activity, 0)
        self.fixture.call("/api/state?visible=1")
        self.assertGreater(self.fixture.activity_monitor.last_activity, time.time() - 60)

    def test_error_status_from_loop_json_and_from_meta_when_loop_json_stayed_at_continue(self):
        self.fixture.start()
        self.fixture.set_loop("error", reason="it broke")
        self.assertEqual(self.state()["loop"]["status"], "error")
        self.assertEqual(self.state()["loop"]["reason"], "it broke")
        self.fixture.set_loop("continue")
        self.fixture.write_json("meta.json", {"stopped": "error", "reason": "cost cap", "finished_at": "2026-10-15T11:00:00"})
        loop = self.state()["loop"]
        self.assertEqual((loop["status"], loop["reason"]), ("error", "cost cap"))

    def test_stopped_status_when_continue_without_a_live_lock(self):
        self.fixture.start()
        self.fixture.set_loop("continue")
        self.fixture.write(self.fixture.at("LOOP.lock"), "99999999")
        loop = self.state()["loop"]
        self.assertEqual(loop["status"], "stopped")
        self.assertFalse(loop["running"])
        self.assertIsNone(loop["pid"])

    def test_answers_validate_and_turns_come_from_the_files(self):
        self.fixture.start()
        self.fixture.set_loop("done")
        self.fixture.write(self.fixture.at("ANSWERS.md"),
                           "\n## Answer 2026-10-15T10:00:00 [applied]\nQuestion: Q1?\nUser answer: yes\n"
                           "\n## Answer 2026-10-15T10:05:00 [pending]\nQuestion: Q2?\nUser answer: note\n"
                           "\n## Answer 2026-10-15T10:10:00 [applied]\nQuestion: Q3?\nUser answer: option 1 [automatic answer from the loop: option 1, recommended]\n")
        self.fixture.write(self.fixture.at("VALIDATE.md"), "# Validate\n- [base] doubt | chose: x | revert: y\n")
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.25, "duration_ms": 4000, "result": "ok"})
        self.fixture.snapshot(1, roadmap="## Stage 1\n- [old] status=pending paths=x\n", loop={"status": "continue", "turn": 1, "feature": "a,b"}, head="abc")
        data = self.state()
        self.assertEqual([a["state"] for a in data["answers"]], ["applied", "pending", "automatic"])
        self.assertEqual((data["answers"][0]["date"], data["answers"][0]["question"], data["answers"][0]["answer"]),
                         ("2026-10-15T10:00:00", "Q1?", "yes"))
        self.assertEqual(data["validate"], ["- [base] doubt | chose: x | revert: y"])
        self.assertEqual(len(data["turns"]), 1)
        turn = data["turns"][0]
        self.assertEqual((turn["n"], turn["cost_usd"], turn["duration_s"], turn["status"], turn["feature"], turn["has_snapshot"]),
                         (1, 0.25, 4, "continue", ["a", "b"], True))
        status, detail = self.fixture.call("/api/turn/1")
        self.assertEqual(status, 200)
        self.assertEqual(set(detail), {"n", "turn_log", "roadmap", "features"})
        self.assertEqual(detail["features"][0]["id"], "old")
        self.assertIn("old", detail["roadmap"])
        self.assertEqual(detail["turn_log"]["result"], "ok")
        self.assertEqual(self.fixture.call("/api/turn/7"), (404, {"ok": False, "error": "turn without snapshot"}))
        self.assertEqual(self.fixture.call("/api/turn/abc")[0], 404)

    def test_turns_without_progress_are_counted_from_the_snapshots(self):
        self.fixture.start()
        advanced = ROADMAP.replace("[service-b] status=pending", "[service-b] status=specced")
        for number, roadmap in ((0, ROADMAP), (1, advanced), (2, advanced), (3, advanced)):
            self.fixture.snapshot(number, roadmap=roadmap, loop={"status": "continue"})
            self.fixture.write_json("turns/%03d.json" % number, {"total_cost_usd": 0.1})
        limits = {item["key"]: item for item in self.state()["limits"]}
        self.assertEqual(limits["turns_without_progress"]["value"], 2)

    def test_missing_agent_log_is_an_empty_list_and_events_sum_tokens(self):
        self.fixture.start()
        self.fixture.set_lock_alive()
        self.fixture.set_loop("continue")
        self.assertEqual(self.fixture.call("/api/agent/service-a:executor-back/log")[1], {"id": "service-a:executor-back", "log": []})
        self.fixture.write_json("turns/002.json", {"result": "r"})
        self.fixture.write(os.path.join(self.fixture.turns_dir, "002.events.jsonl"),
                           json.dumps({"agent": "service-a:executor-back", "type": "tool", "text": "Edit x", "tokens": 100, "cost_usd": 0.1, "t": "00:01"}) + "\n"
                           + json.dumps({"agent": "service-a:executor-back", "type": "ai", "text": "ok", "tokens": 50, "t": "00:02"}) + "\nbroken line\n")
        status, log = self.fixture.call("/api/agent/service-a%3Aexecutor-back/log")
        self.assertEqual([item["type"] for item in log["log"]], ["tool", "ai"])
        self.assertEqual(set(log["log"][0]), {"type", "text", "result", "t"})
        agents = {a["id"]: a for a in self.state()["agents"]}
        self.assertEqual(agents["service-a:executor-back"]["tokens"], 150)
        self.assertEqual(agents["service-a:executor-back"]["cost_usd"], 0.1)
        orch = self.fixture.call("/api/agent/orch/log")[1]["log"]
        self.assertEqual([(item["type"], item["text"]) for item in orch], [("ai", "r")])

    def test_turn_result_does_not_leak_credentials(self):
        self.fixture.start()
        self.fixture.snapshot(3, head="abc")
        self.fixture.write_json("turns/003.json", {"raw": "x" * 100, "result": "used token=abcdef123 ok",
                                                   "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "curl -H 'Authorization: Bearer abc'"}}]})
        _, detail = self.fixture.call("/api/turn/3")
        text = json.dumps(detail)
        self.assertNotIn("abcdef123", text)
        self.assertNotIn("Bearer abc", text)
        self.assertNotIn("raw", detail["turn_log"])

    def test_recent_actions_come_from_actions_log_newest_first(self):
        self.fixture.start()
        lines = [{"action": "summary", "t": "2026-10-15T10:00:00", "ok": True, "summary": "s1"}, {"action": "not-in-the-list", "t": "x", "ok": True},
                 {"action": "deliver", "t": "2026-10-15T11:00:00", "ok": False, "summary": "refused"}]
        self.fixture.write(self.fixture.at("actions.log"), "\n".join(json.dumps(item) for item in lines) + "\n{broken\n")
        self.assertEqual(self.state()["recent_actions"], [{"action": "deliver", "t": "2026-10-15T11:00:00", "ok": False, "summary": "refused"},
                                                          {"action": "summary", "t": "2026-10-15T10:00:00", "ok": True, "summary": "s1"}])


class LayoutTests(ReadTestCase):
    def test_files_live_where_the_engine_writes_them(self):
        loop_dir = os.path.join(self.fixture.project_dir, "loop")
        self.assertEqual(self.fixture.at("LOOP.json"), os.path.join(loop_dir, "json", "LOOP.json"))
        self.assertEqual(self.fixture.at("meta.json"), os.path.join(loop_dir, "json", "meta.json"))
        self.assertEqual(self.fixture.at("LOOP.lock"), os.path.join(loop_dir, "internal", "LOOP.lock"))
        self.assertEqual(self.fixture.at("actions.log"), os.path.join(loop_dir, "internal", "actions.log"))
        self.assertEqual(self.fixture.at("ANSWERS.md"), os.path.join(loop_dir, "ANSWERS.md"))
        self.assertEqual(self.fixture.turns_dir, os.path.join(loop_dir, "internal", "turns"))


class WorkReevaluationTests(ReadTestCase):
    def test_worktree_created_later_starts_being_read(self):
        shutil.rmtree(self.fixture.work)  # starts without a worktree: reads the repo itself
        self.fixture.write(os.path.join(self.fixture.repo, ".specs", "project", "ROADMAP.md"), "## Stage 1\n- [repo-only] status=pending paths=x\n")
        self.fixture.start()
        data = self.state()
        self.assertEqual(data["loop"]["status"], "no_loop")
        self.assertEqual(os.path.normcase(data["work"]), os.path.normcase(self.fixture.repo))
        self.assertEqual([f["id"] for f in data["features"]], ["repo-only"])
        self.fixture.write(os.path.join(self.fixture.project_dir, "ROADMAP.md"), ROADMAP)  # a new loop creates the worktree later
        self.fixture.set_loop("awaiting_user", reason="Which path?")
        data = self.state()
        self.assertEqual(data["loop"]["status"], "awaiting_user")
        self.assertEqual(os.path.normcase(data["work"]), os.path.normcase(self.fixture.work))
        self.assertIn("service-a", [f["id"] for f in data["features"]])
        self.assertEqual(os.path.normcase(self.fixture.call("/api/health")[1]["work"]), os.path.normcase(self.fixture.work))


class MultiProjectTests(ReadTestCase):
    def test_each_project_reads_its_own_folder(self):
        other = self.fixture.add_project("other", "p-other02", roadmap="## Stage 1\n- [single] status=pending paths=x\n")
        other.set_loop("awaiting_user", reason="question from the other one")
        self.fixture.set_loop("done")
        self.fixture.start()
        main, second = self.state(), self.state("p-other02")
        self.assertEqual((main["loop"]["status"], second["loop"]["status"]), ("done", "awaiting_user"))
        self.assertEqual([f["id"] for f in second["features"]], ["single"])
        self.assertEqual(second["project_ref"]["id"], "p-other02")
        self.assertEqual(second["project"], "other")
        self.assertTrue(second["monitor"]["url"].endswith("/p/p-other02/"))

    def test_is_any_loop_running_sweeps_every_project_and_is_a_busy_provider(self):
        other = self.fixture.add_project("other", "p-other02")
        module = self.fixture.start()
        service = module.loop_status_service
        self.assertFalse(service.is_any_loop_running())
        self.assertFalse(service.is_busy())
        other.set_lock_alive()
        self.assertTrue(service.is_any_loop_running())
        self.assertIn(service, module.busy_providers)
        self.assertIsNone(self.fixture.activity_monitor.seconds_until_shutdown())

    def test_get_loop_summary_shape(self):
        module = self.fixture.start()
        self.fixture.set_loop("awaiting_user", reason="Which?")
        self.fixture.write_json("meta.json", {"turns": 2})
        context = self.fixture.project_service.get_project_context(PROJECT_ID)
        summary = module.loop_status_service.get_loop_summary(context)
        self.assertEqual(set(summary), {"status", "running", "pid", "json", "meta", "reason"})
        self.assertEqual((summary["status"], summary["running"], summary["pid"], summary["reason"]), ("awaiting_user", False, None, "Which?"))
        self.assertEqual(summary["meta"], {"turns": 2})
        self.assertEqual(summary["json"]["status"], "awaiting_user")


class GraphTests(ReadTestCase):
    def test_graph_brings_dependency_mermaid_state_mermaid_and_text(self):
        self.fixture.start()
        status, data = self.fixture.call("/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(set(data), {"dependencies_mermaid", "states_mermaid", "text"})
        self.assertTrue(data["dependencies_mermaid"].startswith("flowchart LR"))
        self.assertIn("n_service_a --> n_service_b", data["dependencies_mermaid"])
        self.assertIn('blocked["blocked (1)<br/>service-c"]', data["states_mermaid"])
        self.assertIn("Stage 2", data["text"])
        self.assertIn("STUCK on: service-c", data["text"])

    def test_graph_without_roadmap_is_not_an_error(self):
        os.remove(os.path.join(self.fixture.project_dir, "ROADMAP.md"))
        self.fixture.start()
        status, data = self.fixture.call("/api/graph")
        self.assertEqual(status, 200)
        self.assertEqual(data["text"], "(ROADMAP has no features)")


class PlanningRequiredStateTests(ReadTestCase):
    def test_state_says_when_the_project_is_in_planning_mode(self):
        from harness_backend.domain.project import Project
        from harness_backend.repositories.loop_file_repository import loop_state
        self.fixture.start()
        self.assertFalse(self.state()["planning_required"], "a project with a loop worktree already looped")
        fresh = os.path.join(self.fixture.tmp, "fresh")
        self.fixture.write(os.path.join(fresh, ".specs", "project", "ROADMAP.md"), "## Stage 1\n- [a] status=pending\n")
        self.fixture.project_service.register_project(Project(id="p-fresh", name="fresh", repo_path=fresh))
        self.assertTrue(self.state("p-fresh")["planning_required"])
        loop_state.approve_roadmap(fresh, "2026-10-16T10:00:00")
        self.assertFalse(self.state("p-fresh")["planning_required"])


if __name__ == "__main__":
    unittest.main()
