#!/usr/bin/env python3
"""Unsupervised /orchestrator loop: runs `claude -p` in turns until it finishes or gets stuck.

Usage:
  python run_loop.py <repo> [--wave 1] [--e2e] [--push] [--budget 5] [--timeout 60] [--max-stall 10]
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
  The process ends by itself when the model gives its final answer. If it "would wait for the user", nobody is there:
  the turn ends and the orchestrator writes .specs/project/loop/json/LOOP.json {status: awaiting_user, ...}.

Stops (all notify through the LOOP_NOTIFY_CHANNELS channels: console, toast, file, email, webhook; see notify.py):
  done | awaiting_user | error (LOOP.json) | turn timeout | exit!=0 | risky permission denial or one without progress |
  dirty tree outside .specs/ | LOOP.json missing/stale | identical state in 2 turns |
  --max-stall turns WITHOUT PROGRESS (default 10; the counter resets on every status advance in the ROADMAP) |
  account usage limit (waits --limit-wait min, up to --limit-retries times).
No overall turn cap: as long as there is progress, it continues.

Always on stop: writes .specs/project/loop/json/meta.json and generates the summary (loop_summary.py, no Claude) in
--summary-dir (default: loop-summary/ in the current directory; the app passes its output folder).
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
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402  (harness_backend package on sys.path; skill and guard: LOOP_SKILL_DIR > app config.json > the plugin)
import loop_summary  # noqa: E402
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in the app .env)
import delivery  # noqa: E402  (push of the feature branch + PR to the integration branch, only at the end)
import graph_view  # noqa: E402  (progress drawing: text, short, Mermaid)
import audit_deps  # noqa: E402  (forgotten dependency between features)
import monitor_proc  # noqa: E402  (loop web panel: starts detached, independent of this process)
import cli_output  # noqa: E402  (one tolerant parser of the `claude -p` JSON output, shared with the app)
from harness_backend.config import skill_locator  # noqa: E402

APP_ROOT_DIR = app_paths.APP_ROOT_DIR
APP_CLI = os.path.join(APP_ROOT_DIR, "harness.py")  # the command the user pastes: `python <app>/harness.py loop <repo> ...`
IS_WIN = os.name == "nt"
ACTIVE = {}  # run in progress (repo, opts, notify): lets us notify even when the script dies before finish()
# blocked is NOT progress: it scores 0, otherwise a loop that only blocks features would look like it advances (closes the roadmap but delivers nothing)
RANK = {"pending": 0, "speccing": 1, "specced": 2, "approved": 3, "building": 4, "verified": 6, "blocked": 0}
STATUS_OK = {"continue", "awaiting_user", "done", "error"}
LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests|\b429\b|overloaded|quota", re.I)
EXIT = {"done": 0, "awaiting_user": 10, "error": 20}
DISABLED_PUSH_URL = "push-disabled-by-loop://use-delivery.py"
PAUSE_REASON = "paused by the user in the panel; click Resume in the panel or run the command again"
PAUSE_NEXT = "resume when the user wants"


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
    save_json(loop_state.loop_path(work, "meta.json"), meta)
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
    binary = opts.claude or shutil.which("claude")
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
    pending = len(re.findall(r"FILL_IN", read(path)))
    if pending:
        return [".specs/project/TEST-ENVIRONMENT.md still has %d FILL_IN field(s): complete it before running" % pending]
    return []


def nothing_left_to_do(done, total, opts, state=None, tiebreak_possible=False):
    """All verified/blocked = nothing to do, EXCEPT when the user is answering a question (--option/--answer): the answer may
    reopen a quarantined feature (e.g. fix the spec and redo the gate). Without this exception the preflight refused the
    answer because the quarantined feature counted as closed."""
    will_auto_answer = bool(state) and state.get("status") == "awaiting_user" and getattr(opts, "auto_answer", True) and pick_auto_answer(state) is not None
    return bool(total) and done == total and not (opts.option or opts.answer) and not will_auto_answer and not tiebreak_possible


def preflight(repo, opts):
    """Checks everything the loop needs and returns (problems, warnings). Creates nothing."""
    problems, warns = [], []
    work = worktree_of(repo)
    root = work if os.path.isdir(work) else repo
    if not (opts.claude or shutil.which("claude")):
        problems.append("`claude` command not found on PATH")
    if git(repo, "rev-parse", "--is-inside-work-tree")[1] != "true":
        return ["%s is not a git repository" % repo], warns
    if not git(repo, "branch", "--show-current")[1]:
        problems.append("repo has no current branch (detached HEAD)")
    if not os.path.isdir(work):
        _, dirty, _ = git(repo, "status", "--porcelain", "--untracked-files=no")
        if dirty:
            warns.append("the repo has UNcommitted changes; the worktree is born from the last commit and does NOT carry them. Commit or stash first if you need them.")
    if not os.path.exists(os.path.join(root, ".specs", "project", "ROADMAP.md")):
        problems.append("missing .specs/project/ROADMAP.md: generate and APPROVE the roadmap first (panel, or a normal /orchestrator conversation)")
    else:
        grammar = loop_state.parse_roadmap(read(os.path.join(root, ".specs", "project", "ROADMAP.md")))
        problems += ["ROADMAP outside the grammar: " + item for item in grammar[1] + loop_state.check_fields(grammar[0])]
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
    if os.path.exists(os.path.join(root, ".specs", "project", "ROADMAP.md")) and not (opts.option or opts.answer):
        approval = loop_state.roadmap_approval(root)
        if not approval["approved"]:
            problems.append("roadmap not approved: %s (Approve roadmap button in the panel)" % approval["reason"])
    if not re.search(r"^Conformance:\s*(STANDARD|LEGACY)", read(os.path.join(root, "CLAUDE.md")), re.M):
        problems.append("CLAUDE.md has no `Conformance: STANDARD|LEGACY` line (harness.py classify <repo> --write STANDARD|LEGACY, or the Conformance button in the panel)")
    problems += test_environment_problems(root, opts)
    problems += hook_path_problems()
    rules = subprocess.run([sys.executable, os.path.join(HERE, "rules.py"), "test", root, "--quiet"], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if rules.returncode != 0:
        problems.append("a project block rule stopped working (rules.py test): " + " ".join(rules.stdout.splitlines()[-4:])[:300])
    try:
        audit = audit_deps.audit(root)
        loose = [s for s in audit["suggestions"] if not s["cycle"]]
        if loose:
            warns.append("%d forgotten dependency(ies) between features (e.g. %s -> %s). Run audit_deps.py --write and decide; the orchestrator applies `deps=` before each wave."
                         % (len(loose), loose[0]["feature"], loose[0]["depends_on"]))
    except Exception as error:  # the audit never takes the preflight down
        warns.append("dependency audit failed: %s" % error)
    env_problems, env_warns = environment_problems(opts, repo)
    problems += env_problems
    warns += env_warns
    guard = app_paths.locate_guard()
    if not guard:
        problems.append("the orchestrator skill's guard.py was not found (looked in %s): set LOOP_SKILL_DIR or run `harness.py install`"
                        % app_paths.describe_guard_search_paths())
    else:
        code = subprocess.run([sys.executable, guard, "--profile", "executor"], input='{"tool_name":"Bash","tool_input":{"command":"git push"}}', capture_output=True, text=True).returncode
        if code != 2:
            problems.append("guard.py did not block `git push` (code %s, expected 2): do not run unsupervised" % code)
    if not opts.max_cost:
        warns.append("no total cost cap (--max-cost 0): the loop only stops on progress/error, not on spend")
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
    if opts.push:
        explicit = getattr(opts, "push_explicit", False)
        problem = identity_problem(repo, opts)
        no_origin = not git(repo, "remote", "get-url", "origin")[1]
        for text in ([problem] if problem else []) + (["no `origin` remote: nowhere to push the feature branch"] if no_origin else []):
            (problems if explicit else warns).append(("--push: " if explicit else "the final push/PR will not happen: ") + text)
        if not shutil.which("gh"):
            warns.append("`gh` not found: the end of the loop pushes the branch but only returns the compare LINK (the PR is not created)")
    if os.path.isfile(os.path.join(repo, "package.json")) and not opts.setup and not os.path.isdir(work):
        warns.append("Node project: the worktree is born without node_modules. Use --setup \"npm ci\" (otherwise verify returns UNVERIFIED).")
    dotnet = find_dotnet(repo)
    if dotnet and not os.path.isdir(work):
        warns.append(".NET detected (%s): the worktree is born without bin/obj; `dotnet build` does an implicit restore (needs network or the NuGet cache). "
                     "The executor does not run `dotnet restore` nor `dotnet add package` (guard); to restore beforehand, use --setup with `dotnet restore`." % dotnet)
    if (dotnet or os.path.isfile(os.path.join(repo, "package.json"))) and not os.path.isfile(os.path.join(repo, ".gitignore")):
        warns.append("repo has no .gitignore: bin/obj/node_modules generated by the build dirty the worktree and the loop stops on `dirty tree`.")
    return problems, warns


def find_dotnet(repo, max_depth=3):
    """First .sln/.csproj up to `max_depth` folders below the root (ignores bin/obj/node_modules/.git/.specs). Empty if none."""
    skip = {"bin", "obj", "node_modules", ".git", ".specs", ".vs", "packages"}
    for depth in range(max_depth + 1):
        for extension in ("*.sln", "*.slnx", "*.csproj"):
            hits = sorted(path for path in glob.glob(os.path.join(repo, *(["*"] * depth), extension))
                          if not skip.intersection(os.path.relpath(path, repo).split(os.sep)[:-1]))
            if hits:
                return os.path.relpath(hits[0], repo).replace(os.sep, "/")
    return ""


# ---------------------------------------------------------------- worktree (script only)
def ensure_worktree(repo, opts):
    """Creates the sibling worktree and the loop branch ONCE. Reuses it if it exists. Never recreates it every turn."""
    work = worktree_of(repo)
    meta_path = loop_state.loop_path(work, "meta.json")
    if os.path.isdir(work):
        loop_state.migrate_layout(work)  # worktree created with an older layout
        meta = load_json(meta_path)
        _, current, _ = git(work, "branch", "--show-current")
        if not meta.get("branch") or current != meta["branch"]:
            sys.exit("folder %s already exists but is not this loop's worktree (current branch: %s). Remove or rename it and run again." % (work, current))
        print("[loop] reusing worktree %s on branch %s" % (work, meta["branch"]))
        return work, meta
    code, base, err = git(repo, "branch", "--show-current")
    if code != 0 or not base:
        sys.exit("repo has no current branch (detached HEAD?): %s" % err)
    branch = opts.branch or "loop/" + datetime.datetime.now().strftime("%Y%m%d-%H%M")
    if opts.dry_run:
        print("[dry-run] git worktree add -b %s %s %s" % (branch, work, base))
        return work, {"branch": branch, "base": base, "worktree": work, "copied": []}
    code, _, err = git(repo, "worktree", "add", "-b", branch, work, base)
    if code != 0:
        sys.exit("failed to create worktree: " + err)
    copied = []
    for name in (".specs", "CLAUDE.md", ".claude"):
        source, target = os.path.join(repo, name), os.path.join(work, name)
        if os.path.exists(source) and not os.path.exists(target):
            shutil.copytree(source, target) if os.path.isdir(source) else shutil.copy2(source, target)
            copied.append(name)
    loop_state.migrate_layout(work)  # .specs copied from a repo still in an older layout
    meta = {
        "branch": branch, "base": base, "worktree": work, "repo": repo, "copied": copied,
        "created_at": datetime.datetime.now().isoformat(timespec="seconds"), "turns": 0, "cost_usd": 0.0,
        "note": "worktree used ONLY by run_loop.py; the orchestrator commits here and never pushes",
    }
    save_json(meta_path, meta)
    print("[loop] worktree created: %s (branch %s, base %s; copied from the repo: %s)" % (work, branch, base, ", ".join(copied) or "nothing"))
    if opts.setup:
        print("[loop] one-time setup: %s" % opts.setup)
        code = subprocess.run(opts.setup, cwd=work, shell=True).returncode
        if code != 0:
            sys.exit("setup failed (code %s); fix it and run again (the worktree already exists)" % code)
    return work, meta


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


def fingerprint(work):
    _, head, _ = git(work, "rev-parse", "HEAD")
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md")) + read(os.path.join(work, ".specs", "project", "STATE.md")) + head
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def dirty_outside_specs(work, ignore):
    _, out, _ = git(work, "status", "--porcelain")
    rows = []
    for line in out.splitlines():
        path = line[3:].strip().strip('"').replace("\\", "/")
        if path.startswith(".specs/") or path.rstrip("/") in ignore:
            continue
        rows.append(line)
    return rows


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
    save_json(meta_path, meta)
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
def plan_flags(work, wave):
    """Deterministic scheduler: the script computes the turn's phase and features (loop_state.next_work); the orchestrator executes.
    With no readable ROADMAP or nothing to dispatch, returns empty and the orchestrator decides as before."""
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    if not features:
        return ""
    plan = loop_state.next_work(features, wave, headless=True)
    flags = " --phase %s" % plan["phase"]
    if plan["slugs"]:
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


def pause_on_quarantine(specs, prev, state, opts):
    """A feature that just entered quarantine (2 failures) PAUSES the loop and notifies the user, instead of carrying on alone.
    Returns the pause text (and rewrites LOOP.json as awaiting_user, so --answer works) or "" if there is nothing to pause.
    Only acts on a turn the orchestrator left as `continue`: `done`/`awaiting_user`/`error` already notify on their own."""
    if opts.continue_on_blocked or not prev or state.get("status") != "continue":
        return ""
    # Default (user decision): quarantine only PAUSES when it blocks the operation (some feature depends on the blocked one).
    # A loose blocked feature does not stop the loop: it carries on with the others and goes in the final report. --pause-on-blocked always pauses.
    old = loop_state.parse_roadmap(read(os.path.join(prev["dir"], "ROADMAP.md")))[0]
    new = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    fresh = [slug for slug, f in new.items() if f["status"] == "blocked" and old.get(slug, {}).get("status") != "blocked"]
    if not fresh:
        return ""
    if not getattr(opts, "pause_on_blocked", False):
        plan = loop_state.next_work(new, getattr(opts, "wave", 1), headless=True)
        if not plan.get("stuck_by_blocked"):
            return ""
    why = str(state.get("reason") or "").strip()
    reason = ("Quarantine after 2 failures: %s. The loop paused for you to decide (try again with guidance from you, "
              "or skip and carry on with the others)." % ", ".join(fresh)) + ((" Orchestrator detail: " + why[:600]) if why else "")
    state.update(status="awaiting_user", reason=reason, options=["try the feature again", "skip the feature and carry on with the others"],
                 next="apply the user's decision about: " + ", ".join(fresh))
    save_json(os.path.join(specs, loop_state.LOOP_FOLDER_NAME, loop_state.JSON_FOLDER_NAME, "LOOP.json"), state)
    return reason


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
    if len(options) < 2:
        return None
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
    specs = os.path.join(work, ".specs", "project")
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
    append_validate_line(specs, "- [%s] question answered automatically on turn %d: \"%s\" | chose: option %d (%s): %s | revert: --answer with another option"
                         % (key, turn, question[:160], number, why, text[:140]))
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
    append_validate_line(specs, "- [%s] gate tiebreak on turn %d: spec approved without a new gate after several rejections | chose: run with the remaining findings as notes; build and regression decide | revert: --answer asking to redo the gate" % (slugs, turn))
    print("[loop] warning: gate tiebreak for %s (spec approved, findings become notes); noted in .specs/project/loop/VALIDATE.md" % slugs)
    return True


def claude_command(opts, prompt):
    binary = opts.claude or shutil.which("claude") or "claude"
    base = [sys.executable, binary] if binary.endswith(".py") else [binary]
    return base + [
        "-p", prompt, "--output-format", "json", "--permission-mode", opts.permission_mode,
        "--permission-prompts", "none", "--max-budget-usd", str(opts.budget),
    ]


def push_lock_env(cwd):
    """Environment of `claude -p`: push is impossible even through a guard gap (python -c, subshell...). Disables the pushurl of EVERY remote
    for that process only (GIT_CONFIG_*, nothing is written to the repo); the end delivery is done by this script, outside that environment."""
    env = dict(os.environ)
    env["LOOP_HEADLESS"] = "1"
    env.setdefault("LOOP_APP_HOME", APP_ROOT_DIR)  # the headless turn finds the app without depending on config.json
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
    flags = {"stdin": subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace", "cwd": cwd,
             "env": push_lock_env(cwd)}
    if IS_WIN:
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen(command, **flags)
    try:
        out, err = proc.communicate(timeout=timeout_min * 60)
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


def note_tolerated_denials(specs, turn, denials, progress):
    """Notes each tolerated denial in VALIDATE.md; the final summary and the PR show that file as 'For you to validate'."""
    path = os.path.join(specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md")
    current = read(path)
    lines = [
        "- [general] permission denied on turn %d (%s) | chose: carried on, the turn advanced (%s) | revert: check whether what was denied was needed"
        % (turn, denial_text(denial), progress)
        for denial in denials
    ]
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(("" if not current or current.endswith("\n") else "\n") + "\n".join(lines) + "\n")


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
    meta["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta_path = loop_state.loop_path(work, "meta.json")
    meta.setdefault("push", "not done")
    if status == "done" and opts.push:
        problem = identity_problem(work, opts)
        if problem:
            meta["push"] = "refused: " + problem
        else:
            try:
                delivered = delivery.finalize(work, meta["branch"])
            except Exception as error:  # the delivery never takes the notice down
                delivered = {"push": "FAILED: delivery.py: %s" % str(error)[:200], "pr": None, "pr_status": "not created"}
            meta["push"], meta["pr"], meta["pr_status"] = delivered["push"], delivered["pr"], delivered["pr_status"]
    save_json(meta_path, meta)
    try:
        graph_view.write(work)
    except OSError:
        pass
    summary = ""
    try:
        sys.argv = ["loop_summary.py", work, "--branch", meta.get("branch", ""), "--base", meta.get("base", "")]
        if opts.summary_dir:
            sys.argv += ["--out", opts.summary_dir]
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
    parser.add_argument("--wave", type=int, default=1, help="features per turn (only with disjoint PATHS); default 1")
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
    parser.add_argument("--git-user", default="", help="requires this git user.name on push (or LOOP_GIT_USER in the app .env); default: any configured account")
    parser.add_argument("--setup", default="", help='one-time command after creating the worktree (e.g. "npm ci")')
    parser.add_argument("--claude", default="", help="claude binary (or a test .py)")
    parser.add_argument("--option", type=int, default=0, metavar="N", help="answers the question by the NUMBER of the option listed in the notice (same as --answer with that option's text)")
    parser.add_argument("--answer", default="", help="answers the question that stopped the loop (awaiting_user) and continues; goes to ANSWERS.md")
    parser.add_argument("--max-cost", type=float, default=30.0, help="cap of US$ summed over all turns (0 turns it off); when exceeded, stops with an error")
    parser.add_argument("--rewind", type=int, default=None, metavar="N", help="go back to the state right after turn N (state files + git reset --hard of the loop branch). Without --yes it only shows the plan")
    parser.add_argument("--yes", action="store_true", help="confirms --rewind")
    parser.add_argument("--summary-dir", default="", help="folder of the final summary (default: loop-summary/ in the current directory)")
    parser.add_argument("--check", action="store_true", help="only checks the preflight, prints the ready command and exits (creates nothing)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-blocked", action="store_true", help="a quarantined feature NEVER pauses the loop, not even when another depends on it")
    parser.add_argument("--pause-on-blocked", action="store_true", help="a quarantined feature ALWAYS pauses and notifies (default: only pauses when another feature depends on the blocked one)")
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
    problems, warns = preflight(repo, opts)
    for item in warns:
        print("[check] WARNING:", item)
    for item in problems:
        print("[check] PROBLEM:", item)
    if opts.check:
        if not problems:
            print("[check] READY. Command:\n  %s" % ready_command(repo, opts))
        return 1 if problems else 0
    if problems:
        sys.exit("pre-flight failed, the loop did not start. Problems: " + "; ".join(problems))
    work, meta = ensure_worktree(repo, opts)
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
    cost = float(meta.get("cost_usd", 0))
    if not loop_state.load_snapshot(work, turn):  # baseline: state before this run's first turn (return point for --rewind)
        loop_state.snapshot(work, turn, git(work, "rev-parse", "HEAD")[1])
    try:
        while True:
            paused = pause_requested(work, turn)  # before spending the next turn
            if paused:
                print("[loop] PAUSE found: stopping before turn %d" % (turn + 1))
                return finish(work, meta, "awaiting_user", paused, opts)
            turn += 1
            rows = dirty_outside_specs(work, set(meta.get("copied", [])))
            if rows:
                return finish(work, meta, "error", "dirty tree outside .specs/ before turn %d (no automatic reset): %s" % (turn, "; ".join(rows[:5])), opts)
            _, current, _ = git(work, "branch", "--show-current")
            if current != meta["branch"]:
                code, _, err = git(work, "checkout", meta["branch"])
                if code != 0:
                    return finish(work, meta, "error", "worktree is off the loop branch and checkout failed: " + err, opts)
            applied = audit_before_turn(work)
            if applied:
                print("[loop] forgotten dependencies applied to the ROADMAP before the turn: " + "; ".join(applied))
            prompt = "/orchestrator continue --headless --turn %d --wave %d --branch %s%s%s" % (
                turn, opts.wave, meta["branch"], " --e2e" if opts.e2e else "", plan_flags(work, opts.wave))
            started = time.time()
            print("[loop] turn %d | %s" % (turn, datetime.datetime.now().strftime("%H:%M:%S")))
            code, out, err, timed_out = run_turn(claude_command(opts, prompt), work, opts.timeout)
            data = parse_result(out)
            log = dict(data) if data.get("ok") else dict(data, raw=out[-4000:])
            if err:
                log["stderr"] = err[-2000:]
            log["exit_code"], log["timeout"], log["turn"] = code, timed_out, turn
            save_json(os.path.join(logs, "%03d.json" % turn), log)
            cost += float(data.get("total_cost_usd") or 0)
            meta.update({"turns": turn, "cost_usd": round(cost, 4)})
            save_json(loop_state.loop_path(work, "meta.json"), meta)

            if opts.max_cost and cost > opts.max_cost:
                return finish(work, meta, "error", "total cost cap exceeded: US$ %.2f > %.2f (--max-cost); see the summary and decide whether to continue" % (cost, opts.max_cost), opts)
            if timed_out:
                return finish(work, meta, "error", "turn %d exceeded %d min (process terminated)" % (turn, opts.timeout), opts)
            unreadable = unrecognized_output(data)
            failed = code != 0 or data.get("is_error") or unreadable
            if failed and LIMIT_RE.search(out + err):
                limit_hits += 1
                if limit_hits > opts.limit_retries:
                    return finish(work, meta, "error", "account usage limit persisted after %d waits" % opts.limit_retries, opts)
                print("[loop] usage limit; waiting %d min (%d/%d)" % (opts.limit_wait, limit_hits, opts.limit_retries))
                time.sleep(opts.limit_wait * 60)
                turn -= 1
                continue
            limit_hits = 0
            if unreadable and code == 0:
                return finish(work, meta, "error", "claude output on turn %d was not recognized (%s; the CLI output format may have changed, "
                              "run `python harness.py doctor` to compare the installed version with the tested one): %s"
                              % (turn, data.get("detail") or "unrecognized", data.get("sample") or "(empty)"), opts)
            if failed:
                return finish(work, meta, "error", "claude exited with code %s on turn %d: %s" % (code, turn, (err or str(data.get("result") or data.get("sample") or ""))[:300]), opts)
            denials = data.get("permission_denials") or []
            risky = [d for d in denials if is_risky_denial(d, work)]
            if risky:
                return finish(work, meta, "error", "permission denied on turn %d (risky command, not tolerated): %s"
                              % (turn, "; ".join(denial_text(d) for d in risky)[:400]), opts)

            state = load_json(loop_state.state_file(work, "LOOP.json"))
            if state.get("turn") != turn or state.get("status") not in STATUS_OK:
                loop_state.snapshot(work, turn, git(work, "rev-parse", "HEAD")[1])  # the turn ran: the return point exists even with a bad LOOP.json
                return finish(work, meta, "error", "LOOP.json missing, stale or invalid after turn %d (got status=%r turn=%r; expected turn=%d and status in %s). "
                              "The orchestrator must write it with: python loop_state.py loop <root> --status S --turn %d ..." % (
                                  turn, state.get("status"), state.get("turn"), turn, sorted(STATUS_OK), turn), opts)
            # typed state: legal edges, tries, verified only with a commit, paths; saves this turn's snapshot for --rewind
            prev = loop_state.load_snapshot(work, turn - 1)
            _, head_now, _ = git(work, "rev-parse", "HEAD")
            new_commits = 0
            if prev:
                _, count, _ = git(work, "rev-list", "--count", "%s..%s" % (prev["head"], head_now))
                new_commits = int(count) if count.isdigit() else 0
            violations = loop_state.validate(read(os.path.join(prev["dir"], "ROADMAP.md")) if prev else "",
                                             read(os.path.join(specs, "ROADMAP.md")), state, new_commits, os.path.exists(loop_state.state_file(work, "ANSWERS.md")))
            loop_state.snapshot(work, turn, head_now)
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
                note_tolerated_denials(specs, turn, denials, progress)
                meta.setdefault("tolerated_denials", []).append({"turn": turn, "count": len(denials)})
                print("[loop] warning: %d permission denial(s) on turn %d, tolerated (%s); noted in .specs/project/loop/VALIDATE.md" % (len(denials), turn, progress))
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
            printed = fingerprint(work)
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
