"""Error center: the loop stop, failed turns, guard blocks and blocked features become one classified list."""
import datetime
import json
import os
import tempfile
import unittest

from harness_backend.repositories.error_event_repository import ErrorEventRepository
from harness_backend.repositories.loop_file_repository import LoopFileRepository, loop_state
from harness_backend.services.error_center_service import ErrorCenterService, classify, RECOVER_COMMIT, RECOVER_DISCARD, RECOVER_RETRY


class FakeStatus:
    def __init__(self, status, reason="", running=False):
        self.summary = {"status": status, "reason": reason, "running": running, "json": {"turn": 3}}

    def get_loop_summary(self, context):
        return self.summary


class Context:
    def __init__(self, work_dir):
        self.work_dir = work_dir


class TestErrorCenter(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="errcenter-")
        os.makedirs(loop_state.turns_dir(self.work))
        os.makedirs(os.path.dirname(loop_state.loop_path(self.work, "guard.log")), exist_ok=True)
        self.event_repository = ErrorEventRepository()

    def write_turn(self, number, **fields):
        with open(os.path.join(loop_state.turns_dir(self.work), "%03d.json" % number), "w", encoding="utf-8") as handle:
            json.dump(dict({"exit_code": 0, "timeout": False, "turn": number}, **fields), handle)

    def build(self, status):
        return ErrorCenterService(status, LoopFileRepository()).build_errors(Context(self.work))

    def test_dirty_tree_stop_offers_commit_and_discard(self):
        title, hint, recover = classify("dirty tree outside .specs/ before turn 4: M a.py")
        self.assertIn("uncommitted", title)
        self.assertEqual(recover, [RECOVER_COMMIT, RECOVER_DISCARD])
        self.assertTrue(hint)

    def test_unknown_reason_keeps_the_reason_and_offers_retry(self):
        self.assertEqual(classify("something new broke"), ("something new broke", "", [RECOVER_RETRY]))

    def test_the_loop_stop_is_first_and_a_running_loop_has_no_stop_item(self):
        self.write_turn(3, stderr='Background tasks still running 10m after the last turn (subagent "Verify x")')
        result = self.build(FakeStatus("error", "dirty tree outside .specs/ before turn 4: M a.py"))
        self.assertEqual(result["items"][0]["kind"], "loop")
        self.assertEqual(result["counts"], {"error": 1, "warning": 0, "info": 1})
        self.assertEqual(result["problems"], 1)
        running = self.build(FakeStatus("running", running=True))
        self.assertEqual([item["kind"] for item in running["items"]], ["turn"])
        self.assertIn("verifier", running["items"][0]["title"])

    def test_failed_and_timed_out_turns_are_errors(self):
        self.write_turn(1, exit_code=1, stderr="boom")
        self.write_turn(2, timeout=True)
        result = self.build(FakeStatus("continue"))
        self.assertEqual(sorted(item["id"] for item in result["items"]), ["turn-1-failed", "turn-2-timeout"])
        self.assertEqual(result["counts"]["error"], 2)

    def test_guard_blocks_are_grouped_by_rule_and_secrets_are_redacted(self):
        with open(loop_state.loop_path(self.work, "guard.log"), "w", encoding="utf-8") as handle:
            for _ in range(3):
                handle.write(json.dumps({"t": "2026-10-19T22:55:19", "profile": "executor", "rule": "git-push", "decision": "blocked", "detail": "git push token=abc123"}) + "\n")
            handle.write(json.dumps({"profile": "executor", "rule": "x", "decision": "info", "detail": "ignored"}) + "\n")
        result = self.build(FakeStatus("continue"))
        items = result["items"]
        self.assertEqual(result["problems"], 0)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 3)
        self.assertEqual(items[0]["severity"], "info")
        self.assertNotIn("abc123", items[0]["detail"])

    def test_old_guard_blocks_are_still_listed_with_no_window_and_no_turn_when_unknown(self):
        self.write_turn(1)
        self.write_turn(2)
        old = os.path.getmtime(os.path.join(loop_state.turns_dir(self.work), "001.json")) - 3600
        os.utime(os.path.join(loop_state.turns_dir(self.work), "001.json"), (old, old))
        with open(loop_state.loop_path(self.work, "guard.log"), "w", encoding="utf-8") as handle:
            handle.write(json.dumps({"t": "2000-01-01T00:00:00", "profile": "executor", "rule": "git-push", "decision": "blocked", "detail": "old"}) + "\n")
        result = self.build(FakeStatus("continue"))
        self.assertEqual([(item["id"], item["severity"], item["turns"]) for item in result["items"]], [("guard-executor-git-push", "info", [])])
        self.assertEqual(result["counts"], {"error": 0, "warning": 0, "info": 1})
        self.assertEqual(result["problems"], 0)

    def test_every_item_lists_its_turns_and_a_hint_only_when_there_is_something_to_do(self):
        self.write_turn(1, exit_code=1, stderr="boom")
        self.write_turn(2, stderr="some noise")
        result = self.build(FakeStatus("error", "turn 3 exceeded 30 min (process terminated)"))
        by_id = {item["id"]: item for item in result["items"]}
        self.assertEqual((by_id["turn-1-failed"]["turns"], by_id["turn-1-failed"]["turn"]), ([1], 1))
        self.assertEqual(by_id["turn-2-stderr"]["turns"], [2])
        self.assertEqual(by_id["loop-stop"]["turns"], [3])  # LOOP.json (FakeStatus) says 3 as well
        self.assertIn("time limit", by_id["loop-stop"]["happened"])
        self.assertTrue(by_id["loop-stop"]["hint"])
        self.assertEqual(by_id["turn-1-failed"]["hint"], "")
        self.assertTrue(by_id["turn-1-failed"]["happened"])

    def test_the_stop_turn_comes_from_the_reason_before_the_stale_loop_json(self):
        reason = "LOOP.json missing, stale or invalid after turn 5 (got status='continue' turn=4)"
        item = self.build(FakeStatus("error", reason))["items"][0]
        self.assertEqual((item["turn"], item["turns"]), (5, [5]))

    def iso(self, epoch):
        return datetime.datetime.fromtimestamp(epoch).isoformat(timespec="seconds")

    def write_turn_ending_at(self, number, end, **fields):
        self.write_turn(number, **fields)
        path = os.path.join(loop_state.turns_dir(self.work), "%03d.json" % number)
        os.utime(path, (end, end))

    def write_guard(self, *lines):
        with open(loop_state.loop_path(self.work, "guard.log"), "w", encoding="utf-8") as handle:
            for line in lines:
                handle.write(json.dumps(dict({"profile": "executor", "rule": "git-push", "decision": "blocked", "detail": "d"}, **line)) + "\n")

    def guard_items(self, result):
        return [row for row in result["items"] if row["kind"] == "guard"]

    def test_old_guard_lines_without_a_turn_are_mapped_by_timestamp(self):
        base = datetime.datetime(2026, 10, 19, 22, 0, 0).timestamp()
        self.write_turn_ending_at(1, base + 100, duration_ms=60000)  # window 40..100
        self.write_turn_ending_at(2, base + 200)  # window 100..200 (starts when turn 1 ended)
        self.write_turn_ending_at(3, base + 300)
        self.write_guard({"t": self.iso(base + 10)}, {"t": self.iso(base + 70)}, {"t": self.iso(base + 150)}, {"t": self.iso(base + 160)},
                         {"t": self.iso(base + 250)}, {"t": self.iso(base + 400)})
        item = self.guard_items(self.build(FakeStatus("continue")))[0]
        self.assertEqual(item["count"], 6)
        self.assertEqual(item["turns"], [1, 2, 3])
        self.assertEqual(item["turn_counts"], {"1": 1, "2": 2, "3": 1})  # before turn 1 and after the last turn: unknown, never invented
        self.assertEqual((item["rule"], item["profile"], item["turn"]), ("git-push", "executor", 3))

    def test_the_turn_written_by_the_hook_wins_over_the_timestamp(self):
        base = datetime.datetime(2026, 10, 19, 22, 0, 0).timestamp()
        self.write_turn_ending_at(1, base + 100, duration_ms=100000)
        self.write_guard({"t": self.iso(base + 50), "turn": 7}, {"t": self.iso(base + 50), "turn": None})
        item = self.guard_items(self.build(FakeStatus("continue")))[0]
        self.assertEqual((item["turns"], item["turn_counts"]), ([1, 7], {"1": 1, "7": 1}))

    def test_a_planning_block_belongs_to_no_turn(self):
        base = datetime.datetime(2026, 10, 19, 22, 0, 0).timestamp()
        self.write_turn_ending_at(1, base + 100, duration_ms=100000)
        self.write_guard({"t": self.iso(base + 50), "profile": "planning"})
        self.assertEqual(self.guard_items(self.build(FakeStatus("continue")))[0]["turns"], [])

    def test_guard_items_are_per_profile_and_rule_with_the_distinct_attempts(self):
        attempts = ["git push origin x%d token=abc123" % number for number in range(8)]
        self.write_guard(*([{"t": "2026-10-19T22:00:00", "attempt": attempt} for attempt in attempts] + [{"t": "2026-10-19T22:00:01", "attempt": attempts[0]}]),
                         {"t": "2026-10-19T22:00:02", "profile": "verifier", "attempt": "git push"}, {"t": "2026-10-19T22:00:03", "rule": "secrets", "profile": "verifier"})
        items = {item["id"]: item for item in self.guard_items(self.build(FakeStatus("continue")))}
        self.assertEqual(sorted(items), ["guard-executor-git-push", "guard-verifier-git-push", "guard-verifier-secrets"])
        pushes = items["guard-executor-git-push"]
        self.assertEqual(pushes["count"], 9)
        self.assertEqual(len(pushes["attempts"]), 5)
        self.assertEqual(len(set(pushes["attempts"])), 5)
        self.assertTrue(all("abc123" not in attempt for attempt in pushes["attempts"]))
        self.assertEqual(items["guard-verifier-git-push"]["attempts"], ["git push"])
        self.assertEqual(items["guard-verifier-secrets"]["attempts"], [])

    def build_stored(self, status="continue"):
        return ErrorCenterService(FakeStatus(status), LoopFileRepository(), self.event_repository).build_errors(Context(self.work))

    def read_stored_lines(self):
        with open(os.path.join(loop_state.loop_path(self.work, "events"), "errors.jsonl"), encoding="utf-8") as handle:
            return handle.read().splitlines()

    def test_events_are_stored_once_and_survive_a_truncated_guard_log(self):
        base = datetime.datetime(2026, 10, 19, 22, 0, 0).timestamp()
        self.write_turn_ending_at(1, base + 100, duration_ms=100000)
        self.write_guard(*[{"t": self.iso(base + 50)}] * 3)  # three identical lines are three events
        self.assertEqual(self.guard_items(self.build_stored())[0]["count"], 3)
        self.build_stored()
        self.assertEqual(len(self.read_stored_lines()), 3)
        self.write_guard()  # guard.log rotated away
        item = self.guard_items(self.build_stored())[0]
        self.assertEqual((item["count"], item["turns"], item["turn_counts"]), (3, [1], {"1": 3}))

    def test_an_event_without_a_turn_is_filled_in_when_the_turn_file_appears(self):
        base = datetime.datetime(2026, 10, 19, 22, 0, 0).timestamp()
        self.write_guard({"t": self.iso(base + 50)})
        self.assertEqual(self.guard_items(self.build_stored())[0]["turns"], [])
        self.write_turn_ending_at(1, base + 100, duration_ms=100000)
        self.assertEqual(self.guard_items(self.build_stored())[0]["turns"], [1])
        self.assertEqual(len(self.read_stored_lines()), 2)  # the original record + one that only carries the turn
        self.write_guard()
        self.assertEqual(self.guard_items(self.build_stored())[0]["turns"], [1])

    def test_the_stored_texts_are_redacted(self):
        self.write_guard({"t": "2026-10-19T22:00:00", "detail": "token=abc123", "attempt": "curl -H password=hunter2"})
        self.build_stored()
        self.assertNotIn("abc123", "\n".join(self.read_stored_lines()))
        self.assertNotIn("hunter2", "\n".join(self.read_stored_lines()))

    def write_launch_log(self, text):
        launch = loop_state.loop_path(self.work, "monitor-relaunch.log")
        os.makedirs(os.path.dirname(launch), exist_ok=True)
        with open(launch, "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_launch_problems_carry_the_turn_they_were_printed_in(self):
        self.write_launch_log("[loop-launch] 2026-10-19T22:39:53 loop_start\n[check] PROBLEM before any turn\n[loop] turn 1 | 22:40:00\n"
                              "[loop] turn 2 | 22:42:15\nTraceback (most recent call last)\n[loop] turn 3 | 22:43:09\n[loop] error in turn 3\n")
        item = [row for row in self.build_stored()["items"] if row["kind"] == "launch"][0]
        self.assertEqual(item["turns"], [1, 2, 3])
        self.assertEqual(item["turn_counts"], {"1": 1, "2": 1, "3": 1})  # the line before turn 1 is attributed to the first turn of the launch
        self.assertEqual(item["severity"], "warning")
        self.assertEqual(item["when"], "2026-10-19T22:39:53")

    def test_a_launch_without_problems_hides_the_problems_of_an_older_launch(self):
        self.write_launch_log("[loop-launch] 2026-10-19T22:00:00 a\nTraceback\n")
        self.assertEqual(len([row for row in self.build_stored()["items"] if row["kind"] == "launch"]), 1)
        self.write_launch_log("[loop-launch] 2026-10-19T22:00:00 a\nTraceback\n[loop-launch] 2026-10-19T23:00:00 b\nall fine\n")
        self.assertEqual([row for row in self.build_stored()["items"] if row["kind"] == "launch"], [])

    def test_launch_turn_lines_give_the_turn_windows_for_old_guard_lines(self):
        self.write_launch_log("[loop-launch] 2026-10-19T22:39:53 loop_start\n[loop] turn 1 | 22:40:00\n[loop] turn 2 | 22:42:15\n")
        base = datetime.datetime(2026, 10, 19, 22, 40, 0).timestamp()
        self.write_turn_ending_at(1, base + 130)
        self.write_turn_ending_at(2, base + 200)
        self.write_guard({"t": "2026-10-19T22:40:30"}, {"t": "2026-10-19T22:42:12"}, {"t": "2026-10-19T22:42:20"})  # turn 1, the gap, turn 2
        item = self.guard_items(self.build_stored())[0]
        self.assertEqual(item["turn_counts"], {"1": 1, "2": 1})

    def test_blocked_feature_turn_is_the_snapshot_where_it_became_blocked(self):
        roadmap = os.path.join(self.work, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap), exist_ok=True)
        with open(roadmap, "w", encoding="utf-8") as handle:
            handle.write("## Stage 1\n- [a] status=blocked tries=2 paths=src\n")
        for number, status in ((1, "building"), (2, "blocked"), (3, "blocked")):
            target = loop_state.snap_dir(self.work, number)
            os.makedirs(target)
            with open(os.path.join(target, "snapshot.json"), "w", encoding="utf-8") as handle:
                json.dump({}, handle)
            with open(os.path.join(target, "ROADMAP.md"), "w", encoding="utf-8") as handle:
                handle.write("## Stage 1\n- [a] status=%s paths=src\n" % status)
        item = self.build(FakeStatus("continue"))["items"][0]
        self.assertEqual((item["id"], item["turns"]), ("feature-a", [2]))

    def test_stderr_noise_is_info_and_launch_log_is_warning_only_when_fatal(self):
        self.write_turn(1, stderr="some noise")
        launch = loop_state.loop_path(self.work, "monitor-relaunch.log")
        os.makedirs(os.path.dirname(launch), exist_ok=True)
        with open(launch, "w", encoding="utf-8") as handle:
            handle.write("[loop-launch] now\nwarning: error code shown\n")
        result = self.build(FakeStatus("continue"))
        self.assertEqual({item["id"]: item["severity"] for item in result["items"]}, {"turn-1-stderr": "info", "launch-last": "info"})
        with open(launch, "w", encoding="utf-8") as handle:
            handle.write("[loop-launch] now\nTraceback (most recent call last)\n")
        result = self.build(FakeStatus("continue"))
        self.assertEqual((result["counts"], result["problems"]), ({"error": 0, "warning": 1, "info": 1}, 1))
        self.assertEqual(result["items"][0]["id"], "launch-last")

    def test_blocked_feature_is_listed(self):
        roadmap = os.path.join(self.work, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap), exist_ok=True)
        with open(roadmap, "w", encoding="utf-8") as handle:
            handle.write("## Stage 1\n- [a] status=blocked tries=2 paths=src\n")
        self.assertEqual([item["id"] for item in self.build(FakeStatus("continue"))["items"]], ["feature-a"])


if __name__ == "__main__":
    unittest.main()
