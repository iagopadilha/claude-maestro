#!/usr/bin/env python3
"""Unsupervised /orchestrator loop: runs `claude -p` in turns until it finishes or gets stuck.

Usage:
  python run_loop.py <repo> [--wave 3] [--e2e] [--push] [--budget 5] [--timeout 60] [--max-stall 10]
                     [--branch NAME] [--setup "npm ci"] [--permission-mode auto] [--answer "text"] [--check] [--dry-run] [--no-monitor]
  (or `python harness.py loop <repo> ...`, which forwards here)

Panel: by default the script starts (or reuses) the Loop Harness web panel, detached and independent: it prints
`[loop] monitor: <url>`, writes LOOP-CMD.json and the URL in meta.json, and NEVER shuts it down when it ends. --no-monitor or
LOOP_MONITOR=off turns it off. The file .specs/project/loop/internal/PAUSE (created by the panel) makes the loop stop at
awaiting_user before the next turn.

IMPORTANT (user decision): the git worktree exists ONLY for this script. It creates, ONCE, the sibling folder
`../<project>-worktree-loop` on branch `loop/<date>` and works there; the original repo (your folder, your branch) is never touched.
The orchestrator inside claude commits on that branch and NEVER pushes (the guard blocks it and the turn runs with every remote's
pushurl disabled). The delivery is made by this script, at the end, only if status=done, only from the feature branch
(the integration branch/main/... are refused), without force: push + PR to the integration branch (delivery.py; default
`developer`, env LOOP_INTEGRATION_BRANCH), with the link in the notice and in the summary.
Default: ON (--no-push turns it off). Require a git identity with --git-user or LOOP_GIT_USER in the app .env
(~/.loop-harness/.env or <plugin>/shared/.env).

How each turn works:
  claude -p "/orchestrator continue --headless --turn N --wave W ..." --output-format stream-json --verbose
         --permission-mode <mode> --permission-prompts none --max-budget-usd <value>
  Each turn carries the LANES the scheduler chose (`--lanes slug:step,...`): one per feature, each at its own step, up to --wave of them.
  Every lane has its own agent at every step (spec-writer, spec-reviewer, executor, verifier) and advances by itself; no feature waits for
  another one to reach the same phase, and a quarantined feature only holds its dependents.
  The process ends by itself when the model gives its final answer. If it "would wait for the user", nobody is there:
  the turn ends and the orchestrator writes .specs/project/loop/json/LOOP.json {status: awaiting_user, ...}.

Stops (all notify through the LOOP_NOTIFY_CHANNELS channels: console, toast, file, email, webhook; see notify.py):
  done | awaiting_user | error (LOOP.json) | turn timeout | exit!=0 | risky permission denial or one without progress |
  dirty tree outside .specs/ | LOOP.json missing/stale | identical state in 2 turns |
  --max-stall turns WITHOUT PROGRESS (default 10; the counter resets on every status advance in the ROADMAP) |
  account usage limit (first the next --claude-fallback command, e.g. `cc` signed in with another account; when every one is
  limited, waits until the first known reset (else --limit-wait min), up to --limit-retries times, and starts again from --claude;
  while on a fallback, it goes back to --claude by itself once that command's remembered reset moment has passed). The usage windows
  come from the `rate_limit_event` of each turn's stream-json output (engine/usage_windows.py): every limit notice names the window that
  ran out ("5-hour window, back at 17:05", "weekly window, back on Sun 25/10 12:00"), the reset of that blocking window drives the wait
  and the return to --claude, each turn log has `usage_windows` + `limit_status`, meta.json `claude_usage` keeps the last reading per
  command, and the main command running out of its weekly window sends ONE alert per run on every channel |
  unusable account (logged out, revoked token, organization without access: the turn is run again on the next command with a
  notice on every channel and no wait; that command is not used again in the run; when none is left, error naming each command,
  its account and the reason).
No overall turn cap: as long as there is progress, it continues.

Always on stop: writes .specs/project/loop/json/meta.json and generates the summary (loop_summary.py, no Claude) in
--summary-dir (default: the app's output folder, `output_dir` in config.json, ~/Downloads when unset; never the repo).
"""
import datetime
import json
import os
import re
import shutil  # noqa: F401  (tests patch run_loop.shutil.which)
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402  (harness_backend package on sys.path; skill and guard: LOOP_SKILL_DIR > app config.json > the plugin)
import loop_summary  # noqa: E402
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import decisions  # noqa: E402  (AI decisions log: the one grammar owner of VALIDATE.md)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in the app .env)
import delivery  # noqa: E402  (push of the feature branch + PR to the integration branch, only at the end)
import graph_view  # noqa: E402  (progress drawing: text, short, Mermaid)
import cli_output  # noqa: E402  (one tolerant parser of the `claude -p` JSON output, shared with the app)
import claude_binaries  # noqa: E402  (which `claude` command runs, the fallback on an account limit, prompt via stdin for a .cmd wrapper)
import usage_windows  # noqa: E402  (5-hour / weekly usage windows read from the rate_limit_event of the turn output)
import monitor_proc  # noqa: E402  (loop web panel: starts detached, independent of this process)
import project_repos  # noqa: E402  (REPOS.json: the project's repos; one primary repo when the file is absent)
import readiness_checks  # noqa: E402  (subprojects, giant files, system map, uncommitted context, known commands)
import loop_cli  # noqa: E402  (the command-line parser)
from loop_io import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    IS_WIN, run, git, read, load_json, APP_OWNED_META_KEYS, DELIVERY_PENDING_NAME, save_meta, merge_pending_delivery, save_json,
    pid_alive, kill_tree, worktree_of,
)
from claude_accounts import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    claude_names, current_claude, unusable_names, first_usable_index, switch_claude, mark_account_unusable, unusable_stop_message,
    claude_account, LOCAL_NOTICE_CHANNELS, local_notice_channels, SIGN_IN_WORDS, command_base_name, split_account_label,
    AUTH_METHOD_CODES, CC_ACCOUNT_IN_LABEL, claude_auth_fields, describe_claude_used, claude_change_subject,
    claude_change_subject_core, return_to_main_command, remember_reset, limit_suffix, usage_key, record_usage, next_fallback_name,
    other_usage_lines,
)
from limit_waits import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    PAUSE_REASON, PAUSE_NEXT, pause_requested, MAX_LIMIT_WAIT_SECONDS, RESET_MARGIN_SECONDS, limit_wait_seconds,
    main_reset_moment, wait_text, LIMIT_WAIT_STEP_SECONDS, sleep_unless_paused,
)
from claude_turn import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    DEFAULT_ORCHESTRATOR_MODEL, DISABLED_PUSH_URL, LOOP_DISALLOWED_TOOLS, EMPTY_MCP_CONFIG, mcp_read_arguments, model_arguments,
    isolation_arguments, isolation_environment, claude_command, loop_json_correction_prompt, TURN_ENVIRONMENT_REPORTED,
    turn_environment_for, push_lock_env, run_turn, parse_result, unrecognized_output, is_caveman_on,
)
from permission_denials import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    RISKY_DENIAL_RE, SECRET_LIKE_RE, prod_hint_patterns, project_deny_patterns, is_risky_denial, denial_text,
    note_tolerated_denials, denial_progress,
)
from worktree_state import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    RANK, loop_worktrees, roadmap_score, fingerprint, heads_of, count_new_commits, SYSTEM_DOCUMENT, dirty_worktrees,
    dirty_outside_specs, IN_FLIGHT_STATUSES, PORCELAIN_ROW, row_path, feature_scope_rows, commit_leftovers, discard_leftovers,
    settle_dirty_worktrees, rewind, acquire_lock,
)
from worktrees import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    HEAVY_FOLDERS, LAST_RUN_NAME, MIN_GIT_WORKTREE_VERSION, roadmap_features, worktree_repos, step, branch_exists, base_branch_of,
    choose_base, base_sha_of, SETUP_COMMIT_MESSAGE, commit_carried_setup, carry_setup_into, last_run_entry, reattach_branch_of,
    file_sha256, copy_local_files, git_version_problem, worktree_never_ran, refresh_unused_worktree, SYNCED_INVENTORY_RELATIVE,
    SYNCED_SUBPROJECTS_RELATIVE, sync_untracked_map_files, resolve_setup, resolve_copy, ensure_worktree, ensure_extra_worktree,
    ensure_worktrees,
)
from auto_answers import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    CRITICAL_DECISION_PREFIX, write_answer, HARD_STOP_RE, ABANDON_RE, RECOMMENDED_RE, pick_auto_answer, try_auto_answer,
    NO_TIEBREAK_RE, gate_blocked_features, try_gate_tiebreak,
)
from turn_gates import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    COST_CAP_REASON_PREFIX, COST_WARNING_RATIO, plan_flags, audit_before_turn, e2e_unmarked, missing_backend_contract,
    append_validate_line, merge_verified_contracts, pause_on_quarantine, pause_on_critical_decision, effective_cost_cap,
    cost_cap_reason,
)
from preflight_checks import (  # noqa: E402,F401  (re-exported: tests, services and `harness.py` keep using `run_loop.<name>`)
    MAX_NAMED_FEATURES, required_channels, identity_problem, expand_plugin_vars, check_guard_command, hook_path_problems,
    environment_problems, test_environment_problems, fill_env_test_from_fallback, test_environment_warnings,
    test_environment_stop_reason, nothing_left_to_do, approval_problems, repo_names_of, extra_repos_preflight, CHECK_FIXES,
    check_item_kind, check_items, claude_command_problems, repo_branch_problems, base_branch_warnings, roadmap_problems,
    approval_problems_when_roadmap_exists, root_conformance_problems, rules_problems, dependency_audit_warnings, guard_problems,
    cost_cap_warnings, notice_channel_problems, push_problems, build_setup_warnings, feature_path_repo_problems,
    worktree_readiness, mentioned_mcp_servers, claude_config_readable, mcp_readiness, known_test_command, credential_readiness,
    planning_readiness, find_dotnet,
)

APP_ROOT_DIR = app_paths.APP_ROOT_DIR
APP_CLI = os.path.join(APP_ROOT_DIR, "harness.py")  # the command the user pastes: `python <app>/harness.py loop <repo> ...`
ACTIVE = {}  # run in progress (repo, opts, notify): lets us notify even when the script dies before finish()
STATUS_OK = {"continue", "awaiting_user", "done", "error"}
LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests|\b429\b|overloaded|quota", re.I)
EXIT = {"done": 0, "awaiting_user": 10, "paused": 10, "error": 20}


def deliver(ctx, work, only=None):
    """Sends the notice through the configured channels and retries the failed mandatory ones ONCE. Never raises."""
    results = notify.dispatch(ctx, work, only)
    required = set(required_channels())
    failed = [name for name, value in results.items() if name in required and not value.startswith("ok")]
    if failed:
        time.sleep(5)
        results.update(notify.dispatch(ctx, work, failed))
    still = [name for name, value in results.items() if name in required and not value.startswith("ok")]
    if still:
        print("[loop] WARNING: mandatory notice NOT delivered on: " + "; ".join("%s=%s" % (name, results[name]) for name in still))
    return results


def notify_early(reason):
    """Stop notice that does not go through finish() (preflight, worktree, lock, unhandled exception): the user is always notified."""
    if not ACTIVE.get("notify"):
        return
    repo, opts = ACTIVE["repo"], ACTIVE["opts"]
    work = worktree_of(repo)
    work = work if os.path.isdir(work) else ""
    ctx = {"event": "error", "project": os.path.basename(repo), "reason": str(reason)[:1500], "script": os.path.abspath(__file__),
           "repo": repo, "flags": command_flags(opts), "worktree": work, "features": [], "commits": [], "options": [],
           "to_check": [], "next": None, "summary": "", "push": None, "monitor_url": ACTIVE.get("monitor_url")}
    try:
        deliver(ctx, work)
    except Exception as error:  # a notice never takes anything down
        print("[loop] failed to notify: %s" % error)


def notify_wait(subject, body, only=None):
    """Notice for a wait that is not a stop (the loop keeps going): same channels as a stop (or only `only`), never raises."""
    if not ACTIVE.get("notify"):
        return
    repo = ACTIVE["repo"]
    work = worktree_of(repo)
    work = work if os.path.isdir(work) else ""
    ctx = {"event": "custom", "subject": "[loop] %s: %s" % (os.path.basename(repo), subject), "project": os.path.basename(repo),
           "extra_body": body}
    try:
        deliver(ctx, work, only)
    except Exception as error:  # a notice never takes anything down
        print("[loop] failed to notify: %s" % error)


def command_flags(opts):
    """Flags for the resume command that goes in the notice (the same as this run)."""
    flags = " --wave %d" % opts.wave
    if opts.e2e:
        flags += " --e2e"
    if getattr(opts, "push_explicit", False):
        flags += " --push"
    elif getattr(opts, "push", True) is False:
        flags += " --no-push"
    if opts.continue_on_blocked:
        flags += " --continue-on-blocked"
    if getattr(opts, "pause_on_blocked", False):
        flags += " --pause-on-blocked"
    if opts.setup:
        flags += ' --setup "%s"' % opts.setup
    if getattr(opts, "no_monitor", False):
        flags += " --no-monitor"
    return flags


def monitor_enabled(opts):
    """Panel on by default; off with --no-monitor or LOOP_MONITOR=off in the app .env."""
    return not (getattr(opts, "no_monitor", False) or monitor_proc.monitor_disabled_by_environment(notify.envload.get("LOOP_MONITOR")))


def start_monitor(repo, work, meta, opts):
    """Writes LOOP-CMD.json (relaunch command, without --answer/--option) and starts or reuses the panel. Never takes the loop
    down. The panel is independent: this script never stops it, not even when it ends."""
    argv = list(sys.argv[1:])
    if opts.repo in argv:
        argv[argv.index(opts.repo)] = repo
    monitor_proc.write_loop_command(work, [sys.executable, os.path.abspath(__file__)] + argv, os.getcwd())
    if not monitor_enabled(opts):
        return None
    url = monitor_proc.ensure_monitor(repo, work, open_browser=not monitor_proc.browser_open_disabled(notify.envload.get("LOOP_MONITOR_OPEN")))
    if url:
        print("[loop] monitor: %s" % url)
        meta["monitor_url"] = url
        ACTIVE["monitor_url"] = url
    else:
        meta.pop("monitor_url", None)
    save_meta(loop_state.loop_path(work, "meta.json"), meta)
    return url


def build_context(work, meta, status, reason, opts, repo, summary):
    """Notice data (only what comes from LOOP.json, ROADMAP and git log; never project file contents)."""
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    stages = loop_summary.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    features = [(f["slug"], f.get("status", "?")) for stage in stages for f in stage["features"]]
    base = meta.get("base", "")
    _, log, _ = git(work, "log", "--oneline", "--no-decorate", (base + ".." + meta.get("branch", "HEAD")) if base else "-8")
    return {
        "event": status, "project": os.path.basename(repo), "branch": meta.get("branch"), "turn": meta.get("turns"),
        "cost": meta.get("cost_usd"), "reason": reason, "next": state.get("next"),
        "options": state.get("options") if isinstance(state.get("options"), list) else [],
        "to_check": state.get("to_check") if isinstance(state.get("to_check"), list) else [],
        "features": features, "commits": log.splitlines()[:8], "push": meta.get("push"), "summary": summary,
        "pr": meta.get("pr"), "pr_status": meta.get("pr_status"), "validate": delivery.validate_items(work)[:8],
        "graph": graph_view.ascii_graph(graph_view.load(work)), "graph_short": graph_view.short(graph_view.load(work), 220),
        "script": os.path.abspath(__file__), "repo": repo, "flags": command_flags(opts), "worktree": work,
        "monitor_url": meta.get("monitor_url"),
    }


def preflight(repo, opts):
    """Checks everything the loop needs and returns (problems, warnings). Creates nothing. Reads as a table of contents:
    each step below returns its own (problems, warnings) pair, in the order they are reported."""
    work = worktree_of(repo)
    root = work if os.path.isdir(work) else repo
    repos = project_repos.load_repos(repo)
    planning = bool(getattr(opts, "planning", False))
    problems, warns = claude_command_problems(opts)
    if git(repo, "rev-parse", "--is-inside-work-tree")[1] != "true":
        return ["%s is not a git repository" % repo], warns
    features = roadmap_features(root)
    steps = (
        lambda: repo_branch_problems(repo, work),
        lambda: extra_repos_preflight(repos, opts, features),
        lambda: (root_conformance_problems(root), []),
        lambda: (readiness_checks.installer_problems(root, repos) if not os.path.exists(os.path.join(root, ".specs", "project", "ROADMAP.md")) else [], []),
        lambda: roadmap_problems(repo, root, repos, opts, setup_open=bool(problems)),
        lambda: ([] if planning else approval_problems_when_roadmap_exists(root, opts), []),  # --planning: the approval is what this check prepares
        lambda: readiness_checks.subprojects_preflight(root, features, repos),
        lambda: ([], readiness_checks.uncommitted_warnings(repos, repo)),
        lambda: ([], base_branch_warnings(repos, base_branch_of(opts))),
        lambda: (test_environment_problems(root, opts), test_environment_warnings(root, opts)),
        lambda: (hook_path_problems(), []),
        lambda: rules_problems(root),
        lambda: ([], dependency_audit_warnings(root)),
        lambda: environment_problems(opts, repo),
        lambda: guard_problems(),
        lambda: planning_readiness(repo, root, repos, features, opts) if planning else ([], []),
        lambda: ([], cost_cap_warnings(opts)),
        lambda: notice_channel_problems(opts),
        lambda: push_problems(repo, opts),
        lambda: ([], build_setup_warnings(repo, work, opts)),
    )
    for step in steps:
        step_problems, step_warns = step()
        problems += [item for item in step_problems if item not in problems]  # two checks may report the same finding: once, in order
        warns += [item for item in step_warns if item not in warns]
    return problems, warns


def ensure_worktrees_or_stop(repo, opts):
    """Start creates the worktrees that are missing (one per repo defined for the project), before the first turn. Idempotent: the ones
    that exist are reused (`reuse` step). A failure is reported as a clear PROBLEM line and stops the start with it."""
    try:
        return ensure_worktrees(repo, opts)
    except SystemExit as stop:
        if not isinstance(stop.code, str):
            raise
        print("[check] PROBLEM: worktree not created: %s" % stop.code, flush=True)
        sys.exit("the loop did not start: its worktrees could not be created. Problem: %s" % stop.code)


def warn_near_cost_cap(meta, cost, cap):
    """ONE notice when the spend reaches 80 % of the cap (again only if the cap is raised and 80 % of the new one is reached)."""
    if not cap or cost < cap * COST_WARNING_RATIO or cost > cap or meta.get("cost_warned_cap") == cap:
        return False
    meta["cost_warned_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta["cost_warned_cap"] = cap
    link = ACTIVE.get("monitor_url")
    notify_wait("spend at 80 % of the cap",
                "The loop spent US$ %.2f of the US$ %.2f cap (%d %%). It pauses when the cap is passed; raise the limit in the panel to carry on without a pause.%s"
                % (cost, cap, round(cost * 100 / cap), (" Panel: " + link) if link else ""))
    return True


def notify_claude_change(opts, turn, name, account):
    """Notice (every configured channel) when a turn ran on another command or account than the previous turn: `claude` -> `cc`,
    back to `claude`, or the `cc` picker on another account. The first turn of the first run only records it."""
    used = (name, account)
    previous = getattr(opts, "last_claude_used", None)
    opts.last_claude_used = used
    unusable = getattr(opts, "unusable_notice", None)  # set when the change came from an account that cannot be used
    opts.unusable_notice = None
    if not previous or previous == used:
        return
    main_name = command_base_name(getattr(opts, "claude", "") or claude_binaries.DEFAULT_BINARY_NAME)  # --claude, default `claude`
    is_main = command_base_name(name) == main_name
    limit_phrases = getattr(opts, "limit_phrases", None) or {}  # which window ran out on each command, from its last limited turn
    before = describe_claude_used(previous[0], previous[1], command_base_name(previous[0]) == main_name)
    now = describe_claude_used(name, account, is_main)
    print("[loop] turn %d ran on %s; before it was %s" % (turn, now, before))
    same_command = previous[0] == name and not unusable
    only = local_notice_channels() if same_command else None  # another account of the same command is noise for email/webhook
    unusable_subject = ""
    if unusable:
        # it needs a human (log in again, ask the admin): always every channel
        unusable_subject = "%s account unusable" % ("main" if command_base_name(unusable["command"]) == main_name else "backup")
        reason = ("The previous account is unusable (%s), so the loop moved to the backup command. Waiting does not fix a login: "
                  "sign in again (`claude` then /login, or `claude setup-token`) or ask the admin to enable access." % (unusable["reason"] or "no reason in the output"))
    elif same_command:
        reason = "The `%s` account picker moved to another account because the previous one hit its usage limit%s." % (
            command_base_name(name), limit_suffix(limit_phrases.get(command_base_name(previous[0]), "")))
    elif is_main:
        reason = "The usage limit of the main account%s should have reset, so the loop went back to it." % limit_suffix(limit_phrases.get(main_name, ""))
    else:
        reason = "The previous account hit its usage limit%s, so the loop moved to the backup command." % limit_suffix(limit_phrases.get(command_base_name(previous[0]), ""))
    notify_wait(claude_change_subject(name, account, is_main, same_command, unusable_subject),
                "Turn %d now runs on %s.\nBefore it the loop ran on %s.\n%s The loop goes on by itself." % (turn, now, before, reason), only)


def alert_main_weekly_limit(opts, meta, turn, name, blocking):
    """One alert per run, on every channel, when the MAIN command's weekly window ran out: until when it is unusable, where the loop
    goes on, and the last known 5-hour and weekly usage of every other command or account. Sent again only when the reset moment
    changes. Returns True when it sent one."""
    if getattr(opts, "claude_index", 0) != 0 or not usage_windows.is_weekly_block(blocking):
        return False
    alerted = opts.__dict__.setdefault("weekly_alerts", {})
    main_base = command_base_name(name)
    if main_base in alerted and alerted[main_base] == blocking.get("reset_at"):
        return False
    alerted[main_base] = blocking.get("reset_at")
    moment = usage_windows.reset_moment(blocking)
    until = usage_windows.format_moment(moment) if moment else "its reset (time unknown)"
    fallback = next_fallback_name(opts)
    goes_on = "the loop continues on `%s`" % command_base_name(fallback) if fallback else "no other command is configured, the loop waits for the reset"
    lines = other_usage_lines(opts, meta, name)
    print("[loop] main account weekly limit reached on `%s`, unusable until %s; %s" % (main_base, until, goes_on))
    notify_wait("main account weekly limit reached, unusable until %s; %s" % (until, goes_on),
                "Turn %d: the weekly window of the main command `%s` is used up.\nLast known usage of the other commands:\n%s"
                % (turn, main_base, "\n".join(lines) or "- none configured"))
    return True


# ---------------------------------------------------------------- end
def finish(work, meta, status, reason, opts):
    meta["stopped"], meta["reason"] = status, reason
    if status == "paused":  # resumable stop (spend cap): the panel reads the status from LOOP.json
        paused_state = load_json(loop_state.state_file(work, "LOOP.json"))
        paused_state.update(status="paused", reason=reason, turn=int(meta.get("turns", 0)), next="raise the limit or resume in the panel",
                            at=datetime.datetime.now().isoformat(timespec="seconds"))
        paused_state.pop("options", None)
        save_json(loop_state.state_file(work, "LOOP.json"), paused_state)
    meta["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta_path = loop_state.loop_path(work, "meta.json")
    meta.setdefault("push", "not done")
    if status == "done" and opts.push:
        problem = identity_problem(work, opts)
        if problem:
            meta["push"] = "refused: " + problem
        else:
            try:
                delivered = delivery.finalize_project(work, meta, base=base_branch_of(opts))
            except Exception as error:  # the delivery never takes the notice down
                delivered = {"push": "FAILED: delivery.py: %s" % str(error)[:200], "pr": None, "pr_status": "not created"}
            meta["push"], meta["pr"], meta["pr_status"] = delivered["push"], delivered["pr"], delivered["pr_status"]
    save_meta(meta_path, meta)
    try:
        graph_view.write(work)
    except OSError:
        pass
    summary = ""
    try:
        sys.argv = ["loop_summary.py", work, "--branch", meta.get("branch", ""), "--base", meta.get("base", ""),
                    "--out", opts.summary_dir or app_paths.default_output_dir(), "--max-cost", str(effective_cost_cap(meta, opts))]
        import io
        from contextlib import redirect_stdout
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            loop_summary.main()
        summary = buffer.getvalue().strip()
    except Exception as error:  # the summary can never take the notice down
        summary = "summary failed: %s" % error
    repo = meta.get("repo") or opts.repo
    meta["notices"] = deliver(build_context(work, meta, status, reason, opts, os.path.abspath(repo), summary), work)
    save_json(meta_path, meta)
    return EXIT.get(status, 30)


def ready_command(repo, opts):
    """The command `--check` prints: through the app CLI (`harness.py loop`), with this run's flags."""
    flags = " --wave %d%s%s%s%s" % (opts.wave, " --e2e" if opts.e2e else "", " --no-push" if not opts.push else "", (' --setup "%s"' % opts.setup) if opts.setup else "",
                                    " --no-monitor" if opts.no_monitor else "")
    python = os.path.splitext(os.path.basename(sys.executable))[0] or "python"
    return "%s \"%s\" loop \"%s\"%s" % (python, APP_CLI, repo, flags)


def main():
    parser = loop_cli.build_parser()
    opts = parser.parse_args()
    opts.push_explicit = opts.push is True
    opts.push = opts.push is not False

    repo = os.path.abspath(opts.repo)
    ACTIVE.update(repo=repo, opts=opts, notify=not (opts.check or opts.dry_run or opts.rewind is not None))
    if git(repo, "rev-parse", "--is-inside-work-tree")[1] != "true":
        sys.exit("%s is not a git repository" % repo)
    if opts.rewind is not None:
        return rewind(repo, opts)
    if opts.ensure_worktrees:
        ACTIVE["notify"] = False
        opts.only_repos = [name.strip() for name in opts.repos.split(",") if name.strip()] or None
        opts.repo_path = repo
        try:
            ensure_worktrees(repo, opts)
        except SystemExit as stop:
            if isinstance(stop.code, str):
                print("[loop] error: %s" % stop.code, flush=True)
            return 1 if stop.code in (None, 0) or isinstance(stop.code, str) else int(stop.code)
        return 0
    problems, warns = preflight(repo, opts)
    for item in warns:
        print("[check] WARNING:", item)
    for item in problems:
        print("[check] PROBLEM:", item)
    if opts.check:
        if not problems:
            print("[check] READY. Command:\n  %s" % ready_command(repo, opts))
        if opts.json:
            print("[check] JSON: " + json.dumps({"ready": not problems, "problems": problems, "warnings": warns,
                                                  "items": check_items(problems, warns, project_repos.load_repos(repo), repo)}, ensure_ascii=False))
        return 1 if problems else 0
    if problems:
        work_now = worktree_of(repo)
        env_problems = test_environment_problems(work_now if os.path.isdir(work_now) else repo, opts)
        env_reason = test_environment_stop_reason(work_now if os.path.isdir(work_now) else repo, opts, env_problems)
        others = [item for item in problems if item not in env_problems]
        # the test environment text goes FIRST: the notice clips the reason and this is the part the user has to act on
        sys.exit("pre-flight failed, the loop did not start. " + (env_reason + " " if env_reason else "")
                 + ("Problems: " + "; ".join(others) if others else ("Problems: " + "; ".join(problems) if not env_reason else "")))
    repos = project_repos.load_repos(repo)
    opts.repo_names, opts.primary_name = repo_names_of(repos), (project_repos.primary_of(repos).name if len(repos) > 1 else "")
    work, meta = ensure_worktrees_or_stop(repo, opts)
    opts.reference_dirs = [item.path for item in project_repos.reference_repos(repos)]
    opts.extra_worktrees = [item["worktree"] for item in (meta.get("repos") or {}).values()
                            if isinstance(item, dict) and not item.get("primary") and item.get("worktree")]
    if opts.dry_run:
        print("[dry-run] turn 1 command:", " ".join(claude_command(opts, "/orchestrator continue --headless --turn 1 --wave %d" % opts.wave)))
        return 0

    start_monitor(repo, work, meta, opts)
    specs = os.path.join(work, ".specs", "project")
    if opts.option:
        options = load_json(loop_state.state_file(work, "LOOP.json")).get("options") or []
        if not 1 <= opts.option <= len(options):
            sys.exit("--option %d invalid: the notice listed %d option(s) (%s)" % (opts.option, len(options), "; ".join("%d) %s" % (i, o) for i, o in enumerate(options, 1)) or "none"))
        opts.answer = str(options[opts.option - 1])
    if opts.answer:
        write_answer(work, opts.answer)
    auto_state = {"total": 0, "per_feature": {}, "tiebreaks": {}}
    if not opts.answer:
        if not try_auto_answer(work, int(meta.get("turns", 0)) + 1, opts, meta, auto_state):  # turn stopped on a trivial question: answer it and carry on without you
            try_gate_tiebreak(work, int(meta.get("turns", 0)) + 1, opts, meta, auto_state)  # spec that failed the gate on the previous run: break the tie

    lock = acquire_lock(work)
    logs = loop_state.turns_dir(work)
    turn = int(meta.get("turns", 0))
    stall, last_score, last_print, limit_hits = 0, roadmap_score(work)[0], "", 0
    opts.last_claude_used = (meta.get("claude"), meta.get("claude_account")) if meta.get("claude") else None  # a resumed run compares with the last turn
    cost = float(meta.get("cost_usd", 0))
    if not loop_state.load_snapshot(work, turn):  # baseline: state before this run's first turn (return point for --rewind)
        loop_state.snapshot(work, turn, git(work, "rev-parse", "HEAD")[1], heads_of(work, meta))
    try:
        while True:
            paused = pause_requested(work, turn)  # before spending the next turn
            if paused:
                print("[loop] PAUSE found: stopping before turn %d" % (turn + 1))
                return finish(work, meta, "awaiting_user", paused, opts)
            cap = effective_cost_cap(meta, opts)
            if cap and cost > cap:  # resumed over the cap without raising it: pause again before spending anything
                return finish(work, meta, "paused", cost_cap_reason(cost, cap), opts)
            turn += 1
            for dirty_name, rows in settle_dirty_worktrees(work, meta, turn, opts):
                where = "in repo %s " % dirty_name if dirty_name else ""
                return finish(work, meta, "error", "dirty tree outside .specs/ %sbefore turn %d (no automatic reset; these files are outside the paths of the features in progress): %s. "
                              "In the panel use \"Commit leftovers and try again\" or \"Discard leftovers and try again\"." % (where, turn, "; ".join(rows[:5])), opts)
            opts.leftovers = ""  # the recovery choice applies to the first turn of this run only
            meta["decisions_mark"] = decisions.line_count(work)  # critical decisions written after this line stop the loop at the end of the turn
            save_meta(loop_state.loop_path(work, "meta.json"), meta)
            _, current, _ = git(work, "branch", "--show-current")
            if current != meta["branch"]:
                code, _, err = git(work, "checkout", meta["branch"])
                if code != 0:
                    return finish(work, meta, "error", "worktree is off the loop branch and checkout failed: " + err, opts)
            applied = audit_before_turn(work)
            if applied:
                print("[loop] forgotten dependencies applied to the ROADMAP before the turn: " + "; ".join(applied))
            prompt = "/orchestrator continue --headless --turn %d --wave %d --branch %s%s%s" % (
                turn, opts.wave, meta["branch"], " --e2e" if opts.e2e else "", plan_flags(work, opts.wave, opts.primary_name))
            started = time.time()
            print("[loop] turn %d | %s" % (turn, datetime.datetime.now().strftime("%H:%M:%S")))
            return_to_main_command(opts)
            claude_name = current_claude(opts)
            code, out, err, timed_out = run_turn(claude_command(opts, prompt), work, opts.timeout)
            data = parse_result(out)
            log = dict(data) if data.get("ok") else dict(data, raw=out[-4000:])
            rate_limit = log.pop("rate_limit", None) or {}  # kept below as `usage_windows` and `limit_status`
            windows = usage_windows.with_local_reset(usage_windows.windows_of(rate_limit))
            log["usage_windows"], log["limit_status"] = windows, str(rate_limit.get("status") or "")
            # what the turn says about limits and logins: the result text and stderr (the rest of a stream-json output is the model's own work)
            scan_text = (str(data.get("result") or "") + "\n" + err) if data.get("ok") else cli_output.strip_rate_limit_events(out) + "\n" + err
            picked_account = claude_binaries.account_from_stderr(err)
            blocking = usage_windows.blocking_window(rate_limit)
            if err:
                log["stderr"] = err[-2000:]
            log["exit_code"], log["timeout"], log["turn"], log["claude"] = code, timed_out, turn, claude_name
            log["caveman"] = is_caveman_on()  # CAV-03: lets the settings screen compare the cost with and without it
            log["claude_account"] = claude_account(opts, claude_name, err)
            log.update(claude_auth_fields(log["claude_account"]))
            meta["claude"], meta["claude_account"] = claude_name, log["claude_account"]
            meta.update(claude_auth_fields(log["claude_account"]))
            record_usage(meta, usage_key(claude_name, picked_account), windows, log["limit_status"])
            save_json(os.path.join(logs, "%03d.json" % turn), log)
            cost += float(data.get("total_cost_usd") or 0)
            meta.update({"turns": turn, "cost_usd": round(cost, 4)})
            save_meta(loop_state.loop_path(work, "meta.json"), meta)

            cap = effective_cost_cap(meta, opts)
            if cap and cost > cap:
                return finish(work, meta, "paused", cost_cap_reason(cost, cap), opts)
            if warn_near_cost_cap(meta, cost, cap):
                save_meta(loop_state.loop_path(work, "meta.json"), meta)
            if timed_out:
                return finish(work, meta, "error", "turn %d exceeded %d min (process terminated)" % (turn, opts.timeout), opts)
            unreadable = unrecognized_output(data)
            failed = code != 0 or data.get("is_error") or unreadable
            limited_by_status = bool(failed and (blocking or data.get("api_error_status") == 429))  # the output's own rate limit data
            account_limited = bool(failed and (claude_binaries.is_usage_limit(scan_text) or limited_by_status))
            limit_phrase = ""
            if account_limited:
                reset = usage_windows.reset_moment(blocking) or claude_binaries.reset_time(scan_text)  # the blocking window first, `[cc] reinicia:` / `limit reached|epoch` otherwise
                limit_phrase = usage_windows.phrase(blocking)
                opts.__dict__.setdefault("limit_phrases", {})[command_base_name(claude_name)] = limit_phrase
                remember_reset(opts, reset, limit_phrase)
                if getattr(opts, "claude_index", 0) == 0:  # the main command: when to try it again while a fallback keeps the turns
                    opts.main_reset = main_reset_moment(opts, reset)
                picked = picked_account
                if not picked and alert_main_weekly_limit(opts, meta, turn, claude_name, blocking):
                    save_meta(loop_state.loop_path(work, "meta.json"), meta)
                if picked and picked not in opts.__dict__.setdefault("picker_tried", []):
                    # the `cc` picker ran on an account that ran out during the turn: run it again, it picks the next free one ("cc" + Enter)
                    opts.picker_tried.append(picked)
                    opts.last_claude_used = getattr(opts, "last_claude_used", None) or (claude_name, log["claude_account"])  # notify the change
                    print("[loop] usage limit on `%s` account %s%s; turn %d again, the picker chooses another account" % (claude_name, picked, limit_suffix(limit_phrase), turn))
                    turn -= 1
                    continue
            if account_limited and switch_claude(opts):
                print("[loop] usage limit on `%s`%s; turn %d again with `%s`" % (claude_name, limit_suffix(limit_phrase), turn, current_claude(opts)))
                meta["claude_switches"] = int(meta.get("claude_switches", 0)) + 1
                opts.last_claude_used = getattr(opts, "last_claude_used", None) or (claude_name, log["claude_account"])  # notify the change
                save_meta(loop_state.loop_path(work, "meta.json"), meta)
                turn -= 1
                continue
            if failed and not account_limited and (claude_binaries.is_account_unusable(scan_text) or data.get("api_error_status") in (401, 403)):
                # the account cannot sign in (logged out, revoked token, organization without access): no wait fixes it, try the next command
                reason = claude_binaries.unusable_reason(str(data.get("result") or "")) or claude_binaries.unusable_reason(scan_text) or "API error %s" % data.get("api_error_status")
                record = mark_account_unusable(opts, claude_name, log["claude_account"], reason, turn)
                meta.setdefault("claude_unusable", []).append(record)
                if switch_claude(opts):
                    print("[loop] account unusable on `%s` (%s); turn %d again with `%s`" % (claude_name, record["reason"], turn, current_claude(opts)))
                    meta["claude_switches"] = int(meta.get("claude_switches", 0)) + 1
                    opts.last_claude_used = getattr(opts, "last_claude_used", None) or (claude_name, log["claude_account"])  # notify the change
                    save_meta(loop_state.loop_path(work, "meta.json"), meta)
                    turn -= 1
                    continue
                return finish(work, meta, "error", unusable_stop_message(opts), opts)
            if failed and (LIMIT_RE.search(scan_text) or limited_by_status):
                limit_hits += 1
                if limit_hits > opts.limit_retries:
                    return finish(work, meta, "error", "account usage limit persisted after %d waits" % opts.limit_retries, opts)
                seconds, resume_at = limit_wait_seconds(opts)
                when = resume_at.strftime("%d/%m %H:%M") if resume_at else ""
                window_text = (getattr(opts, "limit_reset_phrases", None) or {}).get(resume_at, "")  # the window that frees up first
                print("[loop] usage limit on every command (%s); waiting %s%s (%d/%d)" % (", ".join(claude_names(opts)), wait_text(seconds),
                                                                                       " until " + when if when else "", limit_hits, opts.limit_retries))
                notify_wait("credits ran out%s, back in %s" % (limit_suffix(window_text), wait_text(seconds)),
                            "The usage limit was hit on turn %d by every command the loop can use (%s). %s The loop waits and starts "
                            "again by itself, from `%s` (wait %d of %d)."
                            % (turn, ", ".join(claude_names(opts)), ("The first account frees up at %s (in %s%s)." % (when, wait_text(seconds), "; " + window_text if window_text else ""))
                               if when else "The reset time is unknown: it tries again in %s." % wait_text(seconds),
                               claude_names(opts)[first_usable_index(opts)], limit_hits, opts.limit_retries))
                paused = sleep_unless_paused(work, turn - 1, seconds)
                if paused:
                    return finish(work, meta, "awaiting_user", paused, opts)
                opts.claude_index, opts.picker_tried, opts.limit_resets, opts.main_reset = first_usable_index(opts), [], [], None  # after the wait, the main command again (the first one whose account works)
                turn -= 1
                continue
            limit_hits = 0
            opts.picker_tried, opts.limit_resets = [], []
            if unreadable and code == 0:
                return finish(work, meta, "error", "claude output on turn %d was not recognized (%s; the CLI output format may have changed, "
                              "run `python harness.py doctor` to compare the installed version with the tested one): %s"
                              % (turn, data.get("detail") or "unrecognized", data.get("sample") or "(empty)"), opts)
            if failed:
                return finish(work, meta, "error", "claude exited with code %s on turn %d: %s" % (code, turn, (err or str(data.get("result") or data.get("sample") or ""))[:300]), opts)
            notify_claude_change(opts, turn, claude_name, log["claude_account"])
            denials = data.get("permission_denials") or []
            risky = [d for d in denials if is_risky_denial(d, work)]
            if risky:
                return finish(work, meta, "error", "permission denied on turn %d (risky command, not tolerated): %s"
                              % (turn, "; ".join(denial_text(d) for d in risky)[:400]), opts)

            state = load_json(loop_state.state_file(work, "LOOP.json"))
            if (state.get("turn") != turn or state.get("status") not in STATUS_OK) and data.get("session_id"):
                # the model finished the turn (e.g. while waiting on background subagents) without writing LOOP.json: ONE correction turn in the
                # same session, only asking for the file (no new work), before failing the loop
                print("[loop] LOOP.json not written on turn %d; one correction turn in the same session" % turn)
                fix_code, fix_out, fix_err, fix_timeout = run_turn(
                    claude_command(opts, loop_json_correction_prompt(turn, meta["branch"])) + ["--resume", data["session_id"]], work, opts.timeout)
                fix_data = parse_result(fix_out)
                cost += float(fix_data.get("total_cost_usd") or 0)
                meta.update({"cost_usd": round(cost, 4), "loop_json_corrections": int(meta.get("loop_json_corrections", 0)) + 1})
                save_meta(loop_state.loop_path(work, "meta.json"), meta)
                state = load_json(loop_state.state_file(work, "LOOP.json"))
            if state.get("turn") != turn or state.get("status") not in STATUS_OK:
                loop_state.snapshot(work, turn, git(work, "rev-parse", "HEAD")[1], heads_of(work, meta))  # the turn ran: the return point exists even with a bad LOOP.json
                return finish(work, meta, "error", "LOOP.json missing, stale or invalid after turn %d (got status=%r turn=%r; expected turn=%d and status in %s). "
                              "The orchestrator must write it with: python loop_state.py loop <root> --status S --turn %d ..." % (
                                  turn, state.get("status"), state.get("turn"), turn, sorted(STATUS_OK), turn), opts)
            # typed state: legal edges, tries, verified only with a commit, paths; saves this turn's snapshot for --rewind
            prev = loop_state.load_snapshot(work, turn - 1)
            _, head_now, _ = git(work, "rev-parse", "HEAD")
            new_commits = count_new_commits(prev, work, meta, head_now)
            violations = loop_state.validate(read(os.path.join(prev["dir"], "ROADMAP.md")) if prev else "",
                                             read(os.path.join(specs, "ROADMAP.md")), state, new_commits, os.path.exists(loop_state.state_file(work, "ANSWERS.md")),
                                             opts.repo_names, opts.primary_name)
            loop_state.snapshot(work, turn, head_now, heads_of(work, meta))
            try:
                graph_view.write(work)  # .specs/project/loop/GRAPH.md always current
            except OSError:
                pass
            if violations:
                return finish(work, meta, "error", "invalid state after turn %d (use --rewind %d to go back): %s" % (turn, turn - 1, "; ".join(violations[:4])), opts)
            if denials:
                progress = denial_progress(prev, specs, state, new_commits)
                if not progress:
                    return finish(work, meta, "error", "permission denied on turn %d and the turn did not advance (no new commit, no ROADMAP change): %s"
                                  % (turn, "; ".join(denial_text(d) for d in denials)[:400]), opts)
                note_tolerated_denials(work, turn, denials, progress)
                meta.setdefault("tolerated_denials", []).append({"turn": turn, "count": len(denials)})
                print("[loop] warning: %d permission denial(s) on turn %d, tolerated (%s); noted in .specs/project/loop/VALIDATE.md" % (len(denials), turn, progress))
            merge_verified_contracts(work, meta, state, specs)
            critical = pause_on_critical_decision(work, state, meta.get("decisions_mark", 0))
            if critical:
                return finish(work, meta, "awaiting_user", critical, opts)
            paused = pause_on_quarantine(specs, prev, state, opts)
            if paused:
                if try_auto_answer(work, turn, opts, meta, auto_state):
                    continue
                return finish(work, meta, "awaiting_user", paused, opts)
            status = state["status"]
            reason = str(state.get("reason") or state.get("next") or "")
            if status == "done" and opts.e2e:
                pending = e2e_unmarked(specs)
                if pending:
                    return finish(work, meta, "error", "E2E phase ended without recording the result in the ROADMAP (e2e=ok|failed|not-covered): %s" % "; ".join(pending), opts)
            if status == "awaiting_user" and try_auto_answer(work, turn, opts, meta, auto_state):
                continue
            if status == "done" and not opts.e2e and try_gate_tiebreak(work, turn, opts, meta, auto_state):
                continue
            if status in ("done", "awaiting_user", "error"):
                return finish(work, meta, status, reason, opts)

            score, total, done = roadmap_score(work)
            if total and done == total and not opts.e2e:
                if try_gate_tiebreak(work, turn, opts, meta, auto_state):
                    continue
                return finish(work, meta, "done", "all features verified or blocked (%d/%d)" % (done, total), opts)
            printed = fingerprint(work, meta)
            if printed == last_print:
                return finish(work, meta, "error", "identical state (ROADMAP+STATE+HEAD) in 2 consecutive turns: the loop is not advancing", opts)
            last_print = printed
            if score > last_score:
                stall = 0
            else:
                stall += 1
                if stall >= opts.max_stall:
                    return finish(work, meta, "error", "%d consecutive turns without ROADMAP progress" % stall, opts)
            last_score = score  # baseline always follows the score (a blocked feature lowers the score without stopping the others' progress)
            print("[loop] turn %d ok in %.0fs | progress %d (%d/%d closed) | no progress: %d/%d | cost US$ %.2f" % (turn, time.time() - started, score, done, total, stall, opts.max_stall, cost))
    except KeyboardInterrupt:
        return finish(work, meta, "error", "interrupted by the user (Ctrl+C)", opts)
    finally:
        try:
            os.remove(lock)
        except OSError:
            pass


def entry():
    """Entry point: any exit of the script notifies the user (finish() covers the normal stops; this covers the rest)."""
    try:
        return main()
    except SystemExit as stop:
        if isinstance(stop.code, str):  # sys.exit("message"): preflight, worktree, lock, --answer without a question...
            notify_early(stop.code)
        raise
    except Exception as error:
        import traceback
        traceback.print_exc()
        notify_early("unhandled exception in the script: %s: %s" % (type(error).__name__, error))
        return 20


if __name__ == "__main__":
    sys.exit(entry())
