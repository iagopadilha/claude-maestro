#!/usr/bin/env python3
"""Readiness checks that need the subprojects list, the code inventory and the agent-context files (stdlib only).

Called by `run_loop.py` from `preflight` and `planning_readiness` (`--check` / `--check --planning`). Every function returns plain
text lines and never raises into the caller; a warning never becomes a problem.

  subprojects_preflight(root, features, repos=None) -> (problems, warns)   GATE-01..GATE-03
  giant_preflight(root, features, repos=None)       -> warns               GIANT-05
  system_map_warning(root, features)                -> warns               SYS-08
  uncommitted_warnings(repos)                       -> warns               WARN-01
  known_commands(folder)                            -> {build, run, test, summary}

Decisions:
- Only a CONFIRMED monorepo is checked by `subprojects_preflight`; a single project or an unconfirmed list gives nothing.
- A feature of `kind=both` needs no extra rule: each subproject its paths touch is required (both of them when it names both).
- A giant file counts as touched by a feature when a feature path IS the file or is a folder that holds it (the feature may change
  anything below its paths). It is cleared when a `refactor-*` feature whose paths name the file (the file itself or a folder that
  holds it) is in the feature's `deps=` chain (transitive), or when the feature is itself a `refactor-*` one, or when the feature has `note=giant-accepted`.
- Inventories live in the primary repo (`<root>/.specs/project/loop/internal/inventory/*.json`, paths relative to that root), so a
  path prefixed with another repo (`other:src`) is not matched against them.
"""
import glob
import json
import os
import re
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import context_files  # noqa: E402
import loop_state  # noqa: E402
import subprojects  # noqa: E402
import setup_files  # noqa: E402

REPO_PATH_PREFIX = re.compile(r"^([a-z0-9._-]+):(?![/\\])(.+)$")  # same grammar as project_repos / loop_state
INVENTORY_GLOB = os.path.join(".specs", "project", "loop", "internal", "inventory", "*.json")
GIANT_ACCEPTED_NOTE = "giant-accepted"
REFACTOR_PREFIX = "refactor-"
CODEBASE_FOLDER = "docs/codebase"


# ---------------------------------------------------------------- small readers
def read_text(path):
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def load_json(path):
    try:
        data = json.loads(read_text(path) or "null")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def feature_path_list(fields):
    return [item.strip() for item in str(fields.get("paths") or "").split(",") if item.strip()]


def folder_has_map(folder):
    """True when `<folder>/docs/codebase/` holds at least one markdown document."""
    return bool(glob.glob(os.path.join(folder, *CODEBASE_FOLDER.split("/"), "*.md")))


def repo_folders(root, repos):
    """{repo name: folder} for the repos of the project; the primary (and the single-project case) is `root`."""
    folders = {}
    for item in repos or []:
        folders[item.name] = root if item.primary else item.path
    return folders


# ---------------------------------------------------------------- GATE-01..GATE-03
def resolve_path(root, raw_path, repos):
    """(entry key, folder, relative path) for a feature path. A `<repo>:` prefix names a repo of the project or of the stored list."""
    stored_repos = subprojects.read_repos(root)[0]
    folders = repo_folders(root, repos)
    primary = next((item for item in repos or [] if item.primary), None)
    match = REPO_PATH_PREFIX.match(raw_path)
    if match and (match.group(1) in folders or match.group(1) in stored_repos):
        name, relative = match.group(1), subprojects.normalize(match.group(2))
        if primary is not None and name == primary.name:
            return subprojects.default_repo_name(root), root, relative
        return name, folders.get(name, root), relative
    return subprojects.default_repo_name(root), root, subprojects.normalize(raw_path)


def subproject_gaps(folder, project):
    """What a non-skipped subproject lacks for its agents: a list of `conformance` and/or `map`."""
    base = subprojects.normalize(project["path"])
    subfolder = os.path.join(folder, *base.split("/"))
    gaps = []
    if subprojects.read_conformance(folder, base) == "-":
        gaps.append("conformance")
    if project.get("has_code") is not False and not folder_has_map(subfolder):
        gaps.append("map")
    return gaps


def problem_text(base, gaps):
    parts = []
    if "conformance" in gaps:
        parts.append("no `Conformance: STANDARD|LEGACY` line in %s/CLAUDE.md (set it in the Subprojects table)" % base)
    if "map" in gaps:
        parts.append("no map in %s/%s (run Map codebase for it)" % (base, CODEBASE_FOLDER))
    return "subproject `%s` is touched by the roadmap but has %s" % (base, " and ".join(parts))


def touched_subprojects(root, features, repos):
    """(touched {(key, base): (folder, project, [slugs])}, outside {slug: [paths]}, skipped {(key, base): (project, [slugs])},
    confirmed {key: (folder, entry)}) for the features of a confirmed monorepo."""
    touched, outside, skipped, confirmed = {}, {}, {}, {}
    for slug, fields in features.items():
        for raw in feature_path_list(fields):
            key, folder, relative = resolve_path(root, raw, repos)
            if key not in confirmed:
                entry = subprojects.load(root, key)["entry"]
                if not (entry and entry.get("monorepo") is True and entry.get("confirmed") is True):
                    continue
                confirmed[key] = (folder, entry)
            project = subprojects.owner_in(confirmed[key][1], relative)
            if project is None:
                outside.setdefault(slug, []).append(raw)
                continue
            token = (key, subprojects.normalize(project["path"]))
            if project.get("skip") is True:
                skipped.setdefault(token, (project, []))[1].append(slug)
            else:
                touched.setdefault(token, (confirmed[key][0], project, []))[2].append(slug)
    return touched, outside, skipped, confirmed


def subprojects_preflight(root, features, repos=None):
    """GATE-01..GATE-03 -> (problems, warns). ([], []) for anything that is not a confirmed monorepo."""
    problems, warns = [], []
    try:
        touched, outside, skipped, confirmed = touched_subprojects(root, features, repos)
        for (key, base), (folder, project, slugs) in sorted(touched.items()):
            gaps = subproject_gaps(folder, project)
            if gaps:
                problems.append("%s (features: %s)" % (problem_text(base, gaps), ", ".join(sorted(set(slugs)))))
        for slug, paths in outside.items():
            warns.append("feature %s: paths outside any subproject (%s): the agents use the root rules only" % (slug, ", ".join(paths)))
        for (key, base), (project, slugs) in sorted(skipped.items()):
            warns.append("feature %s: subproject `%s` skipped: no map, no Conformance for its agents" % (", ".join(sorted(set(slugs))), base))
        for key, (folder, entry) in confirmed.items():
            for project in subprojects.entry_projects(entry):
                base = subprojects.normalize(project["path"])
                if project.get("skip") is True or (key, base) in touched:
                    continue
                gaps = subproject_gaps(folder, project)
                if gaps:
                    warns.append("subproject `%s` is not touched by any feature yet but has no %s" % (base, " and no ".join(
                        "Conformance line" if gap == "conformance" else "map" for gap in gaps)))
    except Exception as error:  # noqa: BLE001  (the check never takes the preflight down)
        warns.append("subprojects check failed: %s" % error)
    return problems, warns


def installer_problems(root, repos=None):
    """RD-02: while the roadmap does not exist yet, a confirmed monorepo whose subprojects still miss the Conformance line or the map is an
    open installer step: one problem for it (the first real missing step), so the "generate the roadmap" line is not the first thing said."""
    try:
        problems = []
        for item in repos or []:
            entry = subprojects.load(root, item.name)["entry"]
            if not (entry and entry.get("monorepo") is True and entry.get("confirmed") is True) or getattr(item, "role", "edit") == "reference":
                continue
            for project in subprojects.entry_projects(entry):
                gaps = subproject_gaps(item.path, project)
                if project.get("skip") is not True and gaps:
                    problems.append("setup installer not finished: %ssubproject `%s` has no %s" % (
                        "repo %s, " % item.name if len(repos) > 1 else "", subprojects.normalize(project["path"]),
                        " and no ".join("Conformance line" if gap == "conformance" else "map" for gap in gaps)))
        return problems
    except Exception:  # noqa: BLE001  (the check never takes the preflight down)
        return []


# ---------------------------------------------------------------- SYS-08
def system_map_warning(root, features, repos=None):
    """Two or more subprojects touched and no root `docs/codebase/SYSTEM.md` -> one warning."""
    try:
        touched = touched_subprojects(root, features, repos)[0]
    except Exception:  # noqa: BLE001
        return []
    if len(touched) < 2 or os.path.isfile(os.path.join(root, *subprojects.SYSTEM_DOCUMENT.split("/"))):
        return []
    return ["no system map: the agents have no description of how the subprojects talk (features touch %s; Map the system in the Subprojects table)"
            % ", ".join(sorted(base for _, base in touched))]


# ---------------------------------------------------------------- GIANT-05
def giant_files_of(root):
    """{file path relative to root: lines} from every inventory of the project."""
    giants = {}
    for path in sorted(glob.glob(os.path.join(root, INVENTORY_GLOB))):
        for item in load_json(path).get("giant") or []:
            if isinstance(item, dict) and item.get("path"):
                giants[subprojects.normalize(item["path"])] = item.get("lines")
    return giants


def path_covers(path, giant):
    return bool(path) and (giant == path or giant.startswith(path + "/"))


def feature_touches_giant(slug, fields, paths, giant):
    """GIANT-01: the feature names the giant file in its paths, or a folder of its paths holds it AND the feature names the file."""
    if giant in paths:
        return True
    if not any(path_covers(path, giant) for path in paths):
        return False
    text = " ".join([slug] + [str(value) for value in (fields or {}).values()]).lower()
    return os.path.basename(giant).lower() in text


def own_paths(root, fields, primary_names=()):
    """The feature's paths of the primary repo (a path prefixed with another repo is dropped), normalized."""
    own = {subprojects.default_repo_name(root)} | set(primary_names)
    paths = []
    for raw in feature_path_list(fields):
        match = REPO_PATH_PREFIX.match(raw)
        if match and match.group(1) not in own:
            continue
        paths.append(subprojects.normalize(match.group(2) if match else raw))
    return paths


def dependency_chain(features, slug):
    """Every feature slug `slug` depends on, directly or not (`deps=` slugs only)."""
    seen, pending = set(), list(loop_state.split_deps(features.get(slug, {}))[0])
    while pending:
        current = pending.pop()
        if current in seen or current not in features:
            continue
        seen.add(current)
        pending += loop_state.split_deps(features[current])[0]
    return seen


def giant_preflight(root, features, repos=None):
    """GIANT-05 -> warns (one line listing the files and the features)."""
    try:
        giants = giant_files_of(root)
        if not giants or not features:
            return []
        primary_names = [item.name for item in repos or [] if item.primary]
        paths_by_feature = {slug: own_paths(root, fields, primary_names) for slug, fields in features.items()}
        offenders = {}  # giant file -> [feature slugs]
        for slug, fields in features.items():
            if slug.startswith(REFACTOR_PREFIX) or GIANT_ACCEPTED_NOTE in str(fields.get("note") or ""):  # a refactor feature IS the fix
                continue
            chain = [item for item in dependency_chain(features, slug) if item.startswith(REFACTOR_PREFIX)]
            for giant in giants:
                if not feature_touches_giant(slug, fields, paths_by_feature[slug], giant):
                    continue
                if any(path_covers(path, giant) for item in chain for path in paths_by_feature.get(item, [])):
                    continue
                offenders.setdefault(giant, []).append(slug)
        if not offenders:
            return []
        files = ", ".join("%s (%s lines)" % (giant, giants[giant]) if giants[giant] else giant for giant in sorted(offenders))
        slugs = sorted({slug for names in offenders.values() for slug in names})
        return ["giant file(s) touched without a refactor: %s; features: %s. Add a `refactor-*` feature to their `deps=` or mark them `note=giant-accepted`"
                % (files, ", ".join(slugs))]
    except Exception as error:  # noqa: BLE001
        return ["giant-file check failed: %s" % error]


# ---------------------------------------------------------------- WARN-01
def uncommitted_blocks(repos, primary_repo=None):
    """RD-01: [{"repo", "setup_files", "other_changes"}], one per edit repo with something uncommitted, none once its loop worktree exists."""
    return setup_files.uncommitted_blocks(repos, primary_repo)


def uncommitted_warnings(repos, primary_repo=None):
    """RD-01: ONE warning per repo about uncommitted changes (the setup files that will be carried, then the other changes that will not).
    It replaces the three repeated warnings of the single-repo and multi-repo checks."""
    return [setup_files.block_text(block) for block in uncommitted_blocks(repos, primary_repo)]


# ---------------------------------------------------------------- build / run / test commands
def find_dotnet(folder, max_depth=3):
    """First .sln/.csproj up to `max_depth` folders below `folder` (ignores bin/obj/node_modules/.git/.specs). Empty if none."""
    skip = {"bin", "obj", "node_modules", ".git", ".specs", ".vs", "packages"}
    for depth in range(max_depth + 1):
        for extension in ("*.sln", "*.slnx", "*.csproj"):
            hits = sorted(path for path in glob.glob(os.path.join(folder, *(["*"] * depth), extension))
                          if not skip.intersection(os.path.relpath(path, folder).split(os.sep)[:-1]))
            if hits:
                return os.path.relpath(hits[0], folder).replace(os.sep, "/")
    return ""


def known_commands(folder):
    """How a project folder is built, run and tested, as far as the folder itself says:
    {"build", "run", "test": command text or "", "summary": what `run_loop.known_test_command` always returned ('' when unknown)}."""
    commands = {"build": "", "run": "", "test": "", "summary": ""}
    package = load_json(os.path.join(folder, "package.json"))
    scripts = package.get("scripts") if isinstance(package.get("scripts"), dict) else {}
    for slot, names in (("build", ("build",)), ("run", ("dev", "start")), ("test", ("test",))):
        name = next((candidate for candidate in names if candidate in scripts), "")
        if name:
            commands[slot] = "npm test" if slot == "test" else "npm run " + name
    if find_dotnet(folder):
        commands["build"] = commands["build"] or "dotnet build"
        commands["test"] = commands["test"] or "dotnet test"
        commands["run"] = commands["run"] or "dotnet run"
    for name, test_command in (("pytest.ini", "pytest"), ("tox.ini", "tox"), ("go.mod", "go test ./..."), ("pom.xml", "mvn test"),
                               ("Cargo.toml", "cargo test"), ("Makefile", "make test"), ("setup.cfg", "pytest")):
        if os.path.isfile(os.path.join(folder, name)):
            commands["test"] = commands["test"] or test_command
    if "[tool.pytest" in read_text(os.path.join(folder, "pyproject.toml")) or glob.glob(os.path.join(folder, "tests", "**", "test_*.py"), recursive=True):
        commands["test"] = commands["test"] or "python -m unittest / pytest"
    if read_text(os.path.join(folder, *CODEBASE_FOLDER.split("/"), "TESTING.md")).strip():
        commands["summary"] = CODEBASE_FOLDER + "/TESTING.md"
    elif "test" in scripts or "build" in scripts:
        commands["summary"] = "npm test"
    elif find_dotnet(folder):
        commands["summary"] = "dotnet test"
    else:
        commands["summary"] = next((value for value in (commands["test"],) if value), "")
    return commands
