#!/usr/bin/env python3
"""PreToolUse guard of the orchestrator (deterministic hook, does not depend on the model obeying).

Registered once in hooks/hooks.json (plugin agents ignore frontmatter hooks):
    python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py" --profile auto
`--profile auto` reads the hook payload (stdin JSON) and picks the profile from `agent_type` (a `plugin-name:` prefix is stripped):
  spec-writer / spec-reviewer / brownfield-mapper -> spec;  executor-back / executor-front / executor-generic -> executor;
  verifier / verifier-flow -> verifier;  anything else (main session, other agents) -> exit 0 at once, nothing enforced.
Explicit `--profile orchestrator|executor|spec|verifier` keeps working as before.
`--profile planning` is for the app's planning chat (`claude -p` with `--settings`, not an agent): spec's confinement (`specs-only`) without
`state-write`, because planning legitimately writes ROADMAP.md and FLOWS.md; the roadmap approval file stays blocked (`approval-write`).

Reads the hook JSON on stdin. Blocks with exit 2 + the reason on stderr.
Exception: a rule released in `.specs/project/loop/json/PERMISSIONS.json` (written by the orchestrator ONLY when the
user explicitly authorized it). A released rule never blocks, EXCEPT the hard rules (HARD): push to a protected branch
(integration branch, main...), forced push, writing the roadmap approval file and triggering the panel's user-click
endpoints (both belong to the user's click in the panel).
No exception and no match: exit 0.
Internal error: exit 0 (does not stall the loop), logs it AND warns through the configured notice channels
(`harness.py notify` of the Loop Harness app, when the app is installed), at most once an hour.

Loop files live in `.specs/project/loop/` (`json/` for machine state, `internal/` for logs and locks). A project still on
the old flat plugin layout (`.specs/project/PERMISSIONS.json`, `SCOPE.json`, `guard.log`...) keeps working: each file is
read from the new place first and from the flat place as a fallback, until `harness.py state migrate` moves it.

Covers the Bash and PowerShell tools (same command analysis).
"""
import fnmatch
import json
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tempfile
import time

READ_ONLY_GIT = {"status", "diff", "log", "show", "rev-parse", "ls-files", "blame", "describe", "grep", "shortlog"}
WRITE_SQL = re.compile(r"\b(insert|update|delete|drop|create|alter|truncate|replace|grant|revoke|call|merge)\b", re.I)
DB_TOOL = re.compile(r"^mcp__.*(mysql|postgres|mariadb|mssql|sqlite|sql|database)", re.I)
# generic write verbs on an MCP tool name (the part after the last `__`)
WRITE_VERB = re.compile(r"(create|insert|update|delete|remove|drop|write|set|add|put|post|patch|execute|exec|send|grant|revoke|assign|upsert|reorder|upload|publish)", re.I)
# MCP servers/tools that send or change something outside the machine (tickets, issues, chat, email...)
OUTBOUND_HINT = re.compile(r"(ticket|issue|jira|confluence|slack|mail|message|sms|chat|comment|notif|channel|teams|discord)", re.I)
OUTBOUND_VERB = re.compile(r"(create|update|delete|add|send|post|reply|comment|transition|edit|publish)", re.I)
INSTALL = re.compile(
    r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|ci|update|upgrade)\b|\bdotnet\s+(restore|add\s+package)\b|\bpip\s+install\b",
    re.I,
)
# credentials: .secrets/, the plugin's shared .env, a project's .env.test (test VALUES; TEST-ENVIRONMENT.md only holds names), the app's data folder (.env, and app.json with the panel token), SSH keys.
# `[\\/]+` because a Read/Glob/Grep input is checked as JSON text, where a Windows backslash comes doubled.
SECRET_HINT = re.compile(
    r"\.secrets[\\/]|[\\/]_?shared[\\/]+\.env(?!\.example)|\.env\.test(?!\.example)|\.loop-harness[\\/]+(?:\.env(?!\.example)|app\.json)|\bid_(rsa|ed25519)\b", re.I
)
# state only the orchestrator writes: the project files it owns and the WHOLE loop folder (.specs/project/loop/).
# The flat names after `loop` are the old plugin layout (read until migrated). PROJECT.md and TEST-ENVIRONMENT.md stay editable.
STATE_FILE = re.compile(
    r"\.specs[\\/]project[\\/](ROADMAP\.md|FLOWS\.md|STATE\.md|RULES\.md|phases[\\/]|archive[\\/]|loop(?:[\\/]|$)|"
    r"VALIDATE\.md|DEPENDENCIES\.md|GRAPH\.md|ANSWERS\.md|NOTICE\.txt|rules\.tests\.json|SCOPE\.json|LOOP\.json|LOOP\.lock|"
    r"PERMISSIONS\.json|ROADMAP-APPROVED\.json|delivery\.json)", re.I
)
# the roadmap approval is the user's click in the panel: nobody running under the guard writes it, not even the orchestrator
APPROVAL_FILE = re.compile(r"\.specs[\\/]project[\\/](?:loop[\\/](?:json[\\/])?)?ROADMAP-APPROVED\.json", re.I)
# panel endpoints that only the user's click triggers (start/check the loop, approve the roadmap, Act, answer/resume/instruct)
# (a full URL, so grepping the docs for the route text is not a call)
PANEL_TRIGGER_RE = re.compile(r"https?://[^/\s\"']+/p/[^/\s\"']+/api/(?:loop/|roadmap/approve\b|action/|answer\b|resume\b|instruction\b)", re.I)

# loop files: `.json` in loop/json/, `.md` and NOTICE.txt at loop/, the rest (guard.log, locks, markers) in loop/internal/
LOOP_FOLDER = ("project", "loop")
LEGACY_LOOP_FOLDER = ("project",)  # old flat plugin layout
APP_DATA_DIR_NAME = ".loop-harness"


def prod_hints():
    """Substrings that mark an MCP tool as production: env GUARD_PROD_HINTS (comma-separated), default prod,production."""
    raw = os.environ.get("GUARD_PROD_HINTS")
    if raw is None:
        raw = "prod,production"
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def loop_subfolder(name):
    """Same path rule as the engine (`loop_state.loop_path`): "" = the loop folder itself."""
    if name.endswith(".md") or name == "NOTICE.txt":
        return ""
    return "json" if name.endswith(".json") else "internal"


def loop_file(specs_dir, name):
    """Path of a loop file: the new layout when the file exists there, else the old flat plugin path when THAT exists (so a
    block rule does not vanish in a project that was not migrated yet), else the new path."""
    parts = list(LOOP_FOLDER) + ([loop_subfolder(name)] if loop_subfolder(name) else [])
    current = os.path.join(specs_dir, *parts, name)
    if os.path.exists(current):
        return current
    legacy = os.path.join(specs_dir, *LEGACY_LOOP_FOLDER, name)
    if os.path.exists(legacy):
        return legacy
    return current


def integration_branch():
    return (os.environ.get("LOOP_PR_BASE") or os.environ.get("LOOP_INTEGRATION_BRANCH") or "developer").strip().lower()


# ---- push: text of the whole command (catches `python -c "...git push..."`, `$(git push)`, quotes), besides the segment
PROTECTED_NAMES = {"developer", "develop", "main", "master", "release", "production", "dev", integration_branch()}
_PROT = "|".join(re.escape(name) for name in sorted(PROTECTED_NAMES, key=len, reverse=True))
PUSH_CMD = r"(?:\bgit(?:\.exe)?['\"]?(?:\s+-[-\w]+(?:[= ]\S+)?)*\s+push\b|['\"]git(?:\.exe)?['\"]\s*,\s*['\"]push['\"])"
PUSH_RE = re.compile(PUSH_CMD, re.I)
PUSH_PROTECTED_RE = re.compile(PUSH_CMD + r"[^\n]*?(?<![\w.-])(?:" + _PROT + r")(?![\w.-])", re.I)
PUSH_FORCE_RE = re.compile(PUSH_CMD + r"[^\n]*?(?:\s--force\b|\s--force-with-lease\b|\s-f\b|\s\+\S)", re.I)

# scripts that WRITE project state: only the orchestrator runs them (rule, applied dependency, written graph, LOOP.json).
# The same scripts through the app CLI: `harness.py rules write`, `harness.py audit ... --write|--apply`,
# `harness.py graph ... --write`, `harness.py state loop|migrate`.
HARNESS_CLI = r"\bharness\.py[\"']?\s+"
STATE_SCRIPT_RE = re.compile(
    r"\brules\.py\s+write\b|\baudit_deps\.py\b[^\n]*--(?:write|apply)\b|\bgraph_view\.py\b[^\n]*--write\b|\bloop_state\.py\s+(?:loop|migrate)\b"
    r"|" + HARNESS_CLI + r"rules\s+write\b|" + HARNESS_CLI + r"audit\b[^\n]*--(?:write|apply)\b|" + HARNESS_CLI + r"graph\b[^\n]*--write\b"
    r"|" + HARNESS_CLI + r"state\s+(?:loop|migrate)\b", re.I)
DELIVERY_RE = re.compile(r"\bdelivery\.py\b|" + HARNESS_CLI + r"deliver\b", re.I)
# the loop and the panel belong to the user (or to the orchestrator at the user's explicit request): never a subagent
LOOP_RUN_RE = re.compile(r"\brun_loop\.py\b|" + HARNESS_CLI + r"(?:loop|check|serve)\b", re.I)

# ---- shell writes (redirection, tee, Set-Content, sed -i...): only the targets that must NEVER be touched
REDIRECT_RE = re.compile(r"(?<![\d&<])>>?\s*(?!&)(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)")
CMD_TARGET_RE = re.compile(r"\b(?:tee(?:\s+-a)?|Set-Content|Add-Content|Out-File)\s+(?:-\w+\s+)*(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)", re.I)
SED_INPLACE_RE = re.compile(r"\bsed\s+-i\S*\s+(?:-e\s+)?(?:'[^']*'|\"[^\"]*\"|\S+)\s+(\S+)")
NULL_TARGETS = {"/dev/null", "$null", "nul", "null"}

# rules that not even PERMISSIONS.json releases: workflows never push to the integration branch/main nor force, and the
# roadmap approval and the panel's trigger endpoints are only ever the user's click
HARD = {"push-protected", "push-force", "approval-write", "panel-trigger", "grafana-write", "headless-push", "headless-gh", "permissions-write"}

# headless loop (LOOP_HEADLESS=1, exported by the engine): delivery belongs to the engine, so the session only commits;
# PERMISSIONS.json holds the user's releases, so the session never writes it (any profile, HARD)
HEADLESS_RULES = ["headless-push", "headless-gh", "permissions-write"]
PERMISSIONS_FILE = re.compile(r"(?:^|[\\/])PERMISSIONS\.json$", re.I)
PERMISSIONS_CMD_RE = re.compile(r"\brules\.py\s+(?:write|allow|release)\b|" + HARNESS_CLI + r"rules\s+(?:write|allow|release)\b", re.I)
PERMISSIONS_MSG = "PERMISSIONS.json holds the user's releases: only the user changes it, never the loop"
HEADLESS_MSG = "git push / gh: delivery belongs to the engine (harness.py deliver / Finish); in a headless loop you only commit"


def is_headless():
    return os.environ.get("LOOP_HEADLESS") == "1"

PROFILES = {
    # implements code: everything blocked except editing inside the project
    "executor": ["git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "install", "state-write", "project-deny", "path-scope", "repo-only", "delivery", "state-script", "loop-run", "approval-write", "panel-trigger", "grafana-write", "sql-write", "migration-target"],
    # only reads and runs checks
    "verifier": ["git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "state-write", "project-deny", "delivery", "state-script", "loop-run", "approval-write", "panel-trigger", "grafana-write", "sql-write", "migration-target"],
    # writes spec/map: only inside .specs/, .regression/ and docs/codebase/ (the map); never code
    "spec": ["specs-only", "map-docs", "git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "state-write", "project-deny", "delivery", "state-script", "loop-run", "approval-write", "panel-trigger", "grafana-write", "sql-write"],
    # the orchestrator chat: may commit (one per feature) and write state; does not push (only via `harness.py deliver`), does not
    # touch PROD and does not write to tickets/issues/messages (outbound-msg): the main action is never in the feature's scope.
    # It never approves the roadmap nor clicks for the user in the panel (approval-write, panel-trigger).
    # repo-only: it commits and writes only inside the project's worktrees (SCOPE.json `roots`) and the primary .specs/.
    "orchestrator": ["git-push", "pr-release", "git-add-all", "prod-write", "secrets", "project-deny", "outbound-msg", "approval-write", "panel-trigger", "repo-only", "grafana-write", "sql-write"],
    # the app's planning chat (a `claude -p`, not an agent): like spec (only .specs/), but WITHOUT state-write: it writes ROADMAP.md and
    # FLOWS.md itself. ROADMAP-APPROVED.json stays blocked (approval-write): the approval is the user's click in the panel.
    "planning": ["specs-only", "secrets", "approval-write", "panel-trigger", "git-write", "git-push", "pr-release", "prod-write", "outbound-msg", "grafana-write", "sql-write", "capabilities-block"],
    "roadmapmap": ["specs-only", "map-docs", "secrets", "approval-write", "panel-trigger", "git-write", "git-push", "pr-release", "prod-write", "outbound-msg", "grafana-write", "sql-write", "capabilities-block"],  # roadmap generation with --map-first: planning + docs/codebase
    # the app's subproject validation job (a `claude -p`): the planning hardening, but it writes ONE file, the suggestion the server validates
    "subprojects": ["specs-only", "subprojects-only", "secrets", "approval-write", "panel-trigger", "git-write", "git-push", "pr-release", "prod-write", "outbound-msg", "grafana-write", "sql-write", "capabilities-block"],
    # the app's system map job (a `claude -p`): the planning hardening, but it writes ONE file, docs/codebase/SYSTEM.md of the repository root
    "systemmap": ["specs-only", "system-only", "secrets", "approval-write", "panel-trigger", "git-write", "git-push", "pr-release", "prod-write", "outbound-msg", "grafana-write", "sql-write", "capabilities-block"],
}

# hook payload `agent_type` (without the `plugin-name:` prefix) -> profile, used by `--profile auto`
AGENT_PROFILES = {
    "spec-writer": "spec", "spec-reviewer": "spec", "brownfield-mapper": "spec",
    "executor-back": "executor", "executor-front": "executor", "executor-generic": "executor",
    "verifier": "verifier", "verifier-flow": "verifier",
}


def specs_dir_from_env():
    """LOOP_SPECS_DIR: absolute path of the PRIMARY `.specs` folder, exported by the engine in the `claude -p` environment (hooks inherit it).
    SCOPE.json lives only there, so a hook whose cwd is another worktree would find nothing walking up. Ignored unless it is an
    existing folder named `.specs`."""
    value = os.environ.get("LOOP_SPECS_DIR", "").strip().strip("\"'")
    if value and os.path.isabs(value) and os.path.isdir(value) and os.path.basename(os.path.normpath(value)) == ".specs":
        return os.path.normpath(value)
    return None


def find_specs_dir(cwd):
    """LOOP_SPECS_DIR, else walking up from `cwd`: the `.specs` of the git root (the folder holding `.git`, a repo or a worktree) wins
    over a nearer one. A stray `.specs` inside the repo (a test run in a subfolder) would otherwise become the root, and every repo
    file outside that subfolder would be blocked as outside the worktrees."""
    from_env = specs_dir_from_env()
    if from_env:
        return from_env
    here = os.path.abspath(cwd or os.getcwd())
    nearest = None
    for _ in range(8):
        candidate = os.path.join(here, ".specs")
        if os.path.isdir(candidate):
            nearest = nearest or candidate
        if os.path.exists(os.path.join(here, ".git")):
            return candidate if os.path.isdir(candidate) else nearest
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return nearest


def load_permissions(specs_dir):
    empty = {"allow": [], "deny_bash_regex": [], "deny_path_regex": []}
    if not specs_dir:
        return empty
    path = loop_file(specs_dir, "PERMISSIONS.json")
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        merged = dict(empty)
        merged.update(data)
        return merged
    except FileNotFoundError:
        return empty
    except Exception as error:  # unreadable file: releases nothing
        sys.stderr.write("guard: PERMISSIONS.json unreadable (%s); no exception applied\n" % error)
        return empty


ATTEMPT_LIMIT = 200
TURN_FILE_RE = re.compile(r"\d+\.json")


def current_turn(specs_dir):
    """Number of the loop turn in progress, or None when unknown. Known only inside the headless loop (the engine exports
    LOOP_HEADLESS=1 to `claude -p`; LOOP_TURN, when the engine exports it, wins): the turn in progress is the last finished
    `internal/turns/NNN.json` + 1. Outside the loop (planning chat, interactive session) there is no turn: None, never a guess."""
    if not specs_dir:
        return None
    try:
        exported = os.environ.get("LOOP_TURN", "").strip()
        if exported.isdigit() and int(exported) > 0:
            return int(exported)
        if os.environ.get("LOOP_HEADLESS") != "1":
            return None
        folder = os.path.join(specs_dir, *LOOP_FOLDER, "internal", "turns")
        if not os.path.isdir(folder):
            folder = os.path.join(specs_dir, *LEGACY_LOOP_FOLDER, "turns")
        numbers = [int(name[:-5]) for name in os.listdir(folder) if TURN_FILE_RE.fullmatch(name)]
        return (max(numbers) if numbers else 0) + 1
    except Exception:
        return None


def describe_attempt(payload):
    """What the agent tried, in one short line: the shell command, the file of a write/read, else the tool name."""
    try:
        tool = str(payload.get("tool_name") or "")
        tool_input = payload.get("tool_input") or {}
        if tool in ("Bash", "PowerShell"):
            text = tool_input.get("command", "")
        elif tool in ("Write", "Edit", "NotebookEdit", "Read"):
            text = "%s %s" % (tool, tool_input.get("file_path") or tool_input.get("notebook_path") or "")
        else:
            text = tool
        return " ".join(str(text).split())[:ATTEMPT_LIMIT]
    except Exception:
        return ""


def log(specs_dir, profile, rule, decision, detail, attempt=None):
    if not specs_dir:
        return
    try:
        line = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "turn": current_turn(specs_dir),
            "profile": profile,
            "rule": rule,
            "decision": decision,
            "detail": detail[:200],
        }
        if attempt:
            line["attempt"] = attempt
        log_path = loop_file(specs_dir, "guard.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def notify_command():
    """`python <app>/harness.py notify` of the Loop Harness app: LOOP_APP_HOME, else `app_root_dir` of
    `<LOOP_DATA_DIR or ~/.loop-harness>/config.json` (written by `harness.py install`). No app: [] (the alert stays in guard.log)."""
    try:
        app = os.environ.get("LOOP_APP_HOME", "")
        if not app:
            data = os.environ.get("LOOP_DATA_DIR") or os.path.join(os.path.expanduser("~"), APP_DATA_DIR_NAME)
            with open(os.path.join(data, "config.json"), encoding="utf-8") as handle:
                app = json.load(handle).get("app_root_dir", "")
        cli = os.path.join(app, "harness.py") if app else ""
        return [sys.executable, cli, "notify"] if cli and os.path.isfile(cli) else []
    except Exception:
        return []


def alert(specs_dir, profile, message):
    """A fail-open must not be silent: logs it and warns through the LOOP_NOTIFY_CHANNELS channels (`harness.py notify`),
    once an hour. Never raises and never waits for the send (separate process)."""
    log(specs_dir, profile, "guard-failopen", "error", message)
    try:
        if specs_dir:
            marker = loop_file(specs_dir, ".guard-alert")
            os.makedirs(os.path.dirname(marker), exist_ok=True)
        else:
            marker = os.path.join(tempfile.gettempdir(), "loop-guard-alert")
        if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < 3600:
            return
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
        notify = notify_command()
        if not notify:
            return
        body = ("The orchestrator guard failed internally and LET THE ACTION THROUGH (fail-open, so the loop is not stalled).\n"
                "Profile: %s\nReason: %s\n\nSee .specs/project/loop/internal/guard.log and run the guard test suite." % (profile, message[:300]))
        subprocess.Popen(notify + ["--subject", "[guard] internal failure: the gatekeeper let it through", "--body", body],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def split_commands(command):
    return [part.strip() for part in re.split(r"&&|\|\||;|\||\n|&(?!&)", command) if part.strip()]


def git_subcommand(segment):
    # also `"git" push`, `'git' commit` and `& "C:\Program Files\Git\bin\git.exe" push` (quoted executable, PowerShell call operator)
    match = re.match(r"^(?:&\s*)?['\"]?(?:[^'\"\s]*[\\/]|[^'\"]*[\\/](?=git(?:\.exe)?['\"]))?git(?:\.exe)?['\"]?\s+(?:-[-\w]+(?:[= ]\S+)?\s+)*([a-z][\w-]*)", segment)
    return match.group(1) if match else None


def current_branch(cwd):
    try:
        out = subprocess.run(["git", "-C", cwd or ".", "branch", "--show-current"], capture_output=True, text=True, timeout=5)
        return out.stdout.strip()
    except Exception:
        return ""


def default_push(segment):
    """`git push` / `git push origin` (no refspec): pushes the current branch."""
    after = re.split(r"\bpush\b", segment, maxsplit=1)[1] if re.search(r"\bpush\b", segment) else ""
    positional = [token for token in after.split() if not token.startswith("-")]
    return len(positional) <= 1


def shell_write_targets(command):
    """Write targets the shell hits without going through the Write/Edit tools. Best effort, only to protect what must never be touched."""
    found = []
    for pattern in (REDIRECT_RE, CMD_TARGET_RE, SED_INPLACE_RE):
        for match in pattern.finditer(command):
            target = match.group(1).strip("\"'")
            if target and target.lower() not in NULL_TARGETS:
                found.append(target)
    return found


TEMP_HINT = re.compile(r"[\\/](tmp|temp|scratchpad)[\\/]|^/tmp/", re.I)


# Paths are compared case-insensitively only where the filesystem is (Windows, macOS default); on a case-sensitive one the original
# case is kept, so an os.path.exists() on a normalized path still hits the real file. Tests flip this flag to exercise the POSIX branch.
CASE_INSENSITIVE = os.name == "nt" or sys.platform == "darwin"


def fold(text):
    return text.lower() if CASE_INSENSITIVE else text


def norm(path):
    return fold(os.path.normpath(path).replace("\\", "/").rstrip("/"))


def normalize_path(path, base=None, fold_case=None):
    """Canonical, lowercase, forward-slash absolute form of a path as a tool or a shell may spell it: backslashes, `//` collapsed,
    `.` and `..` resolved, git-bash `/c/...` and WSL `/mnt/c/...` drive forms turned into `c:/...` (Windows), a relative path resolved
    against `base` (the hook's cwd)."""
    text = str(path).strip().strip("\"'").replace("\\", "/")
    text = re.sub(r"/{2,}", "/", text)
    if os.name == "nt":
        text = re.sub(r"^/(?:mnt/)?([a-zA-Z])(?=/|$)", r"\1:", text)
    if not (text.startswith("/") or re.match(r"^[a-zA-Z]:/", text)) and base:
        text = normalize_path(base, None, False) + "/" + text
    text = posixpath.normpath(text).rstrip("/")
    return text.lower() if (CASE_INSENSITIVE if fold_case is None else fold_case) else text


def has_git_ancestor(absolute):
    """True when a folder above `absolute` holds a `.git` entry (folder or file): the path is inside a git working tree."""
    here = os.path.dirname(absolute.rstrip("/"))
    while here:
        if os.path.exists(os.path.join(here, ".git")):
            return True
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return False


def temp_exempt(absolute):
    """Genuine temp files only: a tmp/temp/scratchpad segment AND not inside any git working tree (a repo that happens to live under such a folder is code)."""
    return bool(TEMP_HINT.search(absolute + "/")) and not has_git_ancestor(absolute)


SLUG_MARK = re.compile(r"SCOPE_FEATURE:\s*([A-Za-z0-9._-]+)")


TASK_MARK = re.compile(r"SCOPE_TASK:\s*([A-Za-z0-9._-]+/T\d+)")


def executor_task(payload):
    """Same as executor_slug, for the `SCOPE_TASK: <slug>/T<n>` line of the dispatch prompt. None = no task mark (feature scope applies)."""
    return executor_slug(payload, TASK_MARK)


def long_path(path):
    """Windows: a transcript under ~/.claude/projects/<long project name>/... easily passes MAX_PATH (260), and plain open() then
    fails, so the executor identity was silently lost (seen in the real audit loop: 272 chars). The `\\\\?\\` prefix lifts the limit."""
    if os.name != "nt" or path.startswith("\\\\?\\"):
        return path
    absolute = os.path.abspath(path)
    return "\\\\?\\" + absolute if len(absolute) >= 248 else path


def executor_slug(payload, mark=None):
    """Which feature does this executor serve? Best effort: reads the subagent transcript (if the hook brings agent_id) and
    looks for the `SCOPE_FEATURE: <slug>` line of the dispatch prompt. None = could not tell (falls back to the union of the scopes)."""
    mark = mark or SLUG_MARK
    try:
        agent_id = payload.get("agent_id")
        transcript = payload.get("agent_transcript_path")
        if not transcript and agent_id and payload.get("transcript_path"):
            base = os.path.splitext(payload["transcript_path"])[0]
            transcript = os.path.join(base, "subagents", "agent-%s.jsonl" % agent_id)
        transcript = long_path(transcript) if transcript else transcript
        if not transcript or not os.path.isfile(transcript):
            return None
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            match = mark.search(handle.read(40000))
        return match.group(1) if match else None
    except Exception:
        return None


def read_scope_file(specs_dir):
    """SCOPE.json as a dict ({} when absent or unreadable)."""
    try:
        with open(loop_file(specs_dir, "SCOPE.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load_scope(specs_dir, slug=None, task=None):
    """Reads .specs/project/loop/json/SCOPE.json: {"roots": [...], "features": {"slug": ["folder/or/file", ...]}, "tasks": {"slug/T1": [...]}} written by the orchestrator.
    An entry is absolute (a path in any worktree) or relative (to the primary root, the folder holding `.specs/`).
    With a known `task` present in the "tasks" map, only that task's paths; else with a known `slug` present in the file, only that
    feature's paths; otherwise the union of all in-flight features."""
    data = read_scope_file(specs_dir)
    features = data.get("features") or {}
    try:
        tasks = data.get("tasks") or {}
        if task and isinstance(tasks, dict) and task in tasks:
            return list(tasks[task])
        if slug and slug in features:
            return list(features[slug])
        paths = []
        for entries in features.values():
            paths.extend(entries)
        return paths
    except Exception:
        return []


def project_roots(specs_dir):
    """Normalized worktree roots: the primary root (folder holding `.specs/`) first, then the absolute `roots` of SCOPE.json."""
    roots = [norm(os.path.dirname(specs_dir))]
    for item in read_scope_file(specs_dir).get("roots") or []:
        if isinstance(item, str) and os.path.isabs(item) and norm(item) not in roots:
            roots.append(norm(item))
    return roots


def inside_any(absolute, roots):
    return any(absolute == root or absolute.startswith(root + "/") for root in roots)


def load_copied(specs_dir):
    """Absolute, normalized paths of the config files copied into the worktrees (SCOPE.json `copied`, from REPOS.json `copy`)."""
    items = read_scope_file(specs_dir).get("copied") or []
    return [norm(item) for item in items if isinstance(item, str) and os.path.isabs(item)]


CANDIDATE_SPLIT_RE = re.compile(r'''[\s"'`<>|;&(){},=]+''')


def mentions_copied_normalized(text, cwd, copied, roots):
    """Every path-looking token of `text`, normalized (see normalize_path), compared with the copied absolute paths and with their
    tail inside each listed worktree root (so `../wt/./.env.local`, `R://wt/.env.local` or `/r/wt/.env.local` are the same file)."""
    base = cwd or (roots[0] if roots else None)
    tails = set()
    for path in copied:
        for root in roots:
            if path.startswith(root + "/"):
                tails.add(path[len(root) + 1:])
    for token in CANDIDATE_SPLIT_RE.split(text.replace("\\", "/")):
        if not token or not re.search(r"[./\\]", token):
            continue
        absolute = normalize_path(token, base)
        if absolute in copied:
            return True
        for root in roots:
            if absolute.startswith(root + "/") and absolute[len(root) + 1:] in tails:
                return True
    return False


def mentions_copied_secret(text, cwd, specs_dir):
    """True when `text` (a command segment, a file path or a tool input) names a copied config file: by full path, or by its
    path inside the worktree when `cwd` is that worktree. Only the NAME is matched: a build that merely loads the file is not a tool call on it."""
    if not specs_dir or not text:
        return False
    copied = load_copied(specs_dir)
    if not copied:
        return False
    flat = fold(text.replace("\\\\", "/").replace("\\", "/"))
    here = norm(cwd) if cwd else ""
    roots = project_roots(specs_dir)
    if mentions_copied_normalized(text, cwd, copied, roots):
        return True
    for path in copied:
        if path in flat:
            return True
        for root in roots:
            if path.startswith(root + "/") and (here == root or here.startswith(root + "/")):
                relative = path[len(root) + 1:]
                if re.search(r"(?<![\w./-])" + re.escape(relative) + r"(?![\w-])", flat):
                    return True
    return False


# ---- broad reads that would reach a copied config file without NAMING it (rg --no-ignore, grep -r, find -exec cat, globs...)
GLOB_CHARS = re.compile(r"[*?\[\]{}]")
# ripgrep (and ag/ack): the flags that switch the ignore rules off
IGNORE_OFF_RE = re.compile(r"\s(?:--no-ignore\S*|--unrestricted|-[a-zA-Z]*u[a-zA-Z]*)(?=\s|$)", re.I)
IGNORE_AWARE_RE = re.compile(r"(?:^|[\s|(])(?:rg|ripgrep|ag|ack)(?:\.exe)?\s", re.I)
GREP_RE = re.compile(r"(?:^|[\s|(])(?:grep|egrep|fgrep|ugrep|ug)(?:\.exe)?\s", re.I)
GREP_RECURSIVE_RE = re.compile(r"\s(?:--recursive|--dereference-recursive|-d\s*recurse|-[a-zA-Z]*[rR][a-zA-Z]*)(?=\s|$)")
GIT_GREP_BYPASS_RE = re.compile(r"\bgit(?:\.exe)?\s+(?:-[-\w]+(?:[= ]\S+)?\s+)*grep\b[^\n]*?\s--(?:no-index|no-exclude-standard)\b", re.I)
READ_TOOLS = r"(?:cat|grep|egrep|fgrep|head|tail|less|more|strings|xxd|od|base64|cp|sed|awk|type|rg|bat|tac|nl)"
FIND_RE = re.compile(r"(?:^|[\s|(])find(?:\.exe)?\s", re.I)
FIND_READ_RE = re.compile(r"(?:-exec(?:dir)?|-ok(?:dir)?|\|\s*xargs(?:\s+-\S+)*)\s+(?:\S*[\\/])?" + READ_TOOLS + r"\b", re.I)
GCI_RE = re.compile(r"(?:^|[\s|(])(?:Get-ChildItem|gci|dir|ls)(?:\s[^|]*?)?\s-(?:Recurse|r)(?=\s|$)", re.I)
PIPE_READER_RE = re.compile(r"\b(?:Select-String|sls|Get-Content|gc|cat|type|findstr|xargs|grep|egrep|rg|head|tail|more|less|ForEach-Object|foreach)\b|\|\s*%", re.I)
FINDSTR_RE = re.compile(r"(?:^|[\s|(])findstr(?:\s[^|]*?)?\s/[a-z]*s[a-z]*(?=\s|$)", re.I)
WILDCARD_READER_RE = re.compile(r"\b(?:Select-String|sls|Get-Content|gc|findstr)\b", re.I)
COPY_TREE_RE = re.compile(
    r"(?:^|[\s|(])(?:(?:cp|rsync)(?:\.exe)?(?:\s[^|]*?)?\s(?:--recursive|--archive|-[a-zA-Z]*[rRa][a-zA-Z]*)(?=\s|$)"
    r"|Copy-Item(?:\s[^|]*?)?\s-Recurse\b|xcopy(?:\s[^|]*?)?\s/[se]\b|robocopy\s|zip\s|Compress-Archive\s|tar\s+-?[a-zA-Z]*c)", re.I)


def expand_braces(pattern):
    found = re.search(r"\{([^{}]*)\}", pattern)
    if not found:
        return [pattern]
    options = []
    for alternative in found.group(1).split(","):
        options.extend(expand_braces(pattern[:found.start()] + alternative + pattern[found.end():]))
    return options


def last_segment(pattern):
    return fold(re.split(r"[\\/]", pattern.strip("\"'"))[-1])


def glob_covers(pattern, names):
    """Plain glob match of the last segment of `pattern` against the copied file names (a bare `*` covers everything)."""
    return any(fnmatch.fnmatchcase(name, option) for option in expand_braces(last_segment(pattern)) for name in names)


def glob_names_copied(pattern, names, quoted=False):
    """Does a glob single out a copied file? Only a SPECIFIC glob counts (a letter/digit in the fixed part, e.g. `.env*`, `*.local`,
    or an unquoted `.*`): a bare `*` or `**/*` lists everything and is not an attempt at the secret. `!negated` globs never count."""
    if pattern.strip("\"'").startswith("!"):
        return False
    for option in expand_braces(last_segment(pattern)):
        literal = GLOB_CHARS.sub("", option)
        specific = bool(re.search(r"[a-z0-9]", literal)) or (option.startswith(".") and not quoted)
        if specific and any(fnmatch.fnmatchcase(name, option) for name in names):
            return True
    return False


def option_values(plain, long_names, short_names=()):
    """Values of `--name=value`, `--name value` and `-n value` options in a tokenised command (names compared case-insensitively)."""
    values = []
    for index, token in enumerate(plain):
        lowered = token.lower()
        for name in long_names:
            if lowered.startswith(name + "="):
                values.append(token[len(name) + 1:].strip("\"'"))
            elif lowered == name and index + 1 < len(plain):
                values.append(plain[index + 1])
        if lowered in short_names and index + 1 < len(plain):
            values.append(plain[index + 1])
    return values


def target_dirs(tokens, cwd):
    """Folders a command works on: each token that is an existing folder (or the folder part of a glob), else the cwd when no token
    names an existing file or folder (`grep -r x` with no path reads the cwd)."""
    dirs, anything = [], False
    for token in tokens:
        bare = token.strip("\"'")
        if not bare or bare.startswith("-"):
            continue
        if GLOB_CHARS.search(bare):
            parts = re.split(r"([\\/])", bare)
            bare = "".join(parts[:-1]).rstrip("\\/") or "."
        raw = os.path.expanduser(bare)
        raw = raw if os.path.isabs(raw) else os.path.join(cwd or os.getcwd(), raw)
        if os.path.isdir(raw):
            dirs.append(norm(raw))
            anything = True
        elif os.path.isfile(raw):
            anything = True
    if not anything and cwd:
        dirs.append(norm(cwd))
    return dirs


def folder_has_copied(folder, copied):
    """True when `folder` is, or is above, the folder of a copied file."""
    folder = folder.rstrip("/")
    return bool(folder) and any(path.startswith(folder + "/") for path in copied)


def broad_reader(statement, plain, names):
    """Why this statement reads recursively or past the ignore rules (so it would reach a copied file without naming it), or ''."""
    if IGNORE_AWARE_RE.search(statement):
        if IGNORE_OFF_RE.search(statement):
            return "search with the ignore rules switched off (rg --no-ignore/-u/-uu) reads git-ignored config copies"
        if any(glob_names_copied(glob, names) for glob in option_values(plain, ("--glob", "--iglob"), ("-g",))):
            return "search whose --glob singles out a git-ignored config copy"
    if GIT_GREP_BYPASS_RE.search(statement):
        return "git grep --no-index/--no-exclude-standard searches git-ignored files"
    if GREP_RE.search(statement) and GREP_RECURSIVE_RE.search(statement):
        includes = option_values(plain, ("--include",))
        excludes = option_values(plain, ("--exclude",))
        if (includes and not any(glob_covers(item, names) for item in includes)) or any(glob_covers(item, names) for item in excludes):
            return ""
        return "recursive grep over a folder that holds a config copy"
    if FIND_RE.search(statement) and FIND_READ_RE.search(statement):
        patterns = option_values(plain, ("-name", "-iname"))
        negated = any(token in ("!", "-not", "-o") for token in plain)
        if patterns and not negated and not any(glob_covers(item, names) for item in patterns):
            return ""
        return "find feeding the files it lists to a reader (-exec/xargs) over a folder that holds a config copy"
    if GCI_RE.search(statement) and PIPE_READER_RE.search(statement):
        includes = option_values(plain, ("-include", "-filter"))
        excludes = option_values(plain, ("-exclude",))
        if (includes and not any(glob_covers(item, names) for item in includes)) or any(glob_covers(item, names) for item in excludes):
            return ""
        return "recursive listing piped into a reader over a folder that holds a config copy"
    if FINDSTR_RE.search(statement):
        masks = [token for token in plain if token and token[0] not in "/-" and GLOB_CHARS.search(token)]
        if masks and not any(glob_covers(mask, names) for mask in masks):
            return ""
        return "findstr /s over a folder that holds a config copy"
    if COPY_TREE_RE.search(statement):
        return "recursive copy/archive of a folder that holds a config copy (the copy would escape the name check)"
    return ""


def broad_secret_read(statement, cwd, specs_dir):
    """Reason when one statement reaches a copied config file without naming it: a recursive or ignore-bypassing reader whose target
    folder is or contains the copied file's folder, or a glob that singles the copied name out. '' = nothing found. Best effort."""
    copied = load_copied(specs_dir) if specs_dir else []
    if not copied:
        return ""
    names = [path.rsplit("/", 1)[-1] for path in copied]
    tokens = re.findall(r"\"[^\"]*\"|'[^']*'|[^\s|]+", statement)
    plain = [token.strip("\"'") for token in tokens]
    how = broad_reader(statement, plain, names)
    if how and any(folder_has_copied(folder, copied) for folder in target_dirs(tokens, cwd)):
        return how
    for token, bare in zip(tokens, plain):
        if bare.startswith("-") or not GLOB_CHARS.search(bare):
            continue
        parts = re.split(r"[\\/]", bare)
        folder = bare[:len(bare) - len(parts[-1])].rstrip("\\/")
        any_folder = bool(GLOB_CHARS.search(folder))
        where = "" if any_folder else resolve_dir(folder or ".", cwd)
        if not (any_folder or any(path.rsplit("/", 1)[0] == where for path in copied)):
            continue
        if glob_names_copied(bare, names, quoted=token[:1] in ("\"", "'")):
            return "glob that matches a config copy"
        if parts[-1] in ("*", "*.*") and WILDCARD_READER_RE.search(statement):
            return "wildcard read of a folder that holds a config copy"
    return ""


def tool_broad_secret(tool_name, tool_input, cwd, specs_dir):
    """Grep/Glob: ripgrep skips git-ignored files by default, but a `glob` that names the file whitelists it (and Glob lists it), and an
    ignore-bypass option reads it. Blocked when it targets the copied file's folder (or a folder above it)."""
    copied = load_copied(specs_dir) if specs_dir else []
    if not copied or not isinstance(tool_input, dict):
        return ""
    names = [path.rsplit("/", 1)[-1] for path in copied]
    if not folder_has_copied(resolve_dir(tool_input.get("path") or ".", cwd), copied):
        return ""
    if tool_name == "Grep":
        if any(re.search(r"no[-_]?ignore|unrestricted", str(key), re.I) and value for key, value in tool_input.items()):
            return "Grep with the ignore rules switched off reads git-ignored config copies"
        glob = tool_input.get("glob")
        if isinstance(glob, str) and glob_names_copied(glob, names):
            return "Grep glob that singles out a config copy (a glob whitelists a git-ignored file)"
    if tool_name == "Glob":
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and glob_names_copied(pattern, names):
            return "Glob pattern that matches a config copy"
    return ""


def note_identity_gap(specs_dir, profile, payload):
    """Once per loop: records that, with 2+ features in flight, it could not tell which executor called
    (and which fields the hook brought), so the first wave rehearsal can confirm whether identity works."""
    try:
        log_path = loop_file(specs_dir, "guard.log")
        if os.path.isfile(log_path) and "scope-identity" in open(log_path, encoding="utf-8", errors="replace").read():
            return
        log(specs_dir, profile, "scope-identity", "info", "no executor identity; hook fields: " + ",".join(sorted(payload.keys())))
    except Exception:
        pass


SPECS_ONLY_FOLDERS = (".specs/", ".regression/")
MAP_DOCS_FOLDER = "docs/codebase/"  # the brownfield map (versioned): only profiles carrying the `map-docs` rule may write it
SUBPROJECTS_FILE = (".specs", "project", "SUBPROJECTS.json")
SUBPROJECTS_SUGGESTED_PATH = ".specs/project/SUBPROJECTS-SUGGESTED.json"  # the only file of the `subprojects` profile
SYSTEM_MAP_PATH = "docs/codebase/SYSTEM.md"  # the only file of the `systemmap` profile


def confirmed_subproject_map_folders(specs_dir):
    """Relative `<path>/docs/codebase/` folders of every non-skipped project of a CONFIRMED monorepo entry of `.specs/project/SUBPROJECTS.json`
    (keyed by repository name). Tiny tolerant read: a missing, unreadable or odd-shaped file means no extra folder, never a crash."""
    try:
        with open(os.path.join(os.path.dirname(specs_dir), *SUBPROJECTS_FILE), encoding="utf-8") as handle:
            data = json.load(handle)
        folders = []
        for entry in (data.get("repos") or {}).values():
            if not (isinstance(entry, dict) and entry.get("monorepo") is True and entry.get("confirmed") is True):
                continue
            for project in entry.get("projects") or []:
                if not isinstance(project, dict) or project.get("skip") is True:
                    continue
                path = str(project.get("path") or "").replace("\\", "/")
                if path.startswith("/"):
                    continue
                path = path.strip("/")
                if path and path != "." and not path.startswith("..") and "/../" not in "/" + path + "/" and ":" not in path:
                    folders.append(posixpath.normpath(path) + "/" + MAP_DOCS_FOLDER)
        return folders
    except Exception:
        return []


def check_specs_only(target, profile_rules, specs_dir, cwd=None):
    """Spec and map agents write only inside `.specs/`, `.regression/` or `docs/codebase/` (the map) of the repo: never code, never another folder.
    Profiles with the `map-docs` rule (spec, not planning) may also write `docs/codebase/`, where the codebase map lives.
    A temp folder is allowed (scratch files); without a located `.specs/` nothing is enforced (the guard never stalls)."""
    if "specs-only" not in profile_rules or not specs_dir or not target:
        return []
    root = norm(os.path.dirname(specs_dir))
    absolute = normalize_path(target, cwd or root)
    inside_repo = absolute.startswith(root + "/")
    if not inside_repo and temp_exempt(absolute):
        return []
    relative = absolute[len(root) + 1:] if inside_repo else absolute
    if "subprojects-only" in profile_rules or "system-only" in profile_rules:
        pass  # narrower than .specs/: handled below
    elif relative.startswith(SPECS_ONLY_FOLDERS):
        return []
    if "subprojects-only" in profile_rules:
        if relative == fold(SUBPROJECTS_SUGGESTED_PATH):
            return []
        return [("subprojects-only", "the subproject validation writes only %s: %s" % (SUBPROJECTS_SUGGESTED_PATH, relative[:100]))]
    if "system-only" in profile_rules:
        if relative == fold(SYSTEM_MAP_PATH):
            return []
        return [("system-only", "the system map writes only %s: %s" % (SYSTEM_MAP_PATH, relative[:100]))]
    if "map-docs" in profile_rules and (relative.startswith(MAP_DOCS_FOLDER) or relative.startswith(tuple(fold(folder) for folder in confirmed_subproject_map_folders(specs_dir)))):
        return []
    return [("specs-only", "spec and map agents write only inside .specs/ and docs/codebase/ (never code): %s" % relative[:100])]


def check_scope(target, profile_rules, specs_dir, slug=None, cwd=None, task=None):
    """Edits only inside the PATHS of the executor's feature (or, without identity, of the in-flight features) and only inside the repo."""
    hits = []
    if not specs_dir or not target:
        return hits
    root = norm(os.path.dirname(specs_dir))
    absolute = normalize_path(target, cwd or root)
    inside_repo = inside_any(absolute, project_roots(specs_dir))
    if not inside_repo and temp_exempt(absolute):
        return hits
    if "repo-only" in profile_rules and not inside_repo:
        hits.append(("repo-only", "file outside the project's worktrees: only repository code"))
        return hits
    if absolute.startswith(root + "/.regression/"):
        return hits
    scope = load_scope(specs_dir, slug, task)
    if "path-scope" in profile_rules and scope:
        allowed = [norm(item if os.path.isabs(item) else os.path.join(root, item)) for item in scope]
        if not any(absolute == item or absolute.startswith(item.rstrip("/") + "/") for item in allowed):
            shown = [item[len(root) + 1:] if item.startswith(root + "/") else item for item in allowed]
            hits.append(("path-scope", "outside the feature's PATHS (%s). Need a file outside the scope? Return NEEDS_CONFIRM" % ", ".join(shown)[:120]))
    return hits


BUILD_OUTPUT_DIRS = {"bin", "obj", "node_modules", ".pytest_cache", "__pycache__", "dist", "coverage", ".next", ".nuxt", ".venv", "venv",
                     ".mypy_cache", ".gradle", ".turbo", "testresults", ".vs"}
STATEMENT_SPLIT_RE = re.compile(r"&&|\|\||;|\n")


def split_statements(command):
    """Statements of a command, split on `&&`, `||`, `;` and newline OUTSIDE quotes: a `;` inside a quoted sed/awk script (`sed -i 's/a/b/; s/c/d/' f`)
    belongs to the script, and splitting there made the script's tail look like a file path (false `repo-only` block, and a target hidden after it)."""
    statements, current, quote, index = [], [], None, 0
    while index < len(command):
        char = command[index]
        if quote:
            if char == quote:
                quote = None
            current.append(char)
        elif char in "\"'":
            quote = char
            current.append(char)
        elif command.startswith(("&&", "||"), index):
            statements.append("".join(current))
            current, index = [], index + 1
        elif char in ";\n":
            statements.append("".join(current))
            current = []
        else:
            current.append(char)
        index += 1
    statements.append("".join(current))
    return statements
PS_PATH_RE = re.compile(r"-(?:Path|FilePath|LiteralPath)\s+(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)", re.I)


def quoted_ranges(text):
    """(start, end) of every CLOSED quoted string: a `>` inside one is text, not a redirect (`python -c "print('a > b')"`)."""
    ranges, quote, start = [], None, 0
    for index, char in enumerate(text):
        if quote:
            if char == quote:
                ranges.append((start, index))
                quote = None
        elif char in "\"'":
            quote, start = char, index
    return ranges


def confined_write_targets(statement):
    """Redirect/tee/Set-Content/Out-File/sed -i targets that are really executed (not inside a quoted string), null sinks removed."""
    spans = quoted_ranges(statement)
    found = []
    for pattern in (REDIRECT_RE, CMD_TARGET_RE, SED_INPLACE_RE):
        for match in pattern.finditer(statement):
            if any(start < match.start() < end for start, end in spans):
                continue
            target = match.group(1).strip("\"'")
            if target and target.lower() not in NULL_TARGETS:
                found.append(target)
    if re.search(r"\b(?:Set-Content|Add-Content|Out-File)\b", statement, re.I):
        found.extend(match.group(1).strip("\"'") for match in PS_PATH_RE.finditer(statement))
    return found


def check_shell_target(target, cwd, profile_rules, specs_dir, slug, task=None):
    """A shell write is confined like Write/Edit: inside the feature PATHS (path-scope), inside the project's worktrees (repo-only),
    inside .specs/ and docs/codebase/ (specs-only). Exempt: temp folders, /dev/*, build output folders (bin, obj, node_modules,
    .pytest_cache...) inside a worktree. A target built from a variable ($env:X, %X%, $(...)) cannot be resolved here and is not judged."""
    if not specs_dir or not ({"path-scope", "repo-only", "specs-only"} & set(profile_rules)):
        return []
    if target.startswith(("/dev/", "$", "%")) or "$(" in target or "`" in target:
        return []
    path = os.path.expanduser(target)
    if os.name == "nt":
        path = re.sub(r"^/([a-zA-Z])/", r"\1:/", path)  # git-bash style /c/Users/...
    absolute = norm(path if os.path.isabs(path) else os.path.join(cwd or os.path.dirname(specs_dir), path))
    roots = project_roots(specs_dir)
    if inside_any(absolute, roots):
        relative = next(absolute[len(root) + 1:] for root in sorted(roots, key=len, reverse=True) if absolute.startswith(root + "/"))
        if BUILD_OUTPUT_DIRS & {fold(part) for part in relative.split("/")[:-1]}:
            return []
    hits = list(check_specs_only(absolute, profile_rules, specs_dir))
    hits.extend(check_scope(absolute, profile_rules, specs_dir, slug, None, task))
    return hits


CD_RE = re.compile(r"^(?:cd|pushd|Set-Location|sl)\s+(?:-(?:Path|LiteralPath)\s+)?(\"[^\"]+\"|'[^']+'|\S+)", re.I)


def resolve_dir(path, cwd):
    path = os.path.expanduser(path.strip("\"'"))
    return norm(path if os.path.isabs(path) else os.path.join(cwd or os.getcwd(), path))


def git_command_dirs(segment, cwd):
    """For a `git ...` segment: (subcommand, folders the command acts on). The folder is the cwd moved by every `-C <path>`;
    `--git-dir`/`--work-tree` add their own. (None, []) when the segment is not git."""
    try:
        tokens = shlex.split(segment, posix=False)
    except ValueError:
        tokens = segment.split()
    if not tokens or not re.match(r"^(?:.*[\\/])?git(?:\.exe)?$", tokens[0].strip("\"'"), re.I):
        return None, []
    effective, extra, index = cwd, [], 1
    while index < len(tokens) and tokens[index].startswith("-"):
        token = tokens[index]
        has_value = index + 1 < len(tokens)
        if token == "-C" and has_value:
            effective = resolve_dir(tokens[index + 1], effective)
            index += 2
            continue
        if token == "-c" and has_value:
            index += 2
            continue
        inline = re.match(r"^--(?:git-dir|work-tree)=(.+)$", token)
        if inline:
            extra.append(resolve_dir(inline.group(1), cwd))
        elif token in ("--git-dir", "--work-tree") and has_value:
            extra.append(resolve_dir(tokens[index + 1], cwd))
            index += 2
            continue
        index += 1
    sub = tokens[index].strip("\"'") if index < len(tokens) else None
    return sub, [resolve_dir(".", effective)] + extra


def check_bash(command, profile_rules, permissions):
    """Returns the list of violated (rule_id, reason). Applies to Bash and PowerShell."""
    hits = []
    cwd = permissions.get("_cwd")
    specs_dir = permissions.get("_specs_dir")
    roots = project_roots(specs_dir) if specs_dir else []
    running_cwd = cwd or (roots[0] if roots else None)
    start_cwd = running_cwd
    # 1) text of the whole command: catches a push hidden in python -c, $(...), quotes
    if PUSH_RE.search(command):
        if "headless-push" in profile_rules:
            hits.append(("headless-push", HEADLESS_MSG))
        if "git-push" in profile_rules:
            hits.append(("git-push", "git push: only on the user's explicit request (at the end of the flow, delivery is done by `harness.py deliver`)"))
        if PUSH_PROTECTED_RE.search(command):
            hits.append(("push-protected", "push to a protected branch (integration branch/main/master...): never, only feature branches"))
        if PUSH_FORCE_RE.search(command):
            hits.append(("push-force", "forced push: never"))
    # 2) per segment: write git, add -A, gh, install, credentials
    for segment in split_commands(command):
        segment_cwd = running_cwd
        cd_move = CD_RE.match(segment)
        if cd_move:
            running_cwd = resolve_dir(cd_move.group(1), running_cwd)
        if roots and "repo-only" in profile_rules:
            git_sub, git_dirs = git_command_dirs(segment, segment_cwd)
            if git_sub and git_sub not in READ_ONLY_GIT:
                outside = [folder for folder in git_dirs if not inside_any(folder, roots)]
                if outside:
                    hits.append(("repo-only", "git %s outside the project's worktrees (%s): the repo of its -C/cwd must be one of them" % (git_sub, outside[0][:80])))
        sub = git_subcommand(segment)
        if sub:
            if sub == "push":
                if default_push(segment) and current_branch(cwd).lower() in PROTECTED_NAMES:
                    hits.append(("push-protected", "git push without a refspec while on the protected branch %s" % current_branch(cwd)))
            elif sub not in READ_ONLY_GIT and "git-write" in profile_rules:
                hits.append(("git-write", "git %s: write git is the orchestrator's only" % sub))
            if sub == "add" and "git-add-all" in profile_rules and re.search(r"\badd\s+(-A|--all|\.)(\s|$)", segment):
                hits.append(("git-add-all", "git add . / -A: add files by name"))
            if sub == "commit" and "git-add-all" in profile_rules and re.search(r"\s-a[m\s]|--all", segment):
                hits.append(("git-add-all", "git commit -a: add files by name"))
        if "headless-gh" in profile_rules and re.match(r"^gh(\.exe)?\b", segment):
            hits.append(("headless-gh", HEADLESS_MSG))
        if re.match(r"^gh\s+(pr|release|repo|gist|workflow)\b", segment) and "pr-release" in profile_rules:
            if not re.match(r"^gh\s+(pr|repo)\s+(view|list|diff|status|checks)\b", segment):
                hits.append(("pr-release", "gh pr/release/gist: leaves the machine; the end-of-flow PR is made by `harness.py deliver`"))
        if INSTALL.search(segment) and "install" in profile_rules:
            hits.append(("install", "installing a dependency: only if the spec asks for it"))
        if "secrets" in profile_rules and (SECRET_HINT.search(segment) or mentions_copied_secret(segment, segment_cwd, specs_dir)):
            hits.append(("secrets", "credential path (.secrets/shared .env/app data .env or app.json/private key/config copied into a worktree)"))
    if "state-script" in profile_rules and STATE_SCRIPT_RE.search(command):
        hits.append(("state-script", "only the orchestrator writes rule, dependency, graph or LOOP.json (harness.py rules write, audit --write/--apply, graph --write, state loop|migrate)"))
    if "delivery" in profile_rules and DELIVERY_RE.search(command):
        hits.append(("delivery", "only the orchestrator delivers (push of the feature branch + PR)"))
    if "loop-run" in profile_rules and LOOP_RUN_RE.search(command):
        hits.append(("loop-run", "the loop and the panel are started by the user (or by the orchestrator at the user's request), never by a subagent"))
    if "panel-trigger" in profile_rules and PANEL_TRIGGER_RE.search(command):
        hits.append(("panel-trigger", "panel endpoint that only the user's click triggers (loop, roadmap approval, Act, answer/resume/instruction)"))
    # 2b) broad reads that reach a copied config file without naming it (rg --no-ignore, grep -r, find -exec cat, globs...)
    if "secrets" in profile_rules and specs_dir:
        statement_cwd = start_cwd
        for statement in split_statements(command):
            statement = statement.strip()
            reason = broad_secret_read(statement, statement_cwd, specs_dir) if statement else ""
            if reason:
                hits.append(("secrets", reason))
            cd_move = CD_RE.match(statement)
            if cd_move:
                statement_cwd = resolve_dir(cd_move.group(1), statement_cwd)
    # 2c) shell writes are confined like Write/Edit (PATHS, worktrees, .specs/), per statement so a `cd` is followed
    if specs_dir and ({"path-scope", "repo-only", "specs-only"} & set(profile_rules)):
        statement_cwd = start_cwd
        for statement in split_statements(command):
            statement = statement.strip()
            for target in confined_write_targets(statement):
                hits.extend(check_shell_target(target, statement_cwd, profile_rules, specs_dir, permissions.get("_slug"), permissions.get("_task")))
            cd_move = CD_RE.match(statement)
            if cd_move:
                statement_cwd = resolve_dir(cd_move.group(1), statement_cwd)
    # 3) shell write to a file that must never be touched
    if "permissions-write" in profile_rules:
        shell_targets = list(shell_write_targets(command))
        for statement in split_statements(command):
            shell_targets.extend(confined_write_targets(statement))
        if any(PERMISSIONS_FILE.search(str(target).strip("\"'")) for target in shell_targets) or PERMISSIONS_CMD_RE.search(command):
            hits.append(("permissions-write", PERMISSIONS_MSG))
    for target in shell_write_targets(command):
        if APPROVAL_FILE.search(target) and "approval-write" in profile_rules:
            hits.append(("approval-write", "ROADMAP-APPROVED.json is written only by the user's click in the panel"))
        if STATE_FILE.search(target) and "state-write" in profile_rules:
            hits.append(("state-write", "only the orchestrator writes ROADMAP/STATE/RULES/phases and the loop folder (via shell too)"))
        if "secrets" in profile_rules and (SECRET_HINT.search(target) or mentions_copied_secret(target, running_cwd, specs_dir)):
            hits.append(("secrets", "write to a credential path"))
        for pattern in permissions.get("deny_path_regex", []):
            if project_pattern_matches(pattern, target):
                hits.append(("project-deny", "path forbidden by the project: %s" % pattern))
    if "migration-target" in profile_rules:
        reason = migration_target_reason(command)
        if reason:
            hits.append(("migration-target", reason))
    for pattern in permissions.get("deny_bash_regex", []):
        if project_pattern_matches(pattern, command):
            hits.append(("project-deny", "project rule: %s" % pattern))
    return hits


def project_pattern_matches(pattern, text):
    """A broken project regex is skipped (and reported on stderr) instead of crashing the guard: a crash would fail open
    and let every action through, hard rules included. The other rules keep running."""
    try:
        return re.search(pattern, text, re.I) is not None
    except (re.error, TypeError) as error:
        sys.stderr.write("guard: invalid project regex in PERMISSIONS.json skipped (%r: %s)\n" % (pattern, error))
        return False


# Grafana (MCP `grafana`): read-only for every profile. Only the read verbs pass; create_/update_/delete_/add_/install_,
# alerting_manage_*, create/delete_snapshot and a grafana_api_request whose method is not GET are denied (HARD: no PERMISSIONS.json release).
GRAFANA_PREFIX = "mcp__grafana__"
GRAFANA_READ_RE = re.compile(r"^(?:get|list|query|search|find|check|analyze|suggest|validate)_|^generate_deeplink$")


def grafana_write_reason(tool_name, tool_input):
    if not tool_name.lower().startswith(GRAFANA_PREFIX):
        return ""
    verb = tool_name[len(GRAFANA_PREFIX):].lower()
    if verb == "grafana_api_request":
        method = str((tool_input or {}).get("method") or "GET").strip().upper()
        return "" if method == "GET" else "grafana_api_request with method %s (Grafana is read-only)" % method
    if verb in ("create_snapshot", "delete_snapshot") or not GRAFANA_READ_RE.match(verb):
        return "%s writes to Grafana (only read tools are allowed)" % tool_name
    return ""


# ---- MySQL MCP is read only (planning and loop profiles): the query of mcp__mcp-mysql-dev__* / mcp__mcp-mysql-prod__* is inspected
SQL_MCP_TOOL = re.compile(r"^mcp__mcp-mysql-(?:dev|prod)__", re.I)
SQL_WRITE_KEYWORDS = {"insert", "update", "delete", "replace", "merge", "create", "alter", "drop", "truncate", "grant", "revoke",
                      "call", "load", "rename"}
SQL_WRITE_REASON = ("read only through MCP: write the change as a .sql script (or a migration) and ask the user "
                    "(write the script in the feature folder sql/ and return STATUS: NEEDS_CONFIRM: apply <script> on DEV, then answer)")
SQL_STRING_RE = re.compile(r"'(?:[^'\\]|\\.|'')*'|\"(?:[^\"\\]|\\.|\"\")*\"|`[^`]*`", re.S)
SQL_EXECUTABLE_COMMENT_RE = re.compile(r"/\*!\d*\s*(.*?)\*/", re.S)
SQL_BLOCK_COMMENT_RE = re.compile(r"/\*.*?\*/", re.S)
SQL_LINE_COMMENT_RE = re.compile(r"(?:--(?=\s|$)|#)[^\n]*")


def sql_statements(sql):
    """Statements of a SQL text, lowercased, with comments and string/identifier literals removed (a MySQL `/*! ... */` is code and stays)."""
    text = SQL_EXECUTABLE_COMMENT_RE.sub(lambda match: " " + match.group(1) + " ", str(sql))
    text = SQL_BLOCK_COMMENT_RE.sub(" ", text)
    text = SQL_STRING_RE.sub("''", text)
    text = SQL_LINE_COMMENT_RE.sub(" ", text)
    return [part.strip().lstrip("( \t\r\n").lower() for part in text.split(";") if part.strip().strip("( \t\r\n")]


def sql_statement_writes(statement):
    words = re.findall(r"[a-z_@.]+", statement)
    first = words[0] if words else ""
    if first in SQL_WRITE_KEYWORDS:
        return first.upper()
    if first == "set" and re.match(r"set\s+(?:global|persist|persist_only|@@global\.|@@persist\.)", statement):
        return "SET GLOBAL"
    if first == "with":
        for word in words:
            if word in ("insert", "update", "delete", "replace"):
                return word.upper()
    if re.search(r"\binto\s+(?:outfile|dumpfile)\b", statement):
        return "SELECT INTO OUTFILE"
    return ""


def sql_write_reason(tool_name, tool_input):
    """Reason when the MySQL MCP query writes (first keyword of any `;`-separated statement), else ''."""
    if not SQL_MCP_TOOL.match(tool_name or ""):
        return ""
    if isinstance(tool_input, dict):
        texts = [tool_input[key] for key in ("query", "sql", "statement") if isinstance(tool_input.get(key), str)]
        if not texts:
            texts = [value for value in tool_input.values() if isinstance(value, str)]
    else:
        texts = [str(tool_input)]
    for text in texts:
        for statement in sql_statements(text):
            keyword = sql_statement_writes(statement)
            if keyword:
                return "%s through %s: %s" % (keyword, tool_name, SQL_WRITE_REASON)
    return ""


# ---- migrations: allowed on DEV for the executor/verifier, blocked when the command or the environment points to production
MIGRATION_CMD_RE = re.compile(
    r"(?:^|[\s(;&|])(?:\S*[\\/])?dotnet(?:\.exe)?\s+(?:ef\s+(?:migrations\s+(?:add|remove|list|script|bundle)|database\s+(?:update|drop))\b"
    r"|run\b[^\n;&|]*?\s--\s+migrate\w*)", re.I)
PROD_ENV_VALUE = r"[\"']?(?:production|prod)[\"']?"
PROD_ENV_RE = re.compile(
    r"(?:ASPNETCORE_ENVIRONMENT|DOTNET_ENVIRONMENT)[\"']?\s*=\s*" + PROD_ENV_VALUE + r"(?![\w-])"
    r"|--environment(?:\s+|=)" + PROD_ENV_VALUE + r"(?![\w-])", re.I)
CONNECTION_ARG_RE = re.compile(r"--(?:connection|connection-string|connectionstring)(?:\s+|=)(\"[^\"]*\"|'[^']*'|\S+)", re.I)
CONNECTION_ENV_RE = re.compile(r"\b(?:ConnectionStrings?[:_]{1,2}\w*|DB_(?:HOST|CONNECTION)\w*|DATABASE_URL)\s*=\s*(\"[^\"]*\"|'[^']*'|\S+)", re.I)


def prod_hosts():
    """Substrings that mark a connection as PRODUCTION: prod_hints() plus GUARD_PROD_HOSTS (comma-separated host names)."""
    return prod_hints() + [item.strip().lower() for item in os.environ.get("GUARD_PROD_HOSTS", "").split(",") if item.strip()]


def migration_target_reason(command):
    """Reason when a migration command in `command` targets production, else ''."""
    if not MIGRATION_CMD_RE.search(command):
        return ""
    if PROD_ENV_RE.search(command):
        return "migration against the Production environment: migrations run on DEV only"
    inherited = " ".join("%s=%s" % (name, os.environ.get(name, "")) for name in ("ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT"))
    if PROD_ENV_RE.search(inherited):
        return "migration while ASPNETCORE_ENVIRONMENT/DOTNET_ENVIRONMENT is Production: migrations run on DEV only"
    hosts = prod_hosts()
    for pattern in (CONNECTION_ARG_RE, CONNECTION_ENV_RE):
        for match in pattern.finditer(command):
            value = match.group(1).lower()
            if any(host in value for host in hosts):
                return "migration whose connection points to production: migrations run on DEV only"
    return ""


# ---- planning: the capabilities block of PROJECT.md is written by the server between these markers
CAPABILITIES_OPEN = "<!-- maestro:capabilities -->"
CAPABILITIES_CLOSE = "<!-- /maestro:capabilities -->"
CAPABILITIES_FILE_RE = re.compile(r"\.specs[\\/]project[\\/]PROJECT\.md$", re.I)
CAPABILITIES_BLOCK_RE = re.compile(re.escape(CAPABILITIES_OPEN) + r".*?" + re.escape(CAPABILITIES_CLOSE), re.S)


def read_text_file(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace", newline="") as handle:
            return handle.read().replace("\r\n", "\n")
    except OSError:
        return ""


def capabilities_edit_reason(tool_name, tool_input, target, cwd):
    """Reason when a Write/Edit of PROJECT.md would change text inside the capabilities markers, else ''."""
    if not target or not CAPABILITIES_FILE_RE.search(str(target)) or not isinstance(tool_input, dict):
        return ""
    reason = "the capabilities block of PROJECT.md (between maestro:capabilities markers) is written by the server, never by you"
    path = str(target)
    if not os.path.isabs(path) and cwd:
        path = os.path.join(cwd, path)
    current = read_text_file(path)
    if tool_name == "Write":
        content = str(tool_input.get("content") or "").replace("\r\n", "\n")
        if CAPABILITIES_BLOCK_RE.findall(current) != CAPABILITIES_BLOCK_RE.findall(content):
            return reason
        if (CAPABILITIES_OPEN in current) != (CAPABILITIES_OPEN in content) or (CAPABILITIES_CLOSE in current) != (CAPABILITIES_CLOSE in content):
            return reason
        return ""
    if tool_name == "Edit":
        old = str(tool_input.get("old_string") or "").replace("\r\n", "\n")
        new = str(tool_input.get("new_string") or "").replace("\r\n", "\n")
        if "maestro:capabilities" in old or "maestro:capabilities" in new:
            return reason
        if old and any(old in block.group(0) for block in CAPABILITIES_BLOCK_RE.finditer(current)):
            return reason
    return ""


def is_prod_tool(tool_name):
    lowered = tool_name.lower()
    return any(hint in lowered for hint in prod_hints())


def check_tool(tool_name, tool_input, profile_rules, permissions):
    hits = []
    if tool_name in ("Bash", "PowerShell"):
        return check_bash(tool_input.get("command", ""), profile_rules, permissions)
    if "grafana-write" in profile_rules:
        reason = grafana_write_reason(tool_name, tool_input)
        if reason:
            hits.append(("grafana-write", reason))
    if tool_name.startswith("mcp__"):
        short_name = tool_name.rsplit("__", 1)[-1]
        if SQL_MCP_TOOL.match(tool_name):
            if "sql-write" in profile_rules:
                reason = sql_write_reason(tool_name, tool_input)
                if reason:
                    hits.append(("sql-write", reason))
            else:
                sql = tool_input.get("query") or tool_input.get("sql") or json.dumps(tool_input)
                if WRITE_SQL.search(sql):
                    rule = "prod-write" if is_prod_tool(tool_name) else "dev-write"
                    if rule in profile_rules:
                        hits.append((rule, "write SQL in %s" % tool_name))
        elif DB_TOOL.match(tool_name):
            sql = tool_input.get("query") or tool_input.get("sql") or json.dumps(tool_input)
            if WRITE_SQL.search(sql):
                rule = "prod-write" if is_prod_tool(tool_name) else "dev-write"
                if rule in profile_rules:
                    hits.append((rule, "write SQL in %s" % tool_name))
        elif WRITE_VERB.search(short_name) and is_prod_tool(tool_name) and "prod-write" in profile_rules:
            hits.append(("prod-write", "%s writes to production" % tool_name))
        if OUTBOUND_HINT.search(tool_name) and OUTBOUND_VERB.search(short_name) and "outbound-msg" in profile_rules:
            hits.append(("outbound-msg", "%s sends/changes something outside the machine (ticket, issue, message: never in the feature's scope)" % tool_name))
    if tool_name in ("Write", "Edit", "NotebookEdit"):
        target = tool_input.get("file_path", "") or tool_input.get("notebook_path", "")
        if APPROVAL_FILE.search(target) and "approval-write" in profile_rules:
            hits.append(("approval-write", "ROADMAP-APPROVED.json is written only by the user's click in the panel"))
        if PERMISSIONS_FILE.search(target) and "permissions-write" in profile_rules:
            hits.append(("permissions-write", PERMISSIONS_MSG))
        if STATE_FILE.search(target) and "state-write" in profile_rules:
            hits.append(("state-write", "only the orchestrator writes ROADMAP/STATE/RULES/phases and the loop folder"))
        if "secrets" in profile_rules and (SECRET_HINT.search(target) or mentions_copied_secret(target, permissions.get("_cwd"), permissions.get("_specs_dir"))):
            hits.append(("secrets", "credential path"))
        if "capabilities-block" in profile_rules and tool_name in ("Write", "Edit"):
            reason = capabilities_edit_reason(tool_name, tool_input, target, permissions.get("_cwd"))
            if reason:
                hits.append(("capabilities-block", reason))
        if tool_name in ("Write", "Edit", "NotebookEdit"):
            hits.extend(check_specs_only(target, profile_rules, permissions.get("_specs_dir"), permissions.get("_cwd")))
        if tool_name in ("Write", "Edit"):
            hits.extend(check_scope(target, profile_rules, permissions.get("_specs_dir"), permissions.get("_slug"), permissions.get("_cwd"), permissions.get("_task")))
        for pattern in permissions.get("deny_path_regex", []):
            if project_pattern_matches(pattern, target):
                hits.append(("project-deny", "path forbidden by the project: %s" % pattern))
    if tool_name in ("Read", "Glob", "Grep"):
        target = json.dumps(tool_input)
        if "secrets" in profile_rules and (SECRET_HINT.search(target) or mentions_copied_secret(target, permissions.get("_cwd"), permissions.get("_specs_dir"))):
            hits.append(("secrets", "reading a credential"))
        if "secrets" in profile_rules and tool_name in ("Glob", "Grep"):
            reason = tool_broad_secret(tool_name, tool_input, permissions.get("_cwd"), permissions.get("_specs_dir"))
            if reason:
                hits.append(("secrets", reason))
    return hits


def unique(hits):
    seen, out = set(), []
    for rule, why in hits:
        if (rule, why) not in seen:
            seen.add((rule, why))
            out.append((rule, why))
    return out


def profile_for_agent(payload):
    """`--profile auto`: profile of the calling agent, or None (main session / other agents: enforce nothing)."""
    agent = str(payload.get("agent_type") or "").rsplit(":", 1)[-1].strip()
    return AGENT_PROFILES.get(agent)


def main():
    profile = "executor"
    if "--profile" in sys.argv:
        profile = sys.argv[sys.argv.index("--profile") + 1]
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw)
    except Exception as error:
        alert(None, profile, "hook payload unreadable (%s)" % type(error).__name__)
        return 0
    if profile == "auto":
        profile = profile_for_agent(payload)
        if not profile:
            if not is_headless():
                return 0
            profile = "orchestrator"
    profile_rules = list(PROFILES.get(profile, PROFILES["executor"]))
    if is_headless():
        profile_rules += [rule for rule in HEADLESS_RULES if rule not in profile_rules]
    specs_dir = find_specs_dir(payload.get("cwd"))
    try:
        permissions = load_permissions(specs_dir)
        permissions["_specs_dir"] = specs_dir
        permissions["_cwd"] = payload.get("cwd")
        if specs_dir and "path-scope" in profile_rules:
            permissions["_slug"] = executor_slug(payload)
            permissions["_task"] = executor_task(payload)
            if payload.get("tool_name") in ("Write", "Edit") and load_scope(specs_dir):
                # trace to check in the real rehearsal: resolved identity and the fields the hook actually receives
                log(specs_dir, profile, "scope-trace", "info", "slug=%s fields=%s" % (permissions["_slug"], ",".join(sorted(payload.keys()))))
            if not permissions["_slug"] and payload.get("tool_name") in ("Write", "Edit"):
                try:
                    with open(loop_file(specs_dir, "SCOPE.json"), encoding="utf-8") as handle:
                        if len((json.load(handle).get("features") or {})) > 1:
                            note_identity_gap(specs_dir, profile, payload)
                except Exception:
                    pass
        allowed = {entry["rule"] if isinstance(entry, dict) else entry for entry in permissions.get("allow", [])}
        hits = unique(check_tool(payload.get("tool_name", ""), payload.get("tool_input", {}) or {}, profile_rules, permissions))
    except Exception as error:
        alert(specs_dir, profile, "%s: %s" % (type(error).__name__, str(error)))
        return 0
    blocked = [(rule, why) for rule, why in hits if rule in HARD or rule not in allowed]
    for rule, why in hits:
        if rule in allowed and rule not in HARD:
            log(specs_dir, profile, rule, "allowed-by-user", why)
    if not blocked:
        return 0
    attempt = describe_attempt(payload)
    for rule, why in blocked:
        log(specs_dir, profile, rule, "blocked", why, attempt)
    rules = ", ".join(sorted({rule for rule, _ in blocked}))
    sys.stderr.write(
        "BLOCKED by the orchestrator guard [" + rules + "]: " + "; ".join(why for _, why in blocked)
        + ". Do not try to work around it. Return STATUS: NEEDS_CONFIRM with the reason; the orchestrator takes it to the user."
        " This block is final: do not retry this command or a variant of it. Continue without it, or report it in your final return.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
