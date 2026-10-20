"""Repo list of a project (`<primary repo>/.specs/project/REPOS.json`), read by the engine.

Self-contained on purpose: it imports only the domain entity, never an app service. No file = exactly ONE primary repo with
the same worktree path as a single-repo project (`../<repo>-worktree-loop`), so single-repo projects need no migration.
"""
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402,F401  (puts the harness_backend package on sys.path)
from harness_backend.domain.project import RepoRef  # noqa: E402

REPOS_FILE_PARTS = (".specs", "project", "REPOS.json")


def repos_file_path(primary_repo):
    return os.path.join(primary_repo, *REPOS_FILE_PARTS)


def short_name_of(path):
    """Short, lowercase name from the folder name; used in `paths=<repo>:<path>` so it stays free of ':' and ','."""
    base = re.sub(r"[^a-z0-9._-]+", "-", os.path.basename(os.path.normpath(path)).lower()).strip("-")
    return base or "repo"


def unique_name(name, taken):
    """`name`, or `name-2`, `name-3`... when already taken (stable: decided once, at add time, then stored)."""
    candidate, number = name, 2
    while candidate in taken:
        candidate = "%s-%d" % (name, number)
        number += 1
    return candidate


def single_repo(primary_repo):
    return [RepoRef(name=short_name_of(primary_repo), path=primary_repo, primary=True)]


def read_repos_file(primary_repo):
    """The raw file as {"multi": bool, "repos": [dict], "scope_defined": bool}; absent or broken = nothing defined."""
    try:
        with open(repos_file_path(primary_repo), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"multi": False, "repos": [], "scope_defined": False}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        return {"multi": False, "repos": [], "scope_defined": False}
    return {"multi": bool(data.get("multi")), "repos": [item for item in data["repos"] if isinstance(item, dict) and item.get("path")],
            "scope_defined": bool(data.get("scope_defined"))}


def is_scope_defined(primary_repo):
    """True once the user defined WHICH repositories the loop touches (before any loop): by choosing/confirming them in the repository
    manager (REPOS.json `scope_defined`), or by listing more than the primary. No file = not defined yet."""
    data = read_repos_file(primary_repo)
    return data["scope_defined"] or (data["multi"] and len(data["repos"]) > 1)


def load_repos(primary_repo):
    """The project's repos, primary FIRST. One entry (today's behaviour) when the file is absent, unchecked or empty."""
    data = read_repos_file(primary_repo)
    if not data["multi"] or not data["repos"]:
        return single_repo(primary_repo)
    primary_key = os.path.normcase(os.path.normpath(primary_repo))
    repos, taken = [], set()
    for item in data["repos"]:
        path = str(item["path"])
        is_primary = os.path.normcase(os.path.normpath(path)) == primary_key
        name = unique_name(str(item.get("name") or short_name_of(path)), taken)
        taken.add(name)
        copy = [str(entry) for entry in item.get("copy") or [] if isinstance(entry, str)]
        repos.append(RepoRef(name=name, path=path, primary=is_primary, worktree_path=str(item.get("worktree_path") or ""), copy=copy,
                             setup=str(item.get("setup") or "").strip(), copy_configured=isinstance(item.get("copy"), list)))
    if not any(repo.primary for repo in repos):
        repos.insert(0, single_repo(primary_repo)[0])  # the primary always exists, even if the file forgot it
    repos.sort(key=lambda repo: not repo.primary)
    return repos


def primary_of(repos):
    return next((repo for repo in repos if repo.primary), repos[0])


def find_repo(repos, name):
    return next((repo for repo in repos if repo.name == name), None)


def save_repos(primary_repo, multi, repos, scope_defined=None):
    """Writes REPOS.json (the registry service and the engine share this one writer). `scope_defined=None` keeps what the file says."""
    if scope_defined is None:
        scope_defined = read_repos_file(primary_repo)["scope_defined"]
    items = []
    for repo in repos:
        item = {"name": repo.name, "path": repo.path, "primary": repo.primary}
        if repo.worktree_path:
            item["worktree_path"] = repo.worktree_path
        if repo.copy or repo.copy_configured:  # absent key = never configured (auto copy); `[]` = the user chose none
            item["copy"] = list(repo.copy)
        if repo.setup:
            item["setup"] = repo.setup
        items.append(item)
    target = repos_file_path(primary_repo)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump({"multi": bool(multi), "scope_defined": bool(scope_defined), "repos": items}, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
