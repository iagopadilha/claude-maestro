#!/usr/bin/env python3
"""The checks of the preflight (`--check` and before every run): environment, ROADMAP, approval, test environment, notice channels, push, setup and the extra `--check --planning` readiness. Each returns its own (problems, warnings) pair.

Moved out of `run_loop.py` as it was. `preflight` itself stays in `run_loop.py` (it looks up `test_environment_warnings` and `planning_readiness` there, and tests replace them).
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import json
import re
import shutil
import subprocess

import app_paths  # noqa: E402  (harness_backend package on sys.path; skill and guard locations)
import audit_deps  # noqa: E402  (forgotten dependency between features)
import claude_binaries  # noqa: E402  (which `claude` command runs, the fallback on an account limit)
import credential_check  # noqa: E402  (TEST-ENVIRONMENT.md names only: no plaintext secret, every $NAME defined in an .env)
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import mcp_access  # noqa: E402  (read-only MCP servers of the turn, computed from ~/.claude.json: no per-project toggle)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in the app .env)
import project_repos  # noqa: E402  (REPOS.json: the project's repos; one primary repo when the file is absent)
import readiness_checks  # noqa: E402  (subprojects, giant files, system map, uncommitted context, known commands)
import subprojects  # noqa: E402  (monorepo list: owner of a path, confirmed or not)
import test_env_fallback  # noqa: E402  (default .env.test from the machine-level TEST values)
from auto_answers import gate_blocked_features, pick_auto_answer  # noqa: E402
from harness_backend.config import skill_locator  # noqa: E402
from loop_io import git, load_json, read, run  # noqa: E402
from worktree_state import roadmap_score  # noqa: E402
from worktrees import branch_exists, choose_base, worktree_repos  # noqa: E402

HERE = ENGINE_DIR
MAX_NAMED_FEATURES = 6


def required_channels():
    """Mandatory notice channels: LOOP_NOTIFY_REQUIRED (comma-separated; empty or unset = none)."""
    notify.envload.load()
    return notify.envload.get_list("LOOP_NOTIFY_REQUIRED")


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
