"""Production database script: ONE reviewed `.sql` file the user runs on PRODUCTION before deploying. Nothing here touches a database.

Called at delivery (`delivery.finalize_project`) and at close (`close_loop.close`). Per EDIT repository (REPOS.json `role`, missing =
edit) whose loop branch added migrations (the `Migrations/` files that appear between the base branch and the loop branch, read with
git read commands) it runs `dotnet ef migrations script --idempotent <last base migration> <last loop migration>` (the runner is
injectable; this command generates text and never connects to a database), then appends the manual `.sql` scripts the features left in
`.specs/features/*/sql/` in ROADMAP order. Output: `<output_dir>/<project>-prod-database-<YYYY-MM-DD>.sql` (`output_dir` = the app's
output folder, `~/Downloads` by default) with a header "run on PRODUCTION before deploying; review first".

Nothing to script (no migration, no `.sql`) = no file. A failure never blocks a delivery: it becomes a warning in the result (and a
comment inside the file, when the file is written). Stdlib only.
"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loop_state  # noqa: E402

MIGRATIONS_FOLDER = "Migrations"
MIGRATION_FILE = re.compile(r"^(\d{8,}_[\w-]+)\.cs$")  # `20260101120000_AddOrders.cs` (not `.Designer.cs`, not the snapshot)
SQL_FOLDER_PARTS = ("features", "*", "sql")
HEADER = ("-- ============================================================================\n"
          "-- RUN ON PRODUCTION BEFORE DEPLOYING. REVIEW FIRST.\n"
          "-- Project: %s    Generated: %s\n"
          "-- Part 1: idempotent migration script per repository (dotnet ef migrations script --idempotent).\n"
          "-- Part 2: manual .sql scripts of the features, in roadmap order.\n"
          "-- Nothing in this file was run on any database by the loop.\n"
          "-- ============================================================================\n")
DOTNET_TIMEOUT_SECONDS = 300
GIT_TIMEOUT_SECONDS = 60


def run_process(args, cwd=None, timeout=DOTNET_TIMEOUT_SECONDS):
    """(code, stdout, stderr) of a command; an OS error is code 1 with the error text."""
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout, out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git_lines(root, *args):
    code, out, _ = run_process(["git", "-C", root, *args], timeout=GIT_TIMEOUT_SECONDS)
    return [line.strip() for line in out.splitlines() if line.strip()] if code == 0 else None


def git_ok(root, *args):
    return run_process(["git", "-C", root, *args], timeout=GIT_TIMEOUT_SECONDS)[0] == 0


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


# ---------------------------------------------------------------- which repositories are edited
def read_roles(*repo_paths):
    """{repo name or path (normalized): role} from the first readable REPOS.json among `repo_paths` (`role` missing = "edit")."""
    for repo_path in repo_paths:
        if not repo_path:
            continue
        try:
            with open(os.path.join(repo_path, ".specs", "project", "REPOS.json"), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            continue
        roles = {}
        for item in data.get("repos", []) if isinstance(data, dict) and isinstance(data.get("repos"), list) else []:
            if isinstance(item, dict):
                role = "reference" if item.get("role") == "reference" else "edit"
                if item.get("name"):
                    roles[str(item["name"])] = role
                if item.get("path"):
                    roles[os.path.normcase(os.path.abspath(str(item["path"])))] = role
        return roles
    return {}


def role_of(roles, name, path=""):
    if name in roles:
        return roles[name]
    return roles.get(os.path.normcase(os.path.abspath(path)), "edit") if path else "edit"


def entries_from_meta(primary_work, meta):
    """[{name, root, branch, base, primary, repo_path}] from a primary worktree's meta.json (single repo when it has no `repos` map)."""
    repos = meta.get("repos") if isinstance(meta, dict) else None
    if isinstance(repos, dict) and repos:
        names = sorted(repos, key=lambda name: not repos[name].get("primary"))
        return [{"name": name, "root": os.path.abspath(primary_work if index == 0 else (repos[name].get("worktree") or "")),
                 "branch": repos[name].get("branch", ""), "base": repos[name].get("base", ""), "primary": index == 0,
                 "repo_path": repos[name].get("path", "")} for index, name in enumerate(names)]
    meta = meta if isinstance(meta, dict) else {}
    return [{"name": os.path.basename(os.path.abspath(primary_work)), "root": os.path.abspath(primary_work), "branch": meta.get("branch", ""),
             "base": meta.get("base", ""), "primary": True, "repo_path": ""}]


# ---------------------------------------------------------------- migrations per repository
def migration_ids(paths):
    """{(folder, id)} of the migration source files among repo-relative `paths`."""
    found = set()
    for path in paths:
        parts = path.replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[-2] == MIGRATIONS_FOLDER:
            match = MIGRATION_FILE.match(parts[-1])
            if match:
                found.add(("/".join(parts[:-2]), match.group(1)))
    return found


def base_reference(root, base):
    """The ref the loop branch started from: `origin/<base>` when it exists, else the local `<base>`; '' when neither exists."""
    for candidate in ("origin/" + base, base):
        if base and git_ok(root, "rev-parse", "--verify", "--quiet", candidate + "^{commit}"):
            return candidate
    return ""


def migrations_added(root, base, branch):
    """(project folder, last base migration or "0", last loop migration) of the repo's migrations the loop branch added, or None.
    None = nothing added. Raises RuntimeError when git cannot tell."""
    reference = base_reference(root, base)
    if not reference or not branch:
        raise RuntimeError("base branch %r or loop branch %r not found" % (base, branch))
    fork = git_lines(root, "merge-base", reference, branch)
    fork_point = fork[0] if fork else reference
    changed = git_lines(root, "diff", "--name-only", "--diff-filter=A", fork_point, branch)
    if changed is None:
        raise RuntimeError("git diff failed")
    added = migration_ids(changed)
    if not added:
        return None
    at_base = git_lines(root, "ls-tree", "-r", "--name-only", fork_point) or []
    known = migration_ids(at_base)
    folder, last = sorted(added, key=lambda item: item[1])[-1]
    previous = sorted((identifier for place, identifier in known if place == folder))
    return folder, (previous[-1] if previous else "0"), last


def migration_script(root, folder, first, last, runner=None):
    """SQL text of `dotnet ef migrations script --idempotent <first> <last>` run in the project folder; RuntimeError on failure."""
    runner = runner or run_process
    project_dir = os.path.join(root, *folder.split("/")) if folder else root
    code, out, err = runner(["dotnet", "ef", "migrations", "script", "--idempotent", first, last], project_dir)
    if code != 0 or not str(out).strip():
        raise RuntimeError("dotnet ef migrations script failed: %s" % (describe_failure(err or str(out)) or "no output"))
    return strip_build_progress(out)


BUILD_PROGRESS_LINES = ("Build started...", "Build succeeded.")


def strip_build_progress(out):
    """`dotnet ef` prints its build progress on stdout before the SQL: those lines are dropped so the file stays runnable SQL."""
    lines = str(out).splitlines()
    while lines and (not lines[0].strip() or lines[0].strip() in BUILD_PROGRESS_LINES):
        lines.pop(0)
    return "\n".join(lines).strip("\n")


def describe_failure(text):
    """The useful part of a failed `dotnet ef` output: the exception line when there is one (the build prints NuGet warnings first),
    else the last 300 characters."""
    lines = [line.strip() for line in str(text).splitlines() if line.strip()]
    for line in lines:
        if "Exception:" in line:
            return line[:300]
    return " ".join(lines)[-300:]


# ---------------------------------------------------------------- manual scripts
def feature_order(roadmap_root):
    return list(loop_state.parse_roadmap(read_text(os.path.join(roadmap_root, ".specs", "project", "ROADMAP.md")))[0])


def manual_scripts(*spec_roots, roadmap_root=""):
    """[(feature, file name, absolute path)] of `.specs/features/*/sql/*.sql` found in the first root that has any, in ROADMAP feature
    order (features not in the ROADMAP follow alphabetically), files sorted by name."""
    order = feature_order(roadmap_root or (spec_roots[0] if spec_roots else ""))
    for root in spec_roots:
        if not root:
            continue
        found = {}
        for path in glob.glob(os.path.join(root, ".specs", *SQL_FOLDER_PARTS, "*.sql")):
            found.setdefault(os.path.basename(os.path.dirname(os.path.dirname(path))), []).append(path)
        if found:
            folder_order = [loop_state.feature_folder_name(slug) for slug in order]  # the folder is the slug with ':' as '-'
            ranked = sorted(found, key=lambda slug: (folder_order.index(slug) if slug in folder_order else len(folder_order), slug))
            return [(slug, os.path.basename(path), path) for slug in ranked for path in sorted(found[slug])]
    return []


# ---------------------------------------------------------------- the file
def safe_name(text):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "").strip()).strip("-") or "project"


def output_path(project_name, output_dir="", today=None):
    if not output_dir:
        import app_paths
        output_dir = app_paths.default_output_dir()
    return os.path.join(output_dir, "%s-prod-database-%s.sql" % (safe_name(project_name), (today or datetime.date.today()).isoformat()))


def generate(project_name, entries, roadmap_root, output_dir="", runner=None, today=None, roles=None, extra_spec_roots=()):
    """Builds the production script. `entries` = [{name, root (worktree on the loop branch), branch, base, repo_path?}].
    Returns {"path": str ('' when nothing to script or not written), "repos": [names with migrations], "sql_files": [...], "warnings": [...]}.
    Never raises."""
    result = {"path": "", "repos": [], "sql_files": [], "warnings": []}
    try:
        roles = roles if roles is not None else {}
        sections = []
        for entry in entries:
            name = entry.get("name", "")
            if role_of(roles, name, entry.get("repo_path", "")) != "edit":
                continue
            try:
                found = migrations_added(entry["root"], entry.get("base", ""), entry.get("branch", ""))
                if not found:
                    continue
                folder, first, last = found
                body = migration_script(entry["root"], folder, first, last, runner)
                sections.append("-- ---- %s: migrations %s -> %s (project folder: %s) ----\n%s\n" % (name, first, last, folder or ".", body))
                result["repos"].append(name)
            except (RuntimeError, OSError, KeyError) as error:
                message = "%s: production script of the migrations could not be generated (%s)" % (name, str(error)[:300])
                result["warnings"].append(message)
                sections.append("-- WARNING: %s\n" % message.replace("\n", " "))
        scripts = manual_scripts(roadmap_root, *extra_spec_roots, roadmap_root=roadmap_root)
        for slug, filename, path in scripts:
            text = read_text(path).strip("\n")
            if text:
                sections.append("-- ---- manual script: %s/%s ----\n%s\n" % (slug, filename, text))
                result["sql_files"].append("%s/%s" % (slug, filename))
        if not result["repos"] and not result["sql_files"]:
            return result  # nothing to script (a warning-only run writes no file)
        path = output_path(project_name, output_dir, today)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        stamp = (today or datetime.date.today()).isoformat()
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(HEADER % (project_name, stamp) + "\n" + "\n".join(sections))
        result["path"] = path
    except Exception as error:  # a delivery is never blocked by this
        result["warnings"].append("production database script failed: %s" % str(error)[:300])
    return result


def generate_for_meta(primary_work, meta, output_dir="", runner=None, today=None, project_name=""):
    """Delivery entry point: entries and roles from the primary worktree's meta.json (see `entries_from_meta`)."""
    entries = entries_from_meta(primary_work, meta)
    primary = next((entry for entry in entries if entry["primary"]), entries[0])
    roles = read_roles(primary.get("repo_path"), primary_work)
    name = project_name or re.sub(r"-worktree-loop.*$", "", os.path.basename(os.path.abspath(primary.get("repo_path") or primary_work)))
    extra = (primary.get("repo_path"),) if primary.get("repo_path") else ()
    return generate(name, entries, primary_work, output_dir, runner, today, roles, extra)
