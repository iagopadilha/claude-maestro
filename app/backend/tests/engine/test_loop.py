#!/usr/bin/env python3
"""Tests of the /orchestrator loop WITHOUT spending tokens: real run_loop.py + simulated claude (fake_claude.py) + temporary git repos.

Run (from app/):  python -m unittest discover -s backend/tests/engine -t backend -v
Nothing leaves the machine: notice channel forced to `file`, empty .env, summary in a temp folder, no remote, no push, no panel.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402  (engine on sys.path, temporary LOOP_DATA_DIR, LOOP_SKILL_DIR = fake plugin with the copied guard)
ENGINE = engine_env.ENGINE_DIR
import loop_state  # noqa: E402
import run_loop  # noqa: E402

RUN_LOOP = os.path.join(ENGINE, "run_loop.py")
FAKE = os.path.join(HERE, "fake_claude.py")
GUARD = engine_env.GUARD
PLUGIN_ROOT = os.path.join(engine_env.REPO_ROOT_DIR, "plugin")


def sh(args, cwd, check=True):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=check)


def loop_file(root, *parts):
    return os.path.join(root, ".specs", "project", "loop", *parts)


class Project:
    """Temporary git repo with a ROADMAP + a simulated claude scenario."""

    def __init__(self, roadmap, scenario, conformance="STANDARD", approve=True):
        self.tmp = tempfile.mkdtemp(prefix="looptest-")
        self.repo = os.path.join(self.tmp, "proj")
        os.makedirs(self.repo)
        sh(["git", "init", "-q", "-b", "main"], self.repo)
        for key, value in (("user.name", "tester"), ("user.email", "t@t.t"), ("commit.gpgsign", "false"), ("core.autocrlf", "false")):
            sh(["git", "config", key, value], self.repo)
        with open(os.path.join(self.repo, "README.md"), "w") as handle:
            handle.write("base\n")
        sh(["git", "add", "README.md"], self.repo)
        sh(["git", "commit", "-q", "-m", "base"], self.repo)
        project = os.path.join(self.repo, ".specs", "project")
        os.makedirs(project)
        with open(os.path.join(project, "ROADMAP.md"), "w", encoding="utf-8") as handle:
            handle.write(roadmap)
        if approve:
            self.approve_roadmap()
        if conformance:
            with open(os.path.join(self.repo, "CLAUDE.md"), "w") as handle:
                handle.write("Conformance: %s\n" % conformance)
        self.scenario = os.path.join(self.tmp, "scenario.json")
        with open(self.scenario, "w") as handle:
            json.dump(scenario, handle)
        self.envfile = os.path.join(self.tmp, "empty.env")
        open(self.envfile, "w").close()
        self.summary = os.path.join(self.tmp, "summary")
        self.work = self.repo + "-worktree-loop"
        self.extra_env = {}

    def approve_roadmap(self, root=None):
        """Writes ROADMAP-APPROVED.json like the panel's "Approve roadmap" button (loop_state.approve_roadmap); the preflight
        requires it. A ROADMAP that is invalid on purpose cannot be approved: it stays unapproved and the test checks the grammar error."""
        try:
            loop_state.approve_roadmap(root or self.repo, "2026-01-01T00:00:00")
        except ValueError:
            pass

    def write_test_environment(self, content="Complete test environment.\n"):
        """TEST-ENVIRONMENT.md that the preflight requires when the ROADMAP has regression=yes or the loop runs with --e2e."""
        with open(os.path.join(self.repo, ".specs", "project", "TEST-ENVIRONMENT.md"), "w", encoding="utf-8") as handle:
            handle.write(content)

    def env(self):
        env = dict(os.environ)
        env.update(LOOP_ENV_FILE=self.envfile, LOOP_NOTIFY_CHANNELS="file", FAKE_SCENARIO=self.scenario, PYTHONIOENCODING="utf-8", LOOP_PR_MODE="link",
                   LOOP_MONITOR="off")  # these tests never start the real panel; test_monitor_launch.py turns it on with a stub
        for key in ("LOOP_NOTIFY_TO", "LOOP_NOTIFY_REQUIRED", "LOOP_SMTP_HOST", "LOOP_SMTP_FROM", "LOOP_WEBHOOK_URL", "LOOP_PR_BASE",
                    "LOOP_INTEGRATION_BRANCH", "LOOP_GIT_USER", "GUARD_PROD_HINTS", "LOOP_MONITOR_URL"):
            env.pop(key, None)
        env.update(self.extra_env)
        return env

    def run(self, *flags, fake=True, silent=True):
        command = [sys.executable, RUN_LOOP, self.repo, "--summary-dir", self.summary, "--max-stall", "5"]
        if silent:
            command += ["--allow-silent"]  # tests do not send email/webhook/toast; the mandatory notice has its own tests
        if fake:
            command += ["--claude", FAKE]
        return subprocess.run(command + list(flags), capture_output=True, text=True, encoding="utf-8", env=self.env(), timeout=300)

    def loop_json(self):
        return json.load(open(loop_file(self.work, "json", "LOOP.json"), encoding="utf-8"))

    def meta(self):
        return json.load(open(loop_file(self.work, "json", "meta.json"), encoding="utf-8"))

    def notice(self):
        path = loop_file(self.work, "NOTICE.txt")
        return open(path, encoding="utf-8").read() if os.path.isfile(path) else ""

    def validate_text(self):
        return open(loop_file(self.work, "VALIDATE.md"), encoding="utf-8").read()

    def answers_text(self):
        return open(loop_file(self.work, "ANSWERS.md"), encoding="utf-8").read()

    def roadmap(self, root=None):
        text = open(os.path.join(root or self.work, ".specs", "project", "ROADMAP.md"), encoding="utf-8").read()
        return loop_state.parse_roadmap(text)[0]

    def snapshots(self):
        base = loop_file(self.work, "internal", "snapshots")
        out = []
        for name in sorted(os.listdir(base)):
            text = open(os.path.join(base, name, "ROADMAP.md"), encoding="utf-8").read()
            out.append((int(name), loop_state.parse_roadmap(text)[0]))
        return out

    def commits(self):
        return sh(["git", "log", "--format=%s", "--no-decorate"], self.work).stdout.splitlines()

    def cleanup(self):
        sh(["git", "worktree", "prune"], self.repo, check=False)
        shutil.rmtree(self.tmp, ignore_errors=True)


class LoopCase(unittest.TestCase):
    def make(self, roadmap, scenario, **kwargs):
        project = Project(roadmap, scenario, **kwargs)
        self.addCleanup(project.cleanup)
        return project

    def done(self, result, code, project=None):
        """Failure message with the script output (for diagnosis)."""
        self.assertEqual(result.returncode, code, "stdout:\n%s\nstderr:\n%s" % (result.stdout[-2500:], result.stderr[-1500:]))


ROAD_BASIC = """# Roadmap
## Stage 1 - Base
- [api] status=pending
- [ui] status=pending
## Stage 2 - Screens
- [screen] status=pending
"""
SCEN_BASIC = {"features": {
    "api": {"paths": "apps/api"},
    "ui": {"paths": "packages/ui"},
    "screen": {"paths": "apps/web", "deps": "api,dir:packages/ui"},
}}


class TestFullFlow(LoopCase):
    def test_happy_path_with_deps_stages_and_wave(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        result = project.run("--wave", "2")
        self.done(result, 0)
        self.assertEqual(project.loop_json()["status"], "done")
        self.assertTrue(all(f["status"] == "verified" for f in project.roadmap().values()))
        feats = [c for c in project.commits() if c.startswith("feat(")]
        self.assertEqual(len(feats), 3, project.commits())
        order = list(reversed(feats))  # oldest to newest
        self.assertEqual(order[-1], "feat(screen): implement", "screen depends on api and ui: it must be last")
        # stage 2 never moves while stage 1 has an open feature
        for turn, features in project.snapshots():
            open_stage1 = any(features[s]["status"] not in ("verified", "blocked") for s in ("api", "ui"))
            if open_stage1:
                self.assertEqual(features["screen"]["status"], "pending", "turn %d: stage 2 spec before stage 1 closed" % turn)

    def test_wave_of_2_runs_api_and_ui_together(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--wave", "2"), 0)
        verified_per_turn = [sum(f["status"] == "verified" for f in features.values()) for _, features in project.snapshots()]
        self.assertIn(2, [b - a for a, b in zip(verified_per_turn, verified_per_turn[1:])], "api and ui should close in the same turn: %s" % verified_per_turn)

    def test_never_pushes_and_loop_branch_is_isolated(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run(), 0)
        self.assertTrue(project.meta()["push"].startswith("not done"), project.meta()["push"])  # push is on by default, but there is no origin remote
        main_log = sh(["git", "log", "--format=%s", "main"], project.repo).stdout.splitlines()
        self.assertEqual(main_log, ["base"], "the user's branch must not receive loop commits")
        self.assertTrue(project.meta()["branch"].startswith("loop/"))

    def test_turn_logs_and_state_live_in_the_loop_folder(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run(), 0)
        self.assertTrue(os.path.isfile(loop_file(project.work, "internal", "turns", "001.json")))
        self.assertEqual(json.load(open(loop_file(project.work, "internal", "turns", "001.json"), encoding="utf-8"))["turn"], 1)
        self.assertTrue(os.path.isfile(loop_file(project.work, "GRAPH.md")))
        self.assertFalse(os.path.exists(os.path.join(project.work, ".specs", "loop-logs")))

    def test_e2e_marks_all_and_only_finishes_in_the_final_phase(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.write_test_environment()
        self.done(project.run("--e2e"), 0)
        features = project.roadmap()
        self.assertTrue(all(f.get("e2e") == "ok" for f in features.values()), features)

    def test_e2e_without_test_environment_refuses_the_preflight(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        result = project.run("--e2e")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("TEST-ENVIRONMENT.md", result.stderr + result.stdout)

    def test_e2e_with_a_fill_in_field_refuses_the_preflight(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.write_test_environment("Token: FILL_IN\n")
        result = project.run("--e2e")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("FILL_IN", result.stderr + result.stdout)

    def test_e2e_without_recording_the_result_in_the_roadmap_does_not_finish(self):
        project = self.make(ROAD_BASIC, dict(SCEN_BASIC, skip_e2e_mark=True))
        project.write_test_environment()
        self.done(project.run("--e2e"), 20)
        self.assertIn("e2e=", project.meta()["reason"])
        self.assertIn("without e2e=", project.meta()["reason"])

    def test_common_permission_denial_on_a_turn_that_advanced_carries_on_and_is_noted_in_validate(self):
        denial = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "python build_evidence.py > evidence/baseline.md"}}
        project = self.make(ROAD_BASIC, dict(SCEN_BASIC, denials=[denial]))
        self.done(project.run(), 0)
        validate = project.validate_text()
        self.assertIn("- [general] turn=1 level=important permission denied (", validate)
        self.assertIn("build_evidence.py", validate)
        self.assertIn("tolerated_denials", project.meta())

    def test_risky_permission_denial_stops_the_loop(self):
        denial = {"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "git push origin main"}}
        project = self.make(ROAD_BASIC, dict(SCEN_BASIC, denials=[denial]))
        self.done(project.run(), 20)
        self.assertIn("risky", project.meta()["reason"])

    def test_mcp_tool_permission_denial_stops_the_loop(self):
        denial = {"tool_name": "mcp__prod-db__write", "tool_use_id": "t1", "tool_input": {"name": "x"}}
        project = self.make(ROAD_BASIC, dict(SCEN_BASIC, denials=[denial]))
        self.done(project.run(), 20)
        self.assertIn("risky", project.meta()["reason"])

    def test_cost_and_summary_are_generated(self):
        project = self.make(ROAD_BASIC, dict(SCEN_BASIC, cost=0.25))
        self.done(project.run(), 0)
        self.assertAlmostEqual(project.meta()["cost_usd"], 0.25 * project.meta()["turns"], places=3)
        self.assertTrue(os.path.isdir(project.summary) and os.listdir(project.summary), "summary in --summary-dir")
        self.assertTrue(all(name.startswith("loop-summary-") for name in os.listdir(project.summary)), os.listdir(project.summary))


class TestEscalation(LoopCase):
    def test_overlapping_folders_serialize(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n- [c] status=pending\n"
        scen = {"features": {"a": {"paths": "lib/core"}, "b": {"paths": "lib/core/x"}, "c": {"paths": "apps/c"}}}
        project = self.make(road, scen)
        self.done(project.run("--wave", "3"), 0)
        verified = [sum(f["status"] == "verified" for f in features.values()) for _, features in project.snapshots()]
        jumps = [b - a for a, b in zip(verified, verified[1:])]
        self.assertEqual(max(jumps), 2, "a and c together (2), b alone afterwards; never 3 at once: %s" % verified)
        self.assertEqual(sum(jumps), 3)

    def test_importers_of_the_same_folder_run_together(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
        scen = {"features": {"a": {"paths": "apps/a", "deps": "dir:packages/ui"}, "b": {"paths": "apps/b", "deps": "dir:packages/ui"}}}
        project = self.make(road, scen)
        self.done(project.run("--wave", "2"), 0)
        verified = [sum(f["status"] == "verified" for f in features.values()) for _, features in project.snapshots()]
        self.assertIn(2, [b - a for a, b in zip(verified, verified[1:])], verified)

    def test_blocked_dependency_stops_and_notifies_the_user(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
        scen = {"features": {"a": {"paths": "apps/a", "exec": "fail"}, "b": {"paths": "apps/b", "deps": "a"}}}
        project = self.make(road, scen)
        self.done(project.run("--no-auto-answer"), 10)
        features = project.roadmap()
        self.assertEqual(features["a"]["status"], "blocked")
        self.assertEqual(features["a"]["tries"], "2")
        self.assertEqual(features["b"]["status"], "approved", "b must never run while its dependency is blocked")
        self.assertIn("Quarantine", project.loop_json()["reason"])
        self.assertEqual([c for c in project.commits() if "(b)" in c], [])

    def test_continue_on_blocked_goes_on_until_nothing_is_left_to_do(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
        scen = {"features": {"a": {"paths": "apps/a", "exec": "fail"}, "b": {"paths": "apps/b", "deps": "a"}}}
        project = self.make(road, scen)
        self.done(project.run("--continue-on-blocked"), 10)
        self.assertIn("stuck by a blocked dependency: b", project.loop_json()["reason"])

    def test_loose_quarantine_does_not_stop_the_loop(self):
        road = "## Stage 1\n- [x] status=pending\n- [y] status=pending\n"
        scen = {"features": {"x": {"paths": "apps/x", "exec": "fail"}, "y": {"paths": "apps/y"}}}
        project = self.make(road, scen)
        self.done(project.run(), 0)  # x blocked, nobody depends on it: y goes on and the loop finishes
        self.assertEqual(project.roadmap()["x"]["status"], "blocked")
        self.assertEqual(project.roadmap()["y"]["status"], "verified")

    def test_two_failures_pause_the_loop_and_notify_then_the_user_resumes(self):
        road = "## Stage 1\n- [x] status=pending\n- [y] status=pending\n"
        scen = {"features": {"x": {"paths": "apps/x", "exec": "fail"}, "y": {"paths": "apps/y"}}}
        project = self.make(road, scen)
        self.done(project.run("--wave", "1", "--pause-on-blocked", "--no-auto-answer"), 10)  # x fails twice, enters quarantine and the loop PAUSES (y has not run yet)
        self.assertEqual(project.roadmap()["x"]["status"], "blocked")
        self.assertEqual(project.roadmap()["y"]["status"], "pending")  # --wave 1 = one lane: y has not even had its spec yet
        loop = project.loop_json()
        self.assertEqual(loop["status"], "awaiting_user")
        self.assertIn("Quarantine after 2 failures: x", loop["reason"])
        self.assertEqual(loop["options"], ["try the feature again", "skip the feature and carry on with the others"])
        notice = project.notice()
        self.assertIn("Aguardando sua decisão", notice)
        self.assertIn("acesse o painel de controle", notice)
        self.done(project.run("--pause-on-blocked", "--answer", "skip x and carry on"), 0)  # the user decides: carry on with y
        self.assertEqual(project.roadmap()["y"]["status"], "verified")
        self.assertEqual(project.roadmap()["x"]["status"], "blocked")

    def test_gate_rejects_twice_goes_to_quarantine(self):
        road = "## Stage 1\n- [m] status=pending\n"
        scen = {"features": {"m": {"paths": "apps/m", "scope": "Medium", "gate": "block"}}}
        project = self.make(road, scen)
        self.done(project.run(), 0)
        self.assertEqual(project.roadmap()["m"]["status"], "blocked")
        self.assertEqual(project.roadmap()["m"]["tries"], "2")


class TestScriptGuards(LoopCase):
    def test_illegal_transition_stops_the_loop_with_error_and_rewind_hint(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
        scen = {"features": {"a": {"paths": "apps/a"}, "b": {"paths": "apps/b"}}, "cheat": {"turn": 2, "type": "illegal"}}
        project = self.make(road, scen)
        self.done(project.run(), 20)
        self.assertIn("illegal transition", project.loop_json()["reason"] + project.meta()["reason"])
        self.assertIn("--rewind", project.meta()["reason"])

    def test_invented_status_in_loop_json_is_refused_with_diagnosis_and_snapshot(self):
        project = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a"}}, "bad_status": True})
        self.done(project.run(), 20)
        reason = project.meta()["reason"]
        self.assertIn("in_progress", reason)  # says what came, not just "invalid"
        self.assertIn("loop_state.py loop", reason)  # and how to write it right
        self.assertTrue(os.path.isfile(loop_file(project.work, "internal", "snapshots", "001", "snapshot.json")))

    def test_forgotten_loop_json_gets_one_correction_turn_in_the_same_session(self):
        project = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a"}}, "forget_loop_json": True})
        project.run()
        self.assertGreaterEqual(project.meta().get("loop_json_corrections", 0), 1)
        self.assertNotIn("missing, stale", project.meta().get("reason", ""))

    def test_loop_json_still_missing_after_the_correction_turn_stops_with_error(self):
        project = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a"}}, "forget_loop_json_always": True})
        self.done(project.run(), 20)
        self.assertIn("missing, stale", project.meta()["reason"])
        self.assertEqual(project.meta().get("loop_json_corrections"), 1)

    def test_loop_command_writes_valid_json_and_refuses_wrong_enum(self):
        tmp = tempfile.mkdtemp(prefix="loopjson-")
        self.addCleanup(shutil.rmtree, tmp, True)
        run = lambda *a: subprocess.run([sys.executable, os.path.join(ENGINE, "loop_state.py"), "loop", tmp, *a], capture_output=True, text=True)
        written = run("--status", "continue", "--turn", "3", "--feature", "a", "--next", "x", "--options", "one|two", "--to-check", "c1")
        self.assertEqual(written.returncode, 0)
        self.assertIn("LOOP.json written: status=continue turn=3", written.stdout)
        data = json.load(open(loop_file(tmp, "json", "LOOP.json"), encoding="utf-8"))
        self.assertEqual((data["status"], data["turn"], data["feature"], data["options"], data["to_check"]), ("continue", 3, "a", ["one", "two"], ["c1"]))
        self.assertRegex(data["at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d$")
        self.assertEqual(run("--status", "in_progress", "--turn", "3").returncode, 2)
        self.assertEqual(run("--status", "awaiting_user", "--turn", "3").returncode, 2)  # no reason
        self.assertEqual(run("--status", "continue", "--turn", "x").returncode, 2)

    def test_verified_without_a_commit_is_refused(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n"
        scen = {"features": {"a": {"paths": "apps/a"}, "b": {"paths": "apps/b"}}, "cheat": {"turn": 3, "type": "no_commit"}}  # --wave 1: turn 1 specs a, turn 2 builds a, turn 3 specs b (which the fake then marks verified without a commit)
        project = self.make(road, scen)
        self.done(project.run("--wave", "1"), 20)
        self.assertIn("verified requires the feature's commit", project.meta()["reason"])

    def test_identical_state_in_two_turns_stops(self):
        project = self.make(ROAD_BASIC, {"idle": True})
        self.done(project.run(), 20)
        self.assertIn("identical state", project.meta()["reason"])

    def test_total_cost_cap(self):
        road = "## Stage 1\n- [a] status=pending\n- [b] status=pending\n- [c] status=pending\n"
        scen = {"features": {s: {"paths": "apps/" + s} for s in "abc"}, "cost": 4.0}
        project = self.make(road, scen)
        self.done(project.run("--wave", "1", "--max-cost", "10"), 10)  # a cap hit is a resumable pause (was an error)
        self.assertIn("cost cap", project.meta()["reason"])

    def test_dirty_tree_outside_specs_stops(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--dry-run"), 0)
        self.done(project.run(), 0)  # creates the worktree and finishes
        # new run with a stray file in the worktree: it must refuse before spending a turn
        shutil.rmtree(loop_file(project.work, "internal", "snapshots"), ignore_errors=True)
        with open(os.path.join(project.work, "stray.txt"), "w") as handle:
            handle.write("x")
        roadmap = os.path.join(project.work, ".specs", "project", "ROADMAP.md")
        text = open(roadmap, encoding="utf-8").read().replace("status=verified", "status=approved")
        open(roadmap, "w", encoding="utf-8").write(text)
        self.done(project.run(), 20)
        self.assertIn("dirty tree", project.meta()["reason"])

    def test_pause_file_stops_before_the_next_turn(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--dry-run"), 0)
        self.done(project.run("--wave", "1", "--max-cost", "0.15"), 10)  # pauses after 2 turns on the cost cap, worktree kept
        pause = loop_file(project.work, "internal", "PAUSE")
        os.makedirs(os.path.dirname(pause), exist_ok=True)
        open(pause, "w").close()
        turns = project.meta()["turns"]
        self.done(project.run(), 10)
        self.assertFalse(os.path.exists(pause))
        self.assertEqual(project.meta()["turns"], turns, "the pause spends no turn")
        self.assertEqual(project.loop_json()["reason"], run_loop.PAUSE_REASON)
        self.assertEqual(project.loop_json()["next"], "resume when the user wants")


class TestHumanInTheLoop(LoopCase):
    def test_question_answer_and_resume(self):
        road = "## Stage 1\n- [a] status=pending\n"
        project = self.make(road, {"features": {"a": {"paths": "apps/a", "ask": True}}})
        self.done(project.run("--no-auto-answer"), 10)
        self.assertEqual(project.loop_json()["status"], "awaiting_user")
        self.assertIn("options", project.loop_json()["reason"])
        self.done(project.run("--answer", "a"), 0)
        answers = project.answers_text()
        self.assertRegex(answers, r"## Answer \S+ \[applied\]")
        self.assertIn("Question: 1. a:", answers)
        self.assertIn("User answer: a", answers)
        self.assertEqual(project.roadmap()["a"]["status"], "verified")

    def test_option_answers_by_option_number(self):
        project = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a", "ask": True}}})
        self.done(project.run("--no-auto-answer"), 10)
        bad = project.run("--option", "9")
        self.assertNotEqual(bad.returncode, 0)
        self.assertIn("invalid", bad.stderr)
        self.done(project.run("--option", "2"), 0)
        self.assertIn("User answer: b", project.answers_text())

    def test_notice_carries_numbered_options_and_short_command(self):
        project = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a", "ask": True}}})
        self.done(project.run("--no-auto-answer"), 10)
        notice = project.notice()
        self.assertIn("Opção 1: a", notice)
        self.assertIn("Opção 2: b", notice)
        self.assertIn("acesse o painel de controle", notice)

    def test_answer_without_a_pending_question_is_refused(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run(), 0)
        result = project.run("--answer", "hi")
        self.assertNotEqual(result.returncode, 0)

    def test_rewind_restores_state_and_git(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run(), 0)
        snapshot_head = json.load(open(loop_file(project.work, "internal", "snapshots", "001", "snapshot.json")))["head"]
        plan = project.run("--rewind", "1", fake=False)
        self.done(plan, 0)
        self.assertIn("nothing was changed", plan.stdout)
        self.done(project.run("--rewind", "1", "--yes", fake=False), 0)
        self.assertEqual(sh(["git", "rev-parse", "HEAD"], project.work).stdout.strip(), snapshot_head)
        statuses = {s: f["status"] for s, f in project.roadmap().items()}
        self.assertNotEqual(set(statuses.values()), {"verified"}, statuses)
        self.done(project.run(), 0)  # carries on from where it went back to
        self.assertTrue(all(f["status"] == "verified" for f in project.roadmap().values()))


class TestPreFlight(LoopCase):
    def test_check_ready_prints_the_app_command(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        result = project.run("--check")
        self.done(result, 0)
        self.assertIn("[check] READY. Command:", result.stdout)
        command = result.stdout.split("[check] READY. Command:", 1)[1].strip().splitlines()[0]
        self.assertIn('"%s" loop "%s"' % (os.path.join(engine_env.APP_ROOT_DIR, "harness.py"), project.repo), command)
        self.assertIn("--wave 3", command)
        self.assertFalse(os.path.exists(project.work), "--check creates nothing")

    def test_check_warns_about_dotnet_project_and_missing_gitignore(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        os.makedirs(os.path.join(project.repo, "src", "App"))
        with open(os.path.join(project.repo, "src", "App", "App.csproj"), "w") as handle:
            handle.write("<Project Sdk=\"Microsoft.NET.Sdk\" />")
        result = project.run("--check")
        self.done(result, 0)
        self.assertIn("[check] WARNING: .NET detected (src/App/App.csproj)", result.stdout)
        self.assertIn("no .gitignore", result.stdout)

    def test_find_dotnet_ignores_bin_and_obj_and_prefers_the_shallowest(self):
        tmp = tempfile.mkdtemp(prefix="dotnet-")
        self.addCleanup(shutil.rmtree, tmp, True)
        for path in ("obj/x/Old.csproj", "src/Deep/Deep.csproj", "Shallow.sln"):
            os.makedirs(os.path.dirname(os.path.join(tmp, path)) or tmp, exist_ok=True)
            open(os.path.join(tmp, path), "w").close()
        self.assertEqual(run_loop.find_dotnet(tmp), "Shallow.sln")
        os.remove(os.path.join(tmp, "Shallow.sln"))
        self.assertEqual(run_loop.find_dotnet(tmp), "src/Deep/Deep.csproj")

    def test_check_without_conformance_is_a_problem(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC, conformance=None)
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("[check] PROBLEM: CLAUDE.md has no `Conformance: STANDARD|LEGACY` line", result.stdout)

    def test_check_roadmap_outside_the_grammar(self):
        project = self.make("## Stage 1\n- [a] status=pending deps=ghost\n", SCEN_BASIC)
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("nonexistent feature", result.stdout)

    def test_check_unapproved_roadmap_is_a_problem(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC, approve=False)
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("roadmap not approved: roadmap not approved in the panel yet", result.stdout)

    def test_regression_yes_without_test_environment_is_a_problem(self):
        project = self.make("## Stage 1\n- [refactor-a] status=pending regression=yes\n", {"features": {"refactor-a": {"paths": "apps/a"}}})
        result = project.run("--check")
        self.done(result, 1)
        self.assertIn("TEST-ENVIRONMENT.md", result.stdout)


class TestAlwaysNotify(LoopCase):
    """Every stop of the script notifies the user: success, error, question, exception and early exit."""

    def test_success_error_and_question_generate_a_notice(self):
        ok = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(ok.run(), 0)
        self.assertIn("concluída", ok.notice())
        err = self.make(ROAD_BASIC, {"idle": True})
        self.done(err.run(), 20)
        self.assertIn("interrompida com erro", err.notice())
        ask = self.make("## Stage 1\n- [a] status=pending\n", {"features": {"a": {"paths": "apps/a", "ask": True}}})
        self.done(ask.run("--no-auto-answer"), 10)
        text = ask.notice()
        self.assertIn("Aguardando sua decisão", text)
        self.assertIn("acesse o painel de controle", text)

    def test_preflight_requires_the_mandatory_channels(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.extra_env["LOOP_NOTIFY_REQUIRED"] = "toast,email,webhook"
        result = project.run("--check", silent=False)  # the test environment only has the `file` channel
        self.done(result, 1)
        self.assertIn("mandatory notice channel", result.stdout)
        for name in ("toast", "email", "webhook"):
            self.assertIn(name, result.stdout)

    def test_no_mandatory_channel_by_default_lets_the_preflight_pass(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--check", silent=False), 0)

    def test_early_exit_also_notifies(self):
        project = self.make(ROAD_BASIC, {"idle": True})
        self.done(project.run(), 20)  # ends in error (identical state), worktree created and LOOP.json != awaiting_user
        result = project.run("--answer", "hi")  # sys.exit("--answer only applies when...") after the preflight
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--answer only applies", project.notice())

    def test_preflight_of_an_already_finished_loop_also_notifies(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run(), 0)
        self.assertNotEqual(project.run().returncode, 0)  # "all N features are already verified/blocked"
        self.assertIn("pre-flight failed", project.notice())

    def test_regression_without_a_usable_test_environment_does_not_start_and_notifies_what_is_missing(self):
        project = self.make("## Stage 1\n- [refactor-a] status=pending regression=yes\n", {"features": {"refactor-a": {"paths": "apps/a"}}})
        project.extra_env["LOOP_NOTIFY_CHANNELS"] = "console"  # no worktree yet: the file channel has nowhere to write
        project.write_test_environment("| Admin | `$NOPE_UNDEFINED_TOKEN` |\n")
        result = project.run()  # a real run, not --check
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(os.path.isdir(project.work), "the loop must not create the worktree or spend a turn")
        for expected in ("did NOT start", "regression=yes on: refactor-a", "$NOPE_UNDEFINED_TOKEN", ".specs/project/.env.test", "~/.loop-harness/.env"):
            self.assertIn(expected, result.stdout)  # the console notice, before the exit message

    def test_e2e_without_test_environment_notifies_that_it_was_the_e2e_flag(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.extra_env["LOOP_NOTIFY_CHANNELS"] = "console"
        result = project.run("--e2e")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the loop was started with --e2e", result.stdout)
        self.assertIn("TEST-ENVIRONMENT.md is missing", result.stdout)

    def test_failing_preflight_does_not_start_but_reports_the_problems(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC, conformance=None)
        result = project.run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("pre-flight failed, the loop did not start. Problems:", result.stderr)
        self.assertIn("Conformance", result.stderr)

    def opts(self, **overrides):
        values = {"wave": 1, "e2e": False, "push": False, "setup": "", "check": False, "dry_run": False, "rewind": None, "continue_on_blocked": False}
        values.update(overrides)
        return type("O", (), values)()

    def test_unhandled_exception_notifies(self):
        from unittest import mock
        sent = []
        with mock.patch.object(run_loop, "main", side_effect=RuntimeError("boom")), \
                mock.patch.object(run_loop, "deliver", side_effect=lambda ctx, work, only=None: sent.append(ctx) or {}), \
                mock.patch.dict(run_loop.ACTIVE, {"repo": os.path.join(tempfile.gettempdir(), "proj-x"), "opts": self.opts(), "notify": True}):
            self.assertEqual(run_loop.entry(), 20)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0]["event"], "error")
        self.assertIn("boom", sent[0]["reason"])

    def test_text_sys_exit_notifies_and_numeric_code_does_not(self):
        from unittest import mock
        sent = []
        patches = (mock.patch.object(run_loop, "deliver", side_effect=lambda ctx, work, only=None: sent.append(ctx) or {}),
                   mock.patch.dict(run_loop.ACTIVE, {"repo": os.path.join(tempfile.gettempdir(), "proj-x"), "opts": self.opts(), "notify": True}))
        with patches[0], patches[1]:
            with mock.patch.object(run_loop, "main", side_effect=SystemExit("worktree failed")):
                with self.assertRaises(SystemExit):
                    run_loop.entry()
            with mock.patch.object(run_loop, "main", side_effect=SystemExit(10)):
                with self.assertRaises(SystemExit):
                    run_loop.entry()
        self.assertEqual([c["reason"] for c in sent], ["worktree failed"])

    def test_check_dry_run_and_rewind_do_not_notify(self):
        from unittest import mock
        sent = []
        with mock.patch.object(run_loop, "deliver", side_effect=lambda ctx, work, only=None: sent.append(ctx) or {}), \
                mock.patch.dict(run_loop.ACTIVE, {"repo": "x", "opts": self.opts(check=True), "notify": False}):
            run_loop.notify_early("anything")
        self.assertEqual(sent, [])

    def test_failed_mandatory_channel_is_retried_once(self):
        from unittest import mock
        calls = []

        def fake_dispatch(ctx, work="", only=None):
            calls.append(only)
            return {"email": "ok: sent", "webhook": "FAILED: no response"} if only is None else {"webhook": "ok: HTTP 200"}

        with mock.patch.object(run_loop.notify, "dispatch", side_effect=fake_dispatch), mock.patch.object(run_loop.time, "sleep"), \
                mock.patch.dict(os.environ, {"LOOP_NOTIFY_REQUIRED": "email,webhook"}):
            results = run_loop.deliver({"event": "error"}, "")
        self.assertEqual(calls, [None, ["webhook"]])
        self.assertTrue(all(v.startswith("ok") for v in results.values()), results)

    def test_mandatory_channel_that_keeps_failing_shouts(self):
        from unittest import mock
        import io
        from contextlib import redirect_stdout
        with mock.patch.object(run_loop.notify, "dispatch", return_value={"webhook": "FAILED: no response"}), mock.patch.object(run_loop.time, "sleep"), \
                mock.patch.dict(os.environ, {"LOOP_NOTIFY_REQUIRED": "webhook"}):
            buffer = io.StringIO()
            with redirect_stdout(buffer):
                run_loop.deliver({"event": "error"}, "")
        self.assertIn("[loop] WARNING: mandatory notice NOT delivered on:", buffer.getvalue())

    def test_no_channel_is_mandatory_by_default_and_the_list_comes_from_the_environment(self):
        from unittest import mock
        empty_env = tempfile.mktemp(prefix="empty-", suffix=".env")
        open(empty_env, "w").close()
        self.addCleanup(os.remove, empty_env)
        with mock.patch.dict(os.environ, {"LOOP_ENV_FILE": empty_env}):
            os.environ.pop("LOOP_NOTIFY_REQUIRED", None)
            self.assertEqual(run_loop.required_channels(), [])
            os.environ["LOOP_NOTIFY_REQUIRED"] = "email, webhook"
            self.assertEqual(run_loop.required_channels(), ["email", "webhook"])


class TestHookPaths(unittest.TestCase):
    """The preflight must notice a guard that cannot be reached from the plugin's hooks/hooks.json."""

    def setUp(self):
        from unittest import mock
        self.root = tempfile.mkdtemp(prefix="plugin-")
        self.addCleanup(shutil.rmtree, self.root, True)
        skill = os.path.join(self.root, "skills", "orchestrator")
        patches = (mock.patch.object(run_loop.app_paths, "locate_skill_dir", return_value=skill),
                   mock.patch.object(run_loop.shutil, "which", return_value="/usr/bin/python"))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def write(self, relative, text):
        path = os.path.join(self.root, *relative.split("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)

    HOOKS = json.dumps(engine_env.HOOKS_JSON)

    def test_missing_hooks_json_is_reported(self):
        self.assertTrue(any("hooks/hooks.json not found" in p for p in run_loop.hook_path_problems()))

    def test_guard_target_that_does_not_exist_is_reported(self):
        self.write("hooks/hooks.json", self.HOOKS)
        self.assertTrue(any("does not exist" in p for p in run_loop.hook_path_problems()), run_loop.hook_path_problems())

    def test_hooks_json_without_a_guard_command_is_reported(self):
        self.write("hooks/hooks.json", json.dumps({"hooks": {}}))
        self.assertTrue(any("registers no command that calls guard.py" in p for p in run_loop.hook_path_problems()))

    def test_valid_layout_has_no_problems(self):
        self.write("hooks/hooks.json", self.HOOKS)
        self.write("skills/orchestrator/hooks/guard.py", "# guard\n")
        self.assertEqual(run_loop.hook_path_problems(), [])

    def test_the_real_plugin_layout_is_valid(self):
        from unittest import mock
        with mock.patch.object(run_loop.app_paths, "locate_skill_dir", return_value=engine_env.PLUGIN_SKILL_DIR):
            self.assertEqual(run_loop.hook_path_problems(), [])

    def test_the_fake_plugin_of_the_tests_is_valid(self):
        from unittest import mock
        with mock.patch.object(run_loop.app_paths, "locate_skill_dir", return_value=os.environ["LOOP_SKILL_DIR"]):
            self.assertEqual(run_loop.hook_path_problems(), [])


class TestStateUnit(unittest.TestCase):
    def feats(self, text):
        return loop_state.parse_roadmap(text)[0]

    def test_validate_deps(self):
        text = ("- [a] status=pending deps=b\n- [b] status=pending deps=c\n- [c] status=building paths=x deps=a,zz isolation=xx\n")
        errors = loop_state.check_fields(self.feats(text))
        joined = "\n".join(errors)
        for needle in ("cycle in deps", "nonexistent feature [zz]", "building but depends on a", "invalid isolation"):
            self.assertIn(needle, joined)

    def test_two_failures_require_blocked(self):
        errors = loop_state.check_fields(self.feats("- [a] status=approved paths=x tries=2\n"))
        self.assertTrue(any("must be blocked" in e for e in errors), errors)
        self.assertFalse(loop_state.check_fields(self.feats("- [a] status=blocked tries=2\n")))
        self.assertFalse(loop_state.check_fields(self.feats("- [a] status=approved paths=x tries=1\n")))

    def test_self_dependency(self):
        self.assertIn("depends on itself", "\n".join(loop_state.check_fields(self.feats("- [a] status=pending deps=a\n"))))

    def test_kind_isolation_and_e2e_values_are_english(self):
        good = "- [a] status=approved paths=x kind=generic isolation=same-tree e2e=not-covered origin=adopted\n"
        self.assertEqual(loop_state.check_fields(self.feats(good)), [])
        self.assertTrue(loop_state.check_fields(self.feats("- [a] status=approved paths=x kind=generico\n")))

    def test_stage_heading_is_english_only(self):
        self.assertEqual(self.feats("## Stage 3 - Name\n- [a] status=pending\n")["a"]["stage"], "3")
        self.assertEqual(self.feats("## Other 3\n- [a] status=pending\n")["a"]["stage"], "")

    def test_dir_edit_x_import(self):
        f = self.feats("- [e] status=approved paths=packages/ui\n- [i] status=approved paths=apps/w deps=dir:packages/ui\n- [j] status=approved paths=apps/z deps=dir:packages/ui\n")
        self.assertTrue(loop_state.conflicts(f, ["e", "i"]))
        self.assertFalse(loop_state.conflicts(f, ["i", "j"]))

    def test_next_stage_and_deps(self):
        text = ("## Stage 1\n- [a] status=approved paths=p1\n- [b] status=approved paths=p2 deps=a\n## Stage 2\n- [c] status=pending\n")
        plan = loop_state.next_work(self.feats(text), 3)
        self.assertEqual((plan["phase"], plan["slugs"], plan["waiting_deps"], plan["stage"]), ("execute", ["a"], ["b"], "1"))
        closed = text.replace("[a] status=approved", "[a] status=verified").replace("[b] status=approved", "[b] status=verified")
        self.assertEqual(loop_state.next_work(self.feats(closed), 3)["phase"], "specs")

    def test_next_dependency_on_a_blocked_feature_of_a_previous_stage(self):
        text = ("## Stage 1\n- [a] status=blocked\n## Stage 2\n- [b] status=approved paths=p deps=a\n")
        plan = loop_state.next_work(self.feats(text), 1)
        self.assertEqual(plan["phase"], "none")
        self.assertEqual(plan["stuck_by_blocked"], ["b"])

    def test_isolation_worktree_runs_alone_when_headless(self):
        text = "## Stage S\n- [a] status=approved paths=p1 isolation=worktree\n- [b] status=approved paths=p2\n"
        self.assertEqual(loop_state.next_work(self.feats(text), 3, headless=True)["slugs"], ["a"])
        self.assertEqual(loop_state.next_work(self.feats(text), 3, headless=False)["slugs"], ["a", "b"])

    def test_regression_runs_alone_in_any_mode(self):
        text = "## Stage S\n- [refactor-a] status=approved paths=p1 regression=yes\n- [b] status=approved paths=p2\n"
        for headless in (True, False):
            self.assertEqual(loop_state.next_work(self.feats(text), 3, headless=headless)["slugs"], ["refactor-a"])

    def test_regression_only_on_refactoring_and_valid_value(self):
        text = "## Stage S\n- [b] status=pending regression=yes\n- [refactor-c] status=pending regression=maybe\n- [refactor-d] status=pending regression=ok\n"
        errors = loop_state.check_fields(self.feats(text))
        self.assertTrue(any("[b]" in e and "only applies to a refactoring feature" in e for e in errors), errors)
        self.assertTrue(any("[refactor-c]" in e and "invalid regression" in e for e in errors), errors)
        self.assertFalse(any("[refactor-d]" in e for e in errors), errors)

    def test_blocked_does_not_count_as_progress(self):
        self.assertEqual(run_loop.RANK["blocked"], 0)
        self.assertGreater(run_loop.RANK["verified"], run_loop.RANK["building"])

    def test_transitions(self):
        self.assertTrue(loop_state.reachable("pending", "verified"))
        self.assertFalse(loop_state.reachable("verified", "pending"))
        self.assertFalse(loop_state.reachable("blocked", "verified", has_answer=True))
        self.assertTrue(loop_state.reachable("blocked", "approved", has_answer=True))
        self.assertFalse(loop_state.reachable("blocked", "approved", has_answer=False))

    def test_loop_path_rule(self):
        work = os.path.join(tempfile.gettempdir(), "w")
        base = os.path.join(work, ".specs", "project", "loop")
        self.assertEqual(loop_state.loop_path(work, "ANSWERS.md"), os.path.join(base, "ANSWERS.md"))
        self.assertEqual(loop_state.loop_path(work, "NOTICE.txt"), os.path.join(base, "NOTICE.txt"))
        self.assertEqual(loop_state.loop_path(work, "SCOPE.json"), os.path.join(base, "json", "SCOPE.json"))
        self.assertEqual(loop_state.loop_path(work, "LOOP.lock"), os.path.join(base, "internal", "LOOP.lock"))
        self.assertEqual(loop_state.turns_dir(work), os.path.join(base, "internal", "turns"))
        self.assertEqual(loop_state.state_file(work, "RULES.md"), os.path.join(work, ".specs", "project", "RULES.md"))

    def test_roadmap_approval_follows_features_and_stages(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        roadmap = os.path.join(tmp, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap))
        open(roadmap, "w", encoding="utf-8").write("## Stage 1\n- [a] status=pending\n")
        self.assertFalse(loop_state.roadmap_approval(tmp)["approved"])
        approval = loop_state.approve_roadmap(tmp, "2026-01-01T00:00:00")
        self.assertEqual(approval, {"approved": True, "reason": None, "at": "2026-01-01T00:00:00", "features": 1})
        saved = json.load(open(loop_file(tmp, "json", "ROADMAP-APPROVED.json"), encoding="utf-8"))
        self.assertEqual(sorted(saved), ["at", "features", "signature"])
        open(roadmap, "w", encoding="utf-8").write("## Stage 1\n- [a] status=verified paths=x\n")
        self.assertTrue(loop_state.roadmap_approval(tmp)["approved"], "status changes do not invalidate the approval")
        open(roadmap, "w", encoding="utf-8").write("## Stage 1\n- [a] status=pending\n- [b] status=pending\n")
        self.assertIn("changed after the approval", loop_state.roadmap_approval(tmp)["reason"])

    def test_an_answer_skips_the_signature_but_not_the_approval(self):
        """--answer/--option: rows may change mid-run (no signature match), but a roadmap NEVER approved is still refused."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        roadmap = os.path.join(tmp, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(roadmap))
        open(roadmap, "w", encoding="utf-8").write("## Stage 1\n- [a] status=pending\n")
        for answering in (False, True):
            problems = run_loop.approval_problems(tmp, answering=answering)
            self.assertEqual(len(problems), 1, answering)
        self.assertIn("never approved", run_loop.approval_problems(tmp, answering=True)[0])
        loop_state.approve_roadmap(tmp, "2026-01-01T00:00:00")
        self.assertEqual(run_loop.approval_problems(tmp), [])
        self.assertEqual(run_loop.approval_problems(tmp, answering=True), [])
        open(roadmap, "w", encoding="utf-8").write("## Stage 1\n- [a] status=pending\n- [b] status=pending\n")
        self.assertIn("changed after the approval", run_loop.approval_problems(tmp)[0])
        self.assertEqual(run_loop.approval_problems(tmp, answering=True), [], "the loop may change rows while it runs")


class TestGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guardtest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(loop_file(self.tmp, "json"))
        with open(loop_file(self.tmp, "json", "SCOPE.json"), "w") as handle:
            json.dump({"features": {"a": ["apps/a"], "b": ["apps/b"]}}, handle)
        with open(os.path.join(self.tmp, "tr-a.jsonl"), "w") as handle:
            handle.write(json.dumps({"msg": "SCOPE_FEATURE: a\nPATHS: apps/a"}))

    def code(self, payload, profile="executor"):
        payload.setdefault("cwd", self.tmp)
        return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True).returncode

    def edit(self, path, **extra):
        return {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.tmp, path)}, **extra}

    def test_identity_limits_to_the_scope_of_its_own_feature(self):
        identity = {"agent_transcript_path": os.path.join(self.tmp, "tr-a.jsonl")}
        self.assertEqual(self.code(self.edit("apps/a/x.ts", **identity)), 0)
        self.assertEqual(self.code(self.edit("apps/b/x.ts", **identity)), 2)

    def test_identity_with_the_real_hook_payload(self):
        # format confirmed in a live rehearsal: the hook brings agent_id and transcript_path (of the session), not agent_transcript_path
        session = os.path.join(self.tmp, "session")
        os.makedirs(os.path.join(session, "subagents"))
        with open(os.path.join(session, "subagents", "agent-abc123.jsonl"), "w") as handle:
            handle.write(json.dumps({"msg": "SCOPE_FEATURE: a"}))
        real = {"agent_id": "abc123", "agent_type": "executor-back", "transcript_path": session + ".jsonl", "hook_event_name": "PreToolUse"}
        self.assertEqual(self.code(self.edit("apps/a/x.ts", **real)), 0)
        self.assertEqual(self.code(self.edit("apps/b/x.ts", **real)), 2)

    def test_without_identity_falls_back_to_the_union_and_logs_once(self):
        self.assertEqual(self.code(self.edit("apps/b/x.ts")), 0)
        self.assertEqual(self.code(self.edit("apps/a/x.ts")), 0)
        self.assertEqual(self.code(self.edit("apps/c/x.ts")), 2)
        log = open(loop_file(self.tmp, "internal", "guard.log"), encoding="utf-8").read()
        self.assertEqual(log.count("scope-identity"), 1)

    def test_hard_rules(self):
        bash = lambda c, p="executor": self.code({"tool_name": "Bash", "tool_input": {"command": c}}, p)
        self.assertEqual(bash("git push"), 2)
        self.assertEqual(bash("git commit -m x"), 2)
        self.assertEqual(bash("git status"), 0)
        self.assertEqual(bash("git commit -am x", "orchestrator"), 2)
        self.assertEqual(bash("git commit -m x", "orchestrator"), 0)
        self.assertEqual(bash("git push", "orchestrator"), 2)
        self.assertEqual(self.code({"tool_name": "mcp__mysql-prod__mysql_query", "tool_input": {"query": "DELETE FROM t"}}), 2)
        self.assertEqual(self.code({"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.tmp, ".specs", "project", "ROADMAP.md")}}), 2)
        self.assertEqual(self.code({"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.tmp, ".specs", "project", "ROADMAP.md")}}, "orchestrator"), 0)

    def test_user_exception_releases_the_rule(self):
        with open(loop_file(self.tmp, "json", "PERMISSIONS.json"), "w") as handle:
            json.dump({"allow": [{"rule": "git-push", "quote": "you may push", "at": "x"}]}, handle)
        self.assertEqual(self.code({"tool_name": "Bash", "tool_input": {"command": "git push"}}), 0)


class TestGuardNewRules(unittest.TestCase):
    """PowerShell, hidden push, hard rules, shell writes, outbound tools, failure alert."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guardnew-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        os.makedirs(os.path.join(self.tmp, ".specs", "project"))
        self.env = dict(os.environ, LOOP_ENV_FILE=os.path.join(self.tmp, "empty.env"), LOOP_NOTIFY_CHANNELS="console")
        for key in ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "GUARD_PROD_HINTS", "LOOP_APP_HOME"):
            self.env.pop(key, None)
        open(self.env["LOOP_ENV_FILE"], "w").close()

    def code(self, tool, tool_input, profile="executor", env=None):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.tmp}
        return subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True, env=env or self.env).returncode

    def bash(self, command, profile="executor", tool="Bash"):
        return self.code(tool, {"command": command}, profile)

    def allow(self, *rules):
        os.makedirs(loop_file(self.tmp, "json"), exist_ok=True)
        with open(loop_file(self.tmp, "json", "PERMISSIONS.json"), "w") as handle:
            json.dump({"allow": [{"rule": rule, "quote": "ok", "at": "x"} for rule in rules]}, handle)

    def test_powershell_is_treated_as_bash(self):
        self.assertEqual(self.bash("git push origin feat/x", tool="PowerShell"), 2)
        self.assertEqual(self.bash("git commit -m x", tool="PowerShell"), 2)
        self.assertEqual(self.bash("npm install", tool="PowerShell"), 2)
        self.assertEqual(self.bash("git status; git push", tool="PowerShell"), 2)
        self.assertEqual(self.bash("git status", tool="PowerShell"), 0)

    def test_push_hidden_in_python_and_subshell(self):
        self.assertEqual(self.bash("python -c \"import subprocess;subprocess.run(['git','push'])\""), 2)
        self.assertEqual(self.bash("echo $(git push origin x)"), 2)
        self.assertEqual(self.bash("git -C ../x push"), 2)

    def test_push_to_a_protected_branch_and_force_are_not_released_even_by_the_user(self):
        self.allow("git-push", "push-protected", "push-force")
        self.assertEqual(self.bash("git push origin feat/orchestrator", "orchestrator"), 0)  # feature branch: the user can release it
        for protected in ("developer", "main", "master", "develop", "release"):
            self.assertEqual(self.bash("git push origin %s" % protected, "orchestrator"), 2, protected)
        self.assertEqual(self.bash("git push origin HEAD:developer", "orchestrator"), 2)
        self.assertEqual(self.bash("git push --force origin feat/x", "orchestrator"), 2)
        self.assertEqual(self.bash("git push origin +feat/x", "orchestrator"), 2)
        self.assertEqual(self.bash("git push origin feat/developer-tools", "orchestrator"), 0)  # a similar name is not the protected branch

    def test_push_without_refspec_while_on_a_protected_branch(self):
        sh(["git", "init", "-q", "-b", "developer"], self.tmp)
        self.allow("git-push")
        self.assertEqual(self.bash("git push", "orchestrator"), 2)

    def test_shell_write_to_state_and_credentials(self):
        self.assertEqual(self.bash("echo x > .specs/project/ROADMAP.md"), 2)
        self.assertEqual(self.bash("echo x | tee .specs/project/STATE.md"), 2)
        self.assertEqual(self.bash("Set-Content -Path .specs/project/loop/json/PERMISSIONS.json -Value x", tool="PowerShell"), 2)
        self.assertEqual(self.bash("sed -i 's/a/b/' .specs/project/loop/VALIDATE.md"), 2)
        self.assertEqual(self.bash("echo x > .specs/project/ROADMAP.md", "orchestrator"), 0)
        self.assertEqual(self.bash("dotnet build 2>&1 > build.log"), 0)
        self.assertEqual(self.bash("ls > /dev/null"), 0)

    def test_a_ticket_is_never_written_not_even_by_the_orchestrator(self):
        for tool in ("mcp__helpdesk__update_ticket", "mcp__helpdesk__create_ticket_comment", "mcp__tracker__edit_issue"):
            self.assertEqual(self.code(tool, {}, "orchestrator"), 2, tool)
        self.assertEqual(self.code("mcp__helpdesk__get_ticket", {}, "orchestrator"), 0)

    def test_only_the_orchestrator_calls_the_delivery(self):
        self.assertEqual(self.bash("python engine/delivery.py ."), 2)
        self.assertEqual(self.bash("python engine/delivery.py .", "spec"), 2)
        self.assertEqual(self.bash("python engine/delivery.py .", "orchestrator"), 0)

    def test_fail_open_logs_and_warns_at_most_once_an_hour(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("guard_mod", GUARD)
        guard = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(guard)
        specs = os.path.join(self.tmp, ".specs")
        old = dict(os.environ)
        os.environ.update(self.env)
        try:
            guard.alert(specs, "executor", "test error")
        finally:
            os.environ.clear()
            os.environ.update(old)
        log = open(loop_file(self.tmp, "internal", "guard.log"), encoding="utf-8").read()
        self.assertIn("guard-failopen", log)
        self.assertTrue(os.path.isfile(loop_file(self.tmp, "internal", ".guard-alert")), "mark of 1 notice per hour")


class TestDelivery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="delivery-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "r")
        self.remote = os.path.join(self.tmp, "origin.git")
        os.makedirs(self.repo)
        sh(["git", "init", "-q", "--bare", self.remote], self.tmp)
        sh(["git", "init", "-q", "-b", "developer"], self.repo)
        for key, value in (("user.name", "tester"), ("user.email", "t@t.t"), ("commit.gpgsign", "false")):
            sh(["git", "config", key, value], self.repo)
        open(os.path.join(self.repo, "a.txt"), "w").write("a")
        sh(["git", "add", "a.txt"], self.repo)
        sh(["git", "commit", "-q", "-m", "base"], self.repo)
        sh(["git", "remote", "add", "origin", self.remote], self.repo)
        sh(["git", "checkout", "-q", "-b", "feat/orch"], self.repo)
        os.makedirs(os.path.join(self.repo, ".specs", "project"))
        self.roadmap = os.path.join(self.repo, ".specs", "project", "ROADMAP.md")
        empty_env = os.path.join(self.tmp, "empty.env")  # isolates from any real .env (LOOP_GIT_USER, channels)
        open(empty_env, "w").close()
        for key, value in (("LOOP_PR_MODE", "link"), ("LOOP_ENV_FILE", empty_env)):
            os.environ[key] = value
            self.addCleanup(os.environ.pop, key, None)
        for key in ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "LOOP_GIT_USER"):
            if key in os.environ:
                saved = os.environ.pop(key)
                self.addCleanup(os.environ.__setitem__, key, saved)

    def set_roadmap(self, text):
        open(self.roadmap, "w", encoding="utf-8").write(text)

    def remote_branches(self):
        return sh(["git", "branch", "--list"], self.remote).stdout

    def test_push_only_at_the_end_and_only_of_the_feature_branch(self):
        import delivery
        self.set_roadmap("## Stage 1\n- [a] status=building paths=x\n")
        still_open = delivery.finalize(self.repo)
        self.assertTrue(still_open["push"].startswith("not done: there are still open features"), still_open)
        self.assertEqual(self.remote_branches().strip(), "")
        self.set_roadmap("## Stage 1\n- [a] status=verified paths=x\n- [b] status=blocked tries=2\n")
        end = delivery.finalize(self.repo)
        self.assertEqual(end["push"], "done: origin/feat/orch", end)
        self.assertIn("feat/orch", self.remote_branches())
        saved = json.load(open(loop_file(self.repo, "json", "delivery.json"), encoding="utf-8"))
        self.assertEqual(saved["push"], "done: origin/feat/orch")

    def test_protected_branch_is_refused(self):
        import delivery
        self.set_roadmap("## Stage 1\n- [a] status=verified paths=x\n")
        sh(["git", "checkout", "-q", "developer"], self.repo)
        result = delivery.finalize(self.repo)
        self.assertTrue(result["push"].startswith("refused"), result)
        self.assertEqual(self.remote_branches().strip(), "")

    def test_integration_branch_is_configurable(self):
        import delivery
        os.environ["LOOP_INTEGRATION_BRANCH"] = "staging"
        self.addCleanup(os.environ.pop, "LOOP_INTEGRATION_BRANCH", None)
        self.assertEqual(delivery.integration_branch(), "staging")
        self.assertIn("staging", delivery.protected_names())
        self.set_roadmap("## Stage 1\n- [a] status=verified paths=x\n")
        sh(["git", "checkout", "-q", "-b", "staging"], self.repo)
        self.assertTrue(delivery.finalize(self.repo)["push"].startswith("refused"))

    def test_compare_link_and_pr_body(self):
        import delivery
        self.assertEqual(delivery.compare_url("git@github.com:example-org/x.git", "developer", "feat/a"),
                         "https://github.com/example-org/x/compare/developer...feat/a?expand=1")
        self.assertEqual(delivery.compare_url("https://github.com/example-org/x", "developer", "f"), "https://github.com/example-org/x/compare/developer...f?expand=1")
        self.assertIsNone(delivery.compare_url("/tmp/origin.git", "developer", "f"))
        self.set_roadmap("## Stage 1\n- [a] status=verified paths=x\n- [b] status=blocked tries=2\n")
        os.makedirs(loop_file(self.repo), exist_ok=True)
        open(loop_file(self.repo, "VALIDATE.md"), "w", encoding="utf-8").write("- [a] different button | chose: default | revert: swap\n")
        body = delivery.pr_body(self.repo, "feat/orch", "developer")
        self.assertIn("For you to validate", body)
        self.assertIn("different button", body)
        self.assertIn("Quarantined (NOT delivered, your call)", body)

    def test_in_headless_mode_the_orchestrator_does_not_deliver(self):
        self.set_roadmap("## Stage 1\n- [a] status=verified paths=x\n")
        done = subprocess.run([sys.executable, os.path.join(ENGINE, "delivery.py"), self.repo], capture_output=True, text=True, env=dict(os.environ, LOOP_HEADLESS="1"))
        self.assertEqual(done.returncode, 2)
        self.assertEqual(self.remote_branches().strip(), "")

    def test_claude_p_environment_cannot_push_even_through_a_gap(self):
        env = run_loop.push_lock_env(self.repo)
        self.assertEqual(env["LOOP_HEADLESS"], "1")
        self.assertTrue(env["LOOP_APP_HOME"])
        self.assertIn("push-disabled-by-loop://use-delivery.py", env.values())
        attempt = subprocess.run(["git", "-C", self.repo, "push", "-u", "origin", "feat/orch"], capture_output=True, text=True, env=env)
        self.assertNotEqual(attempt.returncode, 0)
        self.assertEqual(self.remote_branches().strip(), "")


class TestScale(unittest.TestCase):
    def test_small_repo_uses_one_agent_and_large_uses_seven(self):
        import scale
        self.assertEqual(scale.tier_of({"code": 40}), "small")
        self.assertEqual(scale.tier_of({"code": 600}), "medium")
        self.assertEqual(scale.tier_of({"code": 5000}), "large")
        self.assertEqual([len(g) for g in scale.GROUPS["small"]], [7])
        self.assertEqual(len(scale.GROUPS["medium"]), 3)
        self.assertEqual(len(scale.GROUPS["large"]), 7)
        for tier, groups in scale.GROUPS.items():
            self.assertEqual(sorted(sum(groups, [])), sorted(scale.DOCS), tier)  # every document is covered once, no overlap


class TestPermissionDenial(unittest.TestCase):
    """Functions that decide whether a permission denial stops the loop or only becomes a note in VALIDATE.md."""

    def setUp(self):
        self.env_patch = __import__("unittest.mock", fromlist=["patch"]).patch.dict(os.environ, {})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        os.environ.pop("GUARD_PROD_HINTS", None)
        empty_env = tempfile.mktemp(prefix="empty-", suffix=".env")
        open(empty_env, "w").close()
        self.addCleanup(os.remove, empty_env)
        os.environ["LOOP_ENV_FILE"] = empty_env

    def test_a_common_command_is_not_risky(self):
        denial = {"tool_name": "Bash", "tool_input": {"command": "python build.py > .specs/features/x/regression-baseline.json"}}
        self.assertFalse(run_loop.is_risky_denial(denial))

    def test_push_force_rm_and_destructive_sql_are_risky(self):
        for command in ("git push origin main", "git push --force", "rm -rf /tmp/x", "DELETE FROM users", "psql -c 'DROP TABLE x'"):
            denial = {"tool_name": "Bash", "tool_input": {"command": command}}
            self.assertTrue(run_loop.is_risky_denial(denial), command)

    def test_production_hints_are_risky_and_configurable(self):
        denial = {"tool_name": "Bash", "tool_input": {"command": "curl https://api.example.com/prod/orders"}}
        self.assertTrue(run_loop.is_risky_denial(denial))
        self.assertFalse(run_loop.is_risky_denial({"tool_name": "Bash", "tool_input": {"command": "cat product.txt"}}), "a whole word, not a prefix")
        os.environ["GUARD_PROD_HINTS"] = "live"
        self.assertFalse(run_loop.is_risky_denial(denial))
        self.assertTrue(run_loop.is_risky_denial({"tool_name": "Bash", "tool_input": {"command": "deploy --target live"}}))

    def test_project_deny_rule_is_risky_but_a_read_route_is_not(self):
        work = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, work, True)
        os.makedirs(loop_file(work, "json"))
        with open(loop_file(work, "json", "PERMISSIONS.json"), "w", encoding="utf-8") as handle:
            json.dump({"allow": [], "deny_bash_regex": [r"api/v1/orders/cancel(?![/\w])"]}, handle)
        write = {"tool_name": "Bash", "tool_input": {"command": "curl -X POST http://localhost:5101/api/v1/orders/cancel"}}
        read = {"tool_name": "Bash", "tool_input": {"command": "curl http://localhost:5101/api/v1/orders/cancel/reasons"}}
        self.assertTrue(run_loop.is_risky_denial(write, work))
        self.assertFalse(run_loop.is_risky_denial(read, work))
        self.assertFalse(run_loop.is_risky_denial(write), "without the project, its rule does not apply")

    def test_any_mcp_tool_is_risky(self):
        self.assertTrue(run_loop.is_risky_denial({"tool_name": "mcp__metrics__query_logs", "tool_input": {}}))

    def test_denial_text_does_not_leak_a_secret(self):
        denial = {"tool_name": "Bash", "tool_input": {"command": "curl -H 'X-Admin-Token: abcdefghijklmnopqrstuvwxyz0123456789ABCDEF=' http://localhost"}}
        text = run_loop.denial_text(denial)
        self.assertNotIn("abcdefghijklmnopqrstuvwxyz", text)
        self.assertIn("***", text)

    def test_turn_without_commit_roadmap_change_or_question_did_not_advance(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(os.path.join(tmp, "ROADMAP.md"), "w", encoding="utf-8") as handle:
            handle.write("- [a] status=pending\n")
        prev = {"dir": tmp}
        self.assertEqual(run_loop.denial_progress(prev, tmp, {"status": "continue"}, 0), "")
        self.assertEqual(run_loop.denial_progress(prev, tmp, {"status": "continue"}, 1), "new commit")
        self.assertEqual(run_loop.denial_progress(prev, tmp, {"status": "awaiting_user"}, 0), "stopped asking you")


class TestNothingLeftToDo(unittest.TestCase):
    """The preflight only refuses 'nothing to do' when the user is not answering a question."""

    def opts(self, option=0, answer=""):
        import types
        return types.SimpleNamespace(option=option, answer=answer)

    def test_all_closed_without_an_answer_is_nothing_to_do(self):
        self.assertTrue(run_loop.nothing_left_to_do(5, 5, self.opts()))

    def test_all_closed_with_option_is_not_nothing_to_do(self):
        self.assertFalse(run_loop.nothing_left_to_do(5, 5, self.opts(option=1)))

    def test_all_closed_with_answer_is_not_nothing_to_do(self):
        self.assertFalse(run_loop.nothing_left_to_do(5, 5, self.opts(answer="fix the spec")))

    def test_an_open_feature_is_never_nothing_to_do(self):
        self.assertFalse(run_loop.nothing_left_to_do(4, 5, self.opts()))

    def test_empty_roadmap_does_not_fall_here(self):
        self.assertFalse(run_loop.nothing_left_to_do(0, 0, self.opts()))


class TestAutomaticAnswer(unittest.TestCase):
    """pick_auto_answer decides whether the orchestrator's question needs the user or the script answers it alone."""

    def test_picks_the_recommended_option(self):
        state = {"reason": "Which way?", "options": ["a) widen the scope", "b) (recommended) no new file", "c) skip the feature"]}
        self.assertEqual(run_loop.pick_auto_answer(state)[0:3:2], (2, "recommended"))

    def test_without_recommended_picks_the_first_that_does_not_abandon(self):
        state = {"reason": "How to go on?", "options": ["skip the feature", "fix the spec and try again"]}
        self.assertEqual(run_loop.pick_auto_answer(state), (2, "fix the spec and try again", "first option that does not abandon the feature"))

    def test_default_quarantine_picks_try_again(self):
        state = {"reason": "Quarantine after 2 failures", "options": ["try the feature again", "skip the feature and carry on with the others"]}
        self.assertEqual(run_loop.pick_auto_answer(state)[0], 1)

    def test_spec_fix_question_picks_fixing_the_spec(self):
        state = {"reason": "The API config does not suppress the implicit [Required], so a body with a null property invalidates the model state and changes the baseline (R-02). How to go on?",
                 "options": ["fix the spec: a null property goes back to the baseline (suppress the model state check in those cases) and a new RG case",
                             "accept the feature as blocked and end"]}
        self.assertEqual(run_loop.pick_auto_answer(state)[0], 1)

    def test_only_abandon_options_stop(self):
        state = {"reason": "How to go on?", "options": ["end the loop", "skip the feature"]}
        self.assertIsNone(run_loop.pick_auto_answer(state))

    def test_fewer_than_two_options_stop(self):
        self.assertIsNone(run_loop.pick_auto_answer({"reason": "x", "options": ["only one"]}))
        self.assertIsNone(run_loop.pick_auto_answer({"reason": "x"}))

    def test_authorization_production_and_secret_always_stop(self):
        for question in ("Do you authorize the DEV SSO login?", "May I write to production?", "I need the webhook secret", "Delete the table?", "Should I push the branch?"):
            state = {"reason": question, "options": ["yes", "no"]}
            self.assertIsNone(run_loop.pick_auto_answer(state), question)

    def test_a_scheme_name_with_token_is_not_a_secret_question(self):
        state = {"reason": "The factory needs to know whether the scheme is TrackingToken", "options": ["(recommended) the controller decides", "move it to the Domain"]}
        self.assertEqual(run_loop.pick_auto_answer(state)[0], 1)


class TestAutomaticAnswerInTheLoop(LoopCase):
    """With the automatic answer on (default), a trivial question does not stop the loop."""

    ROAD = "## Stage 1\n- [a] status=pending\n"
    SCEN = {"features": {"a": {"paths": "apps/a", "ask": True}}}

    def test_trivial_question_is_answered_alone_and_noted_in_validate(self):
        project = self.make(self.ROAD, self.SCEN)
        self.done(project.run(), 0)
        validate = project.validate_text()
        self.assertIn("- [a] turn=1 level=important question answered automatically:", validate)
        self.assertIn("| chose: option 1 (first option that does not abandon the feature): a | revert: --answer with another option", validate)
        self.assertIn("[automatic answer from the loop: option 1, first option that does not abandon the feature]", project.answers_text())
        self.assertEqual(project.meta()["auto_answers"], [{"turn": 1, "feature": "a", "option": 1}])

    def test_per_feature_cap_of_zero_stops_and_waits_again(self):
        project = self.make(self.ROAD, self.SCEN)
        self.done(project.run("--auto-answers-per-feature", "0"), 10)
        self.assertEqual(project.loop_json()["status"], "awaiting_user")

    def test_no_auto_answer_turns_it_off(self):
        project = self.make(self.ROAD, self.SCEN)
        self.done(project.run("--no-auto-answer"), 10)

    def test_resume_without_an_answer_answers_what_was_left_waiting(self):
        project = self.make(self.ROAD, self.SCEN)
        self.done(project.run("--no-auto-answer"), 10)
        self.done(project.run(), 0)


class TestGateTiebreak(unittest.TestCase):
    """The script breaks the gate tie: after rejections over wording, it tells the orchestrator to approve the spec and run it (once per feature)."""

    def build(self, roadmap, loop_status="done"):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        specs = os.path.join(tmp, ".specs", "project")
        os.makedirs(specs)
        self.loop_json = loop_state.state_file(tmp, "LOOP.json")
        os.makedirs(os.path.dirname(self.loop_json), exist_ok=True)
        with open(os.path.join(specs, "ROADMAP.md"), "w", encoding="utf-8") as handle:
            handle.write(roadmap)
        with open(self.loop_json, "w", encoding="utf-8") as handle:
            json.dump({"status": loop_status, "turn": 3, "reason": "end"}, handle)
        return tmp, specs

    def opts(self, auto_answer=True, gate_tiebreaks=1):
        import types
        return types.SimpleNamespace(auto_answer=auto_answer, gate_tiebreaks=gate_tiebreaks)

    def test_feature_blocked_at_the_gate_gets_a_tiebreak(self):
        tmp, specs = self.build("## Stage E\n- [a] status=verified\n- [c] status=blocked tries=2 note=gate-5-blocked-RG-35a\n")
        meta, auto_state = {}, {"total": 0, "per_feature": {}}
        self.assertTrue(run_loop.try_gate_tiebreak(tmp, 4, self.opts(), meta, auto_state))
        answers = open(loop_state.state_file(tmp, "ANSWERS.md"), encoding="utf-8").read()
        self.assertIn("[pending]", answers)
        self.assertIn("APPROVE the spec of c", answers)
        self.assertIn("Question: Gate tiebreak: c failed the gate several times on spec wording points.", answers)
        state = json.load(open(self.loop_json, encoding="utf-8"))
        self.assertEqual(state["status"], "awaiting_user")
        self.assertEqual(state["options"], ["approve the spec and run it, remaining findings become notes for the executor", "finish without the feature"])
        validate = open(loop_state.state_file(tmp, "VALIDATE.md"), encoding="utf-8").read()
        self.assertIn("- [c] turn=4 level=important gate tiebreak: spec approved without a new gate after several rejections | chose: run with the remaining findings as notes; build and regression decide | revert: --answer asking to redo the gate", validate)
        self.assertEqual(meta["gate_tiebreaks"], [{"turn": 4, "features": ["c"]}])

    def test_only_once_per_feature(self):
        tmp, specs = self.build("## Stage E\n- [c] status=blocked tries=2 note=gate-2\n")
        meta, auto_state = {}, {"total": 0, "per_feature": {}}
        self.assertTrue(run_loop.try_gate_tiebreak(tmp, 4, self.opts(), meta, auto_state))
        with open(self.loop_json, "w", encoding="utf-8") as handle:
            json.dump({"status": "done", "turn": 5}, handle)
        self.assertFalse(run_loop.try_gate_tiebreak(tmp, 6, self.opts(), meta, auto_state))

    def test_blocked_by_execution_failure_gets_no_tiebreak(self):
        tmp, _ = self.build("## Stage E\n- [c] status=blocked tries=2 note=executor-failed-2x\n")
        self.assertFalse(run_loop.try_gate_tiebreak(tmp, 4, self.opts(), {}, {"total": 0, "per_feature": {}}))

    def test_off_with_no_auto_answer_or_zero(self):
        tmp, _ = self.build("## Stage E\n- [c] status=blocked tries=2 note=gate-3\n")
        self.assertFalse(run_loop.try_gate_tiebreak(tmp, 4, self.opts(auto_answer=False), {}, {"total": 0, "per_feature": {}}))
        self.assertFalse(run_loop.try_gate_tiebreak(tmp, 4, self.opts(gate_tiebreaks=0), {}, {"total": 0, "per_feature": {}}))

    def test_without_a_gate_blocked_feature_does_nothing(self):
        tmp, _ = self.build("## Stage E\n- [a] status=verified\n- [b] status=pending\n")
        self.assertFalse(run_loop.try_gate_tiebreak(tmp, 4, self.opts(), {}, {"total": 0, "per_feature": {}}))


class TestTiebreakAtStart(unittest.TestCase):
    """A loop that ended 'done' with a feature blocked at the gate is not 'nothing to do': the tiebreak runs at start."""

    def test_all_closed_but_a_gate_blocked_feature_makes_a_tiebreak_possible(self):
        import types
        opts = types.SimpleNamespace(option=0, answer="", auto_answer=True, gate_tiebreaks=1)
        self.assertTrue(run_loop.nothing_left_to_do(5, 5, opts, {"status": "done"}))
        self.assertFalse(run_loop.nothing_left_to_do(5, 5, opts, {"status": "done"}, tiebreak_possible=True))


class TestMandatoryStops(unittest.TestCase):
    """The agent decides the simple things alone; access, an important business rule and a discrepancy always go to the user."""

    def test_access_business_rule_and_discrepancy_stop(self):
        for question in ("Which access do I use for the database?", "Change the business rule of the shipping fee?", "There is a discrepancy between the expected and the obtained",
                         "The real data diverge", "Result inconsistent with the baseline"):
            state = {"reason": question, "options": ["(recommended) carry on", "other"]}
            self.assertIsNone(run_loop.pick_auto_answer(state), question)

    def test_a_trivial_fix_question_is_still_answered_alone(self):
        state = {"reason": "Mark 2 checks of RG-35a as not-covered or write the case?", "options": ["(recommended) mark not-covered", "write the case"]}
        self.assertEqual(run_loop.pick_auto_answer(state)[0], 1)

    def test_gate_blocked_by_business_rule_or_access_gets_no_tiebreak(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        specs = os.path.join(tmp, ".specs", "project")
        os.makedirs(specs)
        with open(os.path.join(specs, "ROADMAP.md"), "w", encoding="utf-8") as handle:
            handle.write("## Stage E\n- [a] status=blocked tries=2 note=gate-3-business-rule-shipping\n"
                         "- [b] status=blocked tries=2 note=gate-2-access-to-the-database\n"
                         "- [c] status=blocked tries=2 note=gate-5-control-writes\n")
        self.assertEqual(run_loop.gate_blocked_features(specs), ["c"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
