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
import setup_approval  # noqa: E402

REPOS_FILE_PARTS = (".specs", "project", "REPOS.json")
ROLE_EDIT, ROLE_REFERENCE = "edit", "reference"
ROLES = (ROLE_EDIT, ROLE_REFERENCE)
SCOPE_SOURCE_PLANNING = "planning"  # `scope_source` of a scope applied from the planning chat's REPOS-SUGGESTED.json
REPO_PATH_PREFIX = re.compile(r"^([a-z0-9._-]+):(?![/\\])(.+)$")  # same grammar as loop_state: `<repo>:<path>`; a drive letter is no prefix


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
    """The raw file as {"multi": bool, "repos": [dict], "scope_defined": bool, "scope_source": str}; absent or broken = nothing defined.
    `multi` is derived (more than one entry); the key in the file is only informative."""
    try:
        with open(repos_file_path(primary_repo), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"multi": False, "repos": [], "scope_defined": False, "scope_source": ""}
    if not isinstance(data, dict) or not isinstance(data.get("repos"), list):
        return {"multi": False, "repos": [], "scope_defined": False, "scope_source": ""}
    repos = [item for item in data["repos"] if isinstance(item, dict) and item.get("path")]
    return {"multi": len(repos) > 1, "repos": repos, "scope_defined": bool(data.get("scope_defined")),
            "scope_source": str(data.get("scope_source") or "")}


def normalize_role(value):
    return value if value in ROLES else ROLE_EDIT


def is_scope_defined(primary_repo):
    """Always True now that the confirm step is gone (RP-01): the primary is always `edit`, so a project always has at least one repository
    the loop may change, and the list is managed by the planning chat (or by hand in the Workspace dialog). Kept for the callers that still
    ask (an old project with REPOS.json `scope_defined` keeps working the same way)."""
    return bool(edit_repos(load_repos(primary_repo)))


def load_repos(primary_repo):
    """The project's repos, primary FIRST: every entry of the file (no `multi` cut), the primary always `edit`. One entry when the
    file is absent, broken or empty."""
    data = read_repos_file(primary_repo)
    if not data["repos"]:
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
                             setup=str(item.get("setup") or "").strip(), copy_configured=isinstance(item.get("copy"), list),
                             role=ROLE_EDIT if is_primary else normalize_role(item.get("role")), reason=str(item.get("reason") or "").strip()))
    if not any(repo.primary for repo in repos):
        repos.insert(0, single_repo(primary_repo)[0])  # the primary always exists, even if the file forgot it
    repos.sort(key=lambda repo: not repo.primary)
    return repos


def primary_of(repos):
    return next((repo for repo in repos if repo.primary), repos[0])


def find_repo(repos, name):
    return next((repo for repo in repos if repo.name == name), None)


def edit_repos(repos):
    """Repos a feature may change (role `edit`; the primary always counts)."""
    return [repo for repo in repos if repo.primary or repo.role != ROLE_REFERENCE]


def reference_repos(repos):
    """Read-only repos (role `reference`, never the primary): passed to the loop's claude with `--add-dir`, no worktree."""
    return [repo for repo in repos if not repo.primary and repo.role == ROLE_REFERENCE]


def repos_changed_by_roadmap(primary_name, features):
    """Set of repo names named by `paths=` of the ROADMAP features. `features` = the dict returned by `loop_state.parse_roadmap`
    ({slug: fields}) or any iterable of field dicts. An unprefixed path belongs to the primary, so `primary_name` is in the set
    whenever a feature has any path. Names are not checked against REPOS.json here."""
    rows = features.values() if isinstance(features, dict) else features
    changed = set()
    for feature in rows:
        for entry in str((feature or {}).get("paths") or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            match = REPO_PATH_PREFIX.match(entry)
            changed.add(match.group(1) if match else primary_name)
    return changed


def save_repos(primary_repo, multi, repos, scope_defined=None, scope_source=None, approve_setup_for=()):
    """Writes REPOS.json (the registry service and the engine share this one writer). `scope_defined=None` / `scope_source=None` keep what
    the file says. `multi` is ignored: it is written derived (more than one repo). The primary is saved as `edit`.
    `approve_setup_for` = names of repos whose `setup` the USER just typed in the app: only those texts are recorded as approved
    (`setup_approval`); a setup merely carried over from the file stays as approved or not as it was (the file is agent-writable)."""
    current = read_repos_file(primary_repo)
    if scope_defined is None:
        scope_defined = current["scope_defined"]
    if scope_source is None:
        scope_source = current["scope_source"]
    items = []
    for repo in repos:
        item = {"name": repo.name, "path": repo.path, "primary": repo.primary, "role": ROLE_EDIT if repo.primary else normalize_role(repo.role)}
        if repo.reason:
            item["reason"] = repo.reason
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
        payload = {"multi": len(items) > 1, "scope_defined": bool(scope_defined), "repos": items}
        if scope_source:
            payload["scope_source"] = scope_source
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    for repo in repos:
        if repo.name in approve_setup_for and repo.setup:
            setup_approval.approve_setup(primary_repo, repo.path, repo.setup)
