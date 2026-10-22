#!/usr/bin/env python3
"""Subprojects of a monorepo: detection (no AI), the stored list and the owner of a path.

Usage:
  python subprojects.py detect <root> [--json]
  python subprojects.py show <root> [--path <p>] [--repo <name>]
  python subprojects.py for-paths <root> --paths a,b [--repo <name>]

`detect` reads the folder layout only (rules DET-01..DET-05) and writes nothing.
`show` prints the stored entry of `.specs/project/SUBPROJECTS.json` (keyed by repo name, default = the root folder name) and,
with --path, the subproject that owns that path (longest prefix; `<repo>:path` uses that repo's list).
`for-paths` prints, for the touched paths, one line per touched non-skipped subproject:
  SUBPROJECT: <path> | CLAUDE: <path>/CLAUDE.md | MAP: <path>/docs/codebase | SKILL: <backend-dev|frontend-dev|-> | CONFORMANCE: <STANDARD|LEGACY|->
plus `SYSTEM: docs/codebase/SYSTEM.md` when that file exists and at least one SUBPROJECT line was printed.
It prints NOTHING (exit 0) for a single project or a list that is not confirmed: no line = nothing changes for the agents.
Nothing here raises into a caller: an unreadable list becomes an `error` text.

Python API (imported by run_loop and the services):
  detect(root) -> {monorepo, projects:[{path, name, stack, skill, skip, reason, has_code, [warning]}], reason, warnings}
  load(root, repo_name=None) -> {"entry": dict or None, "error": str}
  owner_of(root, path, repo_name=None) -> project entry or None
  monorepo_confirmed(root, repo_name=None) -> bool
  for_paths_lines(root, paths, repo_name=None) -> [line, ...]
"""
import argparse
import glob
import json
import os
import re
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
from classify_structure import SKIP_DIR_NAMES  # noqa: E402
from scale import CODE_EXT  # noqa: E402

SUBPROJECTS_RELATIVE_PATH = os.path.join(".specs", "project", "SUBPROJECTS.json")
SYSTEM_DOCUMENT = "docs/codebase/SYSTEM.md"
PROJECT_FILE_PATTERNS = ("*.sln", "*.slnx", "*.csproj", "package.json", "pyproject.toml", "go.mod", "pom.xml", "Cargo.toml",
                         "composer.json", "Gemfile")
CONTAINER_NAMES = ("apps", "packages", "services", "src")
# Skipped at any depth on top of the shared list, plus every hidden folder (`packages` only counts when it holds a project file).
EXTRA_SKIP_NAMES = frozenset({"docs"})
PROJECT_FILE_DEPTH = 2
STACK_BY_FILE = (("pyproject.toml", "python"), ("go.mod", "go"), ("pom.xml", "java"), ("Cargo.toml", "rust"),
                 ("composer.json", "php"), ("Gemfile", "ruby"))
SKILL_BY_STACK = {"dotnet": "backend-dev", "nestjs": "backend-dev", "react": "frontend-dev"}
CONFORMANCE_LINE = re.compile(r"^Conformance:\s*(STANDARD|LEGACY)\b", re.MULTILINE)


def normalize(path):
    return str(path or "").replace("\\", "/").strip().strip("/")


def is_skipped_folder(name):
    return name in SKIP_DIR_NAMES or name in EXTRA_SKIP_NAMES or name.startswith(".")


def subfolders(folder):
    try:
        return sorted(name for name in os.listdir(folder) if os.path.isdir(os.path.join(folder, name)))
    except OSError:
        return []


def project_files_in(folder):
    found = []
    for pattern in PROJECT_FILE_PATTERNS:
        found += glob.glob(os.path.join(glob.escape(folder), pattern))
    return sorted(found)


def project_files_within(folder, depth=PROJECT_FILE_DEPTH):
    """Project files in `folder` and up to `depth` levels below it, never inside a skipped folder."""
    found = project_files_in(folder)
    if depth > 0:
        for name in subfolders(folder):
            if not is_skipped_folder(name):
                found += project_files_within(os.path.join(folder, name), depth - 1)
    return found


def read_package(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def workspace_patterns(package):
    workspaces = package.get("workspaces")
    if isinstance(workspaces, dict):
        workspaces = workspaces.get("packages")
    return [item for item in workspaces if isinstance(item, str)] if isinstance(workspaces, list) else []


def infer_stack(folder):
    files = project_files_within(folder)
    names = [os.path.basename(path) for path in files]
    if any(name.lower().endswith((".sln", ".slnx", ".csproj")) for name in names):
        return "dotnet"
    packages = [path for path in files if os.path.basename(path) == "package.json"]
    packages.sort(key=lambda path: (os.path.dirname(path) != folder, len(path)))
    if packages:
        package = read_package(packages[0])
        dependencies = {}
        for key in ("dependencies", "devDependencies", "peerDependencies"):
            if isinstance(package.get(key), dict):
                dependencies.update(package[key])
        if "@nestjs/core" in dependencies:
            return "nestjs"
        if "react" in dependencies:
            return "react"
        return "node"
    for file_name, stack in STACK_BY_FILE:
        if file_name in names:
            return stack
    return "unknown"


def has_code_files(folder):
    for _current, dirs, names in os.walk(folder):
        dirs[:] = [name for name in dirs if name not in SKIP_DIR_NAMES]
        if any(os.path.splitext(name)[1].lower() in CODE_EXT for name in names):
            return True
    return False


def make_project(root, relative_path, taken_names):
    folder = os.path.join(root, *relative_path.split("/"))
    stack = infer_stack(folder)
    name = relative_path.replace("/", "-")
    unique, counter = name, 2
    while unique in taken_names:
        unique, counter = "%s-%d" % (name, counter), counter + 1
    taken_names.add(unique)
    project = {"path": relative_path, "name": unique, "stack": stack, "skill": SKILL_BY_STACK.get(stack, ""), "skip": False,
               "reason": "", "has_code": has_code_files(folder)}
    if os.path.exists(os.path.join(folder, ".git")):
        project["warning"] = "has its own .git (a separate repository inside the folder)"
    return project


def candidates_by_folders(root):
    """Depth 1 first; when none qualifies, the children of the container folders (apps, packages, services, src)."""
    found, containers = [], []
    for name in subfolders(root):
        folder = os.path.join(root, name)
        if name in CONTAINER_NAMES and not project_files_in(folder):
            if not name.startswith("."):
                containers.append(name)
        elif not is_skipped_folder(name) and project_files_within(folder):
            found.append(name)
    if found:
        return found
    for container in containers:
        for name in subfolders(os.path.join(root, container)):
            if not is_skipped_folder(name) and project_files_within(os.path.join(root, container, name)):
                found.append("%s/%s" % (container, name))
    return found


def candidates_by_workspaces(root, patterns):
    included, excluded = [], set()
    for pattern in patterns:
        negated = pattern.startswith("!")
        for match in sorted(glob.glob(os.path.join(glob.escape(root), *normalize(pattern.lstrip("!")).split("/")))):
            relative = normalize(os.path.relpath(match, root))
            if not os.path.isdir(match) or any(is_skipped_folder(part) for part in relative.split("/")):
                continue
            if negated:
                excluded.add(relative)
            elif relative not in included:
                included.append(relative)
    return [path for path in included if path not in excluded]


def detect(root):
    """Script detection (DET-01..DET-05). Returns {monorepo, projects, reason, warnings}; never raises."""
    root = os.path.abspath(root)
    result = {"monorepo": False, "projects": [], "reason": "", "warnings": []}
    try:
        root_files = project_files_in(root)
        root_names = [os.path.basename(path) for path in root_files]
        has_solution = any(name.lower().endswith((".sln", ".slnx")) for name in root_names)
        patterns = workspace_patterns(read_package(os.path.join(root, "package.json"))) if "package.json" in root_names else []
        if root_files and not (patterns and not has_solution):
            result["reason"] = "the root has a project file: a single project"
            return result
        paths = candidates_by_workspaces(root, patterns) if patterns else candidates_by_folders(root)
        if len(paths) < 2:
            result["reason"] = "fewer than two project folders: not a monorepo"
            return result
        taken_names = set()
        result["projects"] = [make_project(root, path, taken_names) for path in paths]
        result["monorepo"] = True
        result["reason"] = "workspaces of the root package.json" if patterns else "%d project folders under the root" % len(paths)
        result["warnings"] = ["%s: %s" % (project["path"], project["warning"]) for project in result["projects"] if "warning" in project]
    except OSError as error:
        result = {"monorepo": False, "projects": [], "reason": "detection failed: %s" % error, "warnings": []}
    return result


LOOP_WORKTREE_SUFFIX = "-worktree-loop"  # the loop worktree is `../<repo>-worktree-loop`: its list is still keyed by the repo name


def default_repo_name(root):
    name = os.path.basename(os.path.abspath(root))
    return name[:-len(LOOP_WORKTREE_SUFFIX)] if name.endswith(LOOP_WORKTREE_SUFFIX) else name


def read_repos(root):
    """Returns (repos dict, error text). Never raises."""
    path = os.path.join(root, SUBPROJECTS_RELATIVE_PATH)
    if not os.path.exists(path):
        return {}, ""
    try:
        with open(path, encoding="utf-8-sig") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as error:
        return {}, "unreadable SUBPROJECTS.json: %s" % error
    repos = data.get("repos") if isinstance(data, dict) else None
    if not isinstance(repos, dict):
        return {}, "SUBPROJECTS.json has no `repos` object"
    return repos, ""


def load(root, repo_name=None):
    """The stored entry of a repo: {"entry": dict or None, "error": str}. `entry` is None when absent or unreadable."""
    repos, error = read_repos(root)
    entry = repos.get(repo_name or default_repo_name(root))
    return {"entry": entry if isinstance(entry, dict) else None, "error": error}


def entry_projects(entry):
    projects = entry.get("projects") if isinstance(entry, dict) else None
    return [project for project in projects if isinstance(project, dict) and project.get("path")] if isinstance(projects, list) else []


def monorepo_confirmed(root, repo_name=None):
    entry = load(root, repo_name)["entry"]
    return bool(entry and entry.get("monorepo") is True and entry.get("confirmed") is True)


def split_repo_prefix(root, path, repo_name):
    """`<repo>:path` -> (repo, path) when <repo> is a repo of the stored file; otherwise (default repo, path)."""
    text = str(path or "").strip()
    prefix, separator, rest = text.partition(":")
    if separator and len(prefix) > 1 and "/" not in prefix and "\\" not in prefix and prefix in read_repos(root)[0]:
        return prefix, normalize(rest)
    return repo_name or default_repo_name(root), normalize(text)


def owner_in(entry, path):
    best = None
    for project in entry_projects(entry):
        base = normalize(project["path"])
        if base and (path == base or path.startswith(base + "/")) and (best is None or len(base) > len(normalize(best["path"]))):
            best = project
    return best


def owner_of(root, path, repo_name=None):
    """The subproject entry owning `path` (longest prefix), or None. Skipped subprojects are returned too."""
    repo, relative = split_repo_prefix(root, path, repo_name)
    entry = load(root, repo)["entry"]
    return owner_in(entry, relative) if entry else None


def read_conformance(root, project_path):
    try:
        with open(os.path.join(root, *normalize(project_path).split("/"), "CLAUDE.md"), encoding="utf-8-sig") as handle:
            match = CONFORMANCE_LINE.search(handle.read())
        return match.group(1) if match else "-"
    except OSError:
        return "-"


def for_paths_lines(root, paths, repo_name=None):
    """The SUBPROJECT lines (and SYSTEM) of the touched paths. [] for a single project or an unconfirmed list."""
    lines, seen = [], set()
    for path in paths:
        repo, relative = split_repo_prefix(root, path, repo_name)
        if not monorepo_confirmed(root, repo):
            continue
        project = owner_in(load(root, repo)["entry"], relative)
        if not project or project.get("skip") is True:
            continue
        base = normalize(project["path"])
        if (repo, base) in seen:
            continue
        seen.add((repo, base))
        lines.append("SUBPROJECT: %s | CLAUDE: %s/CLAUDE.md | MAP: %s/docs/codebase | SKILL: %s | CONFORMANCE: %s" % (
            base, base, base, project.get("skill") or "-", read_conformance(root, base)))
    if lines and os.path.isfile(os.path.join(root, *SYSTEM_DOCUMENT.split("/"))):
        lines.append("SYSTEM: " + SYSTEM_DOCUMENT)
    return lines


def command_detect(arguments):
    result = detect(arguments.root)
    if arguments.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    print("%s: %s" % ("monorepo" if result["monorepo"] else "single project", result["reason"]))
    for project in result["projects"]:
        print("  %s | name=%s | stack=%s | skill=%s | code=%s" % (project["path"], project["name"], project["stack"],
                                                                 project["skill"] or "-", "yes" if project["has_code"] else "no"))
    for warning in result["warnings"]:
        print("  warning: " + warning)
    return 0


def command_show(arguments):
    loaded = load(arguments.root, arguments.repo)
    if loaded["error"]:
        print("error: " + loaded["error"])
    if arguments.path:
        owner = owner_of(arguments.root, arguments.path, arguments.repo)
        print(json.dumps(owner, ensure_ascii=False) if owner else "no owner")
    else:
        print(json.dumps(loaded["entry"], ensure_ascii=False, indent=2) if loaded["entry"] else "no entry")
    return 0


def command_for_paths(arguments):
    paths = [item for item in (arguments.paths or "").split(",") if item.strip()]
    for line in for_paths_lines(arguments.root, paths, arguments.repo):
        print(line)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    detect_parser = commands.add_parser("detect")
    detect_parser.add_argument("root")
    detect_parser.add_argument("--json", action="store_true")
    detect_parser.set_defaults(handler=command_detect)
    show_parser = commands.add_parser("show")
    show_parser.add_argument("root")
    show_parser.add_argument("--path")
    show_parser.add_argument("--repo")
    show_parser.set_defaults(handler=command_show)
    paths_parser = commands.add_parser("for-paths")
    paths_parser.add_argument("root")
    paths_parser.add_argument("--paths", default="")
    paths_parser.add_argument("--repo")
    paths_parser.set_defaults(handler=command_for_paths)
    return parser


def main(argv=None):
    arguments = build_parser().parse_args(argv)
    return arguments.handler(arguments)


if __name__ == "__main__":
    sys.exit(main())
