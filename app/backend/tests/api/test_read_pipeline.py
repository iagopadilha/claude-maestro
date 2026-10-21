"""`GET pipeline`: fixed steps, features with wave, per-turn events (agents of the turn + status changes between snapshots), label, files,
live turn, paused/quarantine flags and the cache."""
import os
import subprocess
import unittest

from harness_backend.domain.project import ProjectContext

from .support_read import PROJECT_ID, ReadTestCase, TranscriptFixtureMixin

BEFORE_ROADMAP = """## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base regression=ok

## Stage 2 - Services

- [service-a] status=specced scope=Medium kind=back paths=src/a deps=base,dir:src/common
- [service-b] status=pending scope=Small kind=front paths=src/b deps=service-a
- [service-c] status=building tries=1 scope=Small kind=back paths=src/c deps=base
- [service-d] status=building scope=Small kind=generic paths=src/d deps=service-c
"""
AFTER_ROADMAP = """## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base regression=ok

## Stage 2 - Services

- [service-a] status=approved scope=Medium kind=back paths=src/a deps=base,dir:src/common
- [service-b] status=pending scope=Small kind=front paths=src/b deps=service-a
- [service-c] status=blocked tries=2 note=gate-rejected scope=Small kind=back paths=src/c deps=base
- [service-d] status=verified commit=abc1234def scope=Small kind=generic paths=src/d deps=service-c
"""
SAME_WAVE_ROADMAP = """## Stage 1 - Base

- [slug-a] status=pending scope=Small kind=back paths=src/a
- [slug-b] status=specced scope=Small kind=back paths=src/b
"""


class PipelineTests(TranscriptFixtureMixin, ReadTestCase):
    def pipeline(self):
        status, data = self.fixture.call("/api/pipeline")
        self.assertEqual(status, 200, data)
        return data

    def test_without_turns_it_still_lists_steps_features_waves_and_quarantine(self):
        self.fixture.start()
        data = self.pipeline()
        self.assertEqual([step["key"] for step in data["steps"]], ["spec", "review", "gate", "exec", "verify", "commit"])
        self.assertEqual([step["agent"] for step in data["steps"]], ["spec-writer", "spec-reviewer", "gate", "executor", "verifier", "commit"])
        waves = {feature["id"]: feature["wave"] for feature in data["features"]}
        self.assertEqual(waves, {"base": 1, "service-a": 2, "service-b": 3, "service-c": 2, "service-d": 3})
        by_id = {feature["id"]: feature for feature in data["features"]}
        self.assertEqual(by_id["service-a"]["deps"], ["base"])  # dir: dependencies are not slugs
        self.assertEqual((by_id["service-b"]["stage"], by_id["service-b"]["kind"], by_id["service-b"]["status"]), (2, "front", "pending"))
        self.assertEqual([stage["n"] for stage in data["stages"]], [1, 2])
        self.assertEqual((data["turns"], data["live_turn"], data["paused"], data["quarantine"]), ([], None, False, ["service-c"]))

    def test_events_come_from_the_turn_agents_and_the_label_from_their_steps(self):
        self.setup_projects(number=1)
        self.fixture.start(projects_dir=self.projects)
        turn, = self.pipeline()["turns"]
        self.assertEqual((turn["n"], turn["label"], turn["cost_usd"], turn["duration_s"], turn["decision"]),
                         (1, "spec writing + spec review", 0.96, 13, "turn ok"))
        writer, reviewer = sorted(turn["events"], key=lambda event: event["step"])
        self.assertEqual((writer["feature"], writer["step"], writer["status"], writer["agent"], writer["agent_id"]),
                         ("slug-a", 0, "DONE", "spec-writer", "v001:aaa111"))
        self.assertEqual((reviewer["feature"], reviewer["step"], reviewer["agent"]), ("slug-b", 1, "spec-reviewer"))
        self.assertEqual(writer["out"], "more")
        self.assertEqual(turn["files"], [])

    def test_status_changes_between_snapshots_become_gate_commit_and_blocked_events(self):
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.5, "duration_ms": 2000, "result": "closed the gate"})
        self.fixture.snapshot(0, roadmap=BEFORE_ROADMAP)
        self.fixture.snapshot(1, roadmap=AFTER_ROADMAP, loop={"status": "continue", "turn": 1})
        self.fixture.start()
        turn, = self.pipeline()["turns"]
        self.assertEqual(turn["status"], "continue")
        marks = {(event["feature"], event["step"], event["status"], event["agent"], event["out"]) for event in turn["events"]}
        self.assertEqual(marks, {("service-a", 2, "DONE", "gate", None), ("service-d", 5, "DONE", "commit", "commit abc1234"),
                                 ("service-c", 2, "BLOCKED", "gate", "gate-rejected")})
        self.assertEqual(turn["label"], "gate + commit")

    def test_label_adds_the_wave_when_every_feature_of_the_turn_shares_one(self):
        self.setup_projects(number=1)
        self.fixture.write(os.path.join(self.fixture.project_dir, "ROADMAP.md"), SAME_WAVE_ROADMAP)
        self.fixture.start(projects_dir=self.projects)
        turn, = self.pipeline()["turns"]
        self.assertEqual(turn["label"], "spec writing + spec review · wave 1")

    def test_a_running_loop_adds_the_live_turn_with_running_events(self):
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.1})
        self.fixture.set_loop("continue", turn=1)
        self.fixture.set_lock_alive()
        self.fixture.start()
        data = self.pipeline()
        live = data["turns"][-1]
        self.assertEqual((data["live_turn"], live["n"], live["status"], live["cost_usd"], live["files"]), (2, 2, "running", None, []))
        self.assertEqual([(event["feature"], event["step"], event["status"], event["agent_id"]) for event in live["events"]],
                         [("service-a", 3, "RUNNING", None)])

    def test_awaiting_user_is_paused(self):
        self.fixture.set_loop("awaiting_user")
        self.fixture.start()
        self.assertTrue(self.pipeline()["paused"])

    def test_the_body_is_cached_until_a_turn_file_changes(self):
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.1})
        module = self.fixture.start()
        context = ProjectContext(self.fixture.project_service.get_project(PROJECT_ID))
        first = module.pipeline_service.build_pipeline(context)
        self.assertIs(module.pipeline_service.build_pipeline(context), first)
        self.fixture.write_json("turns/002.json", {"total_cost_usd": 0.2})
        self.assertEqual([turn["n"] for turn in module.pipeline_service.build_pipeline(context)["turns"]], [1, 2])

    def test_files_of_the_turn_come_from_the_numstat_between_the_snapshot_heads(self):
        work = self.fixture.work
        os.makedirs(work, exist_ok=True)

        def git(*args):
            return subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@t", "-C", work] + list(args), capture_output=True, text=True,
                                  check=True).stdout.strip()
        git("init")
        self.fixture.write(os.path.join(work, "src", "x.py"), "a\nb\n")
        git("add", "src")
        git("commit", "-m", "first")
        first = git("rev-parse", "HEAD")
        self.fixture.write(os.path.join(work, "src", "x.py"), "a\nc\nd\n")
        git("add", "src")
        git("commit", "-m", "second")
        second = git("rev-parse", "HEAD")
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.1})
        self.fixture.snapshot(0, roadmap=BEFORE_ROADMAP, head=first)
        self.fixture.snapshot(1, roadmap=BEFORE_ROADMAP, head=second)
        self.fixture.start()
        turn, = self.pipeline()["turns"]
        self.assertEqual(turn["files"], [{"path": "src/x.py", "add": 2, "del": 1}])


if __name__ == "__main__":
    unittest.main()
