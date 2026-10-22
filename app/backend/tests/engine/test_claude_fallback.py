#!/usr/bin/env python3
"""Fallback `claude` command (engine/claude_binaries.py): the order, the prompt through stdin for a .cmd wrapper such as `cc`,
and the loop switching to the next command when the running one hits the account usage limit.

Run (from app/):  python -m unittest backend.tests.engine.test_claude_fallback -v
"""
import argparse
import datetime
import json
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401  (engine on sys.path, temporary LOOP_DATA_DIR)
import claude_binaries  # noqa: E402
import loop_state  # noqa: E402
import run_loop  # noqa: E402
from test_loop import FAKE, ROAD_BASIC, SCEN_BASIC, LoopCase  # noqa: E402

LIMITED = os.path.join(HERE, "fake_claude_limited.py")
PICKER = os.path.join(HERE, "fake_cc_picker.py")
IS_WIN = os.name == "nt"


def write_cmd_wrapper(folder, target):
    """`cc.cmd` that forwards every argument to the python script `target`, like a real account wrapper forwards to claude."""
    path = os.path.join(folder, "cc.cmd")
    with open(path, "w", newline="\r\n") as handle:
        handle.write('@echo off\n"%s" "%s" %%*\n' % (sys.executable, target))
    return path


class TestOrder(unittest.TestCase):
    def test_primary_first_then_fallbacks_without_repeats(self):
        self.assertEqual(claude_binaries.ordered("", [FAKE, "claude", FAKE]), ["claude", FAKE])
        self.assertEqual(claude_binaries.ordered(LIMITED, [FAKE]), [LIMITED, FAKE])

    def test_a_fallback_missing_on_this_machine_is_skipped(self):
        self.assertEqual(claude_binaries.ordered("claude", ["no-such-claude-wrapper", FAKE]), ["claude", FAKE])

    def test_fallbacks_from_config_accept_a_list_or_one_name(self):
        self.assertEqual(claude_binaries.read_fallbacks("cc"), ["cc"])
        self.assertEqual(claude_binaries.read_fallbacks(["cc", " ", 3]), ["cc"])
        self.assertEqual(claude_binaries.read_fallbacks(None), [])

    def test_usage_limit_switches_but_overloaded_does_not(self):
        self.assertTrue(claude_binaries.is_usage_limit("Claude AI usage limit reached|1767225600"))
        self.assertTrue(claude_binaries.is_usage_limit("API Error: 429 Too Many Requests"))
        self.assertFalse(claude_binaries.is_usage_limit("API Error: 529 Overloaded"))

    def test_swap_keeps_every_argument_after_the_base(self):
        argv = claude_binaries.base_argv(FAKE) + ["-p", "x", "--output-format", "json"]
        swapped = claude_binaries.swap_binary(argv, FAKE, LIMITED)
        self.assertEqual(swapped, [sys.executable, LIMITED, "-p", "x", "--output-format", "json"])


class TestAccount(unittest.TestCase):
    def test_cc_names_the_account_on_stderr(self):
        self.assertEqual(claude_binaries.account_from_stderr("table\n[cc] conta: claude2\nAbrindo"), "claude2")
        self.assertEqual(claude_binaries.account_from_stderr("anything else"), "")

    def test_labels_of_a_login_and_of_a_cc_token(self):
        login = {"method": "login", "email": "dev@x.com", "org": "iData", "plan": "team", "account": "", "error": ""}
        self.assertEqual(claude_binaries.account_label(login), "login, dev@x.com (iData, team)")
        token = {"method": "token", "email": "", "org": "", "plan": "", "account": "", "error": ""}
        self.assertEqual(claude_binaries.account_label(token, "claude2"), "token, cc account claude2")
        self.assertEqual(claude_binaries.account_label({"error": "boom"}), "")

    def test_describe_lists_a_missing_fallback_without_running_it(self):
        rows = claude_binaries.describe(FAKE, ["no-such-claude-wrapper"])
        self.assertEqual([(row["role"], row["found"]) for row in rows], [("main", True), ("fallback", False)])
        self.assertEqual(rows[1]["auth"], {})


class TestLimitWait(unittest.TestCase):
    def test_reset_times_of_cc_and_of_claude(self):
        moment = claude_binaries.reset_time("table\n[cc] reinicia: 2030-01-02T03:04:05.000Z\n")
        self.assertEqual(moment, datetime.datetime(2030, 1, 2, 3, 4, 5, tzinfo=datetime.timezone.utc).astimezone().replace(tzinfo=None))
        self.assertEqual(claude_binaries.reset_time("Claude AI usage limit reached|1767225600"), datetime.datetime.fromtimestamp(1767225600))
        self.assertIsNone(claude_binaries.reset_time("API Error: 529 Overloaded"))

    def test_the_wait_goes_until_the_first_known_reset_plus_a_margin(self):
        now = datetime.datetime.now()
        opts = argparse.Namespace(limit_wait=30, limit_resets=[now + datetime.timedelta(hours=5), now + datetime.timedelta(hours=2)])
        seconds, moment = run_loop.limit_wait_seconds(opts)
        self.assertEqual(moment, opts.limit_resets[1])
        self.assertAlmostEqual(seconds, 2 * 3600 + run_loop.RESET_MARGIN_SECONDS, delta=5)
        self.assertEqual(run_loop.limit_wait_seconds(argparse.Namespace(limit_wait=30, limit_resets=[now - datetime.timedelta(hours=1)])), (1800, None))
        self.assertEqual(run_loop.wait_text(3 * 3600 + 21 * 60), "3h 21min")

    def test_a_pause_from_the_panel_ends_the_wait(self):
        work = tempfile.mkdtemp(prefix="limit-wait-")
        os.makedirs(os.path.dirname(loop_state.loop_path(work, "PAUSE")), exist_ok=True)
        open(loop_state.loop_path(work, "PAUSE"), "w").close()
        self.assertTrue(run_loop.sleep_unless_paused(work, 3, 1))


class TestPromptThroughStdin(unittest.TestCase):
    def test_batch_wrapper_moves_the_prompt_to_stdin(self):
        argv, stdin_text = claude_binaries.prompt_through_stdin(["C:/bin/cc.cmd", "-p", "line 1\nline 2", "--output-format", "json"])
        self.assertEqual(argv, ["C:/bin/cc.cmd", "-p", "--output-format", "json"])
        self.assertEqual(stdin_text, "line 1\nline 2")

    def test_prompt_argument_joins_the_stdin_already_there(self):
        _, stdin_text = claude_binaries.prompt_through_stdin(["cc.bat", "-p", "/orchestrator roadmap"], "context")
        self.assertEqual(stdin_text, "/orchestrator roadmap\n\ncontext")

    def test_other_commands_are_unchanged(self):
        argv = ["claude.exe", "-p", "a\nb"]
        self.assertEqual(claude_binaries.prompt_through_stdin(argv, ""), (argv, ""))
        self.assertEqual(claude_binaries.prompt_through_stdin(["cc.cmd", "-p", "--resume", "s"], "x"), (["cc.cmd", "-p", "--resume", "s"], "x"))

    @unittest.skipUnless(IS_WIN, "cmd.exe wrappers exist only on Windows")
    def test_multi_line_prompt_reaches_the_wrapped_command_whole(self):
        folder = tempfile.mkdtemp(prefix="cc-wrapper-")
        echo = os.path.join(folder, "echo_prompt.py")
        with open(echo, "w") as handle:
            handle.write("import sys\nprint(sys.stdin.read() if sys.argv[1:] == ['-p'] else 'ARGV:' + repr(sys.argv[1:]))\n")
        argv, stdin_text = claude_binaries.prompt_through_stdin([write_cmd_wrapper(folder, echo), "-p", "line 1\nline 2 & 100%PATH%"])
        done = subprocess.run(argv, input=stdin_text, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), "line 1\nline 2 & 100%PATH%")


class TestNoticeAndReturn(unittest.TestCase):
    def sent_notices(self, opts, turn, name, account, channels="console,toast,file,email"):
        """(subject, only) of every notice notify_claude_change sends for this turn."""
        sent = []
        with mock.patch.dict(os.environ, {"LOOP_NOTIFY_CHANNELS": channels}),                 mock.patch.object(run_loop, "notify_wait", lambda subject, body, only=None: sent.append((subject, only))):
            run_loop.notify_claude_change(opts, turn, name, account)
        return sent

    def test_the_subject_says_which_account_and_how_it_signs_in(self):
        self.assertEqual(run_loop.claude_change_subject("cc", "token, cc account claude2", False),
                         "now on the backup account: cc account claude2 (subscription, sign-in token)")
        self.assertEqual(run_loop.claude_change_subject("C:/bin/claude.exe", "login, dev@x.com (iData, team)", True),
                         "back on the main account: dev@x.com (subscription, browser login)")
        self.assertEqual(run_loop.claude_change_subject("cc", "API key", False), "now on the backup account: `cc` (API key, billed per token)")
        self.assertEqual(run_loop.claude_change_subject("cc", "", False), "now on the backup account: `cc`")
        self.assertEqual(run_loop.claude_change_subject("cc.cmd", "token, cc account claude3", False, same_command=True),
                         "cc switched to cc account claude3 (subscription, sign-in token)")

    def test_the_body_names_both_accounts_with_organization_and_plan(self):
        self.assertEqual(run_loop.describe_claude_used("claude", "login, dev@x.com (iData, team)", True),
                         "the main command `claude` (dev@x.com, subscription, browser login; iData, team)")
        self.assertEqual(run_loop.describe_claude_used("cc", "", False), "the backup command `cc` (account unknown)")

    def test_a_change_of_command_goes_to_every_channel(self):
        opts = argparse.Namespace(last_claude_used=("claude", "login, dev@x.com"))
        sent = self.sent_notices(opts, 3, "cc", "token, cc account claude2")
        self.assertEqual(sent, [("now on the backup account: cc account claude2 (subscription, sign-in token)", None)])

    def test_a_change_of_account_inside_the_same_command_stays_local(self):
        opts = argparse.Namespace(last_claude_used=("cc", "token, cc account claude1"))
        sent = self.sent_notices(opts, 4, "cc", "token, cc account claude2")
        self.assertEqual(sent, [("cc switched to cc account claude2 (subscription, sign-in token)", ["console", "toast", "file"])])
        self.assertEqual(self.sent_notices(opts, 5, "cc", "token, cc account claude2"), [], "no change, no notice")

    def test_back_on_the_main_command_after_its_reset_moment(self):
        now = datetime.datetime.now()
        opts = argparse.Namespace(claude="claude", claude_fallback=[FAKE], claude_index=1, picker_tried=["a"],
                                  main_reset=now + datetime.timedelta(hours=1))
        self.assertFalse(run_loop.return_to_main_command(opts), "the reset moment is still ahead: stay on the fallback")
        self.assertEqual(opts.claude_index, 1)
        opts.main_reset = now - datetime.timedelta(minutes=1)
        self.assertTrue(run_loop.return_to_main_command(opts))
        self.assertEqual((opts.claude_index, opts.main_reset, opts.picker_tried), (0, None, []))
        self.assertFalse(run_loop.return_to_main_command(opts), "already on the main command")


class TestLoopFallback(LoopCase):
    def test_limited_main_command_switches_to_the_fallback_and_finishes(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        log = os.path.join(project.tmp, "limited.log")
        project.extra_env["FAKE_LIMITED_LOG"] = log
        result = project.run("--claude", LIMITED, "--claude-fallback", FAKE, "--limit-retries", "0", fake=False)
        self.done(result, 0)
        self.assertIn("usage limit on `%s`" % LIMITED, result.stdout)
        self.assertEqual(project.meta()["claude"], FAKE)
        self.assertEqual(project.meta()["claude_switches"], 1)
        with open(log, encoding="utf-8") as handle:
            self.assertEqual(len(handle.read().splitlines()), 1, "sticky: the limited command runs once, then the fallback keeps the turns")

    def test_every_command_limited_falls_back_to_the_wait(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        second = os.path.join(project.tmp, "second_limited.py")
        with open(LIMITED, encoding="utf-8") as source, open(second, "w", encoding="utf-8") as target:
            target.write(source.read())
        result = project.run("--claude", LIMITED, "--claude-fallback", second, "--limit-retries", "0", fake=False)
        self.assertIn("turn 1 again with `%s`" % second, result.stdout)
        self.done(result, 20)
        self.assertIn("usage limit persisted", project.meta()["reason"])

    @unittest.skipUnless(IS_WIN, "cmd.exe wrappers exist only on Windows")
    def test_fallback_to_a_cmd_wrapper_gets_the_prompt_through_stdin(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        wrapper = write_cmd_wrapper(project.tmp, FAKE)
        result = project.run("--claude", LIMITED, "--claude-fallback", wrapper, "--limit-retries", "0", fake=False)
        self.done(result, 0)
        self.assertEqual(project.loop_json()["status"], "done")
        self.assertTrue(all(f["status"] == "verified" for f in project.roadmap().values()))

    def test_the_turn_log_names_the_cc_account(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.extra_env["FAKE_CC_ACCOUNT"] = "claude2"
        self.done(project.run(), 0)
        self.assertEqual(project.meta()["claude_account"], "cc account claude2")
        with open(os.path.join(loop_state.turns_dir(project.work), "001.json"), encoding="utf-8") as handle:
            turn_log = json.load(handle)
        self.assertEqual(turn_log["claude_account"], "cc account claude2")
        self.assertEqual((turn_log["cc_account"], turn_log["auth_method"], turn_log["auth_email"]), ("claude2", "", ""))
        self.assertEqual(project.meta()["cc_account"], "claude2")

    def picker_env(self, project, accounts, exhausted=""):
        project.extra_env.update(FAKE_PICKER_ACCOUNTS=accounts, FAKE_PICKER_EXHAUSTED=exhausted, LOOP_NOTIFY_CHANNELS="console,file",
                                 FAKE_PICKER_STATE=os.path.join(project.tmp, "picker-calls"))

    def test_the_switch_to_the_fallback_is_notified(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        project.extra_env["LOOP_NOTIFY_CHANNELS"] = "console,file"
        result = project.run("--claude", LIMITED, "--claude-fallback", FAKE, "--limit-retries", "0", fake=False)
        self.done(result, 0)
        self.assertIn("now on the backup account: `fake_claude`", result.stdout)
        self.assertEqual(result.stdout.count("now on the backup account"), 1, "only the change is notified, not every turn")

    def test_an_exhausted_picker_account_runs_the_turn_again_on_the_next_account(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.picker_env(project, "a," + ",".join(["b"] * 20), exhausted="a")
        result = project.run("--claude", PICKER, "--limit-retries", "0", fake=False)
        self.done(result, 0)
        self.assertIn("account a; turn 1 again, the picker chooses another account", result.stdout)
        self.assertEqual(project.meta()["claude_account"], "cc account b")
        self.assertIn("fake_cc_picker switched to cc account b", result.stdout)

    def test_every_account_out_notifies_and_waits_then_starts_again_from_the_main_command(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.picker_env(project, "")
        result = project.run("--claude", LIMITED, "--claude-fallback", PICKER, "--limit-retries", "1", "--limit-wait", "0", fake=False)
        self.done(result, 20)
        self.assertIn("credits ran out", result.stdout)
        self.assertEqual(result.stdout.count("turn 1 again with `%s`" % PICKER), 2, "after the wait it starts again from the main command")

    def test_check_warns_about_a_missing_fallback(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        result = project.run("--check", "--claude", FAKE, "--claude-fallback", "no-such-claude-wrapper", fake=False)
        self.assertIn("fallback command `no-such-claude-wrapper` not found", result.stdout + result.stderr)
        self.assertEqual(result.returncode, 0, "a missing fallback is a warning, never a problem")

    def test_missing_fallback_never_blocks_the_loop(self):
        project = self.make(ROAD_BASIC, SCEN_BASIC)
        self.done(project.run("--claude-fallback", "no-such-claude-wrapper"), 0)
        self.assertEqual(project.loop_json()["status"], "done")


if __name__ == "__main__":
    unittest.main()
