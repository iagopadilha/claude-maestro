"""Reads the loop files of ONE project (`.specs/project/...` inside the `work_dir`). Read only; never writes.

The layout (`loop/json`, `loop/internal`, turns, snapshots) comes from `engine/loop_state.py` by import: the ROADMAP parser
is not rewritten here. Anything missing at the source becomes empty (`{}`, `""`, `[]`), never an exception.
"""
import json
import os
import re
import subprocess
import sys
import threading
from typing import Protocol

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import graph_view  # noqa: E402
import loop_state  # noqa: E402

IS_WINDOWS = os.name == "nt"
TEXT_CACHE_LIMIT = 512
GIT_TIMEOUT_SECONDS = 10
WINDOWS_QUERY_LIMITED_ACCESS = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
WINDOWS_ACCESS_DENIED_ERROR = 5
WINDOWS_STILL_ACTIVE_CODE = 259  # STILL_ACTIVE
TURN_FILE_PATTERN = re.compile(r"\d+\.json")
PAUSE_FILE_NAME = "PAUSE"
ACTIONS_FILE_NAME = "actions.log"
STAGE_CLOSING_FILE_FORMAT = "stage-%02d.md"


def is_process_alive(pid):
    """True if the PID exists and is active."""
    if not isinstance(pid, int) or pid <= 0:
        return False
    if IS_WINDOWS:
        import ctypes
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(WINDOWS_QUERY_LIMITED_ACCESS, False, pid)
        if not handle:
            return kernel.GetLastError() == WINDOWS_ACCESS_DENIED_ERROR  # access denied = the process exists
        code = ctypes.c_ulong()
        succeeded = kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
        kernel.CloseHandle(handle)
        return bool(succeeded) and code.value == WINDOWS_STILL_ACTIVE_CODE
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def read_json_lines(path):
    """JSON objects of a .jsonl/.log file, one per line (unreadable lines are skipped). Missing file = []."""
    items = []
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    item = json.loads(line)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    items.append(item)
    except OSError:
        pass
    return items


class ILoopFileRepository(Protocol):
    def read_json(self, path) -> dict: ...
    def read_text(self, path) -> str: ...
    def read_cached_text(self, path) -> str: ...
    def read_loop_json(self, work_dir) -> dict: ...
    def read_meta(self, work_dir) -> dict: ...
    def read_lock_pid(self, work_dir) -> int: ...
    def is_process_alive(self, pid) -> bool: ...


class LoopFileRepository:
    """Reads ROADMAP, STATE, LOOP.json, meta.json, turns, snapshots, events and actions.log of a work folder."""

    def __init__(self, process_checker=is_process_alive):
        self.process_checker = process_checker
        self.text_cache = {}
        self.text_cache_lock = threading.Lock()
        self.commit_cache = {}

    # ------------------------------------------------ generic reading
    def read_json(self, path):
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def read_text(self, path):
        return loop_state.read(path)

    def read_cached_text(self, path):
        """Text of the file, re-read only when (mtime, size) changes; missing = ''."""
        try:
            info = os.stat(path)
        except OSError:
            return ""
        stamp = (info.st_mtime_ns, info.st_size)
        with self.text_cache_lock:
            stored = self.text_cache.get(path)
            if stored and stored[0] == stamp:
                return stored[1]
        text = loop_state.read(path)
        with self.text_cache_lock:
            if len(self.text_cache) > TEXT_CACHE_LIMIT:
                self.text_cache.clear()
            self.text_cache[path] = (stamp, text)
        return text

    def file_exists(self, path):
        return os.path.exists(path)

    def get_mtime(self, path):
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    # ------------------------------------------------ loop state
    def read_loop_json(self, work_dir):
        return self.read_json(loop_state.state_file(work_dir, "LOOP.json"))

    def read_meta(self, work_dir):
        return self.read_json(loop_state.loop_path(work_dir, "meta.json"))

    def read_loop_command(self, work_dir):
        return self.read_json(loop_state.loop_path(work_dir, "LOOP-CMD.json"))

    def get_lock_path(self, work_dir):
        return loop_state.loop_path(work_dir, "LOOP.lock")

    def read_lock_pid(self, work_dir):
        try:
            return int(loop_state.read(self.get_lock_path(work_dir)).strip() or 0)
        except ValueError:
            return 0

    def is_process_alive(self, pid):
        return self.process_checker(pid)

    def is_pause_requested(self, work_dir):
        return os.path.exists(loop_state.loop_path(work_dir, PAUSE_FILE_NAME))

    def read_roadmap(self, work_dir):
        return loop_state.read(os.path.join(work_dir, ".specs", "project", "ROADMAP.md"))

    def read_state_file(self, work_dir, name):
        """STATE.md, VALIDATE.md, ANSWERS.md... at the place the engine uses."""
        return loop_state.read(loop_state.state_file(work_dir, name))

    def read_roadmap_approval(self, work_dir):
        return loop_state.roadmap_approval(work_dir)

    def read_feature_spec(self, work_dir, slug):
        return self.read_cached_text(os.path.join(work_dir, ".specs", "features", slug, "spec.md"))

    def read_feature_spec_head(self, work_dir, slug, max_lines):
        """First lines of spec.md (only for the title); missing = []."""
        lines = []
        try:
            with open(os.path.join(work_dir, ".specs", "features", slug, "spec.md"), encoding="utf-8", errors="replace") as handle:
                for _ in range(max_lines):
                    line = handle.readline()
                    if not line:
                        break
                    lines.append(line)
        except OSError:
            pass
        return lines

    def read_stage_closing(self, work_dir, number):
        return self.read_cached_text(os.path.join(work_dir, ".specs", "project", "phases", STAGE_CLOSING_FILE_FORMAT % number))

    # ------------------------------------------------ turns and snapshots
    def list_turn_numbers(self, work_dir):
        try:
            names = os.listdir(loop_state.turns_dir(work_dir))
        except OSError:
            return []
        return sorted(int(name[:-5]) for name in names if TURN_FILE_PATTERN.fullmatch(name))

    def get_turn_path(self, work_dir, number):
        return os.path.join(loop_state.turns_dir(work_dir), "%03d.json" % number)

    def read_turn(self, work_dir, number):
        return self.read_json(self.get_turn_path(work_dir, number))

    def load_snapshot(self, work_dir, number):
        return loop_state.load_snapshot(work_dir, number)

    def read_snapshot_loop_json(self, snapshot):
        return self.read_json(os.path.join(snapshot["dir"], "LOOP.json")) if snapshot else {}

    def read_snapshot_roadmap(self, snapshot):
        return loop_state.read(os.path.join(snapshot["dir"], "ROADMAP.md")) if snapshot else ""

    def list_snapshot_numbers(self, work_dir):
        try:
            names = os.listdir(loop_state.loop_path(work_dir, "snapshots"))
        except OSError:
            return []
        return sorted(int(name) for name in names if name.isdigit())

    def read_cached_snapshot_loop_json(self, work_dir, number):
        """LOOP.json of snapshot NNN with an mtime cache; unreadable = None."""
        try:
            data = json.loads(self.read_cached_text(os.path.join(loop_state.snap_dir(work_dir, number), "LOOP.json")) or "{}")
        except ValueError:
            return None
        return data if isinstance(data, dict) else {}

    def read_turn_events(self, work_dir, number):
        """Live agent events of the turn (`NNN.events.jsonl`, optional). Missing = empty list."""
        return read_json_lines(os.path.join(loop_state.turns_dir(work_dir), "%03d.events.jsonl" % number))

    def count_commits_between(self, work_dir, previous_head, current_head):
        """`git rev-list --count previous..current` (read only), cached; failure = None."""
        key = (work_dir, previous_head, current_head)
        if key not in self.commit_cache:
            count = None
            try:
                result = subprocess.run(["git", "-C", work_dir, "rev-list", "--count", "%s..%s" % (previous_head, current_head)],
                                        capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS)
                text = result.stdout.strip()
                count = int(text) if result.returncode == 0 and text.isdigit() else None
            except (OSError, subprocess.SubprocessError):
                count = None
            self.commit_cache[key] = count
        return self.commit_cache[key]

    # ------------------------------------------------ actions (written by the commands module)
    def read_action_lines(self, work_dir):
        """JSON objects of `internal/actions.log` (unreadable lines are skipped)."""
        return read_json_lines(loop_state.loop_path(work_dir, ACTIONS_FILE_NAME))
