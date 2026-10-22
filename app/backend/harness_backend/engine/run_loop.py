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
  claude -p "/orchestrator continue --headless --turn N --wave W ..." --output-format json
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
  while on a fallback, it goes back to --claude by itself once that command's remembered reset moment has passed).
No overall turn cap: as long as there is progress, it continues.

Always on stop: writes .specs/project/loop/json/meta.json and generates the summary (loop_summary.py, no Claude) in
--summary-dir (default: the app's output folder, `output_dir` in config.json, ~/Downloads when unset; never the repo).
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
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
import audit_deps  # noqa: E402  (forgotten dependency between features)
import monitor_proc  # noqa: E402  (loop web panel: starts detached, independent of this process)
import mcp_access  # noqa: E402  (read-only MCP servers of the turn, computed from ~/.claude.json: no per-project toggle)
import test_env_fallback  # noqa: E402  (default .env.test from the machine-level TEST values)
import credential_check  # noqa: E402  (TEST-ENVIRONMENT.md names only: no plaintext secret, every $NAME defined in an .env)
import cli_output  # noqa: E402  (one tolerant parser of the `claude -p` JSON output, shared with the app)
import claude_binaries  # noqa: E402  (which `claude` command runs, the fallback on an account limit, prompt via stdin for a .cmd wrapper)
import project_repos  # noqa: E402  (REPOS.json: the project's repos; one primary repo when the file is absent)
import detect_setup  # noqa: E402  (automatic setup command and local files to copy of a repo)
import subprojects  # noqa: E402  (monorepo list: owner of a path, confirmed or not)
import system_map  # noqa: E402  (contract.md deltas of a verified feature -> docs/codebase/SYSTEM.md of a monorepo)
import readiness_checks  # noqa: E402  (subprojects, giant files, system map, uncommitted context, known commands)
import setup_files  # noqa: E402  (uncommitted setup files: carried into a new worktree, one readiness block per repo)
from harness_backend.config import app_config, skill_locator  # noqa: E402

APP_ROOT_DIR = app_paths.APP_ROOT_DIR
APP_CLI = os.path.join(APP_ROOT_DIR, "harness.py")  # the command the user pastes: `python <app>/harness.py loop <repo> ...`
IS_WIN = os.name == "nt"
MAX_NAMED_FEATURES = 6
ACTIVE = {}  # run in progress (repo, opts, notify): lets us notify even when the script dies before finish()
# blocked is NOT progress: it scores 0, otherwise a loop that only blocks features would look like it advances (closes the roadmap but delivers nothing)
RANK = {"pending": 0, "speccing": 1, "specced": 2, "approved": 3, "building": 4, "verified": 6, "blocked": 0}
STATUS_OK = {"continue", "awaiting_user", "done", "error"}
LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests|\b429\b|overloaded|quota", re.I)
EXIT = {"done": 0, "awaiting_user": 10, "paused": 10, "error": 20}
CRITICAL_DECISION_PREFIX = "critical decision: "  # LOOP.json reason of a critical decision stop; the panel and the notice key on it
COST_CAP_REASON_PREFIX = "cost cap reached: US$ "
COST_WARNING_RATIO = 0.8
DEFAULT_ORCHESTRATOR_MODEL = "opus"
DISABLED_PUSH_URL = "push-disabled-by-loop://use-delivery.py"
PAUSE_REASON = "paused by the user in the panel; click Resume in the panel or run the command again"
PAUSE_NEXT = "resume when the user wants"
HEAVY_FOLDERS = ("node_modules", "bin", "obj", "dist")  # never copied into a worktree (they stay with --setup)
LAST_RUN_NAME = "last-run.json"  # written by close_loop in the primary repo; names the branch a new start re-attaches to
MIN_GIT_WORKTREE_VERSION = (2, 5)
# Command line of `claude -p`: what the loop never needs is refused by the CLI itself, not only by the guard hook.
# No allow-list on purpose: under --permission-mode auto it would only pre-approve, and a narrow one would stop a normal turn.
LOOP_DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "NotebookEdit", "Bash(ssh *)", "Bash(scp *)", "Bash(sftp *)"]
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'  # no MCP server in the headless turn (any MCP denial already stops the loop as risky)


def run(args, cwd=None, timeout=120):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git(cwd, *args):
    return run(["git", "-C", cwd, *args])


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


APP_OWNED_META_KEYS = ("delivery", "max_cost_usd")  # written by the app (start, raise-limit) in the worktree meta: the engine never drops them
DELIVERY_PENDING_NAME = "delivery-pending.json"


def save_meta(path, meta):
    """Saves meta.json keeping what the app wrote there meanwhile: the disk value of the app-owned keys wins over the in-memory copy."""
    on_disk = load_json(path)
    for key in APP_OWNED_META_KEYS:
        if key in on_disk:
            meta[key] = on_disk[key]
    save_json(path, meta)


def merge_pending_delivery(repo, meta):
    """The app wrote the delivery choices before the worktree existed (<repo>/.specs/project/loop/internal/delivery-pending.json): they
    become meta["delivery"] and the pending file is deleted."""
    path = os.path.join(loop_state.internal_dir(repo), DELIVERY_PENDING_NAME)
    pending = load_json(path)
    if isinstance(pending, dict) and pending:
        meta["delivery"] = dict(meta.get("delivery") or {}, **pending)
    try:
        os.remove(path)
    except OSError:
        pass


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WIN:
        _, out, _ = run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"])
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def kill_tree(pid):
    if IS_WIN:
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def worktree_of(repo):
    return os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")


def required_channels():
    """Mandatory notice channels: LOOP_NOTIFY_REQUIRED (comma-separated; empty or unset = none)."""
    notify.envload.load()
    return notify.envload.get_list("LOOP_NOTIFY_REQUIRED")


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


def pause_requested(work, turn):
    """PAUSE file (created by the panel): deletes it and returns the stop text; empty if there is no request. Spends no turn."""
    path = loop_state.loop_path(work, "PAUSE")
    if not os.path.exists(path):
        return ""
    try:
        os.remove(path)
    except OSError:
        pass
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    state.update(status="awaiting_user", turn=turn, reason=PAUSE_REASON, next=PAUSE_NEXT,
                 at=datetime.datetime.now().isoformat(timespec="seconds"))
    state.pop("options", None)
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    return PAUSE_REASON


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


def identity_problem(cwd, opts):
    """Git identity for the push. Any account works; to require a specific one use --git-user or LOOP_GIT_USER (app .env)."""
    _, user, _ = git(cwd, "config", "user.name")
    _, mail, _ = git(cwd, "config", "user.email")
    if not user or not mail:
        return "git user.name/user.email not configured (git config --global user.name ...)"
    wanted = opts.git_user or notify.envload.get("LOOP_GIT_USER")
    if wanted and user != wanted:
        return "git user.name is %r, expected %r (--git-user or LOOP_GIT_USER in the app .env)" % (user, wanted)
    return ""


def expand_plugin_vars(raw, plugin_root):
    """Expands ${CLAUDE_PLUGIN_ROOT} / $CLAUDE_PLUGIN_ROOT to the resolved plugin root (and ${HOME}-style variables)."""
    home = os.path.expanduser("~")
    for name in ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT"):
        raw = raw.replace(name, plugin_root)
    for name in ("${HOME}", "$HOME", "${USERPROFILE}"):
        raw = raw.replace(name, home)
    return os.path.expanduser(raw)


def check_guard_command(command, label, plugin_root, seen, found):
    """One hook command that calls guard.py: (a) interpreter on PATH, (b) target file exists on this machine."""
    match = re.match(r'\s*"?([^\s"]+)"?\s+(?:\\")?"?([^\s"\\]*guard\.py)', command)
    if not match:
        return
    interpreter, raw = match.group(1), match.group(2)
    if interpreter not in seen:
        seen.add(interpreter)
        if not shutil.which(interpreter):
            found.append("hooks call `%s`, which is not on this machine's PATH (macOS/Linux often only have python3: create an alias or adjust the hook command)" % interpreter)
    target = expand_plugin_vars(raw, plugin_root)
    if "$" in target:
        return  # another variable (e.g. CLAUDE_PROJECT_DIR): only Claude Code resolves it
    if not os.path.exists(target):
        found.append("%s points the guard at %s, which does not exist on this machine" % (label, raw))


def hook_path_problems():
    """The guard must be reachable from the plugin's hooks: (a) python interpreter on PATH, (b) <plugin>/hooks/hooks.json exists
    and its guard target exists (`${CLAUDE_PLUGIN_ROOT}` = the plugin that holds the orchestrator skill), (c) a guard target in
    the skills/orchestrator/SKILL.md frontmatter, when there is one, exists. Without a skill folder the guard check reports it."""
    skill = app_paths.locate_skill_dir()
    if not skill:
        return []
    plugin_root = skill_locator.plugin_root_of(skill)
    found, seen = [], set()
    hooks_file = os.path.join(plugin_root, "hooks", "hooks.json")
    if not os.path.isfile(hooks_file):
        found.append("hooks/hooks.json not found in the plugin root (%s): the agents run without the guard" % plugin_root)
    else:
        try:
            with open(hooks_file, encoding="utf-8") as handle:
                data = json.load(handle)
        except ValueError as error:
            found.append("hooks/hooks.json is not valid JSON: %s" % error)
            data = {}
        commands = [hook.get("command", "") for entries in (data.get("hooks") or {}).values() for entry in entries for hook in (entry.get("hooks") or [])]
        guards = [command for command in commands if "guard.py" in command]
        if not guards:
            found.append("hooks/hooks.json registers no command that calls guard.py")
        for command in guards:
            check_guard_command(command, "hooks/hooks.json", plugin_root, seen, found)
    text = read(os.path.join(skill, "SKILL.md"))
    frontmatter = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
    for match in re.finditer(r'command:\s*(.+)', frontmatter):
        if "guard.py" in match.group(1):
            check_guard_command(match.group(1).strip().strip("'"), "skills/orchestrator/SKILL.md", plugin_root, seen, found)
    return found


def environment_problems(opts, repo):
    """claude version, permission modes and git: what changes from machine to machine."""
    problems, warns = [], []
    binary = claude_binaries.resolve(opts.claude or "claude")
    if binary and not binary.endswith(".py"):
        code, out, err = run([binary, "--help"], timeout=60)
        text = out + err
        if "--permission-prompts" not in text:
            problems.append("this `claude` has no --permission-prompts (needs version >= 2.1.259); update Claude Code")
        if "--max-budget-usd" not in text:
            problems.append("this `claude` has no --max-budget-usd; update Claude Code")
        if opts.permission_mode == "auto" and '"auto"' not in text:
            problems.append("this `claude` does not list the `auto` mode; use --permission-mode acceptEdits")
        elif opts.permission_mode == "auto":
            warns.append("`auto` mode depends on the plan/account; if the first turn fails because of it, run with --permission-mode acceptEdits")
    code, out, _ = git(repo, "worktree", "list")
    if code != 0:
        problems.append("this git does not support `git worktree` (needs >= 2.5)")
    return problems, warns


# ---------------------------------------------------------------- preflight (--check and before every run)
def test_environment_problems(root, opts):
    """A roadmap with regression=yes (or --e2e) validates routes by running the real app. Without the test environment file
    (headers, users and the secret manager key of each one) the loop stops at the baseline. Find that out here, before spending a turn."""
    roadmap = read(os.path.join(root, ".specs", "project", "ROADMAP.md"))
    if not (re.search(r"\bregression=(yes|baseline)\b", roadmap) or opts.e2e):
        return []
    path = os.path.join(root, ".specs", "project", "TEST-ENVIRONMENT.md")
    if not os.path.exists(path):
        return ["the ROADMAP has regression=yes (or --e2e), but .specs/project/TEST-ENVIRONMENT.md is missing (template: the app's "
                "templates/TEST_ENVIRONMENT.md): without headers, users and secret manager keys the loop stops at the baseline"]
    text = read(path)
    pending = len(re.findall(r"FILL_IN", text))
    if pending:
        return [".specs/project/TEST-ENVIRONMENT.md still has %d FILL_IN field(s): complete it before running" % pending]
    fill_env_test_from_fallback(root)
    return credential_check.credential_problems(root, text)


def fill_env_test_from_fallback(root):
    """A project with TEST-ENVIRONMENT.md always gets `.specs/project/.env.test` (missing names only, from the machine-level
    TEST values). Never takes the preflight down; returns the names added."""
    try:
        return test_env_fallback.ensure_env_test(root)["added"]
    except Exception as error:  # a read-only folder or an odd .env must not turn the check into a crash
        print("[check] WARNING: could not prepare .specs/project/.env.test: %s" % error)
        return []


def test_environment_warnings(root, opts):
    """Test environment findings that do not stop the loop (sample data with no value)."""
    path = os.path.join(root, ".specs", "project", "TEST-ENVIRONMENT.md")
    if not os.path.exists(path):
        return []
    return credential_check.credential_warnings(root, read(path))


def test_environment_stop_reason(root, opts, problems):
    """Text of the stop notice when the test environment blocks the start: WHO asked for the tests, WHAT is missing and WHERE the
    values go (project .env.test first, then the machine defaults in ~/.loop-harness/.env). Empty when the problem is not that one."""
    if not problems:
        return ""
    features = loop_state.parse_roadmap(read(os.path.join(root, ".specs", "project", "ROADMAP.md")))[0]
    asked = [slug for slug, fields in features.items() if fields.get("regression") in ("yes", "baseline")]
    who = ("regression=yes on: " + ", ".join(asked[:MAX_NAMED_FEATURES])) if asked else "the loop was started with --e2e"
    return ("The loop did NOT start (nothing was spent): the real-app tests were requested (%s) but the test environment cannot run.\n"
            "Missing: %s\n"
            "Where: names go in .specs/project/TEST-ENVIRONMENT.md (names only); values go in .specs/project/.env.test of the project "
            "(define it in .env.test), or in the machine defaults in ~/.loop-harness/.env (generic test user = TEST_USER_1_*). The user's shared "
            "account (plugin shared/.env and ~/.claude/skills/_shared/.env) only counts after you accept the shared fallback in the "
            "Test environment tab (.specs/project/ACCESS.json). "
            "Fix it and start the loop again, or remove regression=yes from the ROADMAP." % (who, "; ".join(problems)))


def nothing_left_to_do(done, total, opts, state=None, tiebreak_possible=False):
    """All verified/blocked = nothing to do, EXCEPT when the user is answering a question (--option/--answer): the answer may
    reopen a quarantined feature (e.g. fix the spec and redo the gate). Without this exception the preflight refused the
    answer because the quarantined feature counted as closed."""
    will_auto_answer = bool(state) and state.get("status") == "awaiting_user" and getattr(opts, "auto_answer", True) and pick_auto_answer(state) is not None
    return bool(total) and done == total and not (opts.option or opts.answer) and not will_auto_answer and not tiebreak_possible


def approval_problems(root, answering=False):
    """The roadmap must have been approved by the user (ROADMAP-APPROVED.json). When answering a question (--answer/--option)
    the signature is not compared (the loop may legitimately change rows mid-run), but the approval file must exist: an
    answer never starts a loop on a roadmap nobody approved."""
    if answering:
        if os.path.isfile(loop_state.loop_path(root, loop_state.APPROVAL_NAME)):
            return []
        return ["roadmap never approved: an answer does not replace the approval (Approve roadmap button in the panel)"]
    approval = loop_state.roadmap_approval(root)
    if approval["approved"]:
        return []
    return ["roadmap not approved: %s (Approve roadmap button in the panel)" % approval["reason"]]


def repo_names_of(repos):
    """Repo names for the grammar check; None for a single-repo project (today's behaviour: no prefix validation)."""
    return [item.name for item in repos] if len(repos) > 1 else None


def roadmap_features(root):
    """{slug: fields} of the ROADMAP of `root` ({} when there is none or it is unreadable)."""
    path = os.path.join(root, ".specs", "project", "ROADMAP.md")
    return loop_state.parse_roadmap(read(path))[0] if os.path.exists(path) else {}


def worktree_repos(repos, features, only=None):
    """The repos that get a worktree (D4): the primary always, plus every NON-primary `edit` repo that a ROADMAP feature names in
    `paths=<repo>:`. `only` (explicit `--repos`) replaces the roadmap filter for the non-primary ones. Reference repos never."""
    primary = project_repos.primary_of(repos)
    changed = project_repos.repos_changed_by_roadmap(primary.name, features)
    return [item for item in project_repos.edit_repos(repos)
            if item.primary or (item.name in only if only else item.name in changed)]


def extra_repos_preflight(repos, opts, features=None):
    """Preflight of the NON-primary repos of a multi-repo project (the primary is checked by preflight itself). Every message names
    the repo. `features` (the parsed ROADMAP) limits the worktree checks to the edit repos a feature changes; None = every edit repo.
    A `reference` repo only needs its folder. Returns (problems, warnings); empty for a single-repo project. Creates nothing."""
    problems, warns = [], []
    targets = None if features is None else {item.name for item in worktree_repos(repos, features)}
    for item in repos:
        if item.primary:
            continue
        tag = "repo %s: " % item.name
        if item.role == project_repos.ROLE_REFERENCE:
            if not os.path.isdir(item.path):
                warns.append(tag + "reference folder %s is missing (moved or deleted); the loop will not be able to read it" % item.path)
            elif not readiness_checks.folder_has_map(item.path) and any(not name.startswith(".") for name in os.listdir(item.path)):
                warns.append("reference repo %s has no codebase map: agents will read the raw code; map it from Workspace" % item.name)
            continue
        if targets is not None and item.name not in targets:
            continue
        if not os.path.isdir(item.path):
            problems.append(tag + "folder %s is missing (moved or deleted); remove it from the project or restore it" % item.path)
            continue
        if git(item.path, "rev-parse", "--is-inside-work-tree")[1] != "true":
            problems.append(tag + "%s is not a git repository" % item.path)
            continue
        if not re.search(r"^Conformance:\s*(STANDARD|LEGACY)", read(os.path.join(item.path, "CLAUDE.md")), re.M):
            problems.append(tag + "its CLAUDE.md has no `Conformance: STANDARD|LEGACY` line (harness.py classify %s --write STANDARD|LEGACY)" % item.path)
        worktree = item.loop_worktree_path()
        if not os.path.isdir(worktree):
            if not git(item.path, "branch", "--show-current")[1]:
                problems.append(tag + "no current branch (detached HEAD)")
        if opts.push and not git(item.path, "remote", "get-url", "origin")[1]:
            warns.append(tag + "no `origin` remote: nowhere to push its feature branch")
    return problems, warns


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


CHECK_FIXES = {
    "missing-roadmap": "Generate and approve the roadmap in Planning",
    "uncommitted": "Commit these files (the worktree is born from the last commit)",
    "dotnet-restore": "Add `dotnet restore` as the Setup line in the ROADMAP header (planning)",
    "node-modules": "Add `npm ci` as the Setup line",
    "permission-mode": "If the first turn fails, use permission mode acceptEdits",
}


def check_item_kind(text):
    """RDY-01: the kind of a check line, from its text (the text itself is never changed)."""
    lowered = text.lower()
    if lowered.startswith("missing .specs/project/roadmap.md"):
        return "missing-roadmap"
    if lowered.startswith("uncommitted changes in "):
        return "uncommitted"
    if lowered.startswith(".net detected"):
        return "dotnet-restore"
    if lowered.startswith("node project"):
        return "node-modules"
    if "`auto` mode" in lowered or "auto mode depends" in lowered:
        return "permission-mode"
    return "other"


def check_items(problems, warns, repos, repo):
    """RDY-01: [{kind, repo, files, text, fix}] for the `--json` output of the check. The uncommitted text lines are replaced by the
    structured one-per-repo items; every other line keeps its text and gets a kind and a fix sentence."""
    items = []
    problem_texts = set(problems)
    for text in list(problems) + list(warns):
        kind = check_item_kind(text)
        if kind == "uncommitted":
            continue
        items.append({"kind": kind, "level": "problem" if text in problem_texts else "warning", "repo": None, "files": [], "text": text,
                      "fix": CHECK_FIXES.get(kind, "")})
    return [dict(item, level=item.get("level") or "warning") for item in readiness_checks.uncommitted_items(repos, repo)] + items


def claude_command_problems(opts):
    problems, warns = [], []
    if not claude_binaries.resolve(opts.claude or "claude"):
        problems.append("`%s` command not found on PATH" % (opts.claude or "claude"))
    for name in getattr(opts, "claude_fallback", None) or []:
        if not claude_binaries.resolve(name):
            warns.append("fallback command `%s` not found on PATH: the loop will not switch to it on a usage limit" % name)
    return problems, warns


def repo_branch_problems(repo, work):
    """Detached HEAD. The uncommitted changes are ONE block per repo (readiness_checks.uncommitted_warnings, RD-01), not repeated here."""
    problems = []
    if not git(repo, "branch", "--show-current")[1]:
        problems.append("repo has no current branch (detached HEAD)")
    return problems, []


def base_branch_warnings(repos, base_branch=""):
    """BR-01: a repo whose worktree is not created yet and has no local integration branch starts from its current branch: say which."""
    warns = []
    for item in project_repos.edit_repos(repos):
        if os.path.isdir(item.loop_worktree_path()) or not os.path.isdir(item.path) or git(item.path, "rev-parse", "--is-inside-work-tree")[1] != "true":
            continue
        base, warning = choose_base(item.path, base_branch)
        if base and warning:
            warns.append(("repo %s: " % item.name if len(repos) > 1 else "") + warning)
    return warns


def roadmap_problems(repo, root, repos, opts, setup_open=False):
    """ROADMAP.md exists, follows the grammar and still has something to do. `setup_open` (RD-02): a real earlier step (the installer, a
    missing Conformance, no claude) is still open, so "generate the roadmap" is not said yet: the first problem is that step."""
    problems = []
    roadmap_path = os.path.join(root, ".specs", "project", "ROADMAP.md")
    if not os.path.exists(roadmap_path):
        if setup_open:
            return problems, []
        problems.append("missing .specs/project/ROADMAP.md: generate and APPROVE the roadmap first (panel, or a normal /orchestrator conversation)")
        return problems, []
    grammar = loop_state.parse_roadmap(read(roadmap_path))
    problems += ["ROADMAP outside the grammar: " + item for item in grammar[1] + loop_state.check_fields(grammar[0], repo_names_of(repos))]
    score, total, done = roadmap_score(root)
    if not total:
        problems.append("ROADMAP.md has no feature line (`- [slug] status=...`)")
    elif nothing_left_to_do(done, total, opts, load_json(loop_state.state_file(root, "LOOP.json")),
                            tiebreak_possible=bool(gate_blocked_features(os.path.join(root, ".specs", "project")))
                            and getattr(opts, "auto_answer", True) and getattr(opts, "gate_tiebreaks", 1) > 0):
        problems.append("all %d features are already verified/blocked: nothing to do" % total)
    else:
        note = " (answering the loop's question: the answer may reopen a quarantined feature)" if done == total else ""
        print("[check] ROADMAP: %d features, %d closed, %d to do%s" % (total, done, total - done, note))
    return problems, []


def approval_problems_when_roadmap_exists(root, opts):
    if not os.path.exists(os.path.join(root, ".specs", "project", "ROADMAP.md")):
        return []
    return approval_problems(root, answering=bool(opts.option or opts.answer))


def root_conformance_problems(root):
    """The root CLAUDE.md must carry a `Conformance: STANDARD|LEGACY` line (a confirmed monorepo has it per subproject instead)."""
    if subprojects.monorepo_confirmed(root):
        return []
    if re.search(r"^Conformance:\s*(STANDARD|LEGACY)", read(os.path.join(root, "CLAUDE.md")), re.M):
        return []
    return ["CLAUDE.md has no `Conformance: STANDARD|LEGACY` line (harness.py classify <repo> --write STANDARD|LEGACY, or the Conformance button in the panel)"]


def rules_problems(root):
    problems = []
    rules = subprocess.run([sys.executable, os.path.join(HERE, "rules.py"), "test", root, "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rules.returncode != 0:
        problems.append("a project block rule stopped working (rules.py test): " + " ".join(rules.stdout.splitlines()[-4:])[:300])
    return problems, []


def dependency_audit_warnings(root):
    warns = []
    try:
        audit = audit_deps.audit(root)
        loose = [s for s in audit["suggestions"] if not s["cycle"]]
        if loose:
            warns.append("%d forgotten dependency(ies) between features (e.g. %s -> %s). Run audit_deps.py --write and decide; the orchestrator applies `deps=` before each wave."
                         % (len(loose), loose[0]["feature"], loose[0]["depends_on"]))
    except Exception as error:  # the audit never takes the preflight down
        warns.append("dependency audit failed: %s" % error)
    return warns


def guard_problems():
    problems = []
    guard = app_paths.locate_guard()
    if not guard:
        problems.append("the orchestrator skill's guard.py was not found (looked in %s): set LOOP_SKILL_DIR or run `harness.py install`"
                        % app_paths.describe_guard_search_paths())
    else:
        code = subprocess.run([sys.executable, guard, "--profile", "executor"], input='{"tool_name":"Bash","tool_input":{"command":"git push"}}', capture_output=True, text=True).returncode
        if code != 2:
            problems.append("guard.py did not block `git push` (code %s, expected 2): do not run unsupervised" % code)
    return problems, []


def cost_cap_warnings(opts):
    return [] if opts.max_cost else ["no total cost cap (--max-cost 0): the loop only stops on progress/error, not on spend"]


def notice_channel_problems(opts):
    """Mandatory notice channels present and configured; unknown or half-configured channels only warn."""
    problems, warns = [], []
    channels = notify.envload.get_list("LOOP_NOTIFY_CHANNELS") or ["console", "toast", "file"]
    if opts.allow_silent:
        warns.append("--allow-silent: mandatory notice turned off (for testing only)")
    else:
        for name in required_channels():
            if name not in channels:
                problems.append("mandatory notice channel missing from LOOP_NOTIFY_CHANNELS: %s (LOOP_NOTIFY_REQUIRED)" % name)
            elif notify.missing_config(name):
                problems.append("mandatory notice channel %s is missing %s in the app .env" % (name, ", ".join(notify.missing_config(name))))
    for name in channels:
        missing = notify.missing_config(name)
        if name not in notify.CHANNELS:
            warns.append("unknown notice channel in LOOP_NOTIFY_CHANNELS: %s" % name)
        elif missing:
            warns.append("channel %s is on but is missing %s in the app .env: it will be skipped" % (name, ", ".join(missing)))
    return problems, warns


def push_problems(repo, opts):
    """`--push`: identity and `origin`; a problem when the push was asked explicitly, else only a warning."""
    problems, warns = [], []
    if opts.push:
        explicit = getattr(opts, "push_explicit", False)
        problem = identity_problem(repo, opts)
        no_origin = not git(repo, "remote", "get-url", "origin")[1]
        for text in ([problem] if problem else []) + (["no `origin` remote: nowhere to push the feature branch"] if no_origin else []):
            (problems if explicit else warns).append(("--push: " if explicit else "the final push/PR will not happen: ") + text)
        if not shutil.which("gh"):
            warns.append("`gh` not found: the end of the loop pushes the branch but only returns the compare LINK (the PR is not created)")
    return problems, warns


def build_setup_warnings(repo, work, opts):
    """Node / .NET repos: what a fresh worktree is born without."""
    warns = []
    if os.path.isfile(os.path.join(repo, "package.json")) and not opts.setup and not os.path.isdir(work):
        warns.append("Node project: the worktree is born without node_modules. Use --setup \"npm ci\" (otherwise verify returns UNVERIFIED).")
    dotnet = find_dotnet(repo)
    if dotnet and not os.path.isdir(work):
        warns.append(".NET detected (%s): the worktree is born without bin/obj; `dotnet build` does an implicit restore (needs network or the NuGet cache). "
                     "The executor does not run `dotnet restore` nor `dotnet add package` (guard); to restore beforehand, use --setup with `dotnet restore`." % dotnet)
    if (dotnet or os.path.isfile(os.path.join(repo, "package.json"))) and not os.path.isfile(os.path.join(repo, ".gitignore")):
        warns.append("repo has no .gitignore: bin/obj/node_modules generated by the build dirty the worktree and the loop stops on `dirty tree`.")
    return warns


# ---------------------------------------------------------------- readiness (--check --planning)
def feature_path_repo_problems(repos, features):
    """Every feature path must belong to an `edit` repo (D4): a path in a `reference` or unlisted repo cannot be changed by the loop."""
    primary = project_repos.primary_of(repos)
    by_name = {item.name: item for item in repos}
    problems = []
    for slug, fields in features.items():
        seen = set()
        for entry in str(fields.get("paths") or "").split(","):
            entry = entry.strip()
            match = project_repos.REPO_PATH_PREFIX.match(entry) if entry else None
            name = match.group(1) if match else primary.name
            if not entry or name in seen:
                continue
            seen.add(name)
            target = by_name.get(name)
            if target is None:
                problems.append("feature %s: path `%s` is in repo `%s`, which is not listed in the project: add it as an edit repo or change the feature" % (slug, entry, name))
            elif target.role == project_repos.ROLE_REFERENCE:
                problems.append("feature %s: path `%s` is in the reference repo `%s` (read only): promote it to edit or change the feature" % (slug, entry, name))
    return problems


def worktree_readiness(repos, features, opts):
    """Can every edit repo a feature changes get its worktree? (folder is a git repo, current branch, a free branch name, no foreign folder)."""
    problems, warns = [], []
    for item in worktree_repos(repos, features):
        tag = "repo %s: " % item.name
        if not os.path.isdir(item.path):
            problems.append(tag + "folder %s is missing: no worktree can be created" % item.path)
            continue
        if git(item.path, "rev-parse", "--is-inside-work-tree")[1] != "true":
            problems.append(tag + "%s is not a git repository: no worktree can be created" % item.path)
            continue
        work = item.loop_worktree_path()
        if os.path.isdir(work):
            meta = load_json(loop_state.loop_path(work, "meta.json"))
            if not meta.get("branch") or git(work, "branch", "--show-current")[1] != meta.get("branch"):
                problems.append(tag + "folder %s already exists but is not this loop's worktree: remove or rename it" % work)
            continue
        if not git(item.path, "branch", "--show-current")[1]:
            problems.append(tag + "no current branch (detached HEAD): the worktree needs a branch to start from")
        if opts.branch and branch_exists(item.path, opts.branch):
            problems.append(tag + "branch `%s` already exists: pick another --branch name" % opts.branch)
    return problems, warns


def mentioned_mcp_servers(*texts):
    """Known MCP servers (mcp_access.SERVER_NAMES) that any of the texts names (`mcp-mysql-dev`, `mcp__grafana__...`, the word grafana)."""
    text = "\n".join(texts).lower()
    return [name for name in mcp_access.SERVER_NAMES if re.search(r"(?<![a-z0-9-])%s(?![a-z0-9-])" % re.escape(name), text)]


def claude_config_readable():
    try:
        with open(mcp_access.claude_config_path(), encoding="utf-8-sig") as handle:
            return isinstance(json.load(handle), dict)
    except (OSError, ValueError):
        return False


def mcp_readiness(root, roadmap_text):
    problems, warns = [], []
    environment_text = read(os.path.join(root, ".specs", "project", "TEST-ENVIRONMENT.md"))
    needed = mentioned_mcp_servers(environment_text, roadmap_text)
    configured = {row["server"] for row in mcp_access.available_servers()}
    missing = [name for name in needed if name not in configured]
    if missing:
        problems.append("MCP server(s) named by TEST-ENVIRONMENT.md or a feature are not configured in %s: %s (add them as user-scope MCP servers: `claude mcp add --scope user`)"
                        % (mcp_access.claude_config_path(), ", ".join(missing)))
    elif not needed and not claude_config_readable():
        warns.append("%s could not be read: the MCP servers (%s) will not be available to the loop" % (mcp_access.claude_config_path(), ", ".join(mcp_access.SERVER_NAMES)))
    return problems, warns


def known_test_command(path):
    """How an edit repo is built/tested, as far as the repo itself says: docs/codebase/TESTING.md or a project file. '' when unknown.
    A confirmed monorepo also counts the non-skipped subprojects (their own docs/codebase/TESTING.md or project files)."""
    known = readiness_checks.known_commands(path)["summary"]
    if known:
        return known
    entry = subprojects.load(path)["entry"]
    for project in subprojects.entry_projects(entry) if entry and entry.get("confirmed") is True else []:
        if project.get("skip") is not True:
            known = readiness_checks.known_commands(os.path.join(path, *subprojects.normalize(project["path"]).split("/")))["summary"]
            if known:
                return known
    return ""


def credential_readiness(root):
    """Names TEST-ENVIRONMENT.md mentions must resolve through the layers (project .env.test, machine .env, shared .env)."""
    path = os.path.join(root, ".specs", "project", "TEST-ENVIRONMENT.md")
    if not os.path.exists(path):
        return [], []
    text = read(path)
    return credential_check.credential_problems(root, text), credential_check.credential_warnings(root, text)


def planning_readiness(repo, root, repos, features, opts):
    """The extra checks of `--check --planning` (the rest of the preflight, guard included, already ran). Returns (problems, warnings)."""
    problems, warns = [], []
    problems += feature_path_repo_problems(repos, features)
    new_problems, new_warns = worktree_readiness(repos, features, opts)
    problems += new_problems
    warns += new_warns
    warns += readiness_checks.giant_preflight(root, features, repos)
    warns += readiness_checks.system_map_warning(root, features, repos)
    new_problems, new_warns = mcp_readiness(root, read(os.path.join(root, ".specs", "project", "ROADMAP.md")))
    problems += new_problems
    warns += new_warns
    new_problems, new_warns = credential_readiness(root)
    problems += [item for item in new_problems if item not in problems]
    warns += [item for item in new_warns if item not in warns]
    for item in worktree_repos(repos, features):
        if os.path.isdir(item.path) and not known_test_command(item.path):
            warns.append("repo %s: no build/test command known (no docs/codebase/TESTING.md, no package.json scripts or project file): the verifier cannot prove its features" % item.name)
    return problems, warns


def find_dotnet(repo, max_depth=3):
    """First .sln/.csproj up to `max_depth` folders below the root (ignores bin/obj/node_modules/.git/.specs). Empty if none."""
    return readiness_checks.find_dotnet(repo, max_depth)


# ---------------------------------------------------------------- worktree (script only)
def step(tag, kind, text):
    """One plain-text step line for the panel: `[loop] [<repo>] <kind>: <text>` (tag only in a multi-repo project, so a single-repo
    project keeps today's output). Kinds: check | warn | worktree-add | reattach | copy | setup | ready | reuse | error. Flushed at once."""
    print(("[loop] [%s] %s: %s" % (tag, kind, text)) if tag else ("[loop] %s: %s" % (kind, text)), flush=True)


def branch_exists(repo, branch):
    return bool(branch) and git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch)[0] == 0


def base_branch_of(opts):
    """The `--base-branch` option (the ROADMAP `Branch base`); "" when the options carry none."""
    return getattr(opts, "base_branch", "") or ""


def choose_base(repo, base_branch=""):
    """(base branch, warning) of a NEW worktree (BR-01): the base branch (`base_branch` = the ROADMAP `Branch base`, else LOOP_PR_BASE /
    LOOP_INTEGRATION_BRANCH, default `developer`) when it exists locally, else the folder's current branch with a warning naming both.
    ("", "") when the folder has no current branch."""
    integration = delivery.integration_branch(base_branch)
    if branch_exists(repo, integration):
        return integration, ""
    current = git(repo, "branch", "--show-current")[1]
    if not current:
        return "", ""
    return current, "the integration branch `%s` does not exist locally: the worktree starts from the current branch `%s`" % (integration, current)


def base_sha_of(repo, base):
    return git(repo, "rev-parse", "--short", "refs/heads/" + base)[1] if branch_exists(repo, base) else ""


SETUP_COMMIT_MESSAGE = "chore(setup): carry codebase maps and agent context"


def commit_carried_setup(work, carried, tag):
    """The carried setup files become ONE commit on the loop branch right away (`git add` of those paths only, the loop's git identity),
    so the worktree stays clean for a safe Finish. Gitignored files are not force-added: they stay engine state that Close tolerates."""
    tracked_candidates = [path for path in carried if git(work, "check-ignore", "-q", "--", path)[0] != 0]
    ignored = [path for path in carried if path not in tracked_candidates]
    tracked_candidates = [path for path in tracked_candidates if git(work, "status", "--porcelain", "--", path)[1].strip()]  # unchanged = nothing to commit
    if ignored:
        text = "carried setup files ignored by .gitignore (left uncommitted as engine state): " + ", ".join(ignored)
        step(tag, "warn", text) if tag else print("[loop] warn: %s" % text, flush=True)
    if not tracked_candidates:
        return False
    code, _, err = git(work, "add", "--", *tracked_candidates)
    if code == 0:
        code, _, err = git(work, "commit", "-q", "-m", SETUP_COMMIT_MESSAGE, "--", *tracked_candidates)
    if code != 0:
        text = "could not commit the carried setup files (%s); they stay uncommitted in the worktree" % (err or "git failed")[:200]
        step(tag, "warn", text) if tag else print("[loop] warn: %s" % text, flush=True)
        return False
    text = "committed %d carried setup file%s: %s" % (len(tracked_candidates), "" if len(tracked_candidates) == 1 else "s", SETUP_COMMIT_MESSAGE)
    step(tag, "commit", text) if tag else print("[loop] %s" % text, flush=True)
    return True


def carry_setup_into(repo, work, tag, entry=None, include_specs=True, also_commit=()):
    """BR-02: the uncommitted setup files of `repo` go into the new worktree (never over a file the branch has). Returns the paths copied."""
    listed = [item["path"] for item in setup_files.list_setup_files(repo, entry)]
    carried = setup_files.carry_setup_files(repo, work, entry, include_specs)
    if carried:
        text = "carried uncommitted setup files: " + ", ".join(carried)
        step(tag, "copy", text) if tag else print("[loop] %s" % text, flush=True)
    commit_carried_setup(work, list(carried) + [path for path in also_commit if path not in carried], tag)
    others = setup_files.list_other_changes(repo, listed)
    if others:
        text = "%d other changed file%s in your folder NOT carried: %s" % (len(others), "" if len(others) == 1 else "s", setup_files.shortened(others))
        step(tag, "warn", text) if tag else print("[loop] warn: %s" % text, flush=True)
    return carried


def last_run_entry(primary_repo, name):
    """What the last Close loop recorded for repo `name` ({"branch", "base", ...}) in the primary repo's last-run.json, or {}."""
    entry = (load_json(loop_state.loop_path(primary_repo, LAST_RUN_NAME)).get("repos") or {}).get(name)
    return entry if isinstance(entry, dict) else {}


def reattach_branch_of(primary_repo, name, repo=None):
    """The branch the next start re-attaches to for repo `name`: the one the last Close loop recorded when it still exists and that run was
    NOT finished (a wrap-up Finish marks `finished`: the next work starts on a new branch). "" when there is nothing to re-attach."""
    recorded = last_run_entry(primary_repo, name)
    if recorded.get("finished"):
        return ""
    branch = recorded.get("branch")
    return branch if branch_exists(repo or primary_repo, branch) else ""


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return "sha256:" + digest.hexdigest()


def copy_local_files(repo, work, names, tag):
    """Copies the ticked git-ignored files (REPOS.json `copy`) from the user's repo into the worktree. Files only, relative to the repo,
    never inside node_modules/bin/obj/dist. Never overwrites. Returns {name: sha256 of the SOURCE} (Close loop compares against it)."""
    hashes, skipped = {}, []
    for name in names:
        relative = str(name).replace("\\", "/").strip().strip("/")
        source, target = os.path.join(repo, relative), os.path.join(work, relative)
        inside = os.path.realpath(source).startswith(os.path.realpath(repo) + os.sep)
        heavy = bool(set(HEAVY_FOLDERS).intersection(relative.split("/")))
        if not relative or not inside or heavy or not os.path.isfile(source):
            skipped.append(relative)
            continue
        if not os.path.exists(target):
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
        hashes[relative] = file_sha256(source)
    if skipped:
        step(tag, "warn", "not copied (missing, a folder or a generated folder): " + ", ".join(skipped))
    return hashes


def git_version_problem(cwd):
    """Reason when git is older than 2.5 (no `git worktree`), else ""."""
    found = re.search(r"(\d+)\.(\d+)", git(cwd, "--version")[1])
    if not found:
        return "could not read the git version"
    version = (int(found.group(1)), int(found.group(2)))
    return "" if version >= MIN_GIT_WORKTREE_VERSION else "git %d.%d is too old for worktrees (needs 2.5 or newer)" % version


def worktree_never_ran(work):
    """True while no loop ran in the worktree: no LOOP.json, no turn record, no lock and `turns` 0 in meta.json (a bare worktree)."""
    loop_folder = os.path.dirname(os.path.dirname(loop_state.loop_path(work, "meta.json")))
    if os.path.exists(os.path.join(loop_folder, "json", "LOOP.json")) or os.path.exists(os.path.join(loop_folder, "internal", "LOOP.lock")):
        return False
    if glob.glob(os.path.join(loop_folder, "internal", "turns", "*.json")):
        return False
    turns = load_json(loop_state.loop_path(work, "meta.json")).get("turns")
    return not (isinstance(turns, int) and not isinstance(turns, bool) and turns > 0)


def refresh_unused_worktree(repo, work, meta_path):
    """A reused worktree that never ran a loop gets `.specs`, `CLAUDE.md` and `.claude` copied again from the primary repo: the copy made at
    creation can be stale (the plan kept changing in the primary while the worktree waited). What the worktree itself holds under
    `.specs/project/loop` (meta.json, a PAUSE, MONITOR.json, LOOP-CMD.json, the relaunch log...) is put back over the fresh copy; only the roadmap
    approval always follows the primary (an approval reset there must not survive here)."""
    if not worktree_never_ran(work):
        return
    loop_folder = os.path.dirname(os.path.dirname(meta_path))
    keep_folder = tempfile.mkdtemp(prefix="loop-refresh-")
    kept = os.path.join(keep_folder, "loop")
    try:
        if os.path.isdir(loop_folder):
            shutil.copytree(loop_folder, kept)
        refreshed = []
        for name in (".specs", "CLAUDE.md", ".claude"):
            source, target = os.path.join(repo, name), os.path.join(work, name)
            if not os.path.exists(source):
                continue
            if os.path.isdir(target):
                shutil.rmtree(target, ignore_errors=True)
            elif os.path.exists(target):
                os.remove(target)
            shutil.copytree(source, target) if os.path.isdir(source) else shutil.copy2(source, target)
            refreshed.append(name)
        loop_state.migrate_layout(work)
        if os.path.isdir(kept):
            shutil.copytree(kept, loop_folder, dirs_exist_ok=True, ignore=shutil.ignore_patterns("ROADMAP-APPROVED.json"))
        if refreshed:
            print("[loop] refreshed from the repo (the worktree never ran a loop): %s" % ", ".join(refreshed), flush=True)
    finally:
        shutil.rmtree(keep_folder, ignore_errors=True)


SYNCED_INVENTORY_RELATIVE = os.path.join(".specs", "project", "loop", "internal", "inventory")
SYNCED_SUBPROJECTS_RELATIVE = os.path.join(".specs", "project", "SUBPROJECTS.json")


def sync_untracked_map_files(repo, work):
    """Makes the git-ignored code inventory (`.specs/project/loop/internal/inventory/*.json`) and `SUBPROJECTS.json` visible from the worktree
    (monorepo-subprojects-map-and-conformance step 13). The inventory is written in the primary repo, usually AFTER the worktree was created
    or in a worktree that already ran (so the whole-`.specs` copy never carries it): each inventory file is copied when missing in the worktree
    or older than the repo's copy; `SUBPROJECTS.json` only when missing (a tracked copy is never overwritten, the tree stays clean).
    Returns the relative paths copied."""
    copied = []
    source_folder = os.path.join(repo, SYNCED_INVENTORY_RELATIVE)
    if os.path.isdir(source_folder):
        for name in sorted(os.listdir(source_folder)):
            source, target = os.path.join(source_folder, name), os.path.join(work, SYNCED_INVENTORY_RELATIVE, name)
            if not name.lower().endswith(".json") or not os.path.isfile(source):
                continue
            if os.path.isfile(target) and os.path.getmtime(target) >= os.path.getmtime(source):
                continue
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(source, target)
            copied.append(os.path.join(SYNCED_INVENTORY_RELATIVE, name))
    source, target = os.path.join(repo, SYNCED_SUBPROJECTS_RELATIVE), os.path.join(work, SYNCED_SUBPROJECTS_RELATIVE)
    if os.path.isfile(source) and not os.path.exists(target):
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        copied.append(SYNCED_SUBPROJECTS_RELATIVE)
    return copied


def resolve_setup(item, cli_setup=""):
    """(command, auto): the stored setup always wins, then the CLI one, else what the repo's manifests say (nothing runs to detect)."""
    stored = item.setup or cli_setup
    if stored:
        return stored, False
    detected = detect_setup.detect_setup(item.path)
    return detected or "", bool(detected)


def resolve_copy(item):
    """Files to copy into the worktree: the stored list when configured (`[]` = none), else the suggested git-ignored local config."""
    if item.copy_configured or item.copy:
        return list(item.copy)
    if not os.path.isdir(item.path):
        return []
    code, output, _ = git(item.path, *detect_setup.IGNORED_FILES_ARGUMENTS)
    return [entry["path"] for entry in detect_setup.suggest_copy_files(item.path, output if code == 0 else "")]


def ensure_worktree(repo, opts, tag="", copy_files=(), copy_hashes=None, setup=None, setup_auto=False):
    """Creates the sibling worktree and the loop branch ONCE. Reuses it if it exists. Never recreates it every turn.
    When a previous Close loop recorded a branch that still exists (last-run.json) and the folder is gone, the worktree is re-attached
    to that branch instead of creating a new one (a new branch only with --branch). `tag` = the repo name in a multi-repo project (step
    lines get the `[<repo>]` tag); `copy_files`/`copy_hashes` = the ticked git-ignored files and where their hashes are returned;
    `setup` = the one-time command (None = the CLI --setup)."""
    setup = opts.setup if setup is None else setup
    work = worktree_of(repo)
    meta_path = loop_state.loop_path(work, "meta.json")
    if os.path.isdir(work):
        loop_state.migrate_layout(work)  # worktree created with an older layout
        meta = load_json(meta_path)
        _, current, _ = git(work, "branch", "--show-current")
        if not meta.get("branch") or current != meta["branch"]:
            if tag:
                step(tag, "error", "folder %s already exists but is not this loop's worktree (current branch: %s)" % (work, current))
            sys.exit("folder %s already exists but is not this loop's worktree (current branch: %s). Remove or rename it and run again." % (work, current))
        if not opts.dry_run:
            refresh_unused_worktree(repo, work, meta_path)
            synced = sync_untracked_map_files(repo, work)
            if synced:
                print("[loop] copied from the repo into the worktree: %s" % ", ".join(path.replace(os.sep, "/") for path in synced), flush=True)
        if tag:
            step(tag, "reuse", "worktree %s on branch %s" % (work, meta["branch"]))
            step(tag, "ready", "path=%s branch=%s base=%s" % (work, meta["branch"], meta.get("base", "")))
        else:
            print("[loop] reusing worktree %s on branch %s" % (work, meta["branch"]))
        return work, meta
    if tag:
        problem = git_version_problem(repo)
        if problem:
            step(tag, "error", problem)
            sys.exit("repo %s: %s" % (tag, problem))
    code, current_branch, err = git(repo, "branch", "--show-current")
    if code != 0 or not current_branch:
        if tag:
            step(tag, "error", "repo has no current branch (detached HEAD?): %s" % err)
        sys.exit("repo has no current branch (detached HEAD?): %s" % err)
    base, base_warning = choose_base(repo, base_branch_of(opts))
    if tag:
        step(tag, "check", "git ok; current branch %s; the worktree starts from %s" % (current_branch, base))
    if base_warning:
        step(tag, "warn", base_warning) if tag else print("[loop] warn: %s" % base_warning, flush=True)
    recorded_name = tag or project_repos.short_name_of(repo)
    recorded = last_run_entry(repo, recorded_name)
    reattach = "" if opts.branch else reattach_branch_of(repo, recorded_name)
    branch = reattach or opts.branch or "loop/" + datetime.datetime.now().strftime("%Y%m%d-%H%M")
    if reattach:
        base = recorded.get("base") or base
    if opts.dry_run:
        print("[dry-run] git worktree add %s%s %s" % ("" if reattach else "-b %s " % branch, work, reattach or base))
        return work, {"branch": branch, "base": base, "worktree": work, "copied": []}
    if reattach:
        git(repo, "worktree", "prune")  # the removed folder may still be registered against the branch
        code, _, err = git(repo, "worktree", "add", work, branch)
        if code != 0:
            if tag:
                step(tag, "error", "failed to re-attach worktree: " + err)
            sys.exit("failed to re-attach worktree to %s: %s" % (branch, err))
        step(tag, "reattach", "%s (existing branch recorded by the last Close loop; no new branch)" % branch)
    else:
        if tag:
            step(tag, "worktree-add", "git worktree add -b %s %s %s" % (branch, work, base))
        code, _, err = git(repo, "worktree", "add", "-b", branch, work, base)
        if code != 0:
            if tag:
                step(tag, "error", "failed to create worktree: " + err)
            sys.exit("failed to create worktree: " + err)
    copied = []
    for name in (".specs", "CLAUDE.md", ".claude"):
        source, target = os.path.join(repo, name), os.path.join(work, name)
        if os.path.exists(source) and not os.path.exists(target):
            shutil.copytree(source, target) if os.path.isdir(source) else shutil.copy2(source, target)
            copied.append(name)
    # the root CLAUDE.md copied whole (not "carried") is a setup file too when it holds an app block: it joins the setup commit
    copied_claude = ["CLAUDE.md"] if "CLAUDE.md" in copied and setup_files.has_app_block(work, "CLAUDE.md") else []
    carried = carry_setup_into(repo, work, tag, subprojects.load(repo)["entry"], also_commit=copied_claude)
    loop_state.migrate_layout(work)  # .specs copied from a repo still in an older layout
    sync_untracked_map_files(repo, work)  # a `.specs` that already existed in the worktree (tracked) still gets the ignored inventory
    hashes = copy_local_files(repo, work, copy_files, tag) if copy_files else {}
    if copy_hashes is not None:
        copy_hashes.update(hashes)
    meta = {
        "branch": branch, "base": base, "base_sha": base_sha_of(repo, base), "worktree": work, "repo": repo, "copied": copied,
        "carried_setup_files": carried,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"), "turns": 0, "cost_usd": 0.0,
        "note": "worktree used ONLY by run_loop.py; the orchestrator commits here and never pushes",
    }
    merge_pending_delivery(repo, meta)
    save_meta(meta_path, meta)
    if tag:
        step(tag, "copy", ", ".join(copied + list(hashes)) or "nothing")
    else:
        print("[loop] worktree created: %s (branch %s, base %s; copied from the repo: %s)" % (work, branch, base, ", ".join(copied) or "nothing"))
    if setup:
        note = " (auto-detected)" if setup_auto else ""
        step(tag, "setup", setup + note) if tag else print("[loop] one-time setup: %s" % setup)
        code = subprocess.run(setup, cwd=work, shell=True).returncode
        if code != 0:
            if tag:
                step(tag, "error", "setup failed (code %s)" % code)
            sys.exit("%ssetup failed (code %s); fix it and run again (the worktree already exists)" % ("repo %s: " % tag if tag else "", code))
    if tag:
        step(tag, "ready", "path=%s branch=%s base=%s" % (work, branch, base))
    return work, meta


def ensure_extra_worktree(item, opts, branch, known):
    """Worktree of a NON-primary repo, on the same loop branch as the primary. `known` = what meta.repos recorded on a previous run.
    Born from the user's folder; only the ticked `copy` files are copied (no .specs, CLAUDE.md or .claude: the loop runs in the primary).
    Returns the meta.repos entry. Every step is a `[loop] [<repo>] <kind>: <text>` line."""
    name, work = item.name, item.loop_worktree_path()
    entry = {"path": item.path, "worktree": work, "branch": branch, "base": known.get("base", ""), "primary": False, "copied": dict(known.get("copied") or {})}
    if os.path.isdir(work):
        _, current, _ = git(work, "branch", "--show-current")
        if current != branch:
            step(name, "error", "folder %s already exists but is not this loop's worktree (current branch: %s, expected %s)" % (work, current, branch))
            sys.exit("repo %s: folder %s already exists but is not this loop's worktree (current branch: %s). Remove or rename it and run again." % (name, work, current))
        entry["base"] = entry["base"] or git(item.path, "branch", "--show-current")[1]
        step(name, "reuse", "worktree %s on branch %s" % (work, branch))
        step(name, "ready", "path=%s branch=%s base=%s" % (work, branch, entry["base"]))
        return entry
    problem = git_version_problem(item.path)
    if problem:
        step(name, "error", problem)
        sys.exit("repo %s: %s" % (name, problem))
    code, current_branch, err = git(item.path, "branch", "--show-current")
    if code != 0 or not current_branch:
        step(name, "error", "repo has no current branch (detached HEAD?): %s" % err)
        sys.exit("repo %s has no current branch (detached HEAD?): %s" % (name, err))
    base, base_warning = choose_base(item.path, base_branch_of(opts))
    entry["base"] = base
    step(name, "check", "git ok; current branch %s; the worktree starts from %s" % (current_branch, base))
    if base_warning:
        step(name, "warn", base_warning)
    recorded = last_run_entry(opts.repo_path, name)
    reattach = branch_exists(item.path, branch) and recorded.get("branch") == branch and not recorded.get("finished")
    if reattach:
        entry["base"] = recorded.get("base") or base
    if opts.dry_run:
        print("[dry-run] [%s] git worktree add %s%s %s" % (name, "" if reattach else "-b %s " % branch, work, branch if reattach else base))
        return entry
    if reattach:
        git(item.path, "worktree", "prune")
        code, _, err = git(item.path, "worktree", "add", work, branch)
        if code == 0:
            step(name, "reattach", "%s (existing branch recorded by the last Close loop; no new branch)" % branch)
    else:
        step(name, "worktree-add", "git worktree add -b %s %s %s" % (branch, work, base))
        code, _, err = git(item.path, "worktree", "add", "-b", branch, work, base)
    if code != 0:
        step(name, "error", "failed to create worktree: " + err)
        sys.exit("repo %s: failed to create worktree: %s" % (name, err))
    entry["copied"] = copy_local_files(item.path, work, resolve_copy(item), name)
    entry["carried_setup_files"] = carry_setup_into(item.path, work, name, subprojects.load(opts.repo_path, name)["entry"], include_specs=False)
    entry["base_sha"] = base_sha_of(item.path, entry["base"])
    step(name, "copy", ", ".join(entry["copied"]) or "nothing")
    setup, auto = resolve_setup(item)
    if setup:
        step(name, "setup", setup + (" (auto-detected)" if auto else ""))
        code = subprocess.run(setup, cwd=work, shell=True).returncode
        if code != 0:
            step(name, "error", "setup failed (code %s)" % code)
            sys.exit("repo %s: setup failed (code %s); fix it and run again (the worktree already exists)" % (name, code))
    elif opts.setup:
        step(name, "warn", "--setup (%s) runs only in the primary repo; set this repo's own setup in the repository manager" % opts.setup)
    step(name, "ready", "path=%s branch=%s base=%s" % (work, branch, entry["base"]))
    return entry


def ensure_worktrees(repo, opts):
    """One worktree for the primary plus one per `edit` repo that a ROADMAP feature changes (`paths=<repo>:`), all on the same loop branch;
    returns (primary worktree, meta). `reference` repos and edit repos nothing changes get no worktree (D4). A project whose only worktree
    is the primary's goes through ensure_worktree alone: same lines, same meta.json as before. Otherwise `meta["repos"] = {name: {path,
    worktree, branch, base, primary, copied}}`; the top-level fields still describe the primary."""
    repos = project_repos.load_repos(repo)
    only = getattr(opts, "only_repos", None)
    work_now = worktree_of(repo)
    features = roadmap_features(work_now if os.path.isdir(work_now) else repo)
    targets = {item.name for item in worktree_repos(repos, features, only)}
    if len(targets) == 1:
        return ensure_worktree(repo, opts)
    primary = project_repos.primary_of(repos)
    hashes = {}
    primary_setup, primary_auto = resolve_setup(primary, opts.setup)
    work, meta = ensure_worktree(repo, opts, tag=primary.name, copy_files=resolve_copy(primary), copy_hashes=hashes,
                                setup=primary_setup, setup_auto=primary_auto)
    previous = meta.get("repos") or {}
    entries = {primary.name: {"path": repo, "worktree": work, "branch": meta["branch"], "base": meta.get("base", ""), "primary": True,
                              "copied": hashes or dict((previous.get(primary.name) or {}).get("copied") or {})}}
    opts.repo_path = repo
    for item in repos:
        if item.primary:
            continue
        if item.name not in targets:  # a reference repo, an edit repo no feature changes, or one left out by --repos: keep what was recorded
            if previous.get(item.name):
                entries[item.name] = previous[item.name]
            continue
        entries[item.name] = ensure_extra_worktree(item, opts, meta["branch"], previous.get(item.name) or {})
    meta["repos"] = entries
    if not opts.dry_run:
        save_meta(loop_state.loop_path(work, "meta.json"), meta)
    return work, meta


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


def loop_worktrees(work, meta):
    """[(repo name, worktree path)] the loop works in, primary first. A single-repo project (no meta.repos) = [("", work)]."""
    entries = meta.get("repos") or {}
    if not entries:
        return [("", work)]
    return [(name, entry.get("worktree") or work) for name, entry in sorted(entries.items(), key=lambda pair: not pair[1].get("primary"))]


# ---------------------------------------------------------------- state
def roadmap_score(work):
    score, total, verified = 0, 0, 0
    for line in read(os.path.join(work, ".specs", "project", "ROADMAP.md")).splitlines():
        match = re.match(r"^- \[[^\]]+\]\s+.*?status=(\w+)", line.strip())
        if match:
            total += 1
            status = match.group(1)
            score += RANK.get(status, 0)
            verified += status in ("verified", "blocked")
    return score, total, verified


def fingerprint(work, meta=None):
    _, head, _ = git(work, "rev-parse", "HEAD")
    head += "".join(value for name, value in sorted((heads_of(work, meta) or {}).items()))  # a commit in any worktree is a change
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md")) + read(os.path.join(work, ".specs", "project", "STATE.md")) + head
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def heads_of(work, meta):
    """{repo name: HEAD of its worktree} for a multi-repo project; None for a single-repo one (its snapshots stay as they were)."""
    if not (meta or {}).get("repos"):
        return None
    return {name: git(path, "rev-parse", "HEAD")[1] for name, path in loop_worktrees(work, meta)}


def count_new_commits(prev, work, meta, head_now):
    """Commits made since the previous turn's snapshot, summed over EVERY worktree of the project (a commit in any listed one counts)."""
    if not prev:
        return 0
    _, count, _ = git(work, "rev-list", "--count", "%s..%s" % (prev["head"], head_now))
    total = int(count) if count.isdigit() else 0
    before = prev.get("heads") or {}
    for name, path in loop_worktrees(work, meta)[1:]:  # the primary is already counted above
        if before.get(name):
            _, extra, _ = git(path, "rev-list", "--count", "%s..HEAD" % before[name])
            total += int(extra) if extra.isdigit() else 0
    return total


def dirty_worktrees(work, meta):
    """[(repo name, rows)] of every worktree with changes outside .specs/. The name is "" for a single-repo project."""
    dirty = []
    for name, path in loop_worktrees(work, meta):
        ignore = set(meta.get("copied", []))
        ignore.update((meta.get("repos", {}).get(name) or {}).get("copied") or {})
        ignore.update(meta.get("carried_setup_files") or [])  # BR-02: carried setup files wait for the first checkpoint commit, they are not leftovers
        ignore.update((meta.get("repos", {}).get(name) or {}).get("carried_setup_files") or [])
        if meta.get("system_md_uncommitted") and os.path.normpath(path) == os.path.normpath(work):
            ignore.add(SYSTEM_DOCUMENT)  # the engine's contract merge left it for the next checkpoint commit
        rows = dirty_outside_specs(path, ignore)
        if rows:
            dirty.append((name, rows))
    return dirty


def dirty_outside_specs(work, ignore):
    _, out, _ = git(work, "status", "--porcelain")
    rows = []
    for line in out.splitlines():
        path = row_path(line)
        if path.startswith(".specs/") or path.rstrip("/") in ignore:
            continue
        if path.endswith("/") and line.startswith("??"):  # an untracked folder is one collapsed row: expand it when an ignored file may sit inside
            inside = [name for name in ignore if name.startswith(path)]
            if inside:
                _, listing, _ = git(work, "status", "--porcelain", "-uall", "--", path)
                rows.extend(row for row in listing.splitlines() if row_path(row) not in ignore)
                continue
        rows.append(line)
    return rows


IN_FLIGHT_STATUSES = ("speccing", "approved", "building")  # features whose executors may leave files behind
PORCELAIN_ROW = re.compile(r"^\s*[A-Z?!]{1,2}\s+(.*)$")  # the helper `run` strips the output, so the first row may have lost its leading space


def row_path(row):
    """Path of a `git status --porcelain` row (the new name of a rename)."""
    match = PORCELAIN_ROW.match(row)
    path = (match.group(1) if match else row).strip().strip('"').replace("\\", "/")
    return path.split(" -> ")[-1].strip('"')


def feature_scope_rows(rows, repo_name, features, primary_name):
    """(inside, outside): the dirty rows that fall under the `paths=` of an in-flight feature, and the rest."""
    scopes = []
    for feature in features.values():
        if feature.get("status") not in IN_FLIGHT_STATUSES:
            continue
        for repo, path in loop_state.feature_repo_paths(feature, primary_name):
            if not repo_name or not repo or repo == repo_name:
                scopes.append(path.strip("/").replace("\\", "/"))
    inside, outside = [], []
    for row in rows:
        path = row_path(row).rstrip("/")
        covered = any(scope and (path == scope or path.startswith(scope + "/") or scope.startswith(path + "/")) for scope in scopes)
        (inside if covered else outside).append(row)
    return inside, outside


def commit_leftovers(path, rows, message):
    """Commits exactly these rows on the loop branch (the executors never run git, so their work is still loose after the turn)."""
    names = [row_path(row) for row in rows]
    code, _, err = git(path, "add", "-A", "--", *names)
    if code != 0:
        return False, "git add failed: " + err[-200:]
    code, _, err = git(path, "commit", "-m", message, "--no-verify")
    return (True, "") if code == 0 else (False, "git commit failed: " + err[-200:])


def discard_leftovers(path, rows):
    """Throws away exactly these rows: tracked files go back to HEAD, untracked ones are deleted."""
    for row in rows:
        name = row_path(row)
        if row.startswith("??"):
            full = os.path.join(path, name)
            if os.path.isdir(full):
                shutil.rmtree(full, ignore_errors=True)
            elif os.path.exists(full):
                os.remove(full)
        else:
            git(path, "checkout", "HEAD", "--", name)
    return True, ""


def settle_dirty_worktrees(work, meta, turn, opts):
    """Runs before every turn. Returns the [(repo name, rows)] that are still dirty (the loop stops on them).
    Leftovers of the previous turn that sit under the paths of an in-flight feature are committed as a checkpoint (the work is the
    feature's own); anything else stays and stops the loop. `--leftovers commit|discard` (panel recovery) decides about ALL of it."""
    mode = getattr(opts, "leftovers", "") or ""
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    still_dirty = []
    for name, rows in dirty_worktrees(work, meta):
        path = dict(loop_worktrees(work, meta)).get(name, work)
        if mode == "discard":
            discard_leftovers(path, rows)
            print("[loop] --leftovers discard: %d leftover file(s) discarded%s" % (len(rows), " in repo " + name if name else ""))
            continue
        inside, outside = (rows, []) if mode == "commit" else feature_scope_rows(rows, name, features, getattr(opts, "primary_name", ""))
        if inside:
            saved, problem = commit_leftovers(path, inside, "chore(loop): checkpoint of work left uncommitted by turn %d" % (turn - 1))
            if saved:
                print("[loop] checkpoint: %d file(s) left by turn %d committed on the loop branch%s" % (len(inside), turn - 1, " (repo " + name + ")" if name else ""))
                meta.setdefault("checkpoints", []).append({"turn": turn - 1, "files": len(inside), "repo": name})
            else:
                outside = rows
                print("[loop] checkpoint failed: " + problem)
        if outside:
            still_dirty.append((name, outside))
    return still_dirty


def write_answer(work, text):
    """Records the user's answer in .specs/project/loop/ANSWERS.md. The orchestrator's next turn reads it first and applies it."""
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    if state.get("status") != "awaiting_user":
        sys.exit("--answer only applies when the loop stopped at awaiting_user (current status: %s)" % (state.get("status") or "no LOOP.json"))
    path = loop_state.state_file(work, "ANSWERS.md")
    block = "\n## Answer %s [pending]\nQuestion: %s\nUser answer: %s\n" % (
        datetime.datetime.now().isoformat(timespec="seconds"), str(state.get("reason") or "").strip()[:1500], text.strip())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
    print("[loop] answer recorded in %s (the next turn applies it and carries on)" % path)


def rewind(repo, opts):
    """Time travel: restores the state (ROADMAP/STATE/LOOP/ANSWERS/RULES) and the loop branch HEAD to right after turn N."""
    work = worktree_of(repo)
    if os.path.isdir(work):
        loop_state.migrate_layout(work)
    meta_path = loop_state.loop_path(work, "meta.json")
    meta = load_json(meta_path)
    if not os.path.isdir(work) or not meta.get("branch"):
        sys.exit("there is no loop worktree for %s; nothing to rewind" % repo)
    lock = loop_state.loop_path(work, "LOOP.lock")
    if os.path.exists(lock) and pid_alive(int(read(lock).strip() or 0)):
        sys.exit("a loop is running in this worktree; stop it before rewinding")
    snap = loop_state.load_snapshot(work, opts.rewind)
    if not snap:
        snapshots = loop_state.loop_path(work, "snapshots")
        have = sorted(os.listdir(snapshots)) if os.path.isdir(snapshots) else []
        sys.exit("no snapshot for turn %d. Available: %s" % (opts.rewind, ", ".join(have) or "none"))
    _, current, _ = git(work, "rev-parse", "HEAD")
    _, lost, _ = git(work, "log", "--oneline", "--no-decorate", "%s..%s" % (snap["head"], current))
    print("[rewind] go back to the state after turn %d (HEAD %s; today %s)" % (opts.rewind, snap["head"][:8], current[:8]))
    print("[rewind] commits leaving branch %s (they stay in the git reflog):\n%s" % (meta["branch"], lost or "  (none)"))
    if not opts.yes:
        print("[rewind] nothing was changed. Confirm with --yes")
        return 0
    if dirty_outside_specs(work, set(meta.get("copied", []))):
        sys.exit("dirty tree outside .specs/ in the worktree: resolve it before rewinding")
    code, _, err = git(work, "reset", "--hard", snap["head"])
    if code != 0:
        sys.exit("git reset failed: " + err)
    loop_state.restore(work, opts.rewind)
    meta.update({"turns": opts.rewind, "stopped": "rewind", "reason": "state restored to after turn %d" % opts.rewind})
    save_meta(meta_path, meta)
    print("[rewind] done. Run the loop command again to continue from turn %d." % (opts.rewind + 1))
    return 0


# ---------------------------------------------------------------- lock
def acquire_lock(work):
    path = loop_state.loop_path(work, "LOOP.lock")
    if os.path.exists(path):
        try:
            other = int(read(path).strip() or 0)
        except ValueError:
            other = 0
        if pid_alive(other):
            sys.exit("a loop is already running in this worktree (PID %s). Wait or stop it." % other)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    return path


# ---------------------------------------------------------------- one turn
def plan_flags(work, wave, primary_name=""):
    """Deterministic scheduler: the script computes the turn's lanes (loop_state.next_work); the orchestrator executes them.
    A lane is ONE feature at ITS OWN step (`--lanes slug:step,...`; step = spec|gate|execute|recover): every lane gets its own agent at every
    step and advances by itself, so no feature waits for another one to reach the same phase. `--phase` is the common step when all the lanes
    are at the same one (and `lanes` when they differ); `--ready` lists every lane's slug (kept for older prompts).
    With no readable ROADMAP or nothing to dispatch, returns empty and the orchestrator decides as before."""
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    if not features:
        return ""
    plan = loop_state.next_work(features, wave, headless=True, primary_name=primary_name)
    flags = " --phase %s" % plan["phase"]
    if plan["lanes"]:
        flags += " --lanes %s" % ",".join("%s:%s" % (lane["slug"], lane["step"]) for lane in plan["lanes"])
        flags += " --ready %s" % ",".join(plan["slugs"])
    if plan["stuck_by_blocked"]:
        flags += " --stuck %s" % ",".join(plan["stuck_by_blocked"])
    return flags


def audit_before_turn(work):
    """Before choosing the wave: writes forgotten `deps=` into the ROADMAP (audit_deps.py --apply). It only increases serialization; the script does it
    because it picks `--ready` before claude runs. Cycle or invalid ROADMAP: nothing is written. Returns ["feature deps+=other", ...]."""
    try:
        done = subprocess.run([sys.executable, os.path.join(HERE, "audit_deps.py"), work, "--apply"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[len("APPLIED: "):] for line in done.stdout.splitlines() if line.startswith("APPLIED: ")]


def e2e_unmarked(specs):
    """With --e2e, `done` requires the final phase result on the line of EACH verified feature: e2e=ok, or e2e=not-covered when
    there is no surface to test (pure lib). `e2e=failed` with `done` is a contradiction. Returns the list of problems."""
    features = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    problems = []
    for slug, f in features.items():
        if f["status"] == "verified" and not f.get("e2e"):
            problems.append("%s without e2e=" % slug)
        elif f.get("e2e") == "failed" and f["status"] != "blocked":
            problems.append("%s with e2e=failed" % slug)
    return problems


SYSTEM_DOCUMENT = system_map.SYSTEM_RELATIVE_PATH.replace("\\", "/")


def missing_backend_contract(work, slug, feature):
    """True for a back-end (kind back/both) feature whose paths fall in a subproject of a confirmed monorepo and that has no contract.md."""
    if str(feature.get("kind") or "").lower() not in ("back", "both"):
        return False
    if os.path.isfile(system_map.contract_path(work, slug)):
        return False
    for path in str(feature.get("paths") or "").split(","):
        owner = subprojects.owner_of(work, path.strip()) if path.strip() else None
        if owner and owner.get("skip") is not True:
            return True
    return False


def merge_verified_contracts(work, meta, state, specs):
    """Contract merge hook (SYS-04, SYS-09, SYS-10, design D22). Only for a CONFIRMED monorepo; otherwise nothing at all (SYS-07).

    Moment chosen: right after a turn is validated, for every feature the ROADMAP now shows as `verified` that was not handled yet,
    one feature at a time in ROADMAP order. The orchestrator verifies and makes its checkpoint commit INSIDE its turn, so the
    engine cannot run before that commit; instead it merges between turns, records `contract_merge: {slug: {changed, warnings}}`
    in LOOP.json (the next turn's orchestrator reads the status) and leaves SYSTEM.md for the NEXT checkpoint commit, as
    dispatch.md says. When no next turn will happen (done / everything closed) the engine commits SYSTEM.md itself so it is not lost.
    Conflict and missing-file warnings go to the loop status, the console and VALIDATE.md. No SCOPE.json change."""
    if not subprojects.monorepo_confirmed(work):
        return
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    handled = meta.setdefault("contract_merged_slugs", [])
    recorded = {}
    for slug, feature in features.items():
        if feature.get("status") != "verified" or slug in handled:
            continue
        result = system_map.merge_contract_deltas(work, [slug])
        handled.append(slug)
        recorded[slug] = {"changed": bool(result["changed"]), "warnings": list(result["warnings"])}
        if missing_backend_contract(work, slug, feature):
            warning = "back-end feature verified without .specs/features/%s/contract.md: SYSTEM.md was not updated" % loop_state.feature_folder_name(slug)
            recorded[slug]["warnings"].append(warning)
            print("[loop] WARNING (contract merge, %s): %s" % (slug, warning))
            decisions.append(work, slug, state.get("turn"), "important", "WARNING contract: " + warning,
                             "went on without the contract delta", "write the contract.md (routes, env, events) and merge it into SYSTEM.md")
        if result["changed"]:
            meta["system_md_uncommitted"] = True
        for warning in result["warnings"]:
            print("[loop] WARNING (contract merge, %s): %s" % (slug, warning))
            append_validate_line(specs, "WARNING contract merge (%s): %s" % (slug, warning))
            if system_map.NO_GRAMMAR_MARK in warning:
                decisions.append(work, slug, state.get("turn"), "important", "WARNING contract: " + warning,
                                 "went on without merging the contract delta",
                                 "rewrite contract.md as lines `- [add|change|remove] route|dto|auth|env <identity> — <purpose> (<file>)` (no table) and merge it into SYSTEM.md")
    if not recorded:
        return
    state["contract_merge"] = recorded
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    score, total, done = roadmap_score(work)
    if meta.get("system_md_uncommitted") and (state.get("status") == "done" or (total and done == total)):
        code, _, err = git(work, "add", "--", SYSTEM_DOCUMENT)
        if code == 0:
            code, _, err = git(work, "commit", "-m", "chore(loop): merge feature contracts into docs/codebase/SYSTEM.md", "--no-verify")
        if code == 0:
            meta["system_md_uncommitted"] = False
        else:
            print("[loop] WARNING: SYSTEM.md contract merge could not be committed: " + err[-200:])
    save_meta(loop_state.loop_path(work, "meta.json"), meta)


def pause_on_quarantine(specs, prev, state, opts):
    """A feature that just entered quarantine (2 failures) PAUSES the loop and notifies the user, instead of carrying on alone.
    Returns the pause text (and rewrites LOOP.json as awaiting_user, so --answer works) or "" if there is nothing to pause.
    Only acts on a turn the orchestrator left as `continue`: `done`/`awaiting_user`/`error` already notify on their own."""
    if opts.continue_on_blocked or not prev or state.get("status") != "continue":
        return ""
    # Default (user decision): a feature that gets stuck ALWAYS pauses the loop and notifies the user. --continue-on-blocked opts out.
    old = loop_state.parse_roadmap(read(os.path.join(prev["dir"], "ROADMAP.md")))[0]
    new = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    fresh = [slug for slug, f in new.items() if f["status"] == "blocked" and old.get(slug, {}).get("status") != "blocked"]
    if not fresh:
        return ""
    why = str(state.get("reason") or "").strip()
    reason = ("Quarantine after 2 failures: %s. The loop paused for you to decide (try again with guidance from you, "
              "or skip and carry on with the others)." % ", ".join(fresh)) + ((" Orchestrator detail: " + why[:600]) if why else "")
    state.update(status="awaiting_user", reason=reason, options=["try the feature again", "skip the feature and carry on with the others"],
                 next="apply the user's decision about: " + ", ".join(fresh))
    save_json(os.path.join(specs, loop_state.LOOP_FOLDER_NAME, loop_state.JSON_FOLDER_NAME, "LOOP.json"), state)
    return reason


# ---------------------------------------------------------------- critical decisions
def pause_on_critical_decision(work, state, mark):
    """A turn that wrote a `level=critical` line in VALIDATE.md (after line `mark`) stops the loop like an open question: LOOP.json becomes
    awaiting_user with the decision as the question. `important` lines never stop it. A turn that ended in `error` stays an error.
    Returns the reason (and rewrites LOOP.json) or "" when there is nothing to stop for."""
    if state.get("status") == "error":
        return ""
    found = decisions.new_critical(work, mark)
    if not found:
        return ""
    first = found[0]
    reason = "%s%s | chose: %s | revert: %s" % (CRITICAL_DECISION_PREFIX, first["doubt"], first["chose"], first["revert"])
    if len(found) > 1:
        reason += " | %d more critical decision(s) in VALIDATE.md" % (len(found) - 1)
    state.update(status="awaiting_user", reason=reason, feature=first["slug"],
                 options=["keep as decided", "change it: <write how>"],
                 next="apply the user's answer about the critical decision, then continue")
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    return reason


# ---------------------------------------------------------------- spend cap
def effective_cost_cap(meta, opts):
    """Cap of US$ of this run: the one raised from the panel (meta max_cost_usd) wins over --max-cost; 0 = no cap."""
    try:
        raised = float(meta.get("max_cost_usd") or 0)
    except (TypeError, ValueError):
        raised = 0.0
    return raised if raised > 0 else float(opts.max_cost or 0)


def cost_cap_reason(cost, cap):
    return "%s%.2f of %.2f" % (COST_CAP_REASON_PREFIX, cost, cap)


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


# ---------------------------------------------------------------- automatic answer
# The headless loop exists to run for hours without stopping. A question the orchestrator itself returns with options and that
# does NOT need the user (a small, reversible dilemma, or "try the quarantined feature again") is answered by the SCRIPT with the
# recommended option (or the first one that does not abandon the feature), noted in VALIDATE.md, and the loop carries on. A rule
# in the skill text alone did not hold (the agent asked anyway); code holds.
# It stops (and leaves the question to the user) when the text talks about authorization, production, something irreversible,
# push/merge/deploy, a secret, a credential, billing or payment, when the only options abandon the feature, and when the
# automatic answer limits are reached.
HARD_STOP_RE = re.compile(
    r"authoriz|production|\bprod\b|irreversib|delete|\bdrop\b|truncate|\bpush\b|\bmerge\b|deploy|secret|password|credential|billing|payment|access|business rules?|discrepan|inconsisten|real data",
    re.I,
)
ABANDON_RE = re.compile(r"\bend\b|finish without|stop|skip|give up|abandon|accept .{0,40}blocked|do not do|cancel", re.I)
RECOMMENDED_RE = re.compile(r"recommended", re.I)


def pick_auto_answer(state):
    """Picks the answer to a question that does not need the user. Returns (number, text, why) or None when it must stop."""
    options = [str(option) for option in (state.get("options") or [])]
    if len(options) < 2 or str(state.get("reason") or "").startswith(CRITICAL_DECISION_PREFIX):
        return None  # a critical decision needs the user: it is never answered by the script
    if HARD_STOP_RE.search(str(state.get("reason") or "") + " " + " ".join(options)):
        return None
    for number, option in enumerate(options, 1):
        if RECOMMENDED_RE.search(option) and not ABANDON_RE.search(option):
            return number, option, "recommended"
    for number, option in enumerate(options, 1):
        if not ABANDON_RE.search(option):
            return number, option, "first option that does not abandon the feature"
    return None


def append_validate_line(specs, line):
    path = os.path.join(specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md")
    current = read(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(("" if not current or current.endswith("\n") else "\n") + line + "\n")


def try_auto_answer(work, turn, opts, meta, auto_state):
    """Answers the pending LOOP.json question on its own, when it can. Returns True when it answered (the loop carries on) and False when it must stop."""
    if not getattr(opts, "auto_answer", True):
        return False
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    if state.get("status") != "awaiting_user":
        return False
    pick = pick_auto_answer(state)
    if not pick:
        return False
    key = str(state.get("feature") or "general")
    used_here = auto_state["per_feature"].get(key, 0)
    if auto_state["total"] >= opts.max_auto_answers or used_here >= opts.auto_answers_per_feature:
        print("[loop] automatic answer limit reached (%d in total, %d on %s): stopping for you to decide" % (auto_state["total"], used_here, key))
        return False
    number, text, why = pick
    question = re.sub(r"\s+", " ", str(state.get("reason") or "")).strip()
    write_answer(work, "%s [automatic answer from the loop: option %d, %s]" % (text, number, why))
    auto_state["total"] += 1
    auto_state["per_feature"][key] = used_here + 1
    meta.setdefault("auto_answers", []).append({"turn": turn, "feature": key, "option": number})
    decisions.append(work, key, turn, "important", "question answered automatically: \"%s\"" % question[:160],
                     "option %d (%s): %s" % (number, why, text[:140]), "--answer with another option")
    print("[loop] warning: question answered automatically (option %d, %s); noted in .specs/project/loop/VALIDATE.md" % (number, why))
    return True


# The gate tiebreak is for spec wording points. A feature blocked by access, a business rule or a discrepancy goes to the user.
NO_TIEBREAK_RE = re.compile(r"business|discrep|inconsisten|access|credential|secret|password", re.I)


def gate_blocked_features(specs):
    """Features quarantined because they failed the gate (note=gate-...): only the spec is on paper, nothing was implemented yet."""
    features = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    return [slug for slug, fields in features.items()
            if fields.get("status") == "blocked" and "gate" in str(fields.get("note") or "") and not NO_TIEBREAK_RE.search(str(fields.get("note") or ""))]


def try_gate_tiebreak(work, turn, opts, meta, auto_state):
    """Gate tiebreak. The spec-reviewer may reject the same spec round after round over wording points (marking a case as
    not-covered, adjusting PATHS, classifying an RG) that the orchestrator itself solves, and the loop then ends `done` without
    the feature. The real check is build + regression, not the spec wording: after the gate rounds the script tells the
    orchestrator to APPROVE the spec, treat the remaining findings as notes for the executor and move on to execution. Once per
    feature (--gate-tiebreaks). Returns True when it answered (the loop carries on)."""
    if not getattr(opts, "auto_answer", True):
        return False
    specs = os.path.join(work, ".specs", "project")
    used = auto_state.setdefault("tiebreaks", {})
    stuck = [slug for slug in gate_blocked_features(specs) if used.get(slug, 0) < getattr(opts, "gate_tiebreaks", 1)]
    if not stuck:
        return False
    slugs = ", ".join(stuck)
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    state.update(
        status="awaiting_user",
        reason="Gate tiebreak: %s failed the gate several times on spec wording points." % slugs,
        options=["approve the spec and run it, remaining findings become notes for the executor", "finish without the feature"],
    )
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    write_answer(
        work,
        "Gate tiebreak (automatic loop decision): after several rejections, APPROVE the spec of %s (status=approved) without redoing the gate. "
        "Treat the remaining findings as mandatory notes for the executor; a case that depends on data, a secret or a write that "
        "TEST-ENVIRONMENT.md does not cover becomes not-covered in regression.md. Record it in VALIDATE.md and move on to execution with "
        "baseline and regression: build and regression decide, not the spec wording." % slugs,
    )
    for slug in stuck:
        used[slug] = used.get(slug, 0) + 1
    meta.setdefault("gate_tiebreaks", []).append({"turn": turn, "features": stuck})
    decisions.append(work, slugs, turn, "important", "gate tiebreak: spec approved without a new gate after several rejections",
                     "run with the remaining findings as notes; build and regression decide", "--answer asking to redo the gate")
    print("[loop] warning: gate tiebreak for %s (spec approved, findings become notes); noted in .specs/project/loop/VALIDATE.md" % slugs)
    return True


def mcp_read_arguments():
    """(mcp config, allowed tools, denied tools) of the turn, computed by `mcp_access.build_access`: every known server the user's
    `~/.claude.json` defines (grafana, mcp-mysql-dev, mcp-mysql-prod) with its read tools only. No server = the empty strict config
    (every other MCP stays out; a missing server is just absent, never a warning)."""
    access = mcp_access.build_access(app_config.get_user_data_dir())
    if not access["config_path"]:
        return EMPTY_MCP_CONFIG, [], []
    return access["config_path"], list(access["allowed_tools"]), list(access["denied_tools"])


def model_arguments(opts):
    """`--model <value>` of every orchestrator turn (normal, correction and resume); an empty option sends no flag."""
    model = str(getattr(opts, "orchestrator_model", DEFAULT_ORCHESTRATOR_MODEL) or "").strip()
    return ["--model", model] if model else []


def claude_names(opts):
    """Commands of the turns, in fallback order: --claude (default `claude`), then each --claude-fallback."""
    return claude_binaries.ordered(opts.claude, getattr(opts, "claude_fallback", None) or [])


def current_claude(opts):
    names = claude_names(opts)
    return names[min(getattr(opts, "claude_index", 0), len(names) - 1)]


def switch_claude(opts):
    """Moves to the next fallback command (sticky until the next wait); False when there is none left."""
    index = getattr(opts, "claude_index", 0)
    if index + 1 >= len(claude_names(opts)):
        return False
    opts.claude_index = index + 1
    return True


def claude_account(opts, name, stderr):
    """Account text of a turn: the `cc` picker names it on stderr; otherwise `<name> auth status`, asked once per command."""
    cache = opts.__dict__.setdefault("claude_auth", {})
    if name not in cache:
        cache[name] = claude_binaries.auth_status(name)
    return claude_binaries.account_label(cache[name], claude_binaries.account_from_stderr(stderr))


def notify_claude_change(opts, turn, name, account):
    """Notice (every configured channel) when a turn ran on another command or account than the previous turn: `claude` -> `cc`,
    back to `claude`, or the `cc` picker on another account. The first turn of the first run only records it."""
    used = (name, account)
    previous = getattr(opts, "last_claude_used", None)
    opts.last_claude_used = used
    if not previous or previous == used:
        return
    main_name = command_base_name(getattr(opts, "claude", "") or claude_binaries.DEFAULT_BINARY_NAME)  # --claude, default `claude`
    is_main = command_base_name(name) == main_name
    before = describe_claude_used(previous[0], previous[1], command_base_name(previous[0]) == main_name)
    now = describe_claude_used(name, account, is_main)
    print("[loop] turn %d ran on %s; before it was %s" % (turn, now, before))
    same_command = previous[0] == name
    only = local_notice_channels() if same_command else None  # another account of the same command is noise for email/webhook
    if same_command:
        reason = "The `%s` account picker moved to another account because the previous one hit its usage limit." % command_base_name(name)
    elif is_main:
        reason = "The usage limit of the main account should have reset, so the loop went back to it."
    else:
        reason = "The previous account hit its usage limit, so the loop moved to the backup command."
    notify_wait(claude_change_subject(name, account, is_main, same_command),
                "Turn %d now runs on %s.\nBefore it the loop ran on %s.\n%s The loop goes on by itself." % (turn, now, before, reason), only)


LOCAL_NOTICE_CHANNELS = ("console", "toast", "file")  # channels that never leave this machine


def local_notice_channels():
    """The configured notice channels that stay on this machine (console, toast, file), in the configured order."""
    configured = notify.envload.get_list("LOOP_NOTIFY_CHANNELS") or list(LOCAL_NOTICE_CHANNELS)
    return [name for name in configured if name in LOCAL_NOTICE_CHANNELS]


# how each sign-in method of `claude auth status` is billed, in plain words (a sign-in token is still the subscription)
SIGN_IN_WORDS = {"login": "subscription, browser login", "token": "subscription, sign-in token", "API key": "API key, billed per token"}


def command_base_name(name):
    """`C:/bin/claude.exe` -> `claude`, `cc.cmd` -> `cc`."""
    return os.path.splitext(os.path.basename(name or ""))[0]


def split_account_label(account):
    """(method, who, organization and plan) of a claude_binaries.account_label text such as
    "login, dev@x.com (iData, team)" or "token, cc account claude2"; missing parts come back as ''."""
    text = (account or "").strip()
    extra = ""
    found = re.search(r"\s*\(([^)]*)\)\s*$", text)
    if found:
        extra, text = found.group(1), text[:found.start()]
    method, _, who = text.partition(", ")
    if method not in SIGN_IN_WORDS:
        method, who = "", text
    return method, who.strip(), extra


AUTH_METHOD_CODES = {"login": "login", "token": "token", "API key": "api_key"}  # label word -> the code kept in the turn log
CC_ACCOUNT_IN_LABEL = re.compile(r"cc account +(\S+)")


def claude_auth_fields(account):
    """Structured sign-in of a turn, taken from its account label (never a secret): {"auth_method": "login"|"token"|"api_key"|"",
    "auth_email", "auth_plan", "cc_account"}. "login, dev@x.com (iData, team)" -> login, dev@x.com, team, ''."""
    method, who, extra = split_account_label(account)
    parts = [part.strip() for part in extra.split(",")] + ["", ""]
    cc_found = CC_ACCOUNT_IN_LABEL.search(who)
    return {"auth_method": AUTH_METHOD_CODES.get(method, ""), "auth_email": who if "@" in who else "",
            "auth_plan": parts[1], "cc_account": cc_found.group(1) if cc_found else ""}


def describe_claude_used(name, account, is_main):
    """Body text of one command and its account: "the main command `claude` (dev@x.com, subscription, browser login; iData, team)"."""
    method, who, extra = split_account_label(account)
    details = ", ".join(part for part in (who, SIGN_IN_WORDS.get(method, "")) if part)
    details += ("; %s" % extra) if extra else ""
    role = "the main command" if is_main else "the backup command"
    return "%s `%s`%s" % (role, command_base_name(name), " (%s)" % details if details else " (account unknown)")


def claude_change_subject(name, account, is_main, same_command=False):
    """Subject that says, in words, which account the loop is on now and how it signs in:
    "now on the backup account: cc account claude2 (subscription, sign-in token)",
    "back on the main account: dev@x.com (subscription, browser login)",
    "cc switched to cc account claude3 (subscription, sign-in token)". The organization and plan stay in the body."""
    method, who, _ = split_account_label(account)
    who = who or "`%s`" % command_base_name(name)
    how = " (%s)" % SIGN_IN_WORDS[method] if method in SIGN_IN_WORDS else ""
    if same_command:
        return "%s switched to %s%s" % (command_base_name(name), who, how)
    return "%s: %s%s" % ("back on the main account" if is_main else "now on the backup account", who, how)


def return_to_main_command(opts):
    """When the loop is on a fallback and the main command's remembered reset moment has passed, goes back to the main command
    (index 0). Returns True when it went back; the next notify_claude_change announces it."""
    moment = getattr(opts, "main_reset", None)
    if getattr(opts, "claude_index", 0) > 0 and moment and datetime.datetime.now() >= moment:
        opts.claude_index, opts.main_reset, opts.picker_tried = 0, None, []
        print("[loop] the main command `%s` should be free again (reset %s): back on it" % (claude_names(opts)[0], moment.strftime("%d/%m %H:%M")))
        return True
    return False


def remember_reset(opts, moment):
    """Keeps a known reset time of a limited command (for the wait when every command is limited)."""
    if moment:
        opts.__dict__.setdefault("limit_resets", []).append(moment)


MAX_LIMIT_WAIT_SECONDS = 8 * 24 * 3600  # the weekly window is the longest one
RESET_MARGIN_SECONDS = 120  # wake up a little after the reset, not on it


def limit_wait_seconds(opts):
    """(seconds, datetime of the reset or None): until the first known reset among the limited commands plus a margin, else --limit-wait."""
    now = datetime.datetime.now()
    future = sorted(moment for moment in getattr(opts, "limit_resets", None) or [] if moment > now)
    if not future:
        return opts.limit_wait * 60, None
    seconds = int((future[0] - now).total_seconds()) + RESET_MARGIN_SECONDS
    return min(max(seconds, 60), MAX_LIMIT_WAIT_SECONDS), future[0]


def wait_text(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    days, hours = divmod(hours, 24)
    return " ".join(part for part in ("%dd" % days if days else "", "%dh" % hours if hours else "", "%dmin" % minutes if minutes or not (days or hours) else "") if part)


LIMIT_WAIT_STEP_SECONDS = 60


def sleep_unless_paused(work, turn, seconds):
    """Sleeps `seconds` in steps, stopping early when the panel asks for a pause; returns the pause text ('' = slept the whole time)."""
    deadline = time.time() + seconds
    while True:
        left = deadline - time.time()
        if left <= 0:
            return ""
        time.sleep(min(LIMIT_WAIT_STEP_SECONDS, left))
        paused = pause_requested(work, turn)
        if paused:
            return paused


def claude_command(opts, prompt):
    base = claude_binaries.base_argv(current_claude(opts))
    # T23: each non-primary worktree is granted as `--add-dir=<path>` (the `=` form: the flag is variadic and would swallow what follows)
    # D4: each `reference` repo (read only, no worktree) is granted the same way, as its own folder
    extra_dirs = ["--add-dir=%s" % path for path in (getattr(opts, "extra_worktrees", None) or []) + (getattr(opts, "reference_dirs", None) or [])]
    mcp_config, allowed_tools, denied_tools = mcp_read_arguments()
    allowed = ["--allowedTools"] + allowed_tools if allowed_tools else []
    return base + [
        "-p", prompt, "--output-format", "json", "--permission-mode", opts.permission_mode,
        "--permission-prompts", "none", "--max-budget-usd", str(opts.budget),
    ] + model_arguments(opts) + extra_dirs + [
        "--strict-mcp-config", "--mcp-config", mcp_config,
    ] + allowed + [
        "--disallowedTools", ",".join(LOOP_DISALLOWED_TOOLS + denied_tools),  # last and as ONE argument: the option is variadic
    ]


def loop_json_correction_prompt(turn, branch):
    """Prompt of the single correction turn (same session, --resume): only write LOOP.json, never redo or start work."""
    return ("/orchestrator continue --headless --turn %d --branch %s --finish-only. Your previous reply ended turn %d WITHOUT writing LOOP.json "
            "(e.g. you stopped to wait for background subagents). Do not start new work and do not wait for background tasks: if a subagent is "
            "still unfinished, record that in the reason and use status continue. Write STATE.md if needed and, as the last action, run: "
            "python \"<app>/harness.py\" state loop <root> --status <continue|awaiting_user|done|error> --turn %d --reason \"...\" --next \"...\"."
            % (turn, branch, turn, turn))


def push_lock_env(cwd):
    """Environment of `claude -p`: push is impossible even through a guard gap (python -c, subshell...). Disables the pushurl of EVERY remote
    for that process only (GIT_CONFIG_*, nothing is written to the repo); the end delivery is done by this script, outside that environment."""
    env = dict(os.environ)
    env["LOOP_HEADLESS"] = "1"
    env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")  # wait for the background subagents (the verifier) instead of killing them 10 min after the last turn; run_turn's timeout still bounds it
    env.setdefault("LOOP_APP_HOME", APP_ROOT_DIR)  # the headless turn finds the app without depending on config.json
    specs_dir = os.path.join(os.path.abspath(cwd), ".specs")  # cwd = the PRIMARY worktree (run_turn is called with `work`)
    if os.path.isdir(specs_dir):
        env["LOOP_SPECS_DIR"] = specs_dir  # the guard finds SCOPE.json even when its cwd is a second worktree
    _, remotes, _ = git(cwd, "remote")
    names = remotes.split()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for name in names:
        env["GIT_CONFIG_KEY_%d" % count] = "remote.%s.pushurl" % name
        env["GIT_CONFIG_VALUE_%d" % count] = DISABLED_PUSH_URL
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def run_turn(command, cwd, timeout_min):
    command, stdin_text = claude_binaries.prompt_through_stdin(command)  # a .cmd wrapper (`cc`) would cut the prompt at a line break
    flags = {"stdin": subprocess.PIPE if stdin_text else subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace", "cwd": cwd,
             "env": push_lock_env(cwd)}
    if IS_WIN:
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen(command, **flags)
    try:
        out, err = proc.communicate(input=stdin_text or None, timeout=timeout_min * 60)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        out, err = proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out or "", err or "", True


def parse_result(text):
    """Normalised result of a turn (cli_output.parse): {"ok": True, "result", "is_error", "total_cost_usd", ...} or
    {"ok": False, "reason": "unrecognized_output", "detail", "sample"} when the CLI printed nothing the loop can read."""
    return cli_output.parse(text)


def unrecognized_output(data):
    return data.get("ok") is False and data.get("reason") == cli_output.UNRECOGNIZED


# ---------------------------------------------------------------- permission denial
# A denial of a common command (write evidence, read a file) that the agent redoes another way does NOT stop the loop: it
# becomes a line in VALIDATE.md and the turn carries on. It always stops when the denied command looks risky, or when the
# turn did not advance.
RISKY_DENIAL_RE = re.compile(r"git\s+push|--force|push\s+-f|rm\s+-rf|DROP\s+(TABLE|DATABASE)|TRUNCATE|DELETE\s+FROM", re.I)
SECRET_LIKE_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def prod_hint_patterns():
    """GUARD_PROD_HINTS (the guard's production words, comma-separated; default prod,production) as whole-word regexes."""
    raw = os.environ.get("GUARD_PROD_HINTS")
    if raw is None:
        raw = notify.envload.get("GUARD_PROD_HINTS", "prod,production")
    return [re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(item.strip()), re.I) for item in raw.split(",") if item.strip()]


def project_deny_patterns(work):
    """The project's `deny_bash_regex` from PERMISSIONS.json (block rules written with rules.py): a denied command that matches
    one of them is never tolerated."""
    if not work:
        return []
    path = loop_state.loop_path(work, "PERMISSIONS.json")
    if not os.path.exists(path):
        path = os.path.join(work, ".specs", "project", "PERMISSIONS.json")
    patterns = []
    for pattern in load_json(path).get("deny_bash_regex") or []:
        try:
            patterns.append(re.compile(str(pattern), re.I))
        except re.error:
            continue
    return patterns


def is_risky_denial(denial, work=""):
    """A denial that always stops the loop: any MCP tool, or a command that looks like push, force, rm -rf, destructive SQL,
    production (GUARD_PROD_HINTS) or something the project forbids (PERMISSIONS.json deny_bash_regex)."""
    if str(denial.get("tool_name") or "").startswith("mcp__"):
        return True
    text = json.dumps(denial.get("tool_input") or {}, ensure_ascii=False)
    if RISKY_DENIAL_RE.search(text):
        return True
    command = str((denial.get("tool_input") or {}).get("command") or text)
    return any(pattern.search(text) for pattern in prod_hint_patterns()) or any(pattern.search(command) for pattern in project_deny_patterns(work))


def denial_text(denial):
    """A short line without secrets (a long sequence becomes ***) for VALIDATE.md, the notice and the summary."""
    tool_input = denial.get("tool_input") or {}
    text = tool_input.get("command") or tool_input.get("file_path") or json.dumps(tool_input, ensure_ascii=False)
    text = SECRET_LIKE_RE.sub("***", re.sub(r"\s+", " ", str(text)).strip())
    return "%s: %s" % (denial.get("tool_name"), text[:140])


def note_tolerated_denials(work, turn, denials, progress):
    """Notes each tolerated denial in VALIDATE.md; the final summary and the PR show that file as 'For you to validate'."""
    for denial in denials:
        decisions.append(work, "general", turn, "important", "permission denied (%s)" % denial_text(denial),
                         "carried on, the turn advanced (%s)" % progress, "check whether what was denied was needed")


def denial_progress(prev, specs, state, new_commits):
    """Why the turn counts as advanced (empty text = it did not advance): new commit, ROADMAP changed or it stopped asking you."""
    if new_commits:
        return "new commit"
    before = read(os.path.join(prev["dir"], "ROADMAP.md")) if prev else ""
    if before != read(os.path.join(specs, "ROADMAP.md")):
        return "ROADMAP changed"
    if state.get("status") in ("done", "awaiting_user"):
        return "stopped asking you"
    return ""


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
    parser = argparse.ArgumentParser(description="Unsupervised /orchestrator loop")
    parser.add_argument("repo")
    parser.add_argument("--wave", type=int, default=3, help="features (lanes) in parallel per turn, each with its own agents at every step (execution only with disjoint PATHS and met dependencies); default 3")
    parser.add_argument("--e2e", action="store_true", help="authorizes the final flow-validation phase (verifier-flow, headless)")
    parser.add_argument("--push", action="store_true", default=None, help="requires the final push+PR (fails the preflight if there is no remote/identity). Without the flag push+PR is already the DEFAULT (feature branch only)")
    parser.add_argument("--no-push", dest="push", action="store_false", help="do not push the branch nor open a PR at the end")
    parser.add_argument("--budget", type=float, default=5.0, help="max US$ per turn")
    parser.add_argument("--timeout", type=int, default=60, help="max minutes per turn")
    parser.add_argument("--max-stall", type=int, default=10, help="consecutive turns without progress before stopping")
    parser.add_argument("--limit-wait", type=int, default=30, help="minutes to wait when the usage limit is hit")
    parser.add_argument("--limit-retries", type=int, default=8)
    parser.add_argument("--permission-mode", default="auto", help="auto | acceptEdits | dontAsk")
    parser.add_argument("--branch", default="")
    parser.add_argument("--base-branch", default="", help="branch new worktrees start from and the PR target (the ROADMAP `Branch base`); default: LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH, else developer")
    parser.add_argument("--git-user", default="", help="requires this git user.name on push (or LOOP_GIT_USER in the app .env); default: any configured account")
    parser.add_argument("--setup", default="", help='one-time command after creating the worktree (e.g. "npm ci")')
    parser.add_argument("--claude", default="", help="claude binary (or a test .py)")
    parser.add_argument("--claude-fallback", action="append", default=[], metavar="COMMAND",
                        help="command tried when the running one hits the account usage limit (repeatable, in order; e.g. `cc`, a wrapper signed in with another account)")
    parser.add_argument("--option", type=int, default=0, metavar="N", help="answers the question by the NUMBER of the option listed in the notice (same as --answer with that option's text)")
    parser.add_argument("--answer", default="", help="answers the question that stopped the loop (awaiting_user) and continues; goes to ANSWERS.md")
    parser.add_argument("--max-cost", type=float, default=50.0, help="cap of US$ summed over all turns (0 turns it off); a cap raised in the panel (meta max_cost_usd) wins; when exceeded the loop PAUSES (resumable)")
    parser.add_argument("--orchestrator-model", default=DEFAULT_ORCHESTRATOR_MODEL, help="model of the orchestrator turns (--model of claude); empty = no flag, the CLI default")
    parser.add_argument("--rewind", type=int, default=None, metavar="N", help="go back to the state right after turn N (state files + git reset --hard of the loop branch). Without --yes it only shows the plan")
    parser.add_argument("--yes", action="store_true", help="confirms --rewind")
    parser.add_argument("--leftovers", choices=["commit", "discard"], default="", help="uncommitted files outside .specs/ left in the worktree: commit them all as a checkpoint or discard them, then carry on (the panel's recovery buttons)")
    parser.add_argument("--summary-dir", default="", help="folder of the final summary (default: the app's output folder, ~/Downloads when unset)")
    parser.add_argument("--json", action="store_true", help="with --check: after the text lines, print one `[check] JSON: {...}` line with problems, warnings and `items` (kind, repo, files, text, fix)")
    parser.add_argument("--check", action="store_true", help="only checks the preflight, prints the ready command and exits (creates nothing)")
    parser.add_argument("--planning", action="store_true", help="with --check: readiness of a roadmap NOT yet approved (skips the approval, adds worktree, feature-path, MCP, credential and test-command checks)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ensure-worktrees", action="store_true", help="only creates (or re-attaches) the worktrees as the loop start does, prints the `[loop] [<repo>] <kind>: <text>` lines and exits; no preflight, no turn")
    parser.add_argument("--repos", default="", help="with --ensure-worktrees: comma-separated repo names to create (the primary is always ensured)")
    parser.add_argument("--continue-on-blocked", action="store_true", help="a quarantined feature NEVER pauses the loop, not even when another depends on it")
    parser.add_argument("--pause-on-blocked", action="store_true", help="a quarantined feature ALWAYS pauses and notifies (this is now the default; kept for compatibility; formerly: only pauses when another feature depends on the blocked one)")
    parser.add_argument("--allow-silent", action="store_true", help="testing only: waives the mandatory notice channels (LOOP_NOTIFY_REQUIRED)")
    parser.add_argument("--no-auto-answer", dest="auto_answer", action="store_false", default=True,
                        help="turns the automatic answer off: every orchestrator question stops the loop and waits for you (default: the script answers on its own the ones that do not need you)")
    parser.add_argument("--no-monitor", action="store_true", help="does not start the loop web panel (default: starts on its own; also off with LOOP_MONITOR=off in the app .env)")
    parser.add_argument("--gate-tiebreaks", type=int, default=1, help="gate tiebreaks per feature: after several rejections the script tells the orchestrator to approve the spec and run it (0 turns it off)")
    parser.add_argument("--max-auto-answers", type=int, default=12, help="cap of automatic answers per script run")
    parser.add_argument("--auto-answers-per-feature", type=int, default=3, help="cap of automatic answers for the same feature (avoids spinning forever on a gate that rejects)")
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
            if err:
                log["stderr"] = err[-2000:]
            log["exit_code"], log["timeout"], log["turn"], log["claude"] = code, timed_out, turn, claude_name
            log["claude_account"] = claude_account(opts, claude_name, err)
            log.update(claude_auth_fields(log["claude_account"]))
            meta["claude"], meta["claude_account"] = claude_name, log["claude_account"]
            meta.update(claude_auth_fields(log["claude_account"]))
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
            account_limited = failed and claude_binaries.is_usage_limit(out + err)
            if account_limited:
                reset = claude_binaries.reset_time(out + err)
                remember_reset(opts, reset)
                if getattr(opts, "claude_index", 0) == 0:  # the main command: when to try it again while a fallback keeps the turns
                    now = datetime.datetime.now()
                    opts.main_reset = (reset if reset and reset > now else now + datetime.timedelta(minutes=opts.limit_wait))                         + datetime.timedelta(seconds=RESET_MARGIN_SECONDS)  # never already past: that would bounce between the commands
                picked = claude_binaries.account_from_stderr(err)
                if picked and picked not in opts.__dict__.setdefault("picker_tried", []):
                    # the `cc` picker ran on an account that ran out during the turn: run it again, it picks the next free one ("cc" + Enter)
                    opts.picker_tried.append(picked)
                    opts.last_claude_used = getattr(opts, "last_claude_used", None) or (claude_name, log["claude_account"])  # notify the change
                    print("[loop] usage limit on `%s` account %s; turn %d again, the picker chooses another account" % (claude_name, picked, turn))
                    turn -= 1
                    continue
            if account_limited and switch_claude(opts):
                print("[loop] usage limit on `%s`; turn %d again with `%s`" % (claude_name, turn, current_claude(opts)))
                meta["claude_switches"] = int(meta.get("claude_switches", 0)) + 1
                opts.last_claude_used = getattr(opts, "last_claude_used", None) or (claude_name, log["claude_account"])  # notify the change
                save_meta(loop_state.loop_path(work, "meta.json"), meta)
                turn -= 1
                continue
            if failed and LIMIT_RE.search(out + err):
                limit_hits += 1
                if limit_hits > opts.limit_retries:
                    return finish(work, meta, "error", "account usage limit persisted after %d waits" % opts.limit_retries, opts)
                seconds, resume_at = limit_wait_seconds(opts)
                when = resume_at.strftime("%d/%m %H:%M") if resume_at else ""
                print("[loop] usage limit on every command (%s); waiting %s%s (%d/%d)" % (", ".join(claude_names(opts)), wait_text(seconds),
                                                                                       " until " + when if when else "", limit_hits, opts.limit_retries))
                notify_wait("credits ran out, back in %s" % wait_text(seconds),
                            "The usage limit was hit on turn %d by every command the loop can use (%s). %s The loop waits and starts "
                            "again by itself, from `%s` (wait %d of %d)."
                            % (turn, ", ".join(claude_names(opts)), ("The first account frees up at %s (in %s)." % (when, wait_text(seconds)))
                               if when else "The reset time is unknown: it tries again in %s." % wait_text(seconds),
                               claude_names(opts)[0], limit_hits, opts.limit_retries))
                paused = sleep_unless_paused(work, turn - 1, seconds)
                if paused:
                    return finish(work, meta, "awaiting_user", paused, opts)
                opts.claude_index, opts.picker_tried, opts.limit_resets, opts.main_reset = 0, [], [], None  # after the wait, the main command again
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
