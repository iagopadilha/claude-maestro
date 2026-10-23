#!/usr/bin/env python3
"""What the loop reads from its worktrees between turns: roadmap score, fingerprint, new commits, leftovers (dirty files) and how they are settled; plus --rewind and the lock.

Moved out of `run_loop.py` as it was.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import hashlib
import json
import re
import shutil

import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import system_map  # noqa: E402  (contract.md deltas of a verified feature -> docs/codebase/SYSTEM.md of a monorepo)
from loop_io import (  # noqa: E402
    LOCK_START_SUFFIX, atomic_write_text, git, load_json, lock_holder_alive, process_start_time, read, read_lock_holder, save_meta,
    status_rows, worktree_of,
)

# blocked is NOT progress: it scores 0, otherwise a loop that only blocks features would look like it advances (closes the roadmap but delivers nothing)
RANK = {"pending": 0, "speccing": 1, "specced": 2, "approved": 3, "building": 4, "verified": 6, "blocked": 0}


def loop_worktrees(work, meta):
    """[(repo name, worktree path)] the loop works in, primary first. A single-repo project (no meta.repos) = [("", work)]."""
    entries = meta.get("repos") or {}
    if not entries:
        return [("", work)]
    return [(name, entry.get("worktree") or work) for name, entry in sorted(entries.items(), key=lambda pair: not pair[1].get("primary"))]


# ---------------------------------------------------------------- state
def roadmap_score(work):
    score, total, verified = 0, 0, 0
    for line in read(os.path.join(work, ".specs", "project", "ROADMAP.md")).splitlines():
        match = re.match(r"^- \[[^\]]+\]\s+.*?status=(\w+)", line.strip())
        if match:
            total += 1
            status = match.group(1)
            score += RANK.get(status, 0)
            verified += status in ("verified", "blocked")
    return score, total, verified


def fingerprint(work, meta=None):
    _, head, _ = git(work, "rev-parse", "HEAD")
    head += "".join(value for name, value in sorted((heads_of(work, meta) or {}).items()))  # a commit in any worktree is a change
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md")) + read(os.path.join(work, ".specs", "project", "STATE.md")) + head
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def heads_of(work, meta):
    """{repo name: HEAD of its worktree} for a multi-repo project; None for a single-repo one (its snapshots stay as they were)."""
    if not (meta or {}).get("repos"):
        return None
    return {name: git(path, "rev-parse", "HEAD")[1] for name, path in loop_worktrees(work, meta)}


def count_new_commits(prev, work, meta, head_now):
    """Commits made since the previous turn's snapshot, summed over EVERY worktree of the project (a commit in any listed one counts)."""
    if not prev:
        return 0
    _, count, _ = git(work, "rev-list", "--count", "%s..%s" % (prev["head"], head_now))
    total = int(count) if count.isdigit() else 0
    before = prev.get("heads") or {}
    for name, path in loop_worktrees(work, meta)[1:]:  # the primary is already counted above
        if before.get(name):
            _, extra, _ = git(path, "rev-list", "--count", "%s..HEAD" % before[name])
            total += int(extra) if extra.isdigit() else 0
    return total


def dirty_worktrees(work, meta):
    """[(repo name, rows)] of every worktree with changes outside .specs/. The name is "" for a single-repo project."""
    dirty = []
    for name, path in loop_worktrees(work, meta):
        ignore = set(meta.get("copied", []))
        ignore.update((meta.get("repos", {}).get(name) or {}).get("copied") or {})
        ignore.update(meta.get("carried_setup_files") or [])  # BR-02: carried setup files wait for the first checkpoint commit, they are not leftovers
        ignore.update((meta.get("repos", {}).get(name) or {}).get("carried_setup_files") or [])
        if meta.get("system_md_uncommitted") and os.path.normpath(path) == os.path.normpath(work):
            ignore.add(SYSTEM_DOCUMENT)  # the engine's contract merge left it for the next checkpoint commit
        rows = dirty_outside_specs(path, ignore)
        if rows:
            dirty.append((name, rows))
    return dirty


def dirty_outside_specs(work, ignore):
    """Rows (`StatusRow`: text `XY path`, plus .status/.path/.origin) of the changes outside .specs/ and outside `ignore`."""
    _, found, _ = status_rows(work)
    rows = []
    for row in found:
        path = row.path
        if path.startswith(".specs/") or path.rstrip("/") in ignore:
            continue
        if path.endswith("/") and row.status == "??":  # an untracked folder is one collapsed row: expand it when an ignored file may sit inside
            inside = [name for name in ignore if name.startswith(path)]
            if inside:
                _, listing, _ = status_rows(work, "-uall", "--", path)
                rows.extend(item for item in listing if item.path not in ignore)
                continue
        rows.append(row)
    return rows


IN_FLIGHT_STATUSES = ("speccing", "approved", "building")  # features whose executors may leave files behind


PORCELAIN_ROW = re.compile(r"^\s*[A-Z?!]{1,2}\s+(.*)$")  # for a plain-text row: `git()` strips its output, so the first row may have lost its leading space
GIT_ESCAPES = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
OCTAL_BYTE = re.compile(r"[0-7]{1,3}")


def unquote_git_path(text):
    """A path as `git status` (without -z) prints it: in double quotes, C escapes, bytes as octal (`configura\\303\\247\\303\\243o`)."""
    if not (len(text) >= 2 and text.startswith('"') and text.endswith('"')):
        return text
    body, raw, index = text[1:-1], bytearray(), 0
    while index < len(body):
        char = body[index]
        if char != "\\" or index + 1 >= len(body):
            raw.extend(char.encode("utf-8"))
            index += 1
            continue
        octal = OCTAL_BYTE.match(body, index + 1)
        if octal:
            raw.append(int(octal.group(), 8) & 0xFF)
            index = octal.end()
            continue
        escaped = body[index + 1]
        raw.extend(bytes([GIT_ESCAPES[escaped]]) if escaped in GIT_ESCAPES else escaped.encode("utf-8"))
        index += 2
    return raw.decode("utf-8", "replace")


def row_path(row):
    """Path of a status row (the new name of a rename). Rows from `dirty_outside_specs` carry it (`.path`); a plain `git status
    --porcelain` line is parsed (quotes and escapes decoded, `old -> new` reduced to the new name)."""
    path = getattr(row, "path", None)
    if path is not None:
        return path
    match = PORCELAIN_ROW.match(row)
    text = (match.group(1) if match else row).strip()
    if " -> " in text:
        text = text.split(" -> ")[-1].strip()
    return unquote_git_path(text).replace("\\", "/")


def feature_scope_rows(rows, repo_name, features, primary_name):
    """(inside, outside): the dirty rows that fall under the `paths=` of an in-flight feature, and the rest."""
    scopes = []
    for feature in features.values():
        if feature.get("status") not in IN_FLIGHT_STATUSES:
            continue
        for repo, path in loop_state.feature_repo_paths(feature, primary_name):
            if not repo_name or not repo or repo == repo_name:
                scopes.append(path.strip("/").replace("\\", "/"))
    inside, outside = [], []
    for row in rows:
        path = row_path(row).rstrip("/")
        covered = any(scope and (path == scope or path.startswith(scope + "/") or scope.startswith(path + "/")) for scope in scopes)
        (inside if covered else outside).append(row)
    return inside, outside


LITERAL_PATHSPECS = "--literal-pathspecs"  # a file named `a[1].txt` or `*.md` is that file, not a glob
PATHS_PER_GIT_CALL = 100  # the Windows command line is limited


def commit_leftovers(path, rows, message):
    """Commits exactly these rows on the loop branch (the executors never run git, so their work is still loose after the turn)."""
    names = [row_path(row) for row in rows]  # a staged rename already holds the removal of its origin
    for start in range(0, len(names), PATHS_PER_GIT_CALL):
        code, _, err = git(path, LITERAL_PATHSPECS, "add", "-A", "--", *names[start:start + PATHS_PER_GIT_CALL])
        if code != 0:
            return False, "git add failed: " + err[-200:]
    code, _, err = git(path, "commit", "-m", message, "--no-verify")
    return (True, "") if code == 0 else (False, "git commit failed: " + err[-200:])


def delete_path(path, name):
    full = os.path.join(path, name.rstrip("/"))
    if os.path.islink(full) or os.path.isfile(full):
        os.remove(full)
    elif os.path.isdir(full):
        shutil.rmtree(full, ignore_errors=True)


def discard_leftovers(path, rows):
    """Throws away exactly these rows: tracked files go back to HEAD, untracked ones (and staged new ones) are deleted, a staged rename/copy is undone."""
    for row in rows:
        name = row_path(row)
        status = getattr(row, "status", None) or str(row)[:2]
        origin = getattr(row, "origin", "")
        if status == "??":
            delete_path(path, name)
        elif status[:1] in ("R", "C"):  # unstage both names, drop the new file, bring the origin back
            git(path, LITERAL_PATHSPECS, "reset", "-q", "HEAD", "--", *[item for item in (name, origin) if item])
            delete_path(path, name)
            if origin:
                git(path, LITERAL_PATHSPECS, "checkout", "HEAD", "--", origin)
        elif status[:1] == "A":  # staged new file: it is not in HEAD, `checkout` cannot restore it
            git(path, LITERAL_PATHSPECS, "rm", "-q", "-f", "--cached", "--", name)
            delete_path(path, name)
        else:
            git(path, LITERAL_PATHSPECS, "checkout", "HEAD", "--", name)
    return True, ""


def settle_dirty_worktrees(work, meta, turn, opts):
    """Runs before every turn. Returns the [(repo name, rows)] that are still dirty (the loop stops on them).
    Leftovers of the previous turn that sit under the paths of an in-flight feature are committed as a checkpoint (the work is the
    feature's own); anything else stays and stops the loop. `--leftovers commit|discard` (panel recovery) decides about ALL of it."""
    mode = getattr(opts, "leftovers", "") or ""
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    still_dirty = []
    for name, rows in dirty_worktrees(work, meta):
        path = dict(loop_worktrees(work, meta)).get(name, work)
        if mode == "discard":
            discard_leftovers(path, rows)
            print("[loop] --leftovers discard: %d leftover file(s) discarded%s" % (len(rows), " in repo " + name if name else ""))
            continue
        inside, outside = (rows, []) if mode == "commit" else feature_scope_rows(rows, name, features, getattr(opts, "primary_name", ""))
        if inside:
            saved, problem = commit_leftovers(path, inside, "chore(loop): checkpoint of work left uncommitted by turn %d" % (turn - 1))
            if saved:
                print("[loop] checkpoint: %d file(s) left by turn %d committed on the loop branch%s" % (len(inside), turn - 1, " (repo " + name + ")" if name else ""))
                meta.setdefault("checkpoints", []).append({"turn": turn - 1, "files": len(inside), "repo": name})
            else:
                outside = rows
                print("[loop] checkpoint failed: " + problem)
        if outside:
            still_dirty.append((name, outside))
    return still_dirty


def rewind(repo, opts):
    """Time travel: restores the state (ROADMAP/STATE/LOOP/ANSWERS/RULES) and the loop branch HEAD to right after turn N."""
    work = worktree_of(repo)
    if os.path.isdir(work):
        loop_state.migrate_layout(work)
    meta_path = loop_state.loop_path(work, "meta.json")
    meta = load_json(meta_path)
    if not os.path.isdir(work) or not meta.get("branch"):
        sys.exit("there is no loop worktree for %s; nothing to rewind" % repo)
    lock = loop_state.loop_path(work, "LOOP.lock")
    if os.path.exists(lock) and lock_holder_alive(lock):
        sys.exit("a loop is running in this worktree; stop it before rewinding")
    snap = loop_state.load_snapshot(work, opts.rewind)
    if not snap:
        snapshots = loop_state.loop_path(work, "snapshots")
        have = sorted(os.listdir(snapshots)) if os.path.isdir(snapshots) else []
        sys.exit("no snapshot for turn %d. Available: %s" % (opts.rewind, ", ".join(have) or "none"))
    _, current, _ = git(work, "rev-parse", "HEAD")
    _, lost, _ = git(work, "log", "--oneline", "--no-decorate", "%s..%s" % (snap["head"], current))
    print("[rewind] go back to the state after turn %d (HEAD %s; today %s)" % (opts.rewind, snap["head"][:8], current[:8]))
    print("[rewind] commits leaving branch %s (they stay in the git reflog):\n%s" % (meta["branch"], lost or "  (none)"))
    if not opts.yes:
        print("[rewind] nothing was changed. Confirm with --yes")
        return 0
    if dirty_outside_specs(work, set(meta.get("copied", []))):
        sys.exit("dirty tree outside .specs/ in the worktree: resolve it before rewinding")
    code, _, err = git(work, "reset", "--hard", snap["head"])
    if code != 0:
        sys.exit("git reset failed: " + err)
    loop_state.restore(work, opts.rewind)
    meta.update({"turns": opts.rewind, "stopped": "rewind", "reason": "state restored to after turn %d" % opts.rewind})
    save_meta(meta_path, meta)
    print("[rewind] done. Run the loop command again to continue from turn %d." % (opts.rewind + 1))
    return 0


# ---------------------------------------------------------------- lock
def create_lock_file(path):
    """Creates the lock file atomically (O_EXCL): exactly one process gets it. Plain PID inside (the panel reads it with int()); the
    start time of this process goes in a sidecar next to it, so a later process that reuses the PID is not taken for the owner."""
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(str(os.getpid()))
    started = process_start_time(os.getpid())
    if started is not None:
        try:
            atomic_write_text(path + LOCK_START_SUFFIX, json.dumps({"pid": os.getpid(), "started": started}))
        except OSError:
            pass  # without the sidecar the lock is judged by the PID alone, as before


def remove_stale_lock(path, stale_pid):
    """Takes a stale lock out of the way. It is renamed first and the PID inside is compared with the one judged stale: if another
    process replaced the file in between (a live lock), it goes back."""
    taken = "%s.stale-%d" % (path, os.getpid())
    try:
        os.replace(path, taken)
    except OSError:
        return  # somebody else removed it first
    if read_lock_holder(taken)[0] != stale_pid:
        try:
            os.rename(taken, path)
        except OSError:
            pass
        return
    for leftover in (taken, path + LOCK_START_SUFFIX):
        try:
            os.remove(leftover)
        except OSError:
            pass


def acquire_lock(work):
    """Takes LOOP.lock for this process or exits when a live loop holds it. A stale lock (dead PID, or a PID now owned by another
    process) is removed and the creation is tried once more, still atomically."""
    path = loop_state.loop_path(work, "LOOP.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for attempt in range(2):
        try:
            create_lock_file(path)
            return path
        except FileExistsError:
            if lock_holder_alive(path):
                break
            if attempt == 0:
                remove_stale_lock(path, read_lock_holder(path)[0])
    sys.exit("a loop is already running in this worktree (PID %s). Wait or stop it." % (read_lock_holder(path)[0] or "unknown"))


def release_lock(path):
    """Removes the lock this process holds (never another loop's)."""
    if read_lock_holder(path)[0] != os.getpid():
        return
    for name in (path, path + LOCK_START_SUFFIX):
        try:
            os.remove(name)
        except OSError:
            pass


SYSTEM_DOCUMENT = system_map.SYSTEM_RELATIVE_PATH.replace("\\", "/")
