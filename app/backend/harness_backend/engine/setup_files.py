#!/usr/bin/env python3
"""Setup files of a repository: what the app generated while planning and what may be uncommitted (BR-02, BR-03, RD-01).

Setup files = `docs/codebase/*.md` of the repo (or of every non-skipped subproject of a confirmed monorepo) plus the root `SYSTEM.md`
map, the root `.gitignore` when the only change is the line the app adds for `.env.test`, the `CLAUDE.md` files that carry an app block, `.specs/project/**` and `.specs/features/**` (never the loop's runtime state under
`.specs/project/loop/` and never an `.env*` file: those may hold secrets). Everything else that is changed in the folder is an "other change".

Python API:
  list_setup_files(repo, entry=None)            -> [{"path", "state"}] uncommitted setup files (any git failure gives [])
  list_other_changes(repo, setup_paths)         -> [path] tracked files changed in the folder that are NOT setup files
  list_present_setup_files(repo, entry=None)   -> [path] every setup file in the folder, committed or not
  carry_setup_files(repo, work, entry=None, include_specs=True) -> [path] missing in `work` copied there, never overwriting a file that exists
  uncommitted_blocks(repos, primary_repo)       -> [{"repo", "setup_files": [...], "other_changes": [...]}] (repos with no worktree yet)
  block_text(block)                             -> the one-line readiness text of a block

This module only READS the user's folder (carry copies out of it into the worktree); it never stages, commits or stashes.
"""
import os
import shutil
import subprocess
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import context_files  # noqa: E402
import subprojects  # noqa: E402

GIT_BINARY = "git"
GIT_TIMEOUT_SECONDS = 30
SPECS_PATHSPECS = (":(glob).specs/project/**", ":(glob).specs/features/**")
EXCLUDED_PATHSPECS = (":(exclude,glob).specs/project/loop/**", ":(exclude,glob).specs/project/.env*", ":(exclude,glob).specs/project/**/.env*")
LOOP_STATE_PREFIX = ".specs/project/loop/"
SPECS_PREFIX = ".specs/"
APP_BLOCK_MARKERS = ("<!-- maestro:context -->", "<!-- CODEBASE:BEGIN -->", "Conformance:", "@.specs/project/RULES.md")
GITIGNORE_PATHSPEC = ":(glob).gitignore"
MONOREPO_ROOT_PATHSPECS = (":(glob)CLAUDE.md", ":(glob)docs/codebase/*.md")  # what the app writes at the root of a monorepo
ROOT_CLAUDE_FILE = "CLAUDE.md"
LIST_LIMIT = 12
COMMIT_MESSAGE = "chore(setup): codebase maps and agent context"
GITIGNORE_FILE = ".gitignore"
APP_IGNORE_LINES = (".specs/project/.env.test",)  # the only line(s) the app adds to the user's .gitignore (test_env_fallback.ensure_ignored)


def run_status(repo, arguments):
    """`git status --porcelain -z <arguments>` -> [(code, path)] in the order git printed them; [] on any failure."""
    try:
        completed = subprocess.run([GIT_BINARY, "-C", repo, "status", "--porcelain", "-z"] + list(arguments), capture_output=True,
                                   timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if completed.returncode != 0:
        return []
    rows, records, index = [], completed.stdout.decode("utf-8", errors="replace").split("\0"), 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], subprojects.normalize(record[3:])
        if "R" in code or "C" in code:
            index += 1  # the origin path
        rows.append((code, path))
    return rows


def has_app_block(repo, path):
    """CLAUDE.md files only count as setup files when they carry something the app wrote (context block, codebase block, Conformance)."""
    try:
        with open(os.path.join(repo, *path.split("/")), encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return False
    return any(marker in text for marker in APP_BLOCK_MARKERS)


def state_of(code):
    letter = "?" if code == "??" else next((one for one in code if one != " "), "M")
    return context_files.STATE_BY_CODE.get(letter, "modified")


def setup_pathspecs(entry):
    """(pathspecs, monorepo) of the setup files. A confirmed monorepo also lists the root CLAUDE.md (the RULES import) and root maps."""
    monorepo = context_files.confirmed_project_folders(entry) is not None
    pathspecs = context_files.build_pathspecs(entry) + list(SPECS_PATHSPECS) + [GITIGNORE_PATHSPEC] + (list(MONOREPO_ROOT_PATHSPECS) if monorepo else []) + list(EXCLUDED_PATHSPECS)
    return pathspecs, monorepo


def is_app_only_gitignore_change(repo, path):
    """True when the uncommitted change of the root `.gitignore` is ONLY the line(s) the app added (an app-generated config, not the user's edit)."""
    if path != GITIGNORE_FILE:
        return False
    try:
        completed = subprocess.run([GIT_BINARY, "-C", repo, "diff", "HEAD", "--unified=0", "--", GITIGNORE_FILE], capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False
    if completed.returncode != 0:
        return False
    changed = [line for line in completed.stdout.decode("utf-8", errors="replace").splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---"))]
    return bool(changed) and all(line.startswith("+") and line[1:].strip() in APP_IGNORE_LINES for line in changed)


def is_setup_path(repo, path, monorepo):
    """True when `path` (found by status or ls-files) is a setup file the app generated."""
    if path == GITIGNORE_FILE:
        return is_app_only_gitignore_change(repo, path)
    if path.startswith(SPECS_PREFIX):
        return not path.startswith(LOOP_STATE_PREFIX)
    kind = context_files.kind_of(path, monorepo)
    if not kind and monorepo and path == ROOT_CLAUDE_FILE:
        kind = context_files.KIND_CLAUDE
    return bool(kind) and not (kind == context_files.KIND_CLAUDE and not has_app_block(repo, path))


def list_setup_files(repo, entry=None):
    """The uncommitted setup files of `repo` (see the module text), sorted by path."""
    try:
        entry = entry if entry is not None else subprojects.load(repo)["entry"]
        pathspecs, monorepo = setup_pathspecs(entry)
    except (OSError, ValueError):
        return []
    items = []
    for code, path in run_status(repo, ["-uall", "--"] + pathspecs):
        if "D" in code:
            continue  # a deleted file has nothing to carry or commit
        if is_setup_path(repo, path, monorepo):
            items.append({"path": path, "state": state_of(code)})
    return sorted(items, key=lambda item: item["path"])


def list_present_setup_files(repo, entry=None):
    """Every setup file that exists in the user's folder, committed or not (tracked + untracked, `.gitignore` honoured). [] on any git failure."""
    try:
        entry = entry if entry is not None else subprojects.load(repo)["entry"]
        pathspecs, monorepo = setup_pathspecs(entry)
        completed = subprocess.run([GIT_BINARY, "-C", repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--"] + pathspecs,
                                   capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if completed.returncode != 0:
        return []
    paths = {subprojects.normalize(record) for record in completed.stdout.decode("utf-8", errors="replace").split("\0") if record}
    return sorted(path for path in paths if is_setup_path(repo, path, monorepo))


def list_other_changes(repo, setup_paths):
    """Tracked files changed in the folder that are not setup files (untracked files are not walked: node_modules and the like)."""
    skip = set(setup_paths)
    paths = []
    for code, path in run_status(repo, ["--untracked-files=no"]):
        if path not in skip and not path.startswith(LOOP_STATE_PREFIX) and path not in paths:
            paths.append(path)
    return sorted(paths)


def carry_setup_files(repo, work, entry=None, include_specs=True):
    """Copies the setup files of `repo` that are MISSING in the new worktree `work`, committed on the user's branch or not (the worktree
    starts from another branch, so files committed on the current one would never arrive). A file that already exists there (the branch
    has it) is never overwritten. `include_specs=False` for a repo whose `.specs` is not used (a non-primary repo). Returns the relative paths copied."""
    copied = []
    for path in list_present_setup_files(repo, entry):
        if not include_specs and path.startswith(SPECS_PREFIX):
            continue
        source, target = os.path.join(repo, *path.split("/")), os.path.join(work, *path.split("/"))
        if not os.path.isfile(source) or os.path.exists(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        copied.append(path)
    return copied


def uncommitted_blocks(repos, primary_repo=None):
    """One block per repo with something uncommitted and no loop worktree yet. `repos` = project_repos items (name, path, primary, role)."""
    blocks = []
    for item in repos or []:
        try:
            if os.path.isdir(item.loop_worktree_path()) or getattr(item, "role", "edit") == "reference":
                continue
            entry = subprojects.load(primary_repo or item.path, None if item.primary or not primary_repo else item.name)["entry"]
            setup = [row["path"] for row in list_setup_files(item.path, entry)]
            other = list_other_changes(item.path, setup)
        except Exception:  # noqa: BLE001  (a repo that cannot be read never takes the readiness down)
            continue
        if setup or other:
            blocks.append({"repo": item.name, "setup_files": setup, "other_changes": other})
    return blocks


def shortened(paths):
    shown = ", ".join(paths[:LIST_LIMIT])
    return shown + (" and %d more" % (len(paths) - LIST_LIMIT) if len(paths) > LIST_LIMIT else "")


def block_text(block):
    """Readiness text of one block (RD-01): the setup files that are carried, then the other changes that are not."""
    parts = []
    setup, other = block["setup_files"], block["other_changes"]
    if setup:
        parts.append("%d setup file%s not committed: they will be carried into the worktree (or commit them from Workspace): %s"
                     % (len(setup), "" if len(setup) == 1 else "s", shortened(setup)))
    if other:
        parts.append("%d other changed file%s will NOT be carried: %s" % (len(other), "" if len(other) == 1 else "s", shortened(other)))
    return "uncommitted changes in %s: %s" % (block["repo"], "; ".join(parts))
