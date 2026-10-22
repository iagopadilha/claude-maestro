#!/usr/bin/env python3
"""Close loop: finish a loop safely. Runs OUTSIDE `claude -p` (the app calls it on the user's click), never pushes anything.

Usage:
  python close_loop.py <primary-repo> --check                       read only: can each repo close, and why not
  python close_loop.py <primary-repo> [--confirm-compare-link] [--repos a,b] [--output-dir DIR] [--force-remove]
Prints one JSON object (shapes: design.md, "close-loop/check" and "close-loop").

`can_close` per repo: the loop is not running, the worktree exists, the push of `loop/*` is confirmed (delivery.json `push`
starts with `done` AND the remote branch holds the worktree HEAD), a PR exists (`PR created` / `PR already existed`), the tree
is clean outside `.specs/` and no copied file changed (worktree and source still match the hash recorded in meta.json).
A compare link without PR is allowed only with the explicit `confirm_compare_link`; nothing else is ever lifted.

`close` order, fixed: (a) verified backup of each worktree, (b) the DURABLE `.specs/` files go back to the primary repo
(ROADMAP with statuses, FLOWS, STATE, features/*/spec|design|tasks, phases/, loop/VALIDATE.md, the summary; never loop/internal,
never loop/json), (c) `last-run.json`, (d) `git worktree remove` WITHOUT --force (only `--force-remove` forces it; non-primary first, the primary, which holds
`.specs/`, last and only if (b) and (c) worked). Local branches and commits stay. One repo failing never stops the others.
"""
import argparse
import datetime
import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402  (the app's output folder: summaries and backups never go to the repo)
import loop_state  # noqa: E402
import project_repos  # noqa: E402
import prod_database_script  # noqa: E402  (the .sql the user runs on PRODUCTION before deploying)

IS_WIN = os.name == "nt"
LOOP_BRANCH_PREFIX = "loop/"
LOOP_WORKTREE_FOLDER_MARK = "-worktree-loop"
FOLDERS_OUTSIDE_BACKUP = ("node_modules", "bin", "obj", ".git")  # same as LoopWorktreeService
PR_CONFIRMED = ("PR created", "PR already existed")
DURABLE_PROJECT_FILES = ("project/ROADMAP.md", "project/FLOWS.md", "project/STATE.md", "project/loop/VALIDATE.md")
DURABLE_FEATURE_FILES = ("spec.md", "design.md", "tasks.md")
BACKUP_STAMP_FORMAT = "%Y-%m-%d-%H%M"
RUN_TOTAL_KEYS = ("cost_usd", "max_cost_usd", "turns")  # recorded in last-run.json (COST-05)
SCRIPT_TIMEOUT_SECONDS = 180
REMOTE_TIMEOUT_SECONDS = 60
REASON_TEXT = {
    "loop_running": "The loop is still running. Stop it or wait for it to finish.",
    "already_closed": "Already closed: an earlier close removed this worktree (see last-run.json). Nothing left to do.",
    "worktree_missing": "The worktree folder does not exist (already removed, or never created).",
    "push_unconfirmed": "The loop branch is not confirmed on the remote (push not done, failed, or the remote does not hold the last commit). Deliver first.",
    "pr_missing": "No PR was created for this repo. Deliver again, or confirm to close with only the compare link.",
    "pr_compare_link": "Only a compare link exists; no PR was created. Confirm to close anyway.",
    "tree_dirty": "The worktree has uncommitted changes; it was kept.",
    "copied_changed": "A copied configuration file changed (in the worktree or in the original folder); the worktree was kept.",
}


# ---------------------------------------------------------------- small helpers
def run(args, cwd=None, timeout=120):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git(cwd, *args, timeout=120):
    return run(["git", "-C", cwd, *args], timeout=timeout)


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)


def timestamp():
    return datetime.datetime.now().isoformat(timespec="seconds")


def is_same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def pid_alive(pid):
    if pid <= 0:
        return False
    if IS_WIN:
        _, out, _ = run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"])
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def loop_is_running(primary_work):
    try:
        with open(loop_state.loop_path(primary_work, "LOOP.lock"), encoding="utf-8") as handle:
            return pid_alive(int(handle.read().strip() or 0))
    except (OSError, ValueError):
        return False


def file_hash(path):
    """`sha256:<hex>` of a file (the format meta.json records), None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for block in iter(lambda: handle.read(1 << 20), b""):
                digest.update(block)
        return "sha256:" + digest.hexdigest()
    except OSError:
        return None


def reason(code, text=""):
    return {"code": code, "text": text or REASON_TEXT.get(code, code)}


# ---------------------------------------------------------------- what the project looks like
def project_entries(primary_repo):
    """[{name, repo_path, primary, worktree, branch, base, copied: {file: hash}, copy: [file]}], primary first.

    The loop worktrees come from the primary's meta.json `repos` (what the loop really created); a single-repo project (no
    `repos` key) is one entry built from the top-level fields, so it closes exactly like a multi-repo one with one repo."""
    primary_repo = os.path.abspath(primary_repo)
    repos = project_repos.load_repos(primary_repo)
    primary = project_repos.primary_of(repos)
    meta = load_json(loop_state.loop_path(primary.loop_worktree_path(), "meta.json"))
    recorded_repos = meta.get("repos") if isinstance(meta.get("repos"), dict) else {}
    last_run_repos = load_json(last_run_path(primary_repo)).get("repos") or {}
    if not meta:  # the primary worktree is already gone: the last close left the record of every repo
        recorded_repos = last_run_repos
    entries = []
    for repo in repos:
        if not repo.primary and not (recorded_repos.get(repo.name) or last_run_repos.get(repo.name) or os.path.isdir(repo.loop_worktree_path())):
            continue  # a `reference` repo, or an edit repo no feature changed, never got a worktree: nothing to close, merge or remove
        if recorded_repos:
            recorded = recorded_repos.get(repo.name) or {}
        else:
            recorded = {"worktree": meta.get("worktree"), "branch": meta.get("branch"), "base": meta.get("base")} if repo.primary else {}
        copied = recorded.get("copied") if isinstance(recorded.get("copied"), dict) else {}
        entries.append({"name": repo.name, "repo_path": repo.path, "primary": repo.primary,
                        "worktree": os.path.abspath(recorded.get("worktree") or repo.loop_worktree_path()),
                        "branch": recorded.get("branch") or "", "base": recorded.get("base") or "",
                        "copied": copied, "copy": list(repo.copy),
                        "closed_before": bool((last_run_repos.get(repo.name) or {}).get("worktree_removed"))})
    return entries, meta


def remote_holds_head(worktree, branch):
    """True when `origin` has `branch` at the same commit as the worktree HEAD (asked to the remote, not to a local ref)."""
    code, head, _ = git(worktree, "rev-parse", "HEAD")
    if code != 0 or not head:
        return False
    code, out, _ = git(worktree, "ls-remote", "--heads", "origin", "refs/heads/" + branch, timeout=REMOTE_TIMEOUT_SECONDS)
    return code == 0 and out.split()[:1] == [head]


def tree_is_dirty(worktree):
    """Uncommitted changes outside `.specs/` (the loop's own state is expected to be there)."""
    code, out, _ = git(worktree, "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).specs")
    return code != 0 or bool(out.strip())


def copied_files_changed(entry):
    """Names of the copied files whose worktree copy or whose source no longer matches the hash recorded at copy time."""
    changed = []
    for name, recorded in sorted(entry["copied"].items()):
        if file_hash(os.path.join(entry["worktree"], name)) != recorded or file_hash(os.path.join(entry["repo_path"], name)) != recorded:
            changed.append(name)
    return changed


def check_repo(entry, running):
    """The `repos[]` item of the check payload (design.md Data Models)."""
    delivery = load_json(loop_state.loop_path(entry["worktree"], "delivery.json"))
    branch = git(entry["worktree"], "branch", "--show-current")[1] if os.path.isdir(entry["worktree"]) else ""
    branch = branch or entry["branch"]
    item = {"name": entry["name"], "can_close": False, "reasons": [], "needs_confirm": False, "branch": branch or None,
            "base": delivery.get("base") or entry["base"] or None, "pr": delivery.get("pr"), "pr_status": delivery.get("pr_status") or "not created",
            "push": delivery.get("push") or "not done"}
    reasons = item["reasons"]
    if running:
        reasons.append(reason("loop_running"))
    if not os.path.isdir(entry["worktree"]):
        reasons.append(reason("already_closed") if entry.get("closed_before") else reason("worktree_missing"))
        return item
    if not branch.startswith(LOOP_BRANCH_PREFIX):
        reasons.append(reason("push_unconfirmed", "The worktree is not on a loop/* branch (%s); it is never closed from here." % (branch or "detached HEAD")))
    elif not (item["push"].startswith("done") and remote_holds_head(entry["worktree"], branch)):
        reasons.append(reason("push_unconfirmed"))
    if item["pr_status"] not in PR_CONFIRMED:
        compare_only = str(item["pr_status"]).startswith("compare link") and bool(item["pr"])
        reasons.append(reason("pr_missing", REASON_TEXT["pr_compare_link"] if compare_only else ""))
    if tree_is_dirty(entry["worktree"]):
        reasons.append(reason("tree_dirty"))
    changed = copied_files_changed(entry)
    if changed:
        reasons.append(reason("copied_changed", "%s (%s)" % (REASON_TEXT["copied_changed"], ", ".join(changed))))
    item["needs_confirm"] = bool(reasons) and all(r["code"] == "pr_missing" for r in reasons) and str(item["pr_status"]).startswith("compare link") and bool(item["pr"])
    item["can_close"] = not reasons
    return item


def can_close(primary_repo):
    """Read only. {"ok", "can_close", "loop_finished", "repos": [...]}; top-level can_close is true only when every repo is."""
    entries, meta = project_entries(primary_repo)
    running = loop_is_running(next(entry["worktree"] for entry in entries if entry["primary"]))
    repos = [check_repo(entry, running) for entry in entries]
    return {"ok": True, "can_close": all(item["can_close"] for item in repos), "loop_finished": bool(meta.get("finished_at")) and not running, "repos": repos}


# ---------------------------------------------------------------- backup (same rules as LoopWorktreeService.back_up_worktree)
def count_backup_files(folder):
    total = 0
    for _, subfolders, files in os.walk(folder):
        subfolders[:] = [name for name in subfolders if name not in FOLDERS_OUTSIDE_BACKUP]
        total += len(files)
    return total


def back_up_worktree(worktree, output_dir):
    """<output_dir>/loop-<name>-<stamp>/ with the summary (loop_summary.py) and a copy of `.specs/`, checked by file count.

    Returns (folder, error): error is None when the backup is verified; nothing is deleted by this function either way."""
    name = re.sub(r"%s.*$" % re.escape(LOOP_WORKTREE_FOLDER_MARK), "", os.path.basename(worktree)) or "project"
    base = os.path.join(os.path.abspath(output_dir), "loop-%s-%s" % (name, datetime.datetime.now().strftime(BACKUP_STAMP_FORMAT)))
    folder, suffix = base, 1
    try:
        while os.path.exists(folder):
            suffix += 1
            folder = "%s-%d" % (base, suffix)
        os.makedirs(folder)
        code, out, err = run([sys.executable, os.path.join(HERE, "loop_summary.py"), worktree, "--out", folder], timeout=SCRIPT_TIMEOUT_SECONDS)
        if code != 0:
            return folder, "backup failed: loop_summary.py did not finish cleanly (%s)" % ((err or out)[-200:] or "no output")
        if not any(file.lower().endswith(".md") for file in os.listdir(folder)):
            return folder, "backup failed: the summary was not generated in %s" % folder
        source = os.path.join(worktree, ".specs")
        if os.path.isdir(source):
            destination = os.path.join(folder, "specs")
            shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*FOLDERS_OUTSIDE_BACKUP))
            source_files, destination_files = count_backup_files(source), count_backup_files(destination)
            if source_files != destination_files:
                return folder, "backup failed: the .specs copy does not match (%d x %d files)" % (source_files, destination_files)
        return folder, None
    except (OSError, shutil.Error) as failure:
        return folder, "backup failed: %s" % str(failure)[:200]


# ---------------------------------------------------------------- durable .specs return
def durable_files(primary_work):
    """Paths (relative to `.specs/`, forward slashes) of the plan that must outlive the worktree. Never loop/internal or loop/json."""
    specs = os.path.join(primary_work, ".specs")
    found = [path for path in DURABLE_PROJECT_FILES if os.path.isfile(os.path.join(specs, *path.split("/")))]
    for name in DURABLE_FEATURE_FILES:
        found += sorted(os.path.relpath(path, specs).replace(os.sep, "/") for path in glob.glob(os.path.join(specs, "features", "*", name)))
    for folder, _, files in os.walk(os.path.join(specs, "project", "phases")):
        found += sorted(os.path.relpath(os.path.join(folder, name), specs).replace(os.sep, "/") for name in files)
    return found


def copy_durable_files(primary_work, primary_repo, backup_folder):
    """Copies the durable plan files, plus the summary the backup produced, into the primary repo's `.specs/`. Returns the relative list."""
    copied = []
    for relative in durable_files(primary_work):
        source = os.path.join(primary_work, ".specs", *relative.split("/"))
        target = os.path.join(primary_repo, ".specs", *relative.split("/"))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        copied.append(relative)
    for name in sorted(os.listdir(backup_folder)) if backup_folder else []:
        if name.startswith("loop-summary") and name.endswith(".md"):
            relative = "project/loop/" + name
            target = os.path.join(primary_repo, ".specs", "project", "loop", name)
            os.makedirs(os.path.dirname(target), exist_ok=True)
            shutil.copy2(os.path.join(backup_folder, name), target)
            copied.append(relative)
    return copied


# ---------------------------------------------------------------- last-run.json
def last_run_path(primary_repo):
    return loop_state.loop_path(primary_repo, "last-run.json")


DEFAULT_MAX_COST_USD = 50.0  # run_loop.py --max-cost default


def command_max_cost(primary_work):
    """`--max-cost` of the saved loop command (LOOP-CMD.json argv, `--max-cost N` or `--max-cost=N`); None when absent or unreadable."""
    argv = load_json(loop_state.loop_path(primary_work, "LOOP-CMD.json")).get("argv")
    for index, item in enumerate(argv if isinstance(argv, list) else []):
        name, _, joined = str(item).partition("=")
        if name == "--max-cost":
            raw = joined or (str(argv[index + 1]) if index + 1 < len(argv) else "")
            try:
                return float(raw)
            except ValueError:
                return None
    return None


def effective_max_cost(meta, primary_work):
    """The cap in force: `meta.max_cost_usd` (raised in the panel) when above 0, else the loop command's --max-cost, else 50."""
    try:
        raised = float((meta or {}).get("max_cost_usd") or 0)
    except (TypeError, ValueError):
        raised = 0
    if raised > 0:
        return raised
    from_command = command_max_cost(primary_work) if primary_work else None
    return from_command if from_command is not None else DEFAULT_MAX_COST_USD


def run_totals(meta, prior, primary_work=""):
    """cost_usd, max_cost_usd (the EFFECTIVE cap) and turns of the run: from the primary worktree's meta.json, else what the last record kept."""
    totals = {}
    for key in RUN_TOTAL_KEYS:
        value = meta.get(key) if meta else None
        totals[key] = value if value is not None else prior.get(key)
    if meta:
        totals["max_cost_usd"] = effective_max_cost(meta, primary_work)
    return totals


def write_last_run(primary_repo, entries, checks, removed, durable, meta=None, primary_work="", finished=False):
    """`<primary repo>/.specs/project/loop/json/last-run.json` (design.md). `removed` = names whose worktree is gone (or about to be).
    `meta` = the primary worktree's meta.json: the record also keeps the run's `cost_usd`, `max_cost_usd` and `turns`.
    `finished` (wrap-up Finish) marks the run and every repo entry `"finished": true`: the next start does NOT re-attach to that branch."""
    prior = load_json(last_run_path(primary_repo))
    repos = {}
    for entry in entries:
        check = checks[entry["name"]]
        if not os.path.isdir(entry["worktree"]) and entry["name"] in (prior.get("repos") or {}):  # closed before: keep what was recorded then
            repos[entry["name"]] = dict(prior["repos"][entry["name"]], worktree_removed=True)
            continue
        branch = check["branch"] or entry["branch"]
        head = git(entry["repo_path"], "rev-parse", "--short=10", branch)[1] if branch else ""
        repos[entry["name"]] = {"path": entry["repo_path"], "branch": branch, "base": entry["base"] or check["base"], "head": head,
                                "pr": check["pr"], "worktree": entry["worktree"], "worktree_removed": entry["name"] in removed or not os.path.isdir(entry["worktree"]),
                                "copied": sorted(entry["copied"]) or list(entry["copy"])}
    if finished:
        for item in repos.values():
            item["finished"] = True
    record = {"closed_at": timestamp(), "multi": len(entries) > 1, "repos": repos, "durable_files": durable or prior.get("durable_files") or []}
    if finished:
        record["finished"] = True
    save_json(last_run_path(primary_repo), dict(record, **run_totals(meta, prior, primary_work)))


# ---------------------------------------------------------------- production database script
def write_prod_database_script(primary_repo, primary_work, entries, checks, output_dir):
    """Production database script of the loop (see prod_database_script.py), made BEFORE any worktree is removed (it needs them).
    Never raises and never blocks a close: a failure is a warning. Returns {"path", "repos", "sql_files", "warnings"}."""
    try:
        script_entries = [{"name": entry["name"], "root": entry["worktree"], "branch": checks[entry["name"]]["branch"] or entry["branch"],
                           "base": entry["base"] or checks[entry["name"]]["base"], "repo_path": entry["repo_path"]}
                          for entry in entries if os.path.isdir(entry["worktree"])]
        roles = prod_database_script.read_roles(primary_repo)
        name = os.path.basename(os.path.abspath(primary_repo))
        return prod_database_script.generate(name, script_entries, primary_work, output_dir, roles=roles, extra_spec_roots=(primary_repo,))
    except Exception as error:
        return {"path": "", "repos": [], "sql_files": [], "warnings": ["production database script failed: %s" % str(error)[:300]]}


# ---------------------------------------------------------------- close
ENGINE_SPECS_FOLDER = os.path.join(".specs", "project", "loop", "json")  # the only place the engine writes into a secondary repo's worktree
# The loop's run state in the PRIMARY worktree (VALIDATE.md, GRAPH.md, DEPENDENCIES.md, NOTICE.txt, internal/ snapshots, turns, guard.log,
# json/): written by the engine and the orchestrator, saved by the verified backup (VALIDATE.md and the summary also go home). Untracked
# content there is never the user's work.
ENGINE_RUN_STATE_FOLDER = os.path.join(".specs", "project", "loop")
REASON_LIMIT = 200


def cut_reason(text, limit=REASON_LIMIT):
    """`text` cut at a word boundary with an ellipsis (never mid-sentence); unchanged when it fits."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-") or text[:limit]
    return cut + "..."


# Git-ignored local files the engine itself writes into the worktree (test_env_fallback.ensure_env_test completes `.env.test` on every start):
# engine-owned, so they never block the removal. The backup taken before the removal keeps them.
ENGINE_GENERATED_IGNORED = (".specs/project/.env.test",)


def is_ignored_copy_of_repo_file(worktree, repo, relative):
    """True when `relative` is git-ignored in the worktree AND either the engine writes it (ENGINE_GENERATED_IGNORED) or the same path in
    the original repo holds identical bytes (a local file copied in). Removing it loses nothing: the repo keeps its copy, the backup has it."""
    if git(worktree, "check-ignore", "-q", "--", relative)[0] != 0:
        return False
    if relative.replace(os.sep, "/") in ENGINE_GENERATED_IGNORED:  # rewritten by the engine on every start; the verified backup keeps it
        return True
    if not repo:
        return False
    try:
        with open(os.path.join(worktree, relative), "rb") as mine, open(os.path.join(repo, relative), "rb") as theirs:
            return mine.read() == theirs.read()
    except OSError:
        return False


def foreign_content_in_specs(worktree, saved_plan=(), repo=None):
    """Relative path of the first thing in the worktree that is NOT engine-owned `.specs/` state, or None when the only untracked
    content is `.specs/` holding nothing but `*.json` under `.specs/project/loop/json/` (delivery.json, meta.json style state).
    `saved_plan` = paths (relative to the worktree root, forward slashes) of durable plan files already copied back to the repository and
    verified identical there: they are saved work, so modified or untracked they do not count as foreign (a repo that TRACKS `.specs/project`)."""
    saved_plan = {path.replace("\\", "/") for path in saved_plan}
    code, out, err = git(worktree, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return "(git status failed: %s)" % cut_reason(err or out or "no detail", 80)
    for line in out.splitlines():
        found = re.match(r"^\s*(\S{1,2})\s+(.*)$", line)  # `git()` strips the output, so the first line may have lost the leading space of its status
        if not found:
            continue
        status, path = found.group(1), found.group(2).strip().strip('"')
        if path.replace("\\", "/") in saved_plan:
            continue
        if status != "??" or not path.replace("\\", "/").startswith(".specs/"):
            return path
    # Tracked files with no local change (a committed `.specs/features/<slug>/contract.md`) are content of the branch, not foreign: git removes
    # them with the worktree. Modified ones were already returned above (status), so every tracked path left here is clean.
    tracked = {path.replace("\\", "/") for path in git(worktree, "ls-files", "--", ".specs")[1].splitlines() if path.strip()}
    specs = os.path.join(worktree, ".specs")
    for folder, _, files in os.walk(specs):
        for name in files:
            full = os.path.join(folder, name)
            relative = os.path.relpath(full, worktree)
            if relative.replace(os.sep, "/") in saved_plan or relative.replace(os.sep, "/") in tracked:
                continue
            if is_ignored_copy_of_repo_file(worktree, repo, relative):
                continue
            if os.path.normcase(relative).startswith(os.path.normcase(ENGINE_RUN_STATE_FOLDER) + os.sep):
                continue
            if not (os.path.normcase(os.path.dirname(relative)).startswith(os.path.normcase(ENGINE_SPECS_FOLDER)) and name.lower().endswith(".json")):
                return relative.replace(os.sep, "/")
    return None


def clear_engine_specs(worktree, saved_plan=(), repo=None):
    """Called only AFTER the backup was verified (and, for `saved_plan`, after the plan was copied back and checked). When the content of
    the worktree is exclusively engine-owned `.specs/` state plus already saved plan files, deletes that state so the normal
    `git worktree remove` can run, WITHOUT --force. Returns the blocking path (nothing deleted) or None.
    Where `.specs/` has tracked files (a repo that commits `.specs/project`), the tracked files are restored to HEAD (the edited copies are
    saved in the repository and in the backup) and the rest is cleaned with `git clean`; the folder is not deleted, or git would see deletions."""
    specs = os.path.join(worktree, ".specs")
    if not os.path.isdir(specs):
        return None
    code, out, _ = git(worktree, "status", "--porcelain", "--untracked-files=all", "--", ".specs")
    if code == 0 and not out.strip():  # gitignored (or tracked and clean): git removes it by itself
        return None
    blocker = foreign_content_in_specs(worktree, saved_plan, repo)
    if blocker:
        return blocker
    if git(worktree, "ls-files", "--", ".specs")[1].strip():
        for command in (("checkout", "--", ".specs"), ("clean", "-fdxq", "--", ".specs")):
            code, out, failure = git(worktree, *command)
            if code != 0:
                return "%s (%s)" % (specs, cut_reason(failure or out or "no detail", 80))
        return None
    try:
        shutil.rmtree(specs)
    except OSError as failure:
        return "%s (%s)" % (specs, cut_reason(failure, 80))
    return None


def remove_worktree(entry, force, saved_plan=()):
    """`git worktree remove` from the user's repo, no --force unless asked. Returns an error text, or None when the folder is gone."""
    if is_same_path(entry["worktree"], entry["repo_path"]):  # belt and braces: never the user's own folder
        return "refused: the worktree is the original repo folder"
    if not force:
        blocker = clear_engine_specs(entry["worktree"], saved_plan, entry["repo_path"])
        if blocker:
            return "the worktree was kept: %s holds content that is not engine-owned .specs/ state, so it was not deleted (backup done; remove it by hand or use force)" % blocker
    code, out, err = git(entry["repo_path"], "worktree", "remove", *(["--force"] if force else []), entry["worktree"])
    git(entry["repo_path"], "worktree", "prune")
    if os.path.isdir(entry["worktree"]):
        return "git worktree remove failed for %s, the folder is still there: %s" % (entry["worktree"], cut_reason(err or out or "no detail"))
    return None


def verified_plan(worktree, primary_repo, durable):
    """Worktree-relative paths of the durable plan files that were copied back AND still match the copy in the primary repo (hash), so a
    modified tracked plan file in the worktree is known to be saved. Anything that differs is left out and keeps blocking the removal."""
    verified = []
    for relative in durable:
        source = os.path.join(worktree, ".specs", *relative.split("/"))
        target = os.path.join(primary_repo, ".specs", *relative.split("/"))
        if os.path.isfile(source) and file_hash(source) is not None and file_hash(source) == file_hash(target):
            verified.append(".specs/" + relative)
    return verified


def only_pr_missing(check):
    return bool(check["reasons"]) and all(item["code"] == "pr_missing" for item in check["reasons"])


def is_forceable(entry, check, force_remove):
    """`force_remove` lifts every check reason of a repo whose worktree exists, EXCEPT a worktree outside `loop/*` (never closed from here).
    The backup, the plan going home and last-run.json still run first: forcing only changes what may be thrown away at the end."""
    return bool(force_remove) and os.path.isdir(entry["worktree"]) and str(check["branch"] or "").startswith(LOOP_BRANCH_PREFIX)


def close(primary_repo, confirm_compare_link=False, only=None, output_dir="", force_remove=False, allow_missing_pr=False, finished=False):
    """Closes the repos that may close; the others are kept with their reasons. Per-repo outcome in the body, never an exception.

    `only` = repo names to close (None = all). `confirm_compare_link` lifts ONLY `pr_missing` of a repo that has a compare link.
    `allow_missing_pr` lifts ONLY `pr_missing` (a confirmed push with no PR and no compare link, e.g. a non-GitHub remote): the push check, the
    clean tree and the copied files still apply and nothing is forced. `force_remove` closes the selected repos even when the check says no (uncommitted files, unpushed commits, push not confirmed): the
    verified backup is still made BEFORE anything is removed, a failed backup keeps the worktree, and the worktree is removed with
    `git worktree remove --force`. The local branch and its commits stay. `finished` (wrap-up Finish only) records the run as finished in
    last-run.json, so the next start begins on a new loop branch instead of re-attaching to this one."""
    primary_repo = os.path.abspath(primary_repo)
    entries, primary_meta = project_entries(primary_repo)
    primary_entry = next(entry for entry in entries if entry["primary"])
    if loop_is_running(primary_entry["worktree"]):
        return {"ok": False, "code": "loop_running", "error": REASON_TEXT["loop_running"]}
    output_dir = output_dir or app_paths.default_output_dir()
    checks = {entry["name"]: check_repo(entry, False) for entry in entries}
    outcome = {entry["name"]: {"status": "kept", "removed_worktree": False, "branch_kept": checks[entry["name"]]["branch"], "reasons": []} for entry in entries}
    closing = []
    for entry in entries:
        check, result = checks[entry["name"]], outcome[entry["name"]]
        if only is not None and entry["name"] not in only:
            result["reasons"] = check["reasons"]
        elif check["can_close"] or (check["needs_confirm"] and confirm_compare_link) or (allow_missing_pr and only_pr_missing(check)) or is_forceable(entry, check, force_remove):
            closing.append(entry)
            result["forced"] = bool(force_remove) and not check["can_close"] and not (check["needs_confirm"] and confirm_compare_link) and not (allow_missing_pr and only_pr_missing(check))
        else:
            result["reasons"] = check["reasons"]
    backups = {}
    for entry in list(closing):  # (a) verified backup; a repo whose backup fails is not touched
        folder, error = back_up_worktree(entry["worktree"], output_dir)
        backups[entry["name"]] = folder
        outcome[entry["name"]]["backup"] = folder
        if error:
            closing.remove(entry)
            outcome[entry["name"]].update(status="failed", reasons=[reason("error", error + " (nothing was deleted)")])
    durable, last_run, primary_safe = [], None, False
    if closing:
        try:  # (b) the plan goes home and (c) the run is recorded BEFORE any worktree is removed
            if os.path.isdir(primary_entry["worktree"]):
                durable = copy_durable_files(primary_entry["worktree"], primary_repo, backups.get(primary_entry["name"], ""))
            write_last_run(primary_repo, entries, checks, {entry["name"] for entry in closing}, durable, primary_meta, primary_entry["worktree"], finished)
            last_run, primary_safe = last_run_path(primary_repo), True
        except (OSError, shutil.Error) as failure:
            if primary_entry in closing:
                closing.remove(primary_entry)
                outcome[primary_entry["name"]].update(status="failed", reasons=[reason("error", "the durable .specs/ files could not be copied back (%s); the primary worktree was kept" % str(failure)[:160])])
    prod_database = write_prod_database_script(primary_repo, primary_entry["worktree"], entries, checks, output_dir) if closing else None
    for entry in sorted(closing, key=lambda item: item["primary"]):  # (d) the primary, which holds `.specs/`, goes last
        result = outcome[entry["name"]]
        error = remove_worktree(entry, force_remove, verified_plan(primary_entry["worktree"], primary_repo, durable) if entry["primary"] else ())
        if error:
            result.update(status="failed", reasons=[reason("error", error)])
        else:
            result.update(status="closed", removed_worktree=True, reasons=[])
    removed = {name for name, result in outcome.items() if result["removed_worktree"]}
    if primary_safe and removed != {entry["name"] for entry in closing}:
        write_last_run(primary_repo, entries, checks, removed, durable, primary_meta, primary_entry["worktree"], finished)  # a removal failed: the record must say so
    closed = [name for name in outcome if outcome[name]["status"] == "closed"]
    response = {"ok": True, "closed": closed, "kept": [name for name in outcome if outcome[name]["status"] != "closed"],
                "backup": backups.get(primary_entry["name"]) or next(iter(backups.values()), None), "durable_copied": durable, "last_run": last_run, "repos": outcome}
    if prod_database and (prod_database["path"] or prod_database["warnings"]):
        response["prod_database"] = prod_database
        response["warnings"] = list(prod_database["warnings"])
    return response


def main():
    parser = argparse.ArgumentParser(description="Close loop: can-close check, verified backup, durable .specs/ back, last-run.json, worktree removal")
    parser.add_argument("primary_repo")
    parser.add_argument("--check", action="store_true", help="read only: print the per-repo can-close check")
    parser.add_argument("--close", action="store_true", help="close (the default; accepted so the app can say it explicitly)")
    parser.add_argument("--confirm-compare-link", action="store_true", help="close a repo that only has a compare link (no PR)")
    parser.add_argument("--repos", default="", help="comma-separated repo names (default: all)")
    parser.add_argument("--output-dir", "--out", dest="output_dir", default="", help="where the backup folders go (default: the app's output folder, ~/Downloads when unset)")
    parser.add_argument("--allow-missing-pr", action="store_true", help="close a repo whose push is confirmed but that has no PR and no compare link")
    parser.add_argument("--force-remove", action="store_true", help="close even when the check refuses (uncommitted, unpushed, push not confirmed): backup first, then git worktree remove --force")
    parser.add_argument("--finished", action="store_true", help="wrap-up Finish: mark last-run.json finished so the next start does not re-attach to this loop branch")
    opts = parser.parse_args()
    if opts.check:
        result = can_close(opts.primary_repo)
    else:
        only = [name.strip() for name in opts.repos.split(",") if name.strip()] or None
        result = close(opts.primary_repo, opts.confirm_compare_link, only, opts.output_dir, opts.force_remove, opts.allow_missing_pr, opts.finished)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
