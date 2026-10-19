#!/usr/bin/env python3
"""Tests of how the panel (the Loop Harness app) is wired into the loop (monitor_proc.py, run_loop.py, notify.py) WITHOUT
starting the real app.

A FAKE app (stub) emulates the contract of `harness.py serve --project <repo>`: writes app.json in LOOP_DATA_DIR (pid, url,
token), answers GET /api/app/health, POST /api/app/projects/open (token required) and GET /p/<id>/api/health, writes
MONITOR.json in the project's work and prints MONITOR_URL=<project url>. The tests inject it by argument (ensure_monitor) or
through the LOOP_MONITOR_SERVER variable (run_loop.py in a subprocess). Each test has its own LOOP_DATA_DIR (a live app does not
leak into another test).
Run:  python -W ignore -m unittest discover -s backend/tests/engine -t backend -v   (from the app root)
"""
import json
import os
import shutil
import sys
import tempfile
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402  (engine on sys.path, temporary LOOP_DATA_DIR, LOOP_SKILL_DIR = fake skill with the copied guard)
import loop_state  # noqa: E402
import monitor_proc  # noqa: E402
import notify  # noqa: E402
import run_loop  # noqa: E402
import test_loop  # noqa: E402  (the module, not the classes: avoids running the loop tests twice)
import fake_app  # noqa: E402  (the fake app: contract of `harness.py serve`)

STUB_APP = fake_app.STUB_APP
NEVER_READY_SERVER = "import time\ntime.sleep(60)\n"
EXITS_AT_ONCE_SERVER = "raise SystemExit(1)\n"
PROJECT_URL = r"^http://127\.0\.0\.1:\d+/p/[0-9a-f]{10}/$"
ASK_ROADMAP = "## Stage 1\n- [a] status=pending\n"
ASK_SCENARIO = {"features": {"a": {"paths": "apps/a", "ask": True}}}


def read_json(path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def kill_pid(pid):
    if monitor_proc.process_is_alive(pid):
        monitor_proc.PROCESSES.kill_tree(pid)
    deadline = time.time() + 5
    while time.time() < deadline and monitor_proc.process_is_alive(pid):
        time.sleep(0.1)


def stop_monitor(work):
    """Stops the fake app that owns this work's MONITOR.json (by pid)."""
    info = monitor_proc.read_monitor_info(work)
    if info:
        kill_pid(info["pid"])


def stop_app():
    """Stops the fake app registered in the app.json of the current LOOP_DATA_DIR."""
    registry = monitor_proc.read_json_file(monitor_proc.app_registry_path())
    if registry and registry.get("pid"):
        kill_pid(registry["pid"])


class StubDirectory:
    """The stub's own folder (outside the test repo, so a live stub does not block the repo cleanup)."""

    def __init__(self, test_case, source=STUB_APP):
        self.folder = tempfile.mkdtemp(prefix="monitorstub-")
        test_case.addCleanup(shutil.rmtree, self.folder, True)
        self.server = os.path.join(self.folder, "harness.py")
        with open(self.server, "w", encoding="utf-8") as handle:
            handle.write(source)
        self.calls_file = os.path.join(self.folder, "calls.txt")
        self.open_calls_file = os.path.join(self.folder, "open.txt")

    @staticmethod
    def count_lines(path):
        if not os.path.isfile(path):
            return 0
        with open(path, encoding="utf-8") as handle:
            return len([line for line in handle.read().splitlines() if line.strip()])

    def launches(self):
        return self.count_lines(self.calls_file)

    def registrations(self):
        """POST /api/app/projects/open received (the initial project of `serve --project` does not count)."""
        return self.count_lines(self.open_calls_file)


class MonitorProject(test_loop.Project):
    """The test_loop Project with the panel ON and pointed at the fake app."""

    def __init__(self, roadmap, scenario, stub_server, **kwargs):
        super().__init__(roadmap, scenario, **kwargs)
        self.stub_server = stub_server
        self.monitor_environment = "on"

    def env(self):
        environment = super().env()
        environment["LOOP_MONITOR"] = self.monitor_environment
        environment["LOOP_MONITOR_SERVER"] = self.stub_server
        return environment

    def loop_file(self, name):
        path = loop_state.loop_path(self.work, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        return path


class MonitorCase(unittest.TestCase):
    def setUp(self):
        """Own LOOP_DATA_DIR per test: one test's app.json is never found by another. The fake app is stopped at the end."""
        previous = os.environ.get("LOOP_DATA_DIR")
        data_dir = tempfile.mkdtemp(prefix="loop-data-")
        os.environ["LOOP_DATA_DIR"] = data_dir
        self.addCleanup(shutil.rmtree, data_dir, True)
        self.addCleanup(lambda: os.environ.pop("LOOP_DATA_DIR", None) if previous is None else os.environ.__setitem__("LOOP_DATA_DIR", previous))
        self.addCleanup(stop_app)  # LIFO: stops the app before restoring the variable and deleting the folder

    def make(self, roadmap=None, scenario=None, server_source=STUB_APP, stub_server=None, **kwargs):
        stub = StubDirectory(self, server_source)
        project = MonitorProject(roadmap or test_loop.ROAD_BASIC, scenario or test_loop.SCEN_BASIC, stub_server or stub.server, **kwargs)
        self.addCleanup(project.cleanup)
        self.addCleanup(stop_monitor, project.work)  # LIFO: the panel stops before the repo cleanup
        return project, stub

    def new_work(self, prefix="monitorwork-"):
        work = tempfile.mkdtemp(prefix=prefix)
        self.addCleanup(shutil.rmtree, work, True)
        self.addCleanup(stop_monitor, work)
        return work

    def done(self, result, code):
        self.assertEqual(result.returncode, code, "stdout:\n%s\nstderr:\n%s" % (result.stdout[-2500:], result.stderr[-1500:]))


class TestMultiProjectApp(MonitorCase):
    def test_live_app_registers_the_project_with_the_token_and_does_not_start_another(self):
        stub = StubDirectory(self)
        first_work, second_work = self.new_work(), self.new_work()
        first = monitor_proc.ensure_monitor(first_work, first_work, server_path=stub.server)
        second = monitor_proc.ensure_monitor(second_work, second_work, server_path=stub.server)
        self.assertRegex(first or "", PROJECT_URL)
        self.assertRegex(second or "", PROJECT_URL)
        self.assertNotEqual(first, second, "each project has its own url in the same app")
        self.assertEqual(first.split("/p/")[0], second.split("/p/")[0], "same app (same port)")
        self.assertEqual(stub.launches(), 1, "one single app for every project")
        self.assertEqual(stub.registrations(), 1, "the second project came in through POST /api/app/projects/open")
        self.assertEqual(monitor_proc.running_monitor_url(second_work), second)

    def test_app_json_with_a_wrong_token_does_not_register_and_starts_another(self):
        stub = StubDirectory(self)
        first_work, second_work = self.new_work(), self.new_work()
        monitor_proc.ensure_monitor(first_work, first_work, server_path=stub.server)
        registry = read_json(monitor_proc.app_registry_path())
        with open(monitor_proc.app_registry_path(), "w", encoding="utf-8") as handle:
            json.dump(dict(registry, token="wrong"), handle)
        self.addCleanup(kill_pid, registry["pid"])
        url = monitor_proc.ensure_monitor(second_work, second_work, server_path=stub.server)
        self.assertRegex(url or "", PROJECT_URL)
        self.assertEqual(stub.launches(), 2)

    def test_app_json_with_a_dead_pid_starts_a_new_app(self):
        stub = StubDirectory(self)
        work = self.new_work()
        os.makedirs(os.path.dirname(monitor_proc.app_registry_path()), exist_ok=True)
        with open(monitor_proc.app_registry_path(), "w", encoding="utf-8") as handle:
            json.dump({"pid": 999999, "url": "http://127.0.0.1:1/", "token": "x"}, handle)
        url = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        self.assertRegex(url or "", PROJECT_URL)
        self.assertEqual(stub.launches(), 1)
        self.assertEqual(stub.registrations(), 0)

    def test_url_comes_from_monitor_url_in_the_log_when_the_app_does_not_write_monitor_json(self):
        stub = StubDirectory(self)
        work = self.new_work()
        os.environ["STUB_NO_MONITOR_JSON"] = "1"
        self.addCleanup(os.environ.pop, "STUB_NO_MONITOR_JSON", None)
        url = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        self.assertRegex(url or "", PROJECT_URL)
        self.assertFalse(os.path.exists(monitor_proc.monitor_file(work)))
        with open(loop_state.loop_path(work, "monitor.log"), encoding="utf-8") as handle:
            self.assertIn("MONITOR_URL=" + url, handle.read())

    def test_start_command_is_harness_serve_with_the_repo(self):
        stub = StubDirectory(self)
        work = self.new_work()
        monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        with open(stub.calls_file, encoding="utf-8") as handle:
            call = handle.read().splitlines()[0]
        self.assertTrue(call.startswith("serve --project "), call)
        self.assertIn(os.path.abspath(work), call)

    def test_without_override_the_default_script_is_harness_py_at_the_app_root(self):
        previous = os.environ.pop(monitor_proc.SERVER_OVERRIDE_VARIABLE, None)
        if previous is not None:
            self.addCleanup(os.environ.__setitem__, monitor_proc.SERVER_OVERRIDE_VARIABLE, previous)
        self.assertEqual(monitor_proc.default_server_path(), os.path.join(engine_env.APP_ROOT_DIR, "harness.py"))

    def test_local_endpoint_keeps_the_project_path(self):
        self.assertEqual(monitor_proc.local_endpoint("http://127.0.0.1:5/p/abc/", "api/health"), "http://127.0.0.1:5/p/abc/api/health")
        self.assertEqual(monitor_proc.local_endpoint("http://127.0.0.1:5", "api/app/health"), "http://127.0.0.1:5/api/app/health")
        self.assertIsNone(monitor_proc.local_endpoint("http://example.com/", "api/health"))


class TestStartsAndReuses(MonitorCase):
    def test_by_default_starts_the_panel_prints_the_url_keeps_it_in_meta_and_it_outlives_the_loop(self):
        project, stub = self.make()
        result = project.run()
        self.done(result, 0)
        url = project.meta()["monitor_url"]
        self.assertRegex(url, PROJECT_URL)
        self.assertIn("[loop] monitor: " + url, result.stdout)
        self.assertEqual(stub.launches(), 1)
        self.assertEqual(monitor_proc.running_monitor_url(project.work), url, "the loop ended and the panel is still up")
        with open(project.loop_file("NOTICE.txt"), encoding="utf-8") as handle:
            self.assertIn("Panel: " + url, handle.read())

    def test_second_run_reuses_the_live_panel(self):
        project, stub = self.make(roadmap=ASK_ROADMAP, scenario=ASK_SCENARIO)
        self.done(project.run("--no-auto-answer"), 10)
        url = project.meta()["monitor_url"]
        self.done(project.run("--answer", "a"), 0)
        self.assertEqual(project.meta()["monitor_url"], url)
        self.assertEqual(stub.launches(), 1, "one single server for the same work")

    def test_ensure_monitor_reuses_a_live_panel_with_the_same_work(self):
        stub = StubDirectory(self)
        work = self.new_work()
        first = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        second = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        self.assertTrue(first and first == second, (first, second))
        self.assertEqual(stub.launches(), 1)

    def test_stale_monitor_json_with_a_dead_pid_is_not_reused(self):
        stub = StubDirectory(self)
        work = self.new_work()
        os.makedirs(os.path.dirname(monitor_proc.monitor_file(work)))
        with open(monitor_proc.monitor_file(work), "w", encoding="utf-8") as handle:
            json.dump({"pid": 999999, "port": 1, "url": "http://127.0.0.1:1/", "repo": work, "work": work}, handle)
        url = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        self.assertTrue(url and url != "http://127.0.0.1:1/", url)
        self.assertEqual(stub.launches(), 1)

    def test_live_panel_of_another_work_is_not_reused(self):
        stub = StubDirectory(self)
        work = self.new_work()
        other = self.new_work("monitorother-")
        monitor_proc.ensure_monitor(other, other, server_path=stub.server)
        info = monitor_proc.read_monitor_info(other)
        os.makedirs(os.path.dirname(monitor_proc.monitor_file(work)), exist_ok=True)
        shutil.copy(monitor_proc.monitor_file(other), monitor_proc.monitor_file(work))  # points the work at the other one's server
        self.assertIsNone(monitor_proc.running_monitor_url(work), "health answers with another work: no use")
        self.assertTrue(info)


class TestPanelFromTheEnvironment(MonitorCase):
    def with_url(self, url):
        previous = os.environ.get(monitor_proc.MONITOR_URL_VARIABLE)
        os.environ[monitor_proc.MONITOR_URL_VARIABLE] = url
        self.addCleanup(lambda: os.environ.pop(monitor_proc.MONITOR_URL_VARIABLE, None) if previous is None
                        else os.environ.__setitem__(monitor_proc.MONITOR_URL_VARIABLE, previous))

    def test_loop_monitor_url_with_a_live_health_is_reused_without_starting_a_process(self):
        live = StubDirectory(self)
        other_work = self.new_work("monitorother-")
        work = tempfile.mkdtemp(prefix="monitorwork-")
        self.addCleanup(shutil.rmtree, work, True)
        url = monitor_proc.ensure_monitor(other_work, other_work, server_path=live.server)
        self.assertTrue(url)
        self.with_url(url)
        spare = StubDirectory(self)
        self.assertEqual(monitor_proc.ensure_monitor(work, work, server_path=spare.server), url)
        self.assertEqual(spare.launches(), 0, "does not start another server")
        self.assertFalse(os.path.exists(monitor_proc.monitor_file(work)))

    def test_loop_monitor_url_with_a_dead_health_falls_back_to_the_normal_behavior(self):
        self.with_url("http://127.0.0.1:1/")
        stub = StubDirectory(self)
        work = self.new_work()
        url = monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        self.assertTrue(url and url != "http://127.0.0.1:1/", url)
        self.assertEqual(stub.launches(), 1)

    def test_loop_monitor_url_outside_localhost_is_ignored(self):
        self.assertFalse(monitor_proc.local_url_answers_health("http://example.com/"))


class TestBrowserOpening(MonitorCase):
    def test_by_default_starts_without_no_browser_and_reusing_does_not_start_again(self):
        stub = StubDirectory(self)
        work = self.new_work()
        monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        monitor_proc.ensure_monitor(work, work, server_path=stub.server)
        with open(stub.calls_file, encoding="utf-8") as handle:
            calls = handle.read().splitlines()
        self.assertEqual(len(calls), 1, "reusing does not relaunch the server (and so does not reopen the browser)")
        self.assertNotIn("--no-browser", calls[0])

    def test_open_browser_false_starts_with_no_browser(self):
        stub = StubDirectory(self)
        work = self.new_work()
        monitor_proc.ensure_monitor(work, work, server_path=stub.server, open_browser=False)
        with open(stub.calls_file, encoding="utf-8") as handle:
            self.assertIn("--no-browser", handle.read())

    def test_loop_monitor_open_off_through_run_loop_starts_with_no_browser(self):
        project, stub = self.make()
        project.monitor_environment = "on"
        original = project.env
        project.env = lambda: dict(original(), LOOP_MONITOR_OPEN="off")
        self.done(project.run(), 0)
        with open(stub.calls_file, encoding="utf-8") as handle:
            self.assertIn("--no-browser", handle.read())

    def test_values_that_turn_the_opening_off(self):
        self.assertTrue(monitor_proc.browser_open_disabled("OFF"))
        self.assertFalse(monitor_proc.browser_open_disabled(""))
        self.assertFalse(monitor_proc.browser_open_disabled("on"))


class TestTurnedOff(MonitorCase):
    def test_no_monitor_starts_nothing_but_writes_the_command(self):
        project, stub = self.make()
        result = project.run("--no-monitor")
        self.done(result, 0)
        self.assertEqual(stub.launches(), 0)
        self.assertFalse(os.path.exists(project.loop_file("MONITOR.json")))
        self.assertNotIn("monitor_url", project.meta())
        self.assertNotIn("[loop] monitor:", result.stdout)
        self.assertTrue(os.path.isfile(project.loop_file("LOOP-CMD.json")))

    def test_loop_monitor_off_in_the_environment_also_turns_it_off(self):
        project, stub = self.make()
        project.monitor_environment = "off"
        self.done(project.run(), 0)
        self.assertEqual(stub.launches(), 0)
        self.assertNotIn("monitor_url", project.meta())

    def test_check_dry_run_and_rewind_do_not_start_the_panel(self):
        project, stub = self.make()
        self.done(project.run("--check"), 0)
        self.done(project.run("--dry-run"), 0)
        self.assertEqual(stub.launches(), 0)
        self.done(project.run(), 0)  # only the real run starts it
        launches = stub.launches()
        stop_monitor(project.work)
        self.done(project.run("--rewind", "1", fake=False), 0)
        self.assertEqual(stub.launches(), launches)

    def test_check_only_mentions_no_monitor_when_the_user_passed_it(self):
        project, _ = self.make()
        self.assertNotIn("--no-monitor", project.run("--check").stdout)
        self.assertIn("--no-monitor", project.run("--check", "--no-monitor").stdout)

    def test_check_prints_the_ready_command_through_the_app_cli(self):
        project, _ = self.make()
        result = project.run("--check")
        self.done(result, 0)
        self.assertIn("[check] READY. Command:", result.stdout)
        self.assertIn('harness.py" loop "%s"' % project.repo, result.stdout)

    def test_command_flags_only_mention_no_monitor_when_passed(self):
        opts = types.SimpleNamespace(wave=1, e2e=False, push=True, continue_on_blocked=False, setup="", no_monitor=False)
        self.assertNotIn("--no-monitor", run_loop.command_flags(opts))
        opts.no_monitor = True
        self.assertIn("--no-monitor", run_loop.command_flags(opts))
        del opts.no_monitor  # old option objects (without the attribute) keep working
        self.assertNotIn("--no-monitor", run_loop.command_flags(opts))


class TestFailureDoesNotBreakTheLoop(MonitorCase):
    def test_missing_server_the_loop_finishes_and_warns(self):
        project, _ = self.make(stub_server=os.path.join(tempfile.gettempdir(), "does-not-exist-harness.py"))
        result = project.run()
        self.done(result, 0)
        self.assertIn("panel unavailable", result.stdout)
        self.assertNotIn("monitor_url", project.meta())
        self.assertTrue(all(f["status"] == "verified" for f in project.roadmap().values()))

    def test_server_that_dies_at_once_returns_none(self):
        stub = StubDirectory(self, EXITS_AT_ONCE_SERVER)
        work = self.new_work()
        self.assertIsNone(monitor_proc.ensure_monitor(work, work, server_path=stub.server, wait_seconds=5))

    def test_server_that_never_gets_ready_times_out_and_returns_none(self):
        stub = StubDirectory(self, NEVER_READY_SERVER)
        work = self.new_work()
        started = time.time()
        self.assertIsNone(monitor_proc.ensure_monitor(work, work, server_path=stub.server, wait_seconds=1.0))
        self.assertLess(time.time() - started, 6)

    def test_missing_interpreter_and_any_exception_return_none(self):
        stub = StubDirectory(self)
        work = self.new_work()
        self.assertIsNone(monitor_proc.ensure_monitor(work, work, server_path=stub.server, python="interpreter-that-does-not-exist-xyz"))


class TestRelaunchCommand(MonitorCase):
    def test_loop_cmd_writes_argv_and_cwd_without_answer_or_option(self):
        project, _ = self.make(roadmap=ASK_ROADMAP, scenario=ASK_SCENARIO)
        self.done(project.run("--no-auto-answer", "--wave", "1"), 10)
        self.done(project.run("--wave", "1", "--answer", "a"), 0)
        command = read_json(project.loop_file("LOOP-CMD.json"))
        self.assertNotIn("--answer", command["argv"])
        self.assertNotIn("--option", command["argv"])
        self.assertNotIn("a", command["argv"][3:], "the answer text does not stay either")
        self.assertIn("--wave", command["argv"])
        self.assertIn(project.repo, command["argv"])
        self.assertTrue(command["argv"][1].endswith("run_loop.py"))
        self.assertEqual(command["cwd"], os.getcwd())

    def test_without_answer_flags_covers_the_argparse_forms(self):
        argv = ["run_loop.py", "repo", "--answer", "free text", "--wave", "2", "--option=3", "--option", "1", "--ans=x", "--no-push"]
        self.assertEqual(monitor_proc.without_answer_flags(argv), ["run_loop.py", "repo", "--wave", "2", "--no-push"])

    def test_write_loop_command_never_raises(self):
        folder = tempfile.mkdtemp(prefix="loopcmd-")
        self.addCleanup(shutil.rmtree, folder, True)
        blocker = os.path.join(folder, ".specs")
        open(blocker, "w").close()  # a file where a folder should be: writing fails
        self.assertIsNone(monitor_proc.write_loop_command(folder, ["python", "x"], folder))


class TestPause(MonitorCase):
    def prepared_worktree(self, project):
        """Creates the loop worktree as run_loop would, to put the PAUSE before the first turn."""
        opts = types.SimpleNamespace(branch="loop/test-pause", dry_run=False, setup="")
        run_loop.ensure_worktree(project.repo, opts)

    def test_pause_ends_at_awaiting_user_without_running_claude(self):
        project, _ = self.make()
        self.prepared_worktree(project)
        pause = project.loop_file("PAUSE")
        open(pause, "w").close()
        result = project.run("--no-monitor")
        self.done(result, 10)
        self.assertFalse(os.path.exists(pause), "the loop consumes (deletes) the PAUSE")
        self.assertEqual(project.loop_json()["status"], "awaiting_user")
        self.assertIn("paused by the user in the panel", project.meta()["reason"])
        self.assertIn("Resume", project.meta()["reason"])
        self.assertEqual(project.meta()["turns"], 0, "no turn spent")
        turns = loop_state.turns_dir(project.work)
        self.assertFalse(os.path.isdir(turns) and os.listdir(turns), "claude did not run")
        self.assertTrue(all(f["status"] == "pending" for f in project.roadmap().values()))

    def test_after_the_pause_running_again_carries_on_normally(self):
        project, _ = self.make()
        self.prepared_worktree(project)
        open(project.loop_file("PAUSE"), "w").close()
        self.done(project.run("--no-monitor"), 10)
        self.done(project.run("--no-monitor"), 0)
        self.assertTrue(all(f["status"] == "verified" for f in project.roadmap().values()))

    def test_pause_requested_without_the_file_does_nothing_and_with_it_deletes_it_and_rewrites_loop_json(self):
        project, _ = self.make()
        self.prepared_worktree(project)
        self.assertEqual(run_loop.pause_requested(project.work, 4), "")
        self.assertFalse(os.path.exists(project.loop_file("LOOP.json")))
        open(project.loop_file("PAUSE"), "w").close()
        reason = run_loop.pause_requested(project.work, 4)
        self.assertIn("paused by the user in the panel", reason)
        self.assertFalse(os.path.exists(project.loop_file("PAUSE")))
        state = project.loop_json()
        self.assertEqual((state["status"], state["turn"]), ("awaiting_user", 4))


class TestNoticeWithPanel(MonitorCase):
    URL = "http://127.0.0.1:47801/"

    def context(self, **extra):
        base = {"event": "awaiting_user", "project": "proj", "branch": "loop/x", "turn": 3, "cost": 1.5, "reason": "Which way to go?",
                "options": ["a (recommended)", "b"], "script": "C:\\run_loop.py", "repo": "C:\\proj", "flags": " --wave 1",
                "worktree": "C:\\proj-worktree-loop", "features": [("a", "verified"), ("b", "pending")], "commits": [], "to_check": [],
                "monitor_url": self.URL}
        base.update(extra)
        return base

    def setUp(self):
        super().setUp()
        previous = os.environ.pop("LOOP_NOTIFY_CONTINUE", None)
        self.addCleanup(lambda: os.environ.pop("LOOP_NOTIFY_CONTINUE", None) if previous is None else os.environ.__setitem__("LOOP_NOTIFY_CONTINUE", previous))

    def test_url_shows_in_every_channel_and_is_the_way_to_continue(self):
        context = self.context()
        _, full = notify.build(context)
        _, html_body = notify.build_html(context)
        _, short = notify.build_short(context)
        for name, body in (("text", full), ("html", html_body), ("short", short)):
            self.assertIn(self.URL, body, name)
        instruction = "Open the panel to continue (answer, resume or approve there): "
        self.assertIn(instruction + self.URL, full)
        self.assertIn(instruction, html_body)
        self.assertIn(instruction + self.URL, short)

    def test_default_notice_gives_the_reason_and_no_resume_command(self):
        context = self.context()
        for name, body in (("text", notify.build(context)[1]), ("html", notify.build_html(context)[1]), ("short", notify.build_short(context)[1])):
            self.assertIn("Which way to go?", body, name)
            self.assertNotIn("--answer", body, name)
            self.assertNotIn("--option", body, name)
            self.assertNotIn("run_loop.py", body, name)

    def test_a_test_notice_or_a_finish_has_no_how_to_continue(self):
        for event in ("test", "done"):
            for body in (notify.build(self.context(event=event))[1], notify.build_short(self.context(event=event))[1]):
                self.assertNotIn("HOW TO CONTINUE", body, event)

    def test_commands_mode_brings_back_the_long_form(self):
        os.environ["LOOP_NOTIFY_CONTINUE"] = "commands"
        context = self.context()
        full = notify.build(context)[1]
        self.assertIn("3) Answer in the panel: " + self.URL, full)
        self.assertIn("--option 1", full)
        self.assertIn("--answer", notify.build_html(context)[1])
        self.assertIn("answer at " + self.URL, notify.build_short(context)[1])

    def test_url_also_shows_on_done_and_error(self):
        for event in ("done", "error"):
            context = self.context(event=event, reason="all good or not")
            self.assertIn(self.URL, notify.build(context)[1], event)
            self.assertIn(self.URL, notify.build_html(context)[1], event)
            self.assertIn(self.URL, notify.build_short(context)[1], event)

    def test_short_text_respects_the_limit_even_with_huge_reason_and_options(self):
        context = self.context(reason="question " * 400, options=["very long option " * 20] * 6, to_check=["check " * 30] * 5,
                               graph_short="X" * 400, push="y" * 300, pr="https://github.com/example-org/proj/pull/1234")
        for event in ("awaiting_user", "error", "done"):
            body = notify.build_short(dict(context, event=event))[1]
            self.assertLessEqual(len(body), notify.MAX_SHORT, event)
            self.assertIn(self.URL, body, event)

    def test_standalone_chat_notice_does_not_change(self):
        context = {"event": "custom", "subject": "hi", "project": "", "extra_body": "text", "monitor_url": self.URL}
        for body in (notify.build(context)[1], notify.build_html(context)[1], notify.build_short(context)[1]):
            self.assertNotIn(self.URL, body)
            self.assertNotIn("3) Answer in the panel", body)

    def test_without_the_panel_up_the_message_says_how_to_open_it(self):
        context = self.context(monitor_url=None)
        for body in (notify.build(context)[1], notify.build_html(context)[1], notify.build_short(context)[1]):
            self.assertIn("Open the panel to continue", body)
            self.assertIn("serve --project", body)
            self.assertNotIn(self.URL, body)

    def test_build_context_brings_monitor_url_from_meta(self):
        project, _ = self.make()
        self.done(project.run(), 0)
        meta = project.meta()
        opts = types.SimpleNamespace(wave=1, e2e=False, push=True, push_explicit=False, continue_on_blocked=False, setup="", no_monitor=False)
        context = run_loop.build_context(project.work, meta, "done", "ok", opts, project.repo, "")
        self.assertEqual(context["monitor_url"], meta["monitor_url"])
        self.assertTrue(context["monitor_url"])
        self.assertIsNone(run_loop.build_context(project.work, dict(meta, monitor_url=None), "done", "ok", opts, project.repo, "")["monitor_url"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
