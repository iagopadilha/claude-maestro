#!/usr/bin/env python3
"""Unsupervised /orchestrator loop: runs `claude -p` in turns until it finishes or gets stuck.

Usage:
  python run_loop.py <repo> [--wave 1] [--e2e] [--push] [--budget 5] [--timeout 60] [--max-stall 10]
                     [--branch NAME] [--setup "npm ci"] [--permission-mode auto] [--answer "text"] [--check] [--dry-run]

IMPORTANT (user decision): the git worktree exists ONLY for this script. It creates, ONCE, the sibling folder
`../<project>-worktree-loop` on branch `loop/<date>` and works there; the original repo (your folder, your branch) is never touched.
The orchestrator inside claude commits on that branch and NEVER pushes (the guard blocks it and the turn runs with every remote's
pushurl disabled). The delivery is made by this script, at the end, only if status=done, only from the feature branch
(the integration branch/main/... are refused), without force: push + PR to the integration branch (delivery.py; default `developer`,
env LOOP_INTEGRATION_BRANCH), with the link in the notice and in the summary.
Default: ON (--no-push turns it off). Require a git identity with --git-user or LOOP_GIT_USER in shared/.env.

How each turn works:
  claude -p "/orchestrator continue --headless --turn N --wave W ..." --output-format json
         --permission-mode <mode> --permission-prompts none --max-budget-usd <value>
  The process ends by itself when the model gives its final answer. If it "would wait for the user", nobody is there:
  the turn ends and the orchestrator writes .specs/project/LOOP.json {status: awaiting_user, ...}.

Stops (all notify through the LOOP_NOTIFY_CHANNELS channels: console, toast, file, email, webhook; see notify.py):
  done | awaiting_user | error (LOOP.json) | turn timeout | exit!=0 | permission denied |
  dirty tree outside .specs/ | LOOP.json missing/stale | identical state in 2 turns |
  --max-stall turns WITHOUT PROGRESS (default 10; the counter resets on every status advance in the ROADMAP) |
  account usage limit (waits --limit-wait min, up to --limit-retries times).
No overall turn cap: as long as there is progress, it continues.

Always on stop: writes .specs/loop-logs/meta.json and generates the summary in loop-summary/ (loop_summary.py, no Claude).
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
PLUGIN_ROOT = os.path.abspath(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.join(HERE, "..", "..", ".."))
sys.path.insert(0, HERE)
import loop_summary  # noqa: E402
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in shared/.env)
import delivery  # noqa: E402  (push of the feature branch + PR to the integration branch, only at the end)
import graph_view  # noqa: E402  (progress drawing: text, short, Mermaid)
import audit_deps  # noqa: E402  (forgotten dependency between features)

IS_WIN = os.name == "nt"
ACTIVE = {}  # run in progress (repo, opts, notify): lets us notify even when the script dies before finish()
# blocked is NOT progress: it scores 0, otherwise a loop that only blocks features would look like it advances (closes the roadmap but delivers nothing)
RANK = {"pending": 0, "speccing": 1, "specced": 2, "approved": 3, "building": 4, "verified": 6, "blocked": 0}
STATUS_OK = {"continue", "awaiting_user", "done", "error"}
LIMIT_RE = re.compile(r"rate.?limit|usage limit|limit reached|too many requests|\b429\b|overloaded|quota", re.I)
EXIT = {"done": 0, "awaiting_user": 10, "error": 20}


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
    """Stop notice that does not go through finish() (pre-flight, worktree, lock, unhandled exception): the user is always notified."""
    if not ACTIVE.get("notify"):
        return
    repo, opts = ACTIVE["repo"], ACTIVE["opts"]
    work = os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")
    work = work if os.path.isdir(work) else ""
    ctx = {"event": "error", "project": os.path.basename(repo), "reason": str(reason)[:1500], "script": os.path.abspath(__file__),
           "repo": repo, "flags": command_flags(opts), "worktree": work, "features": [], "commits": [], "options": [],
           "to_check": [], "next": None, "summary": "", "push": None}
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
    return flags


def build_context(work, meta, status, reason, opts, repo, summary):
    """Notice data (only what comes from LOOP.json, ROADMAP and git log; never project file contents)."""
    state = load_json(os.path.join(work, ".specs", "project", "LOOP.json"))
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
        "graph": graph_view.ascii_graph(graph_view.load(work)),
        "script": os.path.abspath(__file__), "repo": repo, "flags": command_flags(opts), "worktree": work,
    }


def identity_problem(cwd, opts):
    """Git identity for the push. Any account works; to require a specific one use --git-user or LOOP_GIT_USER (shared/.env)."""
    _, user, _ = git(cwd, "config", "user.name")
    _, mail, _ = git(cwd, "config", "user.email")
    if not user or not mail:
        return "git user.name/user.email not configured (git config --global user.name ...)"
    wanted = opts.git_user or notify.envload.get("LOOP_GIT_USER")
    if wanted and user != wanted:
        return "git user.name is %r, expected %r (--git-user or LOOP_GIT_USER in shared/.env)" % (user, wanted)
    return ""


def expand_plugin_vars(raw):
    """Expands ${CLAUDE_PLUGIN_ROOT} / $CLAUDE_PLUGIN_ROOT to the resolved plugin root (and ${HOME}-style variables)."""
    home = os.path.expanduser("~")
    for name in ("${CLAUDE_PLUGIN_ROOT}", "$CLAUDE_PLUGIN_ROOT"):
        raw = raw.replace(name, PLUGIN_ROOT)
    for name in ("${HOME}", "$HOME", "${USERPROFILE}"):
        raw = raw.replace(name, home)
    return os.path.expanduser(raw)


def check_guard_command(command, label, seen, found):
    """One hook command that calls guard.py: (a) interpreter on PATH, (b) target file exists on this machine."""
    match = re.match(r'\s*"?([^\s"]+)"?\s+(?:\\")?"?([^\s"\\]*guard\.py)', command)
    if not match:
        return
    interpreter, raw = match.group(1), match.group(2)
    if interpreter not in seen:
        seen.add(interpreter)
        if not shutil.which(interpreter):
            found.append("hooks call `%s`, which is not on this machine's PATH (macOS/Linux often only have python3: create an alias or adjust the hook command)" % interpreter)
    target = expand_plugin_vars(raw)
    if "$" in target:
        return  # another variable (e.g. CLAUDE_PROJECT_DIR): only Claude Code resolves it
    if not os.path.exists(target):
        found.append("%s points the guard at %s, which does not exist on this machine" % (label, raw))


def hook_path_problems():
    """The guard must be reachable: (a) python interpreter on PATH, (b) hooks/hooks.json exists and its guard target exists,
    (c) the guard target in the skills/orchestrator/SKILL.md frontmatter exists."""
    found, seen = [], set()
    hooks_file = os.path.join(PLUGIN_ROOT, "hooks", "hooks.json")
    if not os.path.isfile(hooks_file):
        found.append("hooks/hooks.json not found in the plugin root (%s)" % PLUGIN_ROOT)
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
            check_guard_command(command, "hooks/hooks.json", seen, found)
    skill = os.path.join(PLUGIN_ROOT, "skills", "orchestrator", "SKILL.md")
    text = read(skill)
    frontmatter = text.split("---", 2)[1] if text.startswith("---") and text.count("---") >= 2 else ""
    for match in re.finditer(r'command:\s*(.+)', frontmatter):
        if "guard.py" in match.group(1):
            check_guard_command(match.group(1).strip().strip("'"), "skills/orchestrator/SKILL.md", seen, found)
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


# ---------------------------------------------------------------- pre-flight (--check and before every run)
def preflight(repo, opts):
    """Checks everything the loop needs and returns (problems, warnings). Creates nothing."""
    problems, warns = [], []
    work = os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")
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
        problems.append("missing .specs/project/ROADMAP.md: generate and APPROVE the roadmap in a normal /orchestrator conversation first")
    else:
        grammar = loop_state.parse_roadmap(read(os.path.join(root, ".specs", "project", "ROADMAP.md")))
        problems += ["ROADMAP outside the grammar: " + item for item in grammar[1] + loop_state.check_fields(grammar[0])]
        score, total, done = roadmap_score(root)
        if not total:
            problems.append("ROADMAP.md has no feature line (`- [slug] status=...`)")
        elif done == total:
            problems.append("all %d features are already verified/blocked: nothing to do" % total)
        else:
            print("[check] ROADMAP: %d features, %d closed, %d to do" % (total, done, total - done))
    if not re.search(r"^Conformance:\s*(STANDARD|LEGACY)", read(os.path.join(root, "CLAUDE.md")), re.M):
        problems.append("CLAUDE.md has no `Conformance: STANDARD|LEGACY` line (classify_structure.py <repo> --write ...)")
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
    except Exception as error:  # the audit never takes the pre-flight down
        warns.append("dependency audit failed: %s" % error)
    env_problems, env_warns = environment_problems(opts, repo)
    problems += env_problems
    warns += env_warns
    guard = os.path.join(PLUGIN_ROOT, "skills", "orchestrator", "hooks", "guard.py")
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
                problems.append("mandatory notice channel missing from LOOP_NOTIFY_CHANNELS: %s" % name)
            elif notify.missing_config(name):
                problems.append("mandatory notice channel %s is missing %s in shared/.env" % (name, ", ".join(notify.missing_config(name))))
    for name in channels:
        missing = notify.missing_config(name)
        if name not in notify.CHANNELS:
            warns.append("unknown notice channel in LOOP_NOTIFY_CHANNELS: %s" % name)
        elif missing:
            warns.append("channel %s is on but is missing %s in shared/.env: it will be skipped" % (name, ", ".join(missing)))
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
    work = os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")
    meta_path = os.path.join(work, ".specs", "loop-logs", "meta.json")
    if os.path.isdir(work):
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
    """Records the user's answer in .specs/project/ANSWERS.md. The orchestrator's next turn reads it first and applies it."""
    state = load_json(os.path.join(work, ".specs", "project", "LOOP.json"))
    if state.get("status") != "awaiting_user":
        sys.exit("--answer only applies when the loop stopped at awaiting_user (current status: %s)" % (state.get("status") or "no LOOP.json"))
    path = os.path.join(work, ".specs", "project", "ANSWERS.md")
    block = "\n## Answer %s [pending]\nQuestion: %s\nUser answer: %s\n" % (
        datetime.datetime.now().isoformat(timespec="seconds"), str(state.get("reason") or "").strip()[:1500], text.strip())
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
    print("[loop] answer recorded in %s (the next turn applies it and carries on)" % path)


def rewind(repo, opts):
    """Time travel: restores the state (ROADMAP/STATE/LOOP/ANSWERS/RULES) and the loop branch HEAD to right after turn N."""
    work = os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")
    meta_path = os.path.join(work, ".specs", "loop-logs", "meta.json")
    meta = load_json(meta_path)
    if not os.path.isdir(work) or not meta.get("branch"):
        sys.exit("there is no loop worktree for %s; nothing to rewind" % repo)
    lock = os.path.join(work, ".specs", "project", "LOOP.lock")
    if os.path.exists(lock) and pid_alive(int(read(lock).strip() or 0)):
        sys.exit("a loop is running in this worktree; stop it before rewinding")
    snap = loop_state.load_snapshot(work, opts.rewind)
    if not snap:
        have = sorted(d for d in os.listdir(os.path.join(work, ".specs", "loop-logs", "snapshots"))) if os.path.isdir(os.path.join(work, ".specs", "loop-logs", "snapshots")) else []
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
    path = os.path.join(work, ".specs", "project", "LOOP.lock")
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
    save_json(os.path.join(specs, "LOOP.json"), state)
    return reason


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
    _, remotes, _ = git(cwd, "remote")
    names = remotes.split()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for name in names:
        env["GIT_CONFIG_KEY_%d" % count] = "remote.%s.pushurl" % name
        env["GIT_CONFIG_VALUE_%d" % count] = "push-disabled-by-loop://use-delivery.py"
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
    try:
        data = json.loads(text)
    except ValueError:
        return {}
    if isinstance(data, list):
        data = next((item for item in reversed(data) if isinstance(item, dict) and item.get("type") == "result"), {})
    return data if isinstance(data, dict) else {}


# ---------------------------------------------------------------- end
def finish(work, meta, status, reason, opts):
    meta["stopped"], meta["reason"] = status, reason
    meta["finished_at"] = datetime.datetime.now().isoformat(timespec="seconds")
    meta_path = os.path.join(work, ".specs", "loop-logs", "meta.json")
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


def main():
    parser = argparse.ArgumentParser(description="Unsupervised /orchestrator loop")
    parser.add_argument("repo")
    parser.add_argument("--wave", type=int, default=1, help="features per turn (only with disjoint PATHS); default 1")
    parser.add_argument("--e2e", action="store_true", help="authorizes the final flow-validation phase (verifier-flow, headless)")
    parser.add_argument("--push", action="store_true", default=None, help="requires the final push+PR (fails pre-flight if there is no remote/identity). Without the flag push+PR is already the DEFAULT (feature branch only)")
    parser.add_argument("--no-push", dest="push", action="store_false", help="do not push the branch nor open a PR at the end")
    parser.add_argument("--budget", type=float, default=5.0, help="max US$ per turn")
    parser.add_argument("--timeout", type=int, default=60, help="max minutes per turn")
    parser.add_argument("--max-stall", type=int, default=10, help="consecutive turns without progress before stopping")
    parser.add_argument("--limit-wait", type=int, default=30, help="minutes to wait when the usage limit is hit")
    parser.add_argument("--limit-retries", type=int, default=8)
    parser.add_argument("--permission-mode", default="auto", help="auto | acceptEdits | dontAsk")
    parser.add_argument("--branch", default="")
    parser.add_argument("--git-user", default="", help="requires this git user.name on push (or LOOP_GIT_USER in shared/.env); default: any configured account")
    parser.add_argument("--setup", default="", help='one-time command after creating the worktree (e.g. "npm ci")')
    parser.add_argument("--claude", default="", help="claude binary (or a test .py)")
    parser.add_argument("--option", type=int, default=0, metavar="N", help="answers the question by the NUMBER of the option listed in the notice (same as --answer with that option's text)")
    parser.add_argument("--answer", default="", help="answers the question that stopped the loop (awaiting_user) and continues; goes to ANSWERS.md")
    parser.add_argument("--max-cost", type=float, default=30.0, help="cap of US$ summed over all turns (0 turns it off); when exceeded, stops with an error")
    parser.add_argument("--rewind", type=int, default=None, metavar="N", help="go back to the state right after turn N (state files + git reset --hard of the loop branch). Without --yes it only shows the plan")
    parser.add_argument("--yes", action="store_true", help="confirms --rewind")
    parser.add_argument("--summary-dir", default="", help="folder of the final summary (default: loop-summary/ in the current directory)")
    parser.add_argument("--check", action="store_true", help="only checks the pre-flight, prints the ready command and exits (creates nothing)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--continue-on-blocked", action="store_true", help="a quarantined feature NEVER pauses the loop, not even when another depends on it")
    parser.add_argument("--pause-on-blocked", action="store_true", help="a quarantined feature ALWAYS pauses and notifies (default: only pauses when another feature depends on the blocked one)")
    parser.add_argument("--allow-silent", action="store_true", help="testing only: waives the mandatory notice channels (LOOP_NOTIFY_REQUIRED)")
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
            flags = " --wave %d%s%s%s" % (opts.wave, " --e2e" if opts.e2e else "", " --no-push" if not opts.push else "", (' --setup "%s"' % opts.setup) if opts.setup else "")
            python = os.path.splitext(os.path.basename(sys.executable))[0] or "python"
            print("[check] READY. Command:\n  %s \"%s\" \"%s\"%s" % (python, os.path.abspath(__file__), repo, flags))
        return 1 if problems else 0
    if problems:
        sys.exit("pre-flight failed, the loop did not start. Problems: " + "; ".join(problems))
    work, meta = ensure_worktree(repo, opts)
    if opts.dry_run:
        print("[dry-run] turn 1 command:", " ".join(claude_command(opts, "/orchestrator continue --headless --turn 1 --wave %d" % opts.wave)))
        return 0

    specs = os.path.join(work, ".specs", "project")
    if opts.option:
        options = load_json(os.path.join(specs, "LOOP.json")).get("options") or []
        if not 1 <= opts.option <= len(options):
            sys.exit("--option %d invalid: the notice listed %d option(s) (%s)" % (opts.option, len(options), "; ".join("%d) %s" % (i, o) for i, o in enumerate(options, 1)) or "none"))
        opts.answer = str(options[opts.option - 1])
    if opts.answer:
        write_answer(work, opts.answer)

    lock = acquire_lock(work)
    logs = os.path.join(work, ".specs", "loop-logs")
    turn = int(meta.get("turns", 0))
    stall, last_score, last_print, limit_hits = 0, roadmap_score(work)[0], "", 0
    cost = float(meta.get("cost_usd", 0))
    if not loop_state.load_snapshot(work, turn):  # baseline: state before this run's first turn (return point for --rewind)
        loop_state.snapshot(work, turn, git(work, "rev-parse", "HEAD")[1])
    try:
        while True:
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
            log = dict(data) if data else {"raw": out[-4000:]}
            if err:
                log["stderr"] = err[-2000:]
            log["exit_code"], log["timeout"], log["turn"] = code, timed_out, turn
            save_json(os.path.join(logs, "%03d.json" % turn), log)
            cost += float(data.get("total_cost_usd") or 0)
            meta.update({"turns": turn, "cost_usd": round(cost, 4)})
            save_json(os.path.join(logs, "meta.json"), meta)

            if opts.max_cost and cost > opts.max_cost:
                return finish(work, meta, "error", "total cost cap exceeded: US$ %.2f > %.2f (--max-cost); see the summary and decide whether to continue" % (cost, opts.max_cost), opts)
            if timed_out:
                return finish(work, meta, "error", "turn %d exceeded %d min (process terminated)" % (turn, opts.timeout), opts)
            failed = code != 0 or data.get("is_error")
            if failed and LIMIT_RE.search(out + err):
                limit_hits += 1
                if limit_hits > opts.limit_retries:
                    return finish(work, meta, "error", "account usage limit persisted after %d waits" % opts.limit_retries, opts)
                print("[loop] usage limit; waiting %d min (%d/%d)" % (opts.limit_wait, limit_hits, opts.limit_retries))
                time.sleep(opts.limit_wait * 60)
                turn -= 1
                continue
            limit_hits = 0
            if failed:
                return finish(work, meta, "error", "claude exited with code %s on turn %d: %s" % (code, turn, (err or str(data.get("result", "")))[:300]), opts)
            denials = data.get("permission_denials") or []
            if denials:
                return finish(work, meta, "error", "permission denied on turn %d: %s" % (turn, json.dumps(denials, ensure_ascii=False)[:400]), opts)

            state = load_json(os.path.join(specs, "LOOP.json"))
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
                                         read(os.path.join(specs, "ROADMAP.md")), state, new_commits, os.path.exists(os.path.join(specs, "ANSWERS.md")))
            loop_state.snapshot(work, turn, head_now)
            try:
                graph_view.write(work)  # .specs/project/GRAPH.md always current
            except OSError:
                pass
            if violations:
                return finish(work, meta, "error", "invalid state after turn %d (use --rewind %d to go back): %s" % (turn, turn - 1, "; ".join(violations[:4])), opts)
            paused = pause_on_quarantine(specs, prev, state, opts)
            if paused:
                return finish(work, meta, "awaiting_user", paused, opts)
            status = state["status"]
            reason = str(state.get("reason") or state.get("next") or "")
            if status == "done" and opts.e2e:
                pending = e2e_unmarked(specs)
                if pending:
                    return finish(work, meta, "error", "E2E phase ended without recording the result in the ROADMAP (e2e=ok|failed|not-covered): %s" % "; ".join(pending), opts)
            if status in ("done", "awaiting_user", "error"):
                return finish(work, meta, status, reason, opts)

            score, total, done = roadmap_score(work)
            if total and done == total and not opts.e2e:
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
        if isinstance(stop.code, str):  # sys.exit("message"): pre-flight, worktree, lock, --answer without a question...
            notify_early(stop.code)
        raise
    except Exception as error:
        import traceback
        traceback.print_exc()
        notify_early("unhandled exception in the script: %s: %s" % (type(error).__name__, error))
        return 20


if __name__ == "__main__":
    sys.exit(entry())
