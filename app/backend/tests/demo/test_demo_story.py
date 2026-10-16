"""Demo mode: the seed is a valid loop project, the panel API reads it (features, turns, agents, guard events, history), one
animator step moves the state, the whole story ends `done` with legal transitions, and the reset brings the seed back.

Everything lives in a temporary folder; no claude, no real data folder (LOOP_DATA_DIR points to the temporary folder).
"""
import json
import os
import subprocess
import sys
import threading
import unittest
import urllib.error
import urllib.request

from harness_backend.api.http_server import AppHttpServer
from harness_backend.api.router import Router
from harness_backend.config.app_config import AppConfig
from harness_backend.demo.demo_animator import DemoAnimator, compute_story_seconds
from harness_backend.demo.demo_project_seeder import ENGINE_STUB_PATH, DemoProjectSeeder
from harness_backend.demo.demo_story_catalog import FEATURES, SEEDED_TURNS, build_turn_plans
from harness_backend.demo.demo_workspace import DemoWorkspace
from harness_backend.domain.project import Project
from harness_backend.infrastructure.activity_monitor import ActivityMonitor
from harness_backend.infrastructure.structured_log import StructuredLog
from harness_backend.repositories.loop_file_repository import loop_state
from harness_backend.services.loop_read_module import build_loop_read_module
from tests.api.support_read import FakeProjectService

PROJECT_ID = "demo00000001"
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
REQUEST_TIMEOUT_SECONDS = 15


class DemoTestCase(unittest.TestCase):
    def setUp(self):
        self.workspace = DemoWorkspace.create()
        self.addCleanup(self.workspace.remove)
        self.previous_data_dir = os.environ.get(DATA_DIR_VARIABLE)
        os.environ[DATA_DIR_VARIABLE] = self.workspace.app_data_dir
        self.addCleanup(self.restore_data_dir)
        self.seeder = DemoProjectSeeder(self.workspace)
        self.player = self.seeder.seed()
        self.server = None

    def restore_data_dir(self):
        if self.previous_data_dir is None:
            os.environ.pop(DATA_DIR_VARIABLE, None)
        else:
            os.environ[DATA_DIR_VARIABLE] = self.previous_data_dir

    def start_read_api(self):
        project_service = FakeProjectService()
        project_service.register_project(Project(id=PROJECT_ID, name="acme-checkout", repo_path=self.workspace.repo_dir))
        config = AppConfig(app_root_dir=self.workspace.base_dir, user_data_dir=self.workspace.app_data_dir, orchestrator_skill_dir="",
                           guard_path="", frontend_dir=os.path.join(self.workspace.base_dir, "frontend"), templates_dir="",
                           engine_dir=os.path.dirname(loop_state.__file__), claude_transcripts_dir=self.workspace.transcripts_dir,
                           output_dir=os.path.join(self.workspace.base_dir, "output"), port_range=(0, 0), idle_hours=0, open_browser=False)
        log = StructuredLog(config.logs_dir)
        router = Router()
        self.server = AppHttpServer(router, config.frontend_dir, ActivityMonitor(0, on_idle=lambda: None), log)
        for controller in build_loop_read_module(config, log, project_service, ActivityMonitor(0, on_idle=lambda: None)).controllers:
            controller.register_routes(router)
        self.server.open_port(config.port_range)
        thread = threading.Thread(target=self.server.serve_until_stopped, daemon=True)
        thread.start()
        self.addCleanup(lambda: (self.server.request_shutdown(), thread.join(10)))

    def get(self, route):
        request = urllib.request.Request("%sp/%s/api/%s" % (self.server.url, PROJECT_ID, route))
        try:
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            with error:
                self.fail("GET %s -> %s %s" % (route, error.code, error.read()[:300]))


class SeedTests(DemoTestCase):
    def test_seed_is_a_valid_loop_project_with_an_approved_roadmap(self):
        repo = self.workspace.repo_dir
        roadmap = loop_state.read(os.path.join(repo, ".specs", "project", "ROADMAP.md"))
        features, errors = loop_state.parse_roadmap(roadmap)
        self.assertEqual(errors + loop_state.check_fields(features), [])
        self.assertEqual(len(features), len(FEATURES))
        self.assertEqual(sorted({fields["stage"] for fields in features.values()}), ["1", "2", "3"])
        self.assertTrue(any(fields.get("deps") for fields in features.values()))
        self.assertTrue(loop_state.roadmap_approval(repo)["approved"])
        result = subprocess.run([sys.executable, os.path.join(os.path.dirname(loop_state.__file__), "loop_state.py"), "validate", repo],
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Conformance: STANDARD", loop_state.read(os.path.join(repo, "CLAUDE.md")))
        self.assertEqual(self.player.get_completed_turns(), SEEDED_TURNS)

    def test_seed_is_a_git_repo_with_commits_on_the_loop_branch(self):
        log = subprocess.run(["git", "-C", self.workspace.repo_dir, "log", "--oneline"], capture_output=True, text=True).stdout.splitlines()
        self.assertGreaterEqual(len(log), 4)  # 2 scaffold commits + one per feature verified in the seeded turns
        branch = subprocess.run(["git", "-C", self.workspace.repo_dir, "branch", "--show-current"], capture_output=True, text=True).stdout
        self.assertEqual(branch.strip(), "loop/acme-checkout")

    def test_loop_command_relaunches_the_engine_stub_never_run_loop(self):
        command = json.loads(loop_state.read(loop_state.loop_path(self.workspace.repo_dir, "LOOP-CMD.json")))
        self.assertEqual(command["argv"][1], ENGINE_STUB_PATH)
        self.assertNotIn("run_loop", " ".join(command["argv"]))


class PanelReadsTheDemoTests(DemoTestCase):
    def test_state_brings_features_turns_agents_and_guard_events(self):
        self.start_read_api()
        state = self.get("state")
        self.assertEqual(len(state["features"]), len(FEATURES))
        self.assertEqual([turn["n"] for turn in state["turns"]], [1, 2])
        self.assertTrue(all(turn["cost_usd"] and turn["duration_s"] for turn in state["turns"]))
        self.assertEqual(state["turns"][1]["commits"], 2)
        self.assertEqual(state["counts"]["verified"], 2)
        self.assertEqual(len(state["validate"]), 2)
        self.assertEqual([event["rule"] for event in state["guard_events"]], ["project-deny"])
        self.assertEqual(state["guard_counts"], {"project-deny": 1})
        roles = {agent["role"] for agent in state["agents"]}
        self.assertTrue({"orchestrator", "spec-reviewer", "executor-back", "verifier"} <= roles, roles)
        logs = [item for agent in state["agents"] for item in agent["log"]]
        self.assertIn("guard", [item["type"] for item in logs])

    def test_history_has_the_transcripts_and_the_blocked_edit_becomes_a_guard_item(self):
        self.start_read_api()
        history = self.get("history")["turns"]
        self.assertEqual([turn["n"] for turn in history], [2, 1])
        self.assertTrue(all(turn["transcript"] for turn in history))
        executor = next(agent for agent in history[0]["agents"] if agent["role"] == "executor-back" and agent["feature"] == "audit-log")
        self.assertIsNotNone(executor["tokens"])
        log = self.get("agent/%s/log" % executor["id"])["log"]
        guard = [item for item in log if item["type"] == "guard"]
        self.assertEqual(len(guard), 1)
        self.assertEqual(guard[0]["rule"], "project-deny")
        self.assertEqual(guard[0]["text"], "Write migrations/0007_drop_users.sql")

    def test_one_animator_step_moves_the_state(self):
        self.start_read_api()
        before = self.get("state")
        self.player.keeps_lock = True
        self.addCleanup(self.player.release_lock)
        self.assertTrue(self.player.apply_next_step())
        after = self.get("state")
        self.assertNotEqual(before["loop"]["status"], after["loop"]["status"])
        self.assertEqual(after["loop"]["status"], "running")
        by_id = {feature["id"]: feature["status"] for feature in after["features"]}
        self.assertEqual(by_id["rate-limiter"], "building")
        orchestrator = next(agent for agent in after["agents"] if agent["id"] == "orch")
        self.assertTrue(orchestrator["log"])


class StoryTests(DemoTestCase):
    def test_whole_story_has_legal_turns_and_ends_done(self):
        while self.player.apply_next_step():
            number = self.player.get_completed_turns()
            if self.player.turn is not None:
                continue
            before, after = loop_state.load_snapshot(self.workspace.repo_dir, number - 1), loop_state.load_snapshot(self.workspace.repo_dir, number)
            if not after or after["turn"] != number:
                continue
            commits = int(subprocess.run(["git", "-C", self.workspace.repo_dir, "rev-list", "--count", "%s..%s" % (before["head"], after["head"])],
                                         capture_output=True, text=True).stdout.strip())
            errors = loop_state.validate(loop_state.read(os.path.join(before["dir"], "ROADMAP.md")),
                                         loop_state.read(os.path.join(after["dir"], "ROADMAP.md")),
                                         json.loads(loop_state.read(os.path.join(after["dir"], "LOOP.json"))), commits, True)
            self.assertEqual(errors, [], "turn %d" % number)
        loop_json = json.loads(loop_state.read(loop_state.state_file(self.workspace.repo_dir, "LOOP.json")))
        self.assertEqual(loop_json["status"], "done")
        features, _ = loop_state.parse_roadmap(loop_state.read(os.path.join(self.workspace.repo_dir, ".specs", "project", "ROADMAP.md")))
        self.assertEqual({fields["status"] for fields in features.values()}, {"verified"})
        guard_lines = [json.loads(line) for line in loop_state.read(loop_state.loop_path(self.workspace.repo_dir, "guard.log")).splitlines()]
        self.assertEqual([line["rule"] for line in guard_lines], ["project-deny", "path-scope", "git-write"])
        self.assertFalse(os.path.exists(loop_state.loop_path(self.workspace.repo_dir, "LOOP.lock")))

    def test_reset_brings_back_the_seeded_story(self):
        while self.player.apply_next_step():
            pass
        player = self.seeder.reset_story()
        self.assertEqual(player.get_completed_turns(), SEEDED_TURNS)
        turns = sorted(name for name in os.listdir(loop_state.turns_dir(self.workspace.repo_dir)) if name.endswith(".json"))
        self.assertEqual(turns, ["001.json", "002.json"])
        self.assertEqual(len(loop_state.read(loop_state.loop_path(self.workspace.repo_dir, "guard.log")).splitlines()), 1)

    def test_question_waits_for_the_answer_clicked_in_the_panel(self):
        animator = DemoAnimator(self.seeder, self.player, StructuredLog(os.path.join(self.workspace.base_dir, "logs")), speed=1000)
        answer_path = loop_state.loop_path(self.workspace.repo_dir, "demo-answer.txt")
        subprocess.run([sys.executable, ENGINE_STUB_PATH, self.workspace.repo_dir, "--wave", "2", "--answer", "On the payment step"],
                       capture_output=True, text=True, timeout=60, check=True)
        self.assertTrue(os.path.isfile(answer_path))
        self.assertEqual(animator.wait_for_answer({"answer": "default", "hold_seconds": 30}), "On the payment step")
        self.assertFalse(os.path.exists(answer_path))
        self.assertEqual(animator.wait_for_answer({"answer": "default", "hold_seconds": 1}), "default")

    def test_engine_stub_refuses_check_and_cycle_time_is_known(self):
        result = subprocess.run([sys.executable, ENGINE_STUB_PATH, self.workspace.repo_dir, "--check"], capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 1)
        self.assertIn("[check] PROBLEM: demo mode", result.stdout)
        self.assertGreater(compute_story_seconds(build_turn_plans()), compute_story_seconds(build_turn_plans(), speed=2))


if __name__ == "__main__":
    unittest.main()
