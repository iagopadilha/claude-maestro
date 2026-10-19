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
never loop/json), (c) `last-run.json`, (d) `git worktree remove` WITHOUT --force (non-primary first, the primary, which holds
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
import loop_state  # noqa: E402
import project_repos  # noqa: E402

IS_WIN = os.name == "nt"
LOOP_BRANCH_PREFIX = "loop/"
LOOP_WORKTREE_FOLDER_MARK = "-worktree-loop"
FOLDERS_OUTSIDE_BACKUP = ("node_modules", "bin", "obj", ".git")  # same as LoopWorktreeService
PR_CONFIRMED = ("PR created", "PR already existed")
DURABLE_PROJECT_FILES = ("project/ROADMAP.md", "project/FLOWS.md", "project/STATE.md", "project/loop/VALIDATE.md")
DURABLE_FEATURE_FILES = ("spec.md", "design.md", "tasks.md")
BACKUP_STAMP_FORMAT = "%Y-%m-%d-%H%M"
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


def write_last_run(primary_repo, entries, checks, removed, durable):
    """`<primary repo>/.specs/project/loop/json/last-run.json` (design.md). `removed` = names whose worktree is gone (or about to be)."""
    prior = load_json(last_run_path(primary_repo))
    repos = {}
    for entry in entries:
        check = checks[entry["name"]]
        if not os.path.isdir(entry["worktree"]) and entry["name"] in (prior.get("repos") or {}):  # closed before: keep what was recorded then
            repos[entry["name"]] = dict(prior["repos"][entry["name"]], worktree_removed=True)
            continue
        branch = check["branch"] or entry["branch"]
        head = git(entry["repo_path"], "rev-parse", "--short=10", branch)[1] if branch else ""
        repos[entry["name"]] = {"path": entry["repo_path"], "branch": branch, "base": check["base"] or entry["base"], "head": head,
                                "pr": check["pr"], "worktree": entry["worktree"], "worktree_removed": entry["name"] in removed or not os.path.isdir(entry["worktree"]),
                                "copied": sorted(entry["copied"]) or list(entry["copy"])}
    save_json(last_run_path(primary_repo), {"closed_at": timestamp(), "multi": len(entries) > 1, "repos": repos,
                                          "durable_files": durable or prior.get("durable_files") or []})


# ---------------------------------------------------------------- close
ENGINE_SPECS_FOLDER = os.path.join(".specs", "project", "loop", "json")  # the only place the engine writes into a secondary repo's worktree
REASON_LIMIT = 200


def cut_reason(text, limit=REASON_LIMIT):
    """`text` cut at a word boundary with an ellipsis (never mid-sentence); unchanged when it fits."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;:.-") or text[:limit]
    return cut + "..."


def foreign_content_in_specs(worktree):
    """Relative path of the first thing in the worktree that is NOT engine-owned `.specs/` state, or None when the only untracked
    content is `.specs/` holding nothing but `*.json` under `.specs/project/loop/json/` (delivery.json, meta.json style state)."""
    code, out, err = git(worktree, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        return "(git status failed: %s)" % cut_reason(err or out or "no detail", 80)
    for line in out.splitlines():
        status, path = line[:2], line[3:].strip().strip('"')
        if status != "??" or not path.replace("\\", "/").startswith(".specs/"):
            return path
    specs = os.path.join(worktree, ".specs")
    for folder, _, files in os.walk(specs):
        for name in files:
            full = os.path.join(folder, name)
            relative = os.path.relpath(full, worktree)
            if not (os.path.normcase(os.path.dirname(relative)).startswith(os.path.normcase(ENGINE_SPECS_FOLDER)) and name.lower().endswith(".json")):
                return relative.replace(os.sep, "/")
    return None


def clear_engine_specs(worktree):
    """Called only AFTER the backup was verified. When the untracked content of the worktree is exclusively engine-owned `.specs/`
    state, deletes that folder so the normal `git worktree remove` can run. Returns the blocking path (nothing deleted) or None."""
    specs = os.path.join(worktree, ".specs")
    if not os.path.isdir(specs):
        return None
    code, out, _ = git(worktree, "status", "--porcelain", "--untracked-files=all", "--", ".specs")
    if code == 0 and not out.strip():  # gitignored (or tracked and clean): git removes it by itself
        return None
    blocker = foreign_content_in_specs(worktree)
    if blocker:
        return blocker
    try:
        shutil.rmtree(specs)
    except OSError as failure:
        return "%s (%s)" % (specs, cut_reason(failure, 80))
    return None


def remove_worktree(entry, force):
    """`git worktree remove` from the user's repo, no --force unless asked. Returns an error text, or None when the folder is gone."""
    if is_same_path(entry["worktree"], entry["repo_path"]):  # belt and braces: never the user's own folder
        return "refused: the worktree is the original repo folder"
    if not force:
        blocker = clear_engine_specs(entry["worktree"])
        if blocker:
            return "the worktree was kept: %s holds content that is not engine-owned .specs/ state, so it was not deleted (backup done; remove it by hand or use force)" % blocker
    code, out, err = git(entry["repo_path"], "worktree", "remove", *(["--force"] if force else []), entry["worktree"])
    git(entry["repo_path"], "worktree", "prune")
    if os.path.isdir(entry["worktree"]):
        return "git worktree remove failed for %s, the folder is still there: %s" % (entry["worktree"], cut_reason(err or out or "no detail"))
    return None


def close(primary_repo, confirm_compare_link=False, only=None, output_dir="", force_remove=False):
    """Closes the repos that may close; the others are kept with their reasons. Per-repo outcome in the body, never an exception.

    `only` = repo names to close (None = all). `confirm_compare_link` lifts ONLY `pr_missing` of a repo that has a compare link."""
    primary_repo = os.path.abspath(primary_repo)
    entries, _ = project_entries(primary_repo)
    primary_entry = next(entry for entry in entries if entry["primary"])
    if loop_is_running(primary_entry["worktree"]):
        return {"ok": False, "code": "loop_running", "error": REASON_TEXT["loop_running"]}
    output_dir = output_dir or os.path.join(os.path.expanduser("~"), "Downloads")
    checks = {entry["name"]: check_repo(entry, False) for entry in entries}
    outcome = {entry["name"]: {"status": "kept", "removed_worktree": False, "branch_kept": checks[entry["name"]]["branch"], "reasons": []} for entry in entries}
    closing = []
    for entry in entries:
        check, result = checks[entry["name"]], outcome[entry["name"]]
        if only is not None and entry["name"] not in only:
            result["reasons"] = check["reasons"]
        elif check["can_close"] or (check["needs_confirm"] and confirm_compare_link):
            closing.append(entry)
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
            write_last_run(primary_repo, entries, checks, {entry["name"] for entry in closing}, durable)
            last_run, primary_safe = last_run_path(primary_repo), True
        except (OSError, shutil.Error) as failure:
            if primary_entry in closing:
                closing.remove(primary_entry)
                outcome[primary_entry["name"]].update(status="failed", reasons=[reason("error", "the durable .specs/ files could not be copied back (%s); the primary worktree was kept" % str(failure)[:160])])
    for entry in sorted(closing, key=lambda item: item["primary"]):  # (d) the primary, which holds `.specs/`, goes last
        result = outcome[entry["name"]]
        error = remove_worktree(entry, force_remove)
        if error:
            result.update(status="failed", reasons=[reason("error", error)])
        else:
            result.update(status="closed", removed_worktree=True, reasons=[])
    removed = {name for name, result in outcome.items() if result["removed_worktree"]}
    if primary_safe and removed != {entry["name"] for entry in closing}:
        write_last_run(primary_repo, entries, checks, removed, durable)  # a removal failed: the record must say so
    closed = [name for name in outcome if outcome[name]["status"] == "closed"]
    response = {"ok": True, "closed": closed, "kept": [name for name in outcome if outcome[name]["status"] != "closed"],
                "backup": backups.get(primary_entry["name"]) or next(iter(backups.values()), None), "durable_copied": durable, "last_run": last_run, "repos": outcome}
    return response


def main():
    parser = argparse.ArgumentParser(description="Close loop: can-close check, verified backup, durable .specs/ back, last-run.json, worktree removal")
    parser.add_argument("primary_repo")
    parser.add_argument("--check", action="store_true", help="read only: print the per-repo can-close check")
    parser.add_argument("--confirm-compare-link", action="store_true", help="close a repo that only has a compare link (no PR)")
    parser.add_argument("--repos", default="", help="comma-separated repo names (default: all)")
    parser.add_argument("--output-dir", "--out", dest="output_dir", default="", help="where the backup folders go (default: ~/Downloads)")
    parser.add_argument("--force-remove", action="store_true", help="pass --force to git worktree remove (never lifts a dirty-tree stop)")
    opts = parser.parse_args()
    if opts.check:
        result = can_close(opts.primary_repo)
    else:
        only = [name.strip() for name in opts.repos.split(",") if name.strip()] or None
        result = close(opts.primary_repo, opts.confirm_compare_link, only, opts.output_dir, opts.force_remove)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
