"""Scratch repo builder: a temp git repo of a given shape (initial commit on `developer`) plus a local bare repo as `origin`.

Shapes (what the repo holds before the loop): back, front, both, monorepo (subprojects under apps/), multi-repo (two repos, `app` primary
and `api`, each with its own bare origin and a REPOS.json in the primary). Only temp folders are touched; nothing leaves the machine.
"""
import os

from fakes import git, write_text

BACK_FILES = {"README.md": "# scratch back\n", "src/__init__.py": "", "src/core.py": "def core():\n    return 1\n", "pyproject.toml": "[project]\nname = \"scratch-back\"\n"}
FRONT_FILES = {"README.md": "# scratch front\n", "web/package.json": "{\"name\": \"scratch-front\", \"version\": \"0.0.1\"}\n", "web/src/main.ts": "export const main = 1\n"}
MONOREPO_FILES = {"README.md": "# scratch monorepo\n", "apps/api/pyproject.toml": "[project]\nname = \"api\"\n", "apps/api/src/core.py": "x = 1\n",
                  "apps/web/package.json": "{\"name\": \"web\"}\n", "apps/web/src/main.ts": "export const main = 1\n"}
SHAPE_FILES = {"back": BACK_FILES, "front": FRONT_FILES, "both": dict(BACK_FILES, **FRONT_FILES), "monorepo": MONOREPO_FILES}
CONFORMANCE = "Conformance: STANDARD\n"


def init_repo(root, name, files, remotes_dir):
    """`<root>/<name>` with one commit on `developer` and a bare `<remotes_dir>/<name>.git` as origin (branch pushed). Returns (repo, remote)."""
    repo, remote = os.path.join(root, name), os.path.join(remotes_dir, name + ".git")
    os.makedirs(repo)
    os.makedirs(remote)
    git(remote, "init", "-q", "--bare")
    git(repo, "init", "-q")
    git(repo, "config", "user.name", "audit")
    git(repo, "config", "user.email", "audit@localhost")
    git(repo, "config", "commit.gpgsign", "false")
    git(repo, "config", "core.autocrlf", "false")
    for relative, text in files.items():
        write_text(os.path.join(repo, relative), text)
    write_text(os.path.join(repo, ".gitignore"), ".specs/\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "initial commit")
    git(repo, "branch", "-M", "developer")
    git(repo, "remote", "add", "origin", remote)
    git(repo, "push", "-q", "-u", "origin", "developer")
    return repo, remote


def build_scratch(shape, root):
    """Builds the repo(s) of `shape` under `root`. Returns {"primary": path, "repos": {name: path}, "remotes": {name: path}, "shape": shape}."""
    remotes_dir = os.path.join(root, "remotes")
    os.makedirs(remotes_dir, exist_ok=True)
    if shape == "multi-repo":
        repos, remotes = {}, {}
        for name, files in (("app", dict(FRONT_FILES, **{"README.md": "# app\n"})), ("api", dict(BACK_FILES, **{"README.md": "# api\n"}))):
            repos[name], remotes[name] = init_repo(root, name, dict(files, **{"CLAUDE.md": CONFORMANCE}), remotes_dir)
        return {"primary": repos["app"], "repos": repos, "remotes": remotes, "shape": shape}
    files = SHAPE_FILES.get(shape, BACK_FILES)  # back-like shapes (waves, pause, budget, ...) use the back repo
    repo, remote = init_repo(root, "proj", files, remotes_dir)
    return {"primary": repo, "repos": {"proj": repo}, "remotes": {"proj": remote}, "shape": shape}


def write_project_files(primary, roadmap, repos=None):
    """ROADMAP.md, CLAUDE.md (Conformance, so the preflight passes) and the roadmap approval, as the panel's Approve button does it."""
    import engine_env  # noqa: F401  (puts the engine on sys.path)
    import loop_state
    write_text(os.path.join(primary, ".specs", "project", "ROADMAP.md"), roadmap)
    write_text(os.path.join(primary, "CLAUDE.md"), CONFORMANCE)
    if repos:
        import project_repos
        project_repos.save_repos(primary, True, [project_repos.RepoRef(name=name, path=path, primary=name == "app") for name, path in repos.items()])
    loop_state.approve_roadmap(primary, "2026-01-01T00:00:00")
