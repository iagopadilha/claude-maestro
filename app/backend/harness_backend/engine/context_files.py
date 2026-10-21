#!/usr/bin/env python3
"""Agent-context files that exist in a repository folder but are not committed (WARN-01, design D16).

The loop worktree is created when the loop starts, from the last commit, so a map (`docs/codebase/*.md`), a `CLAUDE.md` or the
root `docs/codebase/SYSTEM.md` that is only on disk is invisible to the agents. This module only READS: it never stages, copies,
commits or stashes anything and never changes how the worktree is created.

Python API:
  uncommitted_context(repo, subprojects_entry=None) -> [{"path", "kind": map|claude|system, "state"}]   ([] on any git failure)
  uncommitted_warning_text(items) -> the WARNING text ("" for no items)
  uncommitted_chip_summary(items) -> e.g. "maps: 3 files, CLAUDE.md: backend, SYSTEM.md"

The scope is one `git status --porcelain -uall -z` limited with pathspecs to those files, so a huge untracked folder such as
`node_modules` is never walked and `.specs/` is never listed.
Confirmed monorepo: `<path>/docs/codebase/*.md` and `<path>/CLAUDE.md` of every non-skipped subproject plus `docs/codebase/SYSTEM.md`
(the root `CLAUDE.md` is not listed). Anything else: the root `docs/codebase/*.md` and the root `CLAUDE.md`.
"""
import os
import subprocess
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprojects  # noqa: E402

GIT_BINARY = "git"
GIT_TIMEOUT_SECONDS = 30
KIND_MAP, KIND_CLAUDE, KIND_SYSTEM = "map", "claude", "system"
CODEBASE_FOLDER = "docs/codebase"
SYSTEM_FILE = subprojects.SYSTEM_DOCUMENT
CLAUDE_FILE = "CLAUDE.md"
LIST_LIMIT = 12
WARNING_SENTENCE = ("These files are not committed. The loop worktree is created when the loop starts, from the last commit, "
                    "so the agents will not see them. Commit them first.")
STATE_BY_CODE = {"?": "untracked", "A": "added", "M": "modified", "R": "renamed", "C": "added", "T": "modified"}


def confirmed_project_folders(entry):
    """The folders of a confirmed monorepo's non-skipped subprojects; None when the entry is not a confirmed monorepo."""
    if not (isinstance(entry, dict) and entry.get("monorepo") is True and entry.get("confirmed") is True):
        return None
    folders = []
    for project in subprojects.entry_projects(entry):
        folder = subprojects.normalize(project["path"])
        if folder and project.get("skip") is not True and folder not in folders:
            folders.append(folder)
    return folders


def build_pathspecs(entry):
    """Glob pathspecs of the agent-context files (the folder list comes from a confirmed monorepo entry, else the root)."""
    folders = confirmed_project_folders(entry)
    if folders is None:
        return [":(glob)%s/*.md" % CODEBASE_FOLDER, ":(glob)%s" % CLAUDE_FILE]
    specs = []
    for folder in folders:
        specs += [":(glob)%s/%s/*.md" % (folder, CODEBASE_FOLDER), ":(glob)%s/%s" % (folder, CLAUDE_FILE)]
    return specs + [":(glob)%s" % SYSTEM_FILE]


def kind_of(path, is_monorepo):
    """map | claude | system for a path found by the status, or None when it is not an agent-context file."""
    name = path.rsplit("/", 1)[-1]
    if path == SYSTEM_FILE and is_monorepo:
        return KIND_SYSTEM
    if name == CLAUDE_FILE and not (is_monorepo and path == CLAUDE_FILE):
        return KIND_CLAUDE
    if name.endswith(".md") and ("/" + path).endswith("/%s/%s" % (CODEBASE_FOLDER, name)):
        return KIND_MAP
    return None


def parse_status(output, is_monorepo):
    """`git status --porcelain -z` text -> items. Records are `XY path`; a rename/copy is followed by one extra record (the origin)."""
    items, records, index = [], output.split("\0"), 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        code, path = record[:2], subprojects.normalize(record[3:])
        if "R" in code or "C" in code:
            index += 1  # the origin path
        if "D" in code:
            continue  # a deleted file has nothing to carry
        kind = kind_of(path, is_monorepo)
        if kind:
            letter = "?" if code == "??" else next((one for one in code if one != " "), "M")
            items.append({"path": path, "kind": kind, "state": STATE_BY_CODE.get(letter, "modified")})
    return sorted(items, key=lambda item: item["path"])


def uncommitted_context(repo, subprojects_entry=None):
    """The uncommitted agent-context files of `repo` (see the module text). One git call; any failure gives []."""
    try:
        entry = subprojects_entry if subprojects_entry is not None else subprojects.load(repo)["entry"]
        completed = subprocess.run([GIT_BINARY, "-C", repo, "status", "--porcelain", "-uall", "-z", "--"] + build_pathspecs(entry),
                                   capture_output=True, timeout=GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError, ValueError):
        return []
    if completed.returncode != 0:
        return []
    return parse_status(completed.stdout.decode("utf-8", errors="replace"), confirmed_project_folders(entry) is not None)


def uncommitted_warning_text(items):
    """The WARNING of WARN-01: the fixed sentence plus up to 12 names then "and N more". "" when there is nothing to say."""
    if not items:
        return ""
    names = [item["path"] for item in items]
    shown = ", ".join(names[:LIST_LIMIT])
    if len(names) > LIST_LIMIT:
        shown += " and %d more" % (len(names) - LIST_LIMIT)
    return "%s %s" % (WARNING_SENTENCE, shown)


def uncommitted_chip_summary(items):
    """Short text for the workspace chip, e.g. "maps: 3 files, CLAUDE.md: backend, SYSTEM.md"."""
    parts = []
    maps = [item for item in items if item["kind"] == KIND_MAP]
    if maps:
        parts.append("maps: %d file%s" % (len(maps), "" if len(maps) == 1 else "s"))
    owners = [item["path"].rsplit("/", 1)[0] if "/" in item["path"] else "root" for item in items if item["kind"] == KIND_CLAUDE]
    if owners:
        parts.append(CLAUDE_FILE if owners == ["root"] else "%s: %s" % (CLAUDE_FILE, ", ".join(owners)))
    if any(item["kind"] == KIND_SYSTEM for item in items):
        parts.append("SYSTEM.md")
    return ", ".join(parts)
