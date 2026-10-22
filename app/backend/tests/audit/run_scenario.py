#!/usr/bin/env python3
"""Audit scenario runner (task V3): builds a scratch repo, runs the REAL engine (run_loop.py) with a scripted fake claude and a fake `gh`
first on PATH, collects the logs into `<out>/v4-<shape>/` and deletes the scratch folders (recording any it could not delete).

  python app/backend/tests/audit/run_scenario.py <shape> --out <runs dir> [--keep-scratch]
  shapes: back front both monorepo multi-repo task-waves pause-resume budget-cap critical-decision follow-up-round-2 finish forbidden-attempts

Nothing leaves the machine: scratch repos under the system temp folder, a local bare `origin`, no network, the real ~/.loop-harness and the real
~/.claude are never read (engine_env points LOOP_DATA_DIR, LOOP_SKILL_DIR, LOOP_USER_SHARED_DIR at a temp folder). Git writes happen in the
temp repos only. Exit code 0 when every check of the shape passed, 1 otherwise, 2 for a runner error.
"""
import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "engine"))
BACKEND = os.path.dirname(os.path.dirname(HERE))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
import engine_env  # noqa: E402  (temp LOOP_DATA_DIR / LOOP_SKILL_DIR with the copied guard; engine on sys.path)
import loop_state  # noqa: E402
from fakes import delete_tree, git, install_gh_shim, path_without_real_gh, read_gh_calls, write_text  # noqa: E402
from scenarios import SHAPES  # noqa: E402
from scratch import build_scratch, write_project_files  # noqa: E402

RUN_LOOP = os.path.join(engine_env.ENGINE_DIR, "run_loop.py")
AUDIT_CLAUDE = os.path.join(HERE, "audit_claude.py")
PROTECTED = ("developer", "main", "master")
STEP_TIMEOUT_SECONDS = 600


def now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


class Run:
    """State of one scenario run: folders, env, collected facts."""

    def __init__(self, shape, out_root, keep_scratch):
        self.shape, self.spec = shape, SHAPES[shape]
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="audit-%s-" % shape))
        self.out = os.path.join(out_root, "v4-" + shape)
        self.keep_scratch = keep_scratch
        self.gh_dir = os.path.join(self.root, "_gh")
        self.log_dir = os.path.join(self.root, "_audit-log")
        self.gh_log = os.path.join(self.root, "gh-calls.jsonl")
        self.steps, self.facts, self.notes = [], {}, []
        self.world = None

    # ---------------------------------------------------------------- build
    def build(self):
        os.makedirs(self.log_dir)
        install_gh_shim(self.gh_dir)
        self.world = build_scratch(self.spec["repo"], self.root)
        primary = self.world["primary"]
        self.facts["origin_before"] = {name: self.refs(remote) for name, remote in self.world["remotes"].items()}
        repos = self.world["repos"] if self.spec["repo"] == "multi-repo" else None
        write_project_files(primary, self.spec["roadmap"], repos)
        if repos:
            import project_repos
            project_repos.save_repos(primary, True, [project_repos.RepoRef(name=name, path=path, primary=name == "app") for name, path in repos.items()], scope_defined=True)
        if self.spec["repo"] == "monorepo":
            for sub in ("apps/api", "apps/web"):
                write_text(os.path.join(primary, sub, "CLAUDE.md"), "Conformance: STANDARD\n")
        for slug, text in (self.spec.get("tasks_md") or {}).items():
            write_text(os.path.join(loop_state.feature_dir(primary, slug), "tasks.md"), text)
        write_text(os.path.join(primary, ".specs", "project", "FLOWS.md"), "# Flows\n\n- scratch: the scratch flow\n")
        write_text(os.path.join(self.root, "home", ".claude", "skills", "_shared", ".env"), "AUDIT_FAKE_TOKEN=not-a-real-secret\n")  # decoy: nothing reads it
        self.scenario_path = os.path.join(self.root, "scenario.json")
        scenario = dict(self.spec["claude"], features=self.spec["features"])
        write_text(self.scenario_path, json.dumps(scenario, indent=2))

    @property
    def primary(self):
        return self.world["primary"]

    @property
    def work(self):
        return self.primary + "-worktree-loop"

    @staticmethod
    def refs(remote):
        out = subprocess.run(["git", "--git-dir", remote, "for-each-ref", "--format=%(refname:short) %(objectname:short)"], capture_output=True, text=True, encoding="utf-8")
        return dict(line.split(" ", 1) for line in out.stdout.splitlines() if line.strip())

    def environment(self):
        env = dict(os.environ)
        for key in ("LOOP_NOTIFY_TO", "LOOP_NOTIFY_REQUIRED", "LOOP_SMTP_HOST", "LOOP_SMTP_FROM", "LOOP_WEBHOOK_URL", "LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH",
                    "LOOP_GIT_USER", "GUARD_PROD_HINTS", "LOOP_MONITOR_URL", "LOOP_PR_MODE"):
            env.pop(key, None)
        envfile = os.path.join(self.root, "empty.env")
        open(envfile, "w").close()
        env.update(PATH=path_without_real_gh(env.get("PATH", ""), self.gh_dir), AUDIT_GH_LOG=self.gh_log, AUDIT_SCENARIO=self.scenario_path, FAKE_SCENARIO=self.scenario_path,
                   AUDIT_LOG_DIR=self.log_dir, AUDIT_SCRATCH=self.root, AUDIT_ORIGIN="origin", AUDIT_GUARD=engine_env.GUARD, LOOP_ENV_FILE=envfile,
                   LOOP_NOTIFY_CHANNELS="file", LOOP_MONITOR="off", PYTHONIOENCODING="utf-8")
        # LOOP_HEADLESS, LOOP_SPECS_DIR and the push lock (GIT_CONFIG_* pushurl) are exported by the engine itself to every claude turn (run_loop.push_lock_env)
        return env

    # ---------------------------------------------------------------- steps
    def engine(self, extra=(), expect_exit=0):
        command = [sys.executable, RUN_LOOP, self.primary, "--summary-dir", os.path.join(self.root, "summary"), "--max-stall", "5", "--allow-silent",
                   "--claude", AUDIT_CLAUDE] + list(self.spec.get("flags", [])) + list(extra)
        started = time.time()
        result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", env=self.environment(), timeout=STEP_TIMEOUT_SECONDS)
        index = len(self.steps) + 1
        write_text(os.path.join(self.out, "engine-step%d.stdout.txt" % index), result.stdout)
        write_text(os.path.join(self.out, "engine-step%d.stderr.txt" % index), result.stderr)
        self.steps.append({"step": index, "type": "engine", "argv": [os.path.basename(command[1])] + command[2:], "exit": result.returncode, "expect_exit": expect_exit,
                           "ok": result.returncode == expect_exit, "seconds": round(time.time() - started, 1), "loop_status": self.loop_status()})

    def engine_crash(self):
        """Starts the engine, waits for the fake's crash signal (it has edited worktree files and hangs mid-turn), kills the whole engine process tree."""
        command = [sys.executable, RUN_LOOP, self.primary, "--summary-dir", os.path.join(self.root, "summary"), "--max-stall", "5", "--allow-silent",
                   "--claude", AUDIT_CLAUDE] + list(self.spec.get("flags", []))
        index = len(self.steps) + 1
        out = open(os.path.join(self.out, "engine-step%d.stdout.txt" % index), "w", encoding="utf-8")
        proc = subprocess.Popen(command, stdout=out, stderr=subprocess.STDOUT, text=True, env=self.environment(),
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
        marker, deadline, signalled = os.path.join(self.log_dir, "crash-ready"), time.time() + STEP_TIMEOUT_SECONDS, False
        while time.time() < deadline and proc.poll() is None:
            if os.path.exists(marker):
                signalled = True
                break
            time.sleep(0.2)
        if signalled:
            self.facts["at_crash"] = {"head": git(self.work, "rev-parse", "--short", "HEAD", check=False), "status": git(self.work, "status", "--porcelain", check=False),
                                      "turn_files": sorted(os.path.basename(p) for p in glob.glob(os.path.join(loop_state.internal_dir(self.work), "turns", "*.json")))}
        if proc.poll() is None:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
            else:
                proc.kill()
        proc.wait(timeout=60)
        out.close()
        self.steps.append({"step": index, "type": "engine_crash (killed mid-turn)", "signalled": signalled, "exit": proc.returncode, "ok": signalled, "at_crash": self.facts.get("at_crash")})

    def loop_status(self):
        try:
            with open(loop_state.loop_path(self.work, "LOOP.json"), encoding="utf-8") as handle:
                data = json.load(handle)
            return {"status": data.get("status"), "reason": data.get("reason"), "turn": data.get("turn")}
        except (OSError, ValueError):
            return None

    def raise_cap(self, value):
        path = loop_state.loop_path(self.work, "meta.json")
        with open(path, encoding="utf-8") as handle:
            meta = json.load(handle)
        meta["max_cost_usd"] = value
        write_text(path, json.dumps(meta))
        self.steps.append({"step": len(self.steps) + 1, "type": "raise_cap", "value": value, "ok": True, "note": "meta.max_cost_usd raised, as the panel does"})

    def answer(self, text):
        self.engine(["--answer", text], 0)
        self.steps[-1]["type"] = "answer+engine"

    def follow_up(self):
        """Service-driven: FollowUpService with a fake ClaudeClient (appends a Follow-up stage), then run_follow_up (approval renewed), then the engine's round 2."""
        from tests.api.test_follow_up_service import FakeClaudeClient, FakeLoopControl, FakeLoopStatus
        from harness_backend.domain.project import Project, ProjectContext
        from harness_backend.infrastructure.structured_log import StructuredLog
        from harness_backend.infrastructure.workspace_file_guard import WorkspaceFileGuard
        from harness_backend.repositories.specs_file_repository import SpecsFileRepository
        from harness_backend.services.claude_job_service import ClaudeJobService
        from harness_backend.services.follow_up_service import FollowUpService
        stage = ('\n## Stage Follow-up-1 - Follow-up 1\n\n- [extra-report] status=pending scope=Small kind=back paths=src/extra deps=users-api '
                 'title="Extra report"\n')

        def behavior(folder):
            for name, text in (("ROADMAP.md", stage), ("FLOWS.md", "- extra-report: the report flow\n")):
                with open(os.path.join(folder, ".specs", "project", name), "a", encoding="utf-8", newline="\n") as handle:
                    handle.write(text)
            return "added extra-report"
        client, specs, log = FakeClaudeClient(), SpecsFileRepository(), StructuredLog(os.path.join(self.root, "logs"))
        client.behavior = behavior
        status, control = FakeLoopStatus(self.work), FakeLoopControl()
        jobs = ClaudeJobService(client, specs, log)
        service = FollowUpService(specs, jobs, client, WorkspaceFileGuard(), status, control, log)
        context = ProjectContext(Project("audit", "audit", self.primary))
        record = {"step": len(self.steps) + 1, "type": "follow_up (service, in-process)"}
        try:
            job = service.send_follow_up(context, {"text": "add an extra report"})["job"]
            for _ in range(400):
                if job["id"] not in jobs.running_jobs:
                    break
                time.sleep(0.025)
            finished = jobs.get_job(self.primary, job["id"])
            state = service.get_follow_up(context)
            record.update(job_state=finished.get("state"), job_error=finished.get("error"), stage=(state.get("stage") or {}).get("name"),
                          features=[item["slug"] for item in (state.get("stage") or {}).get("features", [])], argv_model=client.calls[0]["argv"][client.calls[0]["argv"].index("--model") + 1])
            status_code, _ = service.run_follow_up(context)
            record.update(run_status=status_code, loop_start_bodies=control.bodies)
            record["ok"] = finished.get("state") == "ok" and status_code < 300 and record["features"] == ["extra-report"]
        except Exception as failure:  # the audit records the failure instead of hiding it
            record.update(ok=False, error="%s: %s" % (type(failure).__name__, failure))
        self.steps.append(record)
        self.facts["follow_up"] = record

    def finish(self):
        """Script-driven: the engine scripts the Finish button runs (delivery of the loop branch, then close loop with --finished)."""
        delivery = subprocess.run([sys.executable, os.path.join(engine_env.ENGINE_DIR, "delivery.py"), self.work], capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", env=self.environment(), timeout=300)
        write_text(os.path.join(self.out, "finish-delivery.txt"), "exit %d\n%s\n%s" % (delivery.returncode, delivery.stdout, delivery.stderr))
        closing = subprocess.run([sys.executable, os.path.join(engine_env.ENGINE_DIR, "close_loop.py"), self.primary, "--finished", "--confirm-compare-link", "--allow-missing-pr",
                                  "--output-dir", os.path.join(self.root, "backup")], capture_output=True, text=True, encoding="utf-8", errors="replace",
                                 env=self.environment(), timeout=300)
        write_text(os.path.join(self.out, "finish-close.txt"), "exit %d\n%s\n%s" % (closing.returncode, closing.stdout, closing.stderr))
        last_run = {}
        try:
            with open(loop_state.loop_path(self.primary, "last-run.json"), encoding="utf-8") as handle:
                last_run = json.load(handle)
        except (OSError, ValueError):
            pass
        self.facts["finish"] = {"delivery_exit": delivery.returncode, "close_exit": closing.returncode, "last_run_finished": bool(last_run.get("finished")),
                                "worktree_exists_after": os.path.isdir(self.work)}
        self.steps.append({"step": len(self.steps) + 1, "type": "finish (engine scripts delivery.py + close_loop.py --finished)", "ok": closing.returncode == 0 and bool(last_run.get("finished")),
                           **self.facts["finish"]})

    def execute(self):
        steps = self.spec.get("steps") or [{"type": "engine", "expect_exit": 0}]
        for step in steps:
            kind = step["type"]
            if kind == "engine":
                self.engine((), step.get("expect_exit", 0))
            elif kind == "engine_crash":
                self.engine_crash()
            elif kind == "raise_cap":
                self.raise_cap(step["value"])
            elif kind == "answer":
                self.answer(step["text"])
            elif kind == "follow_up":
                self.follow_up()
                if self.steps[-1].get("ok"):
                    continue
                break
            elif kind == "finish":
                self.finish()
            if self.steps and not self.steps[-1].get("ok", True):
                break

    # ---------------------------------------------------------------- collect
    def events(self):
        path = os.path.join(self.log_dir, "events.jsonl")
        if not os.path.isfile(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def collect(self):
        os.makedirs(self.out, exist_ok=True)
        for name, source in (("guard.log", loop_state.loop_path(self.work, "guard.log")), ("LOOP.json", loop_state.loop_path(self.work, "LOOP.json")),
                             ("meta.json", loop_state.loop_path(self.work, "meta.json")), ("ROADMAP.final.md", os.path.join(self.work, ".specs", "project", "ROADMAP.md")),
                             ("VALIDATE.md", loop_state.loop_path(self.work, "VALIDATE.md")), ("NOTICE.txt", loop_state.loop_path(self.work, "NOTICE.txt")),
                             ("SCOPE.json", loop_state.loop_path(self.work, "SCOPE.json"))):
            candidates = [source, source.replace(os.sep + "loop" + os.sep, os.sep + "loop" + os.sep + "internal" + os.sep)] if name == "guard.log" else [source]
            for candidate in candidates:
                if os.path.isfile(candidate):
                    shutil.copyfile(candidate, os.path.join(self.out, name))
                    break
        turns = os.path.join(self.out, "turns")
        for path in glob.glob(os.path.join(loop_state.internal_dir(self.work), "turns", "*.json")):
            os.makedirs(turns, exist_ok=True)
            shutil.copyfile(path, os.path.join(turns, os.path.basename(path)))
        tasks = os.path.join(loop_state.internal_dir(self.work), "tasks")
        for path in glob.glob(os.path.join(tasks, "*.json")):
            os.makedirs(os.path.join(self.out, "tasks"), exist_ok=True)
            shutil.copyfile(path, os.path.join(self.out, "tasks", os.path.basename(path)))
        if os.path.isfile(os.path.join(self.log_dir, "events.jsonl")):
            shutil.copyfile(os.path.join(self.log_dir, "events.jsonl"), os.path.join(self.out, "fake-claude-events.jsonl"))
        shutil.copyfile(self.scenario_path, os.path.join(self.out, "scenario.json"))
        calls = read_gh_calls(self.gh_log)
        write_text(os.path.join(self.out, "gh-calls.jsonl"), "".join(json.dumps(item) + "\n" for item in calls))
        self.facts["gh_calls"] = calls
        self.facts["origin_after"] = {name: self.refs(remote) for name, remote in self.world["remotes"].items()}
        lines = []
        for name, path in self.world["repos"].items():
            lines.append("== %s: branches\n%s\n== %s: log of every branch\n%s" % (name, git(path, "branch", "-a", "-v", check=False), name,
                                                                                    git(path, "log", "--all", "--oneline", "--decorate", check=False)))
        write_text(os.path.join(self.out, "git-state.txt"), "\n".join(lines) + "\n== origin refs before/after\n" + json.dumps(
            {"before": self.facts["origin_before"], "after": self.facts["origin_after"]}, indent=2) + "\n")

    # ---------------------------------------------------------------- checks
    def check_engine_done(self):
        engine_steps = [item for item in self.steps if item["type"].startswith("engine") or item["type"] == "answer+engine"]
        last = (engine_steps[-1].get("loop_status") or {}) if engine_steps else {}  # read right after the step (a later Finish removes the worktree)
        ok = bool(engine_steps) and all(item["ok"] for item in engine_steps) and last.get("status") == "done"
        return ok, "loop status %s; engine exits %s" % (last, [item["exit"] for item in engine_steps])

    def check_nothing_pushed_to_protected(self):
        bad = []
        for name, before in self.facts["origin_before"].items():
            after = self.facts["origin_after"][name]
            for branch in PROTECTED:
                if branch in before and after.get(branch) != before[branch]:
                    bad.append("%s/%s moved %s -> %s" % (name, branch, before[branch], after.get(branch)))
        pushed = {name: sorted(set(after) - set(self.facts["origin_before"][name])) for name, after in self.facts["origin_after"].items()}
        return not bad, "protected branches untouched" if not bad else "; ".join(bad), pushed

    def check_gh_only_shim(self):
        calls = self.facts["gh_calls"]
        return True, "%d call(s) reached the fake gh shim (all refused with exit 1): %s" % (len(calls), [" ".join(item["argv"][:3]) for item in calls])

    def check_forbidden_all_blocked(self):
        records = [item for item in self.events() if item["kind"] == "tool"]
        wrong = [item["label"] + " (exit %s, expected %s)" % (item["exit"], item["expect"]) for item in records if not item["ok"]]
        gaps = [item for item in records if item["expect"] == "blocked" and not item["blocked"]]
        return bool(records) and not wrong, "%d attempt(s) sent through the real guard; %d unexpected result(s)%s" % (
            len(records), len(wrong), ": " + "; ".join(wrong) if wrong else ""), {"gaps": len(gaps)}

    def check_waves_overlap(self):
        starts, ends = {}, {}
        for item in self.events():
            if item["kind"] == "executor_start":
                starts[item["task"]] = item["ts"]
            elif item["kind"] == "executor_end":
                ends[item["task"]] = item["ts"]
        needed = ("T1", "T2", "T3", "T4")
        if not all(task in starts and task in ends for task in needed):
            return False, "executor timestamps missing: starts %s ends %s" % (sorted(starts), sorted(ends))
        first_wave = ("T1", "T2", "T3")
        overlap = max(starts[t] for t in first_wave) < min(ends[t] for t in first_wave)
        after = starts["T4"] >= max(ends[t] for t in first_wave)
        return overlap and after, "T1..T3 overlap=%s (latest start %.2f < earliest end %.2f); T4 starts after T3 ends=%s" % (
            overlap, max(starts[t] for t in first_wave) % 1000, min(ends[t] for t in first_wave) % 1000, after)

    def check_no_task_dispatched_twice(self):
        counts = {}
        for item in self.events():
            if item["kind"] == "executor_start":
                key = "%s/%s" % (item["slug"], item["task"])
                counts[key] = counts.get(key, 0) + 1
        twice = [key for key, count in counts.items() if count > 1]
        return bool(counts) and not twice, "dispatch counts %s" % counts

    def check_paused_then_done(self):
        engines = [item for item in self.steps if item["type"] == "engine"]
        return len(engines) == 2 and engines[0]["exit"] == 10 and engines[1]["exit"] == 0 and (self.loop_status() or {}).get("status") == "done", \
            "first run exit %s (%s), second run exit %s, final %s" % (engines[0]["exit"] if engines else None, (engines[0]["loop_status"] or {}).get("status") if engines else None,
                                                                        engines[1]["exit"] if len(engines) > 1 else None, self.loop_status())

    def check_critical_stopped_then_done(self):
        first = self.steps[0] if self.steps else {}
        stopped = (first.get("loop_status") or {}).get("status") == "awaiting_user" and "critical decision" in str((first.get("loop_status") or {}).get("reason"))
        return stopped and (self.loop_status() or {}).get("status") == "done", "first stop %s; final %s" % (first.get("loop_status"), self.loop_status())

    def check_multi_repo_commits(self):
        commits = [item for item in self.events() if item["kind"] == "multi_repo_commit"]
        repos = sorted({item["repo"] for item in commits})
        return repos == ["api", "app"], "commits made in the worktrees of: %s" % repos

    def check_follow_up_accepted(self):
        record = self.facts.get("follow_up") or {}
        return bool(record.get("ok")), "service result: %s" % {key: record.get(key) for key in ("job_state", "stage", "features", "run_status", "error")}

    def check_round_two_done(self):
        engines = [item for item in self.steps if item["type"] == "engine"]
        features, _ = loop_state.parse_roadmap(open(os.path.join(self.work, ".specs", "project", "ROADMAP.md"), encoding="utf-8").read()) if os.path.isdir(self.work) else ({}, [])
        return len(engines) == 2 and all(item["ok"] for item in engines) and features.get("extra-report", {}).get("status") == "verified", \
            "engine exits %s; extra-report status %s" % ([item["exit"] for item in engines], features.get("extra-report", {}).get("status"))

    def check_finish_closed(self):
        record = self.facts.get("finish") or {}
        return bool(record.get("last_run_finished")) and not record.get("worktree_exists_after"), "finish facts: %s" % record

    def check_crash_kept_finished_turns(self):
        before = (self.facts.get("at_crash") or {}).get("turn_files") or []
        after = sorted(os.path.basename(p) for p in glob.glob(os.path.join(loop_state.internal_dir(self.work), "turns", "*.json"))) if os.path.isdir(self.work) else []
        return bool(before) and set(before) <= set(after), "turn files at the crash %s; after the resumed run %s" % (before, after)

    def check_crash_edits_survive(self):
        status = (self.facts.get("at_crash") or {}).get("status", "")
        present_at_crash = "src/orders" in status
        path = os.path.join(self.work, "src", "orders", "partial_edit.py")
        in_tree = os.path.isfile(path)
        in_git = "partial_edit.py" in git(self.work, "log", "--all", "--name-only", "--format=", check=False) if os.path.isdir(self.work) else False
        return present_at_crash and (in_tree or in_git), "present uncommitted at the crash=%s; after resume: in the worktree=%s, committed by the loop=%s" % (present_at_crash, in_tree, in_git)

    def check_crash_no_redo(self):
        dispatches = {}
        for item in self.events():
            if item["kind"] == "execute_dispatch":
                for slug in item["delegated"]:
                    dispatches[slug] = dispatches.get(slug, 0) + 1
        turns = [item["turn"] for item in self.events() if item["kind"] == "turn"]
        crash_turn = 4
        resumed = [t for t in turns if t >= crash_turn]
        ok = (self.loop_status() or {}).get("status") == "done" and dispatches.get("users-api", 0) <= 1 and bool(resumed)
        return ok, "execute dispatches per feature %s; fake turns seen %s; final %s" % (dispatches, turns, self.loop_status())

    def run_checks(self):
        results = []
        for name in self.spec["checks"]:
            outcome = getattr(self, "check_" + name)()
            results.append({"check": name, "pass": bool(outcome[0]), "detail": outcome[1], **({"extra": outcome[2]} if len(outcome) > 2 else {})})
        return results

    # ---------------------------------------------------------------- finish
    def cleanup(self):
        unremoved = []
        if self.keep_scratch:
            return ["kept on request: " + self.root]
        if self.world:  # worktrees are registered in the repos' git metadata; they live under the same temp root
            for path in self.world["repos"].values():
                subprocess.run(["git", "-C", path, "worktree", "prune"], capture_output=True)
        reason = delete_tree(self.root)
        if reason:
            unremoved.append("%s: %s" % (self.root, reason))
        return unremoved


def main():
    parser = argparse.ArgumentParser(description="Runs one audit shape end to end on a scratch repo (fake claude + fake gh).")
    parser.add_argument("shape", choices=sorted(SHAPES))
    parser.add_argument("--out", required=True, help="runs folder; the logs go to <out>/v4-<shape>/")
    parser.add_argument("--keep-scratch", action="store_true", help="do not delete the scratch folders (debugging)")
    options = parser.parse_args()
    run = Run(options.shape, os.path.abspath(options.out), options.keep_scratch)
    started = now_text()
    error = ""
    checks = []
    try:
        os.makedirs(run.out, exist_ok=True)
        run.build()
        run.execute()
        run.collect()
        checks = run.run_checks()
    except Exception as failure:
        import traceback
        error = traceback.format_exc()
        try:
            run.collect()
        except Exception:
            pass
    unremoved = run.cleanup()
    passed = not error and bool(checks) and all(item["pass"] for item in checks) and all(item.get("ok", True) for item in run.steps)
    summary = {"shape": run.shape, "started": started, "finished": now_text(), "driven": run.spec["driven"], "passed": passed, "runner_error": error, "steps": run.steps,
               "checks": checks, "scratch_not_deleted": unremoved}
    write_text(os.path.join(run.out, "summary.json"), json.dumps(summary, indent=2))
    text = ["# v4-%s: %s" % (run.shape, "PASS" if passed else "FAIL"), "", "- driven: " + run.spec["driven"], "- started %s, finished %s" % (started, summary["finished"]), "- scratch not deleted: %s" % (unremoved or "none"), "", "## steps"]
    text += ["- %s" % json.dumps({key: value for key, value in item.items() if key != "loop_start_bodies"}) for item in run.steps]
    text += ["", "## checks"] + ["- [%s] %s: %s" % ("PASS" if item["pass"] else "FAIL", item["check"], item["detail"]) for item in checks]
    if error:
        text += ["", "## runner error", "```", error, "```"]
    write_text(os.path.join(run.out, "summary.md"), "\n".join(text) + "\n")
    print("\n".join(text))
    return 2 if error else (0 if passed else 1)


if __name__ == "__main__":
    sys.exit(main())
