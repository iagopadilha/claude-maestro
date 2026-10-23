#!/usr/bin/env python3
"""Small file, JSON, git and process helpers shared by the engine scripts (`run_loop.py` and the modules split out of it).

Every function here is moved as it was (no behavior change); `run_loop.py` re-exports them, so `run_loop.git`, `run_loop.save_meta`... still work.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import json
import subprocess
import tempfile
import time

import loop_state  # noqa: E402  (typed state: loop folder paths)

IS_WIN = os.name == "nt"


def run(args, cwd=None, timeout=120):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git(cwd, *args):
    return run(["git", "-C", cwd, *args])


def git_raw(cwd, *args, timeout=120):
    """Like `git` but the output is NOT stripped (a `status --porcelain -z` stream starts with the space of the first status)."""
    try:
        out = subprocess.run(["git", "-C", cwd, *args], capture_output=True, timeout=timeout)
        return out.returncode, out.stdout.decode("utf-8", "replace"), out.stderr.decode("utf-8", "replace").strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


class StatusRow(str):
    """One entry of `git status --porcelain -z`. As text it is `XY path` (the path as it is on disk, never quoted or escaped);
    `.status`, `.path` and `.origin` (the old path of a rename/copy, "" otherwise) are the parts."""

    def __new__(cls, status, path, origin=""):
        row = super().__new__(cls, "%s %s" % (status, path))
        row.status, row.path, row.origin = status, path, origin
        return row


def parse_porcelain_z(text):
    """`git status --porcelain -z` output -> [StatusRow]. Records are NUL separated `XY path`; a rename/copy (R/C in either column) is
    followed by one extra record that holds the origin path."""
    rows, records, index = [], text.split("\0"), 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4 or record[2] != " ":
            continue
        status, path, origin = record[:2], record[3:], ""
        if ("R" in status or "C" in status) and index < len(records):
            origin = records[index]
            index += 1
        rows.append(StatusRow(status, path, origin))
    return rows


def status_rows(cwd, *arguments):
    """(exit code, [StatusRow], stderr) of `git status --porcelain -z <arguments>` in `cwd`."""
    code, out, err = git_raw(cwd, "status", "--porcelain", "-z", *arguments)
    return code, (parse_porcelain_z(out) if code == 0 else []), err


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


APP_OWNED_META_KEYS = ("delivery", "max_cost_usd")  # written by the app (start, raise-limit) in the worktree meta: the engine never drops them


DELIVERY_PENDING_NAME = "delivery-pending.json"


def save_meta(path, meta):
    """Saves meta.json keeping what the app wrote there meanwhile: the disk value of the app-owned keys wins over the in-memory copy."""
    on_disk = load_json(path)
    for key in APP_OWNED_META_KEYS:
        if key in on_disk:
            meta[key] = on_disk[key]
    save_json(path, meta)


def merge_pending_delivery(repo, meta):
    """The app wrote the delivery choices before the worktree existed (<repo>/.specs/project/loop/internal/delivery-pending.json): they
    become meta["delivery"] and the pending file is deleted."""
    path = os.path.join(loop_state.internal_dir(repo), DELIVERY_PENDING_NAME)
    pending = load_json(path)
    if isinstance(pending, dict) and pending:
        meta["delivery"] = dict(meta.get("delivery") or {}, **pending)
    try:
        os.remove(path)
    except OSError:
        pass


REPLACE_ATTEMPTS = 6


def replace_file(source, target):
    """os.replace that retries a few times on Windows, where a reader holding the target open makes it fail with PermissionError."""
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if not IS_WIN or attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def atomic_write_text(path, text):
    """Writes `text` through a temporary file in the same folder (flush + fsync + replace): a reader sees the old file or the new one, never half."""
    folder = os.path.dirname(path) or "."
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.chmod(temporary, os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644)  # mkstemp makes it 0600
        except OSError:
            pass
        replace_file(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def save_json(path, data):
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))


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


def process_start_time(pid):
    """When the process `pid` started, as an integer that only means something compared with the same function's value for the same
    pid (Windows: creation FILETIME; Linux: starttime of /proc/<pid>/stat). None when it cannot be known (dead process, other systems)."""
    if pid <= 0:
        return None
    if IS_WIN:
        return windows_process_start(pid)
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        return int(text[text.rindex(")") + 2:].split()[19])  # field 22; the name (field 2) may hold spaces and parentheses
    except (OSError, ValueError, IndexError):
        return None


def windows_process_start(pid):
    try:
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype, kernel.OpenProcess.argtypes = wintypes.HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        handle = kernel.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel_time, user_time = (wintypes.FILETIME() for _ in range(4))
            if not kernel.GetProcessTimes(handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel_time), ctypes.byref(user_time)):
                return None
            return (created.dwHighDateTime << 32) | created.dwLowDateTime
        finally:
            kernel.CloseHandle(handle)
    except (ImportError, OSError, AttributeError, ValueError):
        return None


LOCK_START_SUFFIX = ".start"  # LOOP.lock.start: {"pid", "started"} next to the lock; LOOP.lock itself stays a plain PID (the panel reads it with int())
LOCK_FRESH_SECONDS = 5  # an empty lock younger than this is being written by the process that just created it


def read_lock_holder(path):
    """(pid, started, empty) of a lock file. pid 0 = no lock or unreadable; `started` comes from the sidecar and only when it is
    about that same pid (old locks have none: None); `empty` = the file exists with no content yet."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read().strip()
    except OSError:
        return 0, None, False
    if not text:
        return 0, None, True
    try:
        pid = int(text)
    except ValueError:
        return 0, None, False
    started = None
    try:
        with open(path + LOCK_START_SUFFIX, encoding="utf-8") as handle:
            info = json.load(handle)
        if isinstance(info, dict) and info.get("pid") == pid and isinstance(info.get("started"), int):
            started = info["started"]
    except (OSError, ValueError):
        pass
    return pid, started, False


def lock_owner_pid(path):
    """PID of the lock file, or 0 when there is none or the pid now belongs to another process (its start time differs from the one
    the lock recorded). Whether that pid is alive stays the caller's call (the panel uses its own process checker)."""
    pid, started, _ = read_lock_holder(path)
    if pid and started is not None:
        now = process_start_time(pid)
        if now is not None and now != started:
            return 0
    return pid


def lock_holder_alive(path):
    """True when the lock file names a running process that really is its owner: the pid is alive AND, when the lock recorded the
    start time of its owner, the process with that pid still started then (a reused pid is another process: the lock is stale)."""
    pid, started, empty = read_lock_holder(path)
    if empty:
        try:
            return time.time() - os.path.getmtime(path) < LOCK_FRESH_SECONDS
        except OSError:
            return False
    if not pid_alive(pid):
        return False
    if started is not None:
        now = process_start_time(pid)
        if now is not None and now != started:
            return False
    return True


def kill_tree(pid):
    if IS_WIN:
        run(["taskkill", "/PID", str(pid), "/T", "/F"])
    else:
        try:
            os.killpg(os.getpgid(pid), 9)
        except OSError:
            pass


def worktree_of(repo):
    return os.path.join(os.path.dirname(repo), os.path.basename(repo) + "-worktree-loop")
