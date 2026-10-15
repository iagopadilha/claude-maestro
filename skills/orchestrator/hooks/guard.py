#!/usr/bin/env python3
"""PreToolUse guard of the orchestrator (deterministic hook, does not depend on the model obeying).

Registered once in hooks/hooks.json (plugin agents ignore frontmatter hooks):
    python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py" --profile auto
`--profile auto` reads the hook payload (stdin JSON) and picks the profile from `agent_type` (a `plugin-name:` prefix is stripped):
  spec-writer / spec-reviewer / brownfield-mapper -> spec;  executor-back / executor-front / executor-generic -> executor;
  verifier / verifier-flow -> verifier;  anything else (main session, other agents) -> exit 0 at once, nothing enforced.
Explicit `--profile orchestrator|executor|spec|verifier` keeps working as before.

Reads the hook JSON on stdin. Blocks with exit 2 + the reason on stderr.
Exception: a rule released in `.specs/project/PERMISSIONS.json` (written by the orchestrator ONLY when the user
explicitly authorized it). A released rule never blocks, EXCEPT the hard rules (HARD):
push to a protected branch (integration branch, main...) and forced push.
No exception and no match: exit 0.
Internal error: exit 0 (does not stall the loop), logs it AND warns through the configured notice channels
(notify.py), at most once an hour.

Covers the Bash and PowerShell tools (same command analysis).
"""
import json
import os
import re
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
OUTBOUND_HINT = re.compile(r"(ticket|issue|jira|confluence|slack|mail|message|sms|whatsapp|chat|comment|notif|channel|teams|discord)", re.I)
OUTBOUND_VERB = re.compile(r"(create|update|delete|add|send|post|reply|comment|transition|edit|publish)", re.I)
INSTALL = re.compile(
    r"\b(npm|pnpm|yarn|bun)\s+(i|install|add|ci|update|upgrade)\b|\bdotnet\s+(restore|add\s+package)\b|\bpip\s+install\b",
    re.I,
)
SECRET_HINT = re.compile(r"\.secrets[\\/]|[\\/]shared[\\/]\.env(?!\.example)|\bid_(rsa|ed25519)\b", re.I)
STATE_FILE = re.compile(
    r"\.specs[\\/]project[\\/](ROADMAP\.md|STATE\.md|RULES\.md|VALIDATE\.md|DEPENDENCIES\.md|GRAPH\.md|rules\.tests\.json|SCOPE\.json|LOOP\.json|LOOP\.lock|ANSWERS\.md|phases[\\/]|archive[\\/]|PERMISSIONS\.json)", re.I
)


def prod_hints():
    """Substrings that mark an MCP tool as production: env GUARD_PROD_HINTS (comma-separated), default prod,production."""
    raw = os.environ.get("GUARD_PROD_HINTS")
    if raw is None:
        raw = "prod,production"
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def integration_branch():
    return (os.environ.get("LOOP_PR_BASE") or os.environ.get("LOOP_INTEGRATION_BRANCH") or "developer").strip().lower()


# ---- push: text of the whole command (catches `python -c "...git push..."`, `$(git push)`, quotes), besides the segment
PROTECTED_NAMES = {"developer", "develop", "main", "master", "release", "production", "dev", integration_branch()}
_PROT = "|".join(re.escape(name) for name in sorted(PROTECTED_NAMES, key=len, reverse=True))
PUSH_CMD = r"(?:\bgit(?:\.exe)?(?:\s+-[-\w]+(?:[= ]\S+)?)*\s+push\b|['\"]git(?:\.exe)?['\"]\s*,\s*['\"]push['\"])"
PUSH_RE = re.compile(PUSH_CMD, re.I)
PUSH_PROTECTED_RE = re.compile(PUSH_CMD + r"[^\n]*?(?<![\w.-])(?:" + _PROT + r")(?![\w.-])", re.I)
PUSH_FORCE_RE = re.compile(PUSH_CMD + r"[^\n]*?(?:\s--force\b|\s--force-with-lease\b|\s-f\b|\s\+\S)", re.I)

# scripts that WRITE project state: only the orchestrator runs them (rule, applied dependency, written graph)
STATE_SCRIPT_RE = re.compile(r"\brules\.py\s+write\b|\baudit_deps\.py\b[^\n]*--(?:write|apply)\b|\bgraph_view\.py\b[^\n]*--write\b", re.I)

# ---- shell writes (redirection, tee, Set-Content, sed -i...): only the targets that must NEVER be touched
REDIRECT_RE = re.compile(r"(?<![\d&<])>>?\s*(?!&)(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)")
CMD_TARGET_RE = re.compile(r"\b(?:tee(?:\s+-a)?|Set-Content|Add-Content|Out-File)\s+(?:-\w+\s+)*(\"[^\"]+\"|'[^']+'|[^\s;|&<>]+)", re.I)
SED_INPLACE_RE = re.compile(r"\bsed\s+-i\S*\s+(?:-e\s+)?(?:'[^']*'|\"[^\"]*\"|\S+)\s+(\S+)")
NULL_TARGETS = {"/dev/null", "$null", "nul", "null"}

# rules that not even PERMISSIONS.json releases: workflows never push to the integration branch/main nor force
HARD = {"push-protected", "push-force"}

PROFILES = {
    # implements code: everything blocked except editing inside the project
    "executor": ["git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "install", "state-write", "project-deny", "path-scope", "repo-only", "delivery", "state-script"],
    # only reads and runs checks
    "verifier": ["git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "state-write", "project-deny", "delivery", "state-script"],
    # writes spec/map
    "spec": ["git-write", "git-push", "pr-release", "prod-write", "dev-write", "outbound-msg", "secrets", "state-write", "project-deny", "delivery", "state-script"],
    # the orchestrator chat: may commit (one per feature) and write state; does not push (only via loop/delivery.py), does not touch PROD
    # and does not write to tickets/issues/messages (outbound-msg): the main action is never in the feature's scope.
    "orchestrator": ["git-push", "pr-release", "git-add-all", "prod-write", "secrets", "project-deny", "outbound-msg"],
}

# hook payload `agent_type` (without the `plugin-name:` prefix) -> profile, used by `--profile auto`
AGENT_PROFILES = {
    "spec-writer": "spec", "spec-reviewer": "spec", "brownfield-mapper": "spec",
    "executor-back": "executor", "executor-front": "executor", "executor-generic": "executor",
    "verifier": "verifier", "verifier-flow": "verifier",
}


def find_specs_dir(cwd):
    here = os.path.abspath(cwd or os.getcwd())
    for _ in range(8):
        candidate = os.path.join(here, ".specs")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    return None


def load_permissions(specs_dir):
    empty = {"allow": [], "deny_bash_regex": [], "deny_path_regex": []}
    if not specs_dir:
        return empty
    path = os.path.join(specs_dir, "project", "PERMISSIONS.json")
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


def log(specs_dir, profile, rule, decision, detail):
    if not specs_dir:
        return
    try:
        line = {
            "t": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "profile": profile,
            "rule": rule,
            "decision": decision,
            "detail": detail[:200],
        }
        log_path = os.path.join(specs_dir, "project", "guard.log")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(line, ensure_ascii=False) + "\n")
    except Exception:
        pass


def alert(specs_dir, profile, message):
    """A fail-open must not be silent: logs it and warns through the LOOP_NOTIFY_CHANNELS channels (notify.py), once an hour.
    Never raises and never waits for the send (separate process)."""
    log(specs_dir, profile, "guard-failopen", "error", message)
    try:
        folder = os.path.join(specs_dir, "project") if specs_dir else tempfile.gettempdir()
        marker = os.path.join(folder, ".guard-alert" if specs_dir else "loop-guard-alert")
        if os.path.isfile(marker) and time.time() - os.path.getmtime(marker) < 3600:
            return
        with open(marker, "w", encoding="utf-8") as handle:
            handle.write(str(time.time()))
        notify = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "loop", "notify.py")
        if not os.path.isfile(notify):
            return
        body = ("The orchestrator guard failed internally and LET THE ACTION THROUGH (fail-open, so the loop is not stalled).\n"
                "Profile: %s\nReason: %s\n\nSee .specs/project/guard.log and run the guard test suite." % (profile, message[:300]))
        subprocess.Popen([sys.executable, notify, "--subject", "[guard] internal failure: the gatekeeper let it through", "--body", body],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def split_commands(command):
    return [part.strip() for part in re.split(r"&&|\|\||;|\||\n|&(?!&)", command) if part.strip()]


def git_subcommand(segment):
    match = re.match(r"^(?:\S*[\\/])?git(?:\.exe)?\s+(?:-[-\w]+(?:[= ]\S+)?\s+)*([a-z][\w-]*)", segment)
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


def norm(path):
    return os.path.normpath(path).replace("\\", "/").rstrip("/").lower()


SLUG_MARK = re.compile(r"SCOPE_FEATURE:\s*([A-Za-z0-9._-]+)")


def executor_slug(payload):
    """Which feature does this executor serve? Best effort: reads the subagent transcript (if the hook brings agent_id) and
    looks for the `SCOPE_FEATURE: <slug>` line of the dispatch prompt. None = could not tell (falls back to the union of the scopes)."""
    try:
        agent_id = payload.get("agent_id")
        transcript = payload.get("agent_transcript_path")
        if not transcript and agent_id and payload.get("transcript_path"):
            base = os.path.splitext(payload["transcript_path"])[0]
            transcript = os.path.join(base, "subagents", "agent-%s.jsonl" % agent_id)
        if not transcript or not os.path.isfile(transcript):
            return None
        with open(transcript, encoding="utf-8", errors="replace") as handle:
            match = SLUG_MARK.search(handle.read(40000))
        return match.group(1) if match else None
    except Exception:
        return None


def load_scope(specs_dir, slug=None):
    """Reads .specs/project/SCOPE.json: {"features": {"slug": ["folder/or/file", ...]}} written by the orchestrator.
    With a known `slug` present in the file, only that feature's paths; otherwise the union of all in-flight features."""
    try:
        with open(os.path.join(specs_dir, "project", "SCOPE.json"), encoding="utf-8") as handle:
            data = json.load(handle)
        features = data.get("features") or {}
        if slug and slug in features:
            return list(features[slug])
        paths = []
        for entries in features.values():
            paths.extend(entries)
        return paths
    except Exception:
        return []


def note_identity_gap(specs_dir, profile, payload):
    """Once per loop: records that, with 2+ features in flight, it could not tell which executor called
    (and which fields the hook brought), so the first wave rehearsal can confirm whether identity works."""
    try:
        log_path = os.path.join(specs_dir, "project", "guard.log")
        if os.path.isfile(log_path) and "scope-identity" in open(log_path, encoding="utf-8", errors="replace").read():
            return
        log(specs_dir, profile, "scope-identity", "info", "no executor identity; hook fields: " + ",".join(sorted(payload.keys())))
    except Exception:
        pass


def check_scope(target, profile_rules, specs_dir, slug=None):
    """Edits only inside the PATHS of the executor's feature (or, without identity, of the in-flight features) and only inside the repo."""
    hits = []
    if not specs_dir or not target:
        return hits
    root = norm(os.path.dirname(specs_dir))
    absolute = norm(target if os.path.isabs(target) else os.path.join(root, target))
    inside_repo = absolute == root or absolute.startswith(root + "/")
    if not inside_repo and TEMP_HINT.search(absolute + "/"):
        return hits
    if "repo-only" in profile_rules and not inside_repo:
        hits.append(("repo-only", "file outside the repo: only repository code"))
        return hits
    relative = absolute[len(root) + 1:] if inside_repo else absolute
    if relative.startswith(".regression/"):
        return hits
    scope = load_scope(specs_dir, slug)
    if "path-scope" in profile_rules and scope:
        allowed = [norm(item) for item in scope]
        if not any(relative == item or relative.startswith(item.rstrip("/") + "/") for item in allowed):
            hits.append(("path-scope", "outside the feature's PATHS (%s). Need a file outside the scope? Return NEEDS_CONFIRM" % ", ".join(allowed)[:120]))
    return hits


def check_bash(command, profile_rules, permissions):
    """Returns the list of violated (rule_id, reason). Applies to Bash and PowerShell."""
    hits = []
    cwd = permissions.get("_cwd")
    # 1) text of the whole command: catches a push hidden in python -c, $(...), quotes
    if PUSH_RE.search(command):
        if "git-push" in profile_rules:
            hits.append(("git-push", "git push: only on the user's explicit request (at the end of the flow, delivery is done by loop/delivery.py)"))
        if PUSH_PROTECTED_RE.search(command):
            hits.append(("push-protected", "push to a protected branch (integration branch/main/master...): never, only feature branches"))
        if PUSH_FORCE_RE.search(command):
            hits.append(("push-force", "forced push: never"))
    # 2) per segment: write git, add -A, gh, install, credentials
    for segment in split_commands(command):
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
        if re.match(r"^gh\s+(pr|release|repo|gist|workflow)\b", segment) and "pr-release" in profile_rules:
            if not re.match(r"^gh\s+(pr|repo)\s+(view|list|diff|status|checks)\b", segment):
                hits.append(("pr-release", "gh pr/release/gist: leaves the machine; the end-of-flow PR is made by loop/delivery.py"))
        if INSTALL.search(segment) and "install" in profile_rules:
            hits.append(("install", "installing a dependency: only if the spec asks for it"))
        if SECRET_HINT.search(segment) and "secrets" in profile_rules:
            hits.append(("secrets", "credential path (.secrets/shared .env/private key)"))
    if "state-script" in profile_rules and STATE_SCRIPT_RE.search(command):
        hits.append(("state-script", "only the orchestrator writes rule, dependency or graph (rules.py write, audit_deps.py --write/--apply, graph_view.py --write)"))
    if "delivery" in profile_rules and re.search(r"\bdelivery\.py\b", command):
        hits.append(("delivery", "only the orchestrator delivers (push of the feature branch + PR)"))
    # 3) shell write to a file that must never be touched
    for target in shell_write_targets(command):
        if STATE_FILE.search(target) and "state-write" in profile_rules:
            hits.append(("state-write", "only the orchestrator writes ROADMAP/STATE/phases/PERMISSIONS (via shell too)"))
        if SECRET_HINT.search(target) and "secrets" in profile_rules:
            hits.append(("secrets", "write to a credential path"))
        for pattern in permissions.get("deny_path_regex", []):
            if re.search(pattern, target, re.I):
                hits.append(("project-deny", "path forbidden by the project: %s" % pattern))
    for pattern in permissions.get("deny_bash_regex", []):
        if re.search(pattern, command, re.I):
            hits.append(("project-deny", "project rule: %s" % pattern))
    return hits


def is_prod_tool(tool_name):
    lowered = tool_name.lower()
    return any(hint in lowered for hint in prod_hints())


def check_tool(tool_name, tool_input, profile_rules, permissions):
    hits = []
    if tool_name in ("Bash", "PowerShell"):
        return check_bash(tool_input.get("command", ""), profile_rules, permissions)
    if tool_name.startswith("mcp__"):
        short_name = tool_name.rsplit("__", 1)[-1]
        if DB_TOOL.match(tool_name):
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
        target = tool_input.get("file_path", "")
        if STATE_FILE.search(target) and "state-write" in profile_rules:
            hits.append(("state-write", "only the orchestrator writes ROADMAP/STATE/phases/PERMISSIONS"))
        if SECRET_HINT.search(target) and "secrets" in profile_rules:
            hits.append(("secrets", "credential path"))
        if tool_name in ("Write", "Edit"):
            hits.extend(check_scope(target, profile_rules, permissions.get("_specs_dir"), permissions.get("_slug")))
        for pattern in permissions.get("deny_path_regex", []):
            if re.search(pattern, target, re.I):
                hits.append(("project-deny", "path forbidden by the project: %s" % pattern))
    if tool_name in ("Read", "Glob", "Grep"):
        target = json.dumps(tool_input)
        if SECRET_HINT.search(target) and "secrets" in profile_rules:
            hits.append(("secrets", "reading a credential"))
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
            return 0
    profile_rules = PROFILES.get(profile, PROFILES["executor"])
    specs_dir = find_specs_dir(payload.get("cwd"))
    try:
        permissions = load_permissions(specs_dir)
        permissions["_specs_dir"] = specs_dir
        permissions["_cwd"] = payload.get("cwd")
        if specs_dir and "path-scope" in profile_rules:
            permissions["_slug"] = executor_slug(payload)
            if payload.get("tool_name") in ("Write", "Edit") and load_scope(specs_dir):
                # trace to check in the real rehearsal: resolved identity and the fields the hook actually receives
                log(specs_dir, profile, "scope-trace", "info", "slug=%s fields=%s" % (permissions["_slug"], ",".join(sorted(payload.keys()))))
            if not permissions["_slug"] and payload.get("tool_name") in ("Write", "Edit"):
                try:
                    with open(os.path.join(specs_dir, "project", "SCOPE.json"), encoding="utf-8") as handle:
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
    for rule, why in blocked:
        log(specs_dir, profile, rule, "blocked", why)
    rules = ", ".join(sorted({rule for rule, _ in blocked}))
    sys.stderr.write(
        "BLOCKED by the orchestrator guard [" + rules + "]: " + "; ".join(why for _, why in blocked)
        + ". Do not try to work around it. Return STATUS: NEEDS_CONFIRM with the reason; the orchestrator takes it to the user.\n"
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
