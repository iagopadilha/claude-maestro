#!/usr/bin/env python3
"""Creating and re-attaching the loop worktrees: base branch choice, carried setup files, local file copies, the primary worktree and one per extra repo.

Moved out of `run_loop.py` as it was. `ensure_worktrees_or_stop` stays in `run_loop.py` (it calls `ensure_worktrees`, which tests replace).
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import datetime
import glob
import hashlib
import re
import shutil
import subprocess
import tempfile

import delivery  # noqa: E402  (push of the feature branch + PR to the integration branch, only at the end)
import detect_setup  # noqa: E402  (automatic setup command and local files to copy of a repo)
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import project_repos  # noqa: E402  (REPOS.json: the project's repos; one primary repo when the file is absent)
import setup_approval  # noqa: E402  (a stored setup command runs only if the user saved that exact text through the app)
import setup_files  # noqa: E402  (uncommitted setup files: carried into a new worktree, one readiness block per repo)
import subprojects  # noqa: E402  (monorepo list: owner of a path, confirmed or not)
from loop_io import git, load_json, merge_pending_delivery, read, save_meta, worktree_of  # noqa: E402

HEAVY_FOLDERS = ("node_modules", "bin", "obj", "dist")  # never copied into a worktree (they stay with --setup)
LAST_RUN_NAME = "last-run.json"  # written by close_loop in the primary repo; names the branch a new start re-attaches to
MIN_GIT_WORKTREE_VERSION = (2, 5)


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


def refuse_unapproved_setup(item, primary_repo):
    """Stops BEFORE the worktree is created when the repo's stored setup (REPOS.json, agent-writable) is not the text the user saved in the
    app: the step line `[repo] error: ...` and the exit text show why. Nothing runs; the user re-saves the setup and runs again."""
    problem = setup_approval.describe_unapproved_setup(primary_repo, item)
    if problem:
        step(item.name, "error", problem)
        sys.exit("repo %s: %s" % (item.name, problem))


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
    refuse_unapproved_setup(item, opts.repo_path)
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
    if not os.path.isdir(work_now):  # an existing worktree never runs setup again
        refuse_unapproved_setup(primary, repo)
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
