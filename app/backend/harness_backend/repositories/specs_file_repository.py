"""Access to the files of `.specs/` and of a project root (read, atomic write, create without overwriting).

Every read/write coming from a path given by the user goes through `resolve_path_inside_specs`: it refuses an absolute
path, `..`, a drive (C:) and a symbolic link in the middle of the path, and checks with realpath that the target is inside
`.specs/`. The loop layout and the ROADMAP parser come from the engine by import (`loop_state`), never rewritten here.
"""
import datetime
import hashlib
import json
import os
import sys
import threading
import time
from typing import Protocol

from ..domain.project_errors import PathOutsideSpecsError

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import loop_io  # noqa: E402  (LOOP.lock owner, reused pids)
import loop_state  # noqa: E402  (loop folder layout, parse_roadmap, roadmap approval)
import classify_structure  # noqa: E402  (Conformance section of CLAUDE.md)
import run_hints  # noqa: E402  (How to run section of CLAUDE.md)
import test_env_fallback  # noqa: E402  (default .env.test from the machine-level TEST values)

TEXT_ENCODING = "utf-8"
REPLACE_RETRIES = 20  # x REPLACE_RETRY_SECONDS: up to ~1 s for a reader to let go of the file
REPLACE_RETRY_SECONDS = 0.05


def list_test_environment_problems(work_dir, with_e2e=False):
    """Problems of TEST-ENVIRONMENT.md from the engine preflight function (`run_loop.test_environment_problems`), without
    rewriting the rule: empty list = not required (no regression=yes/--e2e) or complete. Late import: run_loop pulls in
    notify/delivery."""
    import types
    import run_loop  # noqa: E402
    return run_loop.test_environment_problems(work_dir, types.SimpleNamespace(e2e=with_e2e))


def compute_bytes_version(content_bytes):
    return hashlib.sha1(content_bytes).hexdigest()


def split_relative_path(relative):
    """Parts of the relative path (separator `/` or `\\`). Raises PathOutsideSpecsError for absolute, drive, `..` and NUL."""
    if not isinstance(relative, str) or not relative.strip() or "\x00" in relative:
        raise PathOutsideSpecsError("empty or invalid path")
    text = relative.strip().replace("\\", "/")
    if text.startswith("/") or os.path.isabs(relative) or os.path.splitdrive(relative)[0] or ":" in text:
        raise PathOutsideSpecsError("absolute paths are not accepted: use the path relative to .specs/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        raise PathOutsideSpecsError("empty or invalid path")
    if any(part == ".." for part in parts):
        raise PathOutsideSpecsError("paths with `..` are not accepted")
    return parts


def is_link_or_junction(path):
    """Symbolic link or Windows junction (`mklink /J`, which does not need an administrator)."""
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path)


def is_inside_folder(root, target):
    normalized_root = os.path.normcase(os.path.realpath(root))
    normalized_target = os.path.normcase(os.path.realpath(target))
    try:
        return os.path.commonpath([normalized_root, normalized_target]) == normalized_root
    except ValueError:  # different drives on Windows
        return False


class ISpecsFileRepository(Protocol):
    def resolve_path_inside_specs(self, specs_dir, relative) -> str: ...
    def read_bytes(self, path): ...
    def read_text(self, path): ...
    def write_text_atomic(self, path, text) -> None: ...
    def create_file_if_missing(self, path, text) -> bool: ...
    def append_text(self, path, text) -> None: ...
    def read_json(self, path) -> dict: ...
    def write_json_atomic(self, path, data) -> None: ...
    def list_json_files(self, folder) -> list: ...
    def list_markdown_files(self, folder, ignored_dirs=()) -> list: ...
    def get_file_info(self, path) -> dict: ...
    def read_loop_lock_pid(self, work_dir) -> int: ...


class SpecsFileRepository:
    def __init__(self):
        self.write_lock = threading.Lock()

    # ------------------------------------------------ safe path
    def resolve_path_inside_specs(self, specs_dir, relative):
        """Absolute path of `relative` inside `specs_dir`, or PathOutsideSpecsError."""
        parts = split_relative_path(relative)
        current = specs_dir
        for part in parts:
            current = os.path.join(current, part)
            if is_link_or_junction(current):
                raise PathOutsideSpecsError("symbolic links (or junctions) inside .specs/ are not accepted")
        if not is_inside_folder(specs_dir, current):
            raise PathOutsideSpecsError("path outside .specs/")
        return current

    # ------------------------------------------------ text
    def read_bytes(self, path):
        """Raw content or None (missing/unreadable/folder)."""
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None

    def read_text(self, path):
        content = self.read_bytes(path)
        return None if content is None else content.decode(TEXT_ENCODING, "replace")

    def write_text_atomic(self, path, text):
        self.write_bytes_atomic(path, text.encode(TEXT_ENCODING))

    def write_bytes_atomic(self, path, content):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        temporary = "%s.%d.tmp" % (path, threading.get_ident())
        with self.write_lock:
            with open(temporary, "wb") as handle:
                handle.write(content)
            for attempt in range(REPLACE_RETRIES):  # Windows: the replace fails while a reader (a poll) holds the target open
                try:
                    os.replace(temporary, path)
                    return
                except PermissionError:
                    if attempt == REPLACE_RETRIES - 1:
                        raise
                    time.sleep(REPLACE_RETRY_SECONDS)

    def create_file_if_missing(self, path, text):
        """True if it created the file; False if it already existed (never overwrites)."""
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "x", encoding=TEXT_ENCODING, newline="\n") as handle:
                handle.write(text)
            return True
        except FileExistsError:
            return False

    def append_text(self, path, text):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with self.write_lock:
            with open(path, "a", encoding=TEXT_ENCODING, newline="\n") as handle:
                handle.write(text)

    # ------------------------------------------------ json
    def read_json(self, path):
        try:
            with open(path, encoding=TEXT_ENCODING) as handle:
                data = json.load(handle)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def write_json_atomic(self, path, data):
        self.write_bytes_atomic(path, json.dumps(data, ensure_ascii=False, indent=2).encode(TEXT_ENCODING))

    def list_json_files(self, folder):
        try:
            names = sorted(os.listdir(folder))
        except OSError:
            return []
        return [os.path.join(folder, name) for name in names if name.endswith(".json") and os.path.isfile(os.path.join(folder, name))]

    # ------------------------------------------------ tree
    def list_markdown_files(self, folder, ignored_dirs=()):
        """`.md` paths relative to `folder` (separator `/`), without following symbolic links and without the ignored
        folders (compared by relative path, e.g. `project/loop/internal`)."""
        if not os.path.isdir(folder):
            return []
        ignored = {item.strip("/") for item in ignored_dirs}
        found = []
        for current, subfolders, files in os.walk(folder, followlinks=False):
            current_relative = os.path.relpath(current, folder).replace("\\", "/")
            current_relative = "" if current_relative == "." else current_relative
            subfolders[:] = sorted(name for name in subfolders
                                   if not is_link_or_junction(os.path.join(current, name))
                                   and ("%s/%s" % (current_relative, name)).strip("/") not in ignored)
            for name in sorted(files):
                path = os.path.join(current, name)
                if name.lower().endswith(".md") and not is_link_or_junction(path):
                    found.append(("%s/%s" % (current_relative, name)).strip("/"))
        return found

    def get_file_info(self, path):
        try:
            info = os.stat(path)
        except OSError:
            return {"size": None, "modified_at": None}
        return {"size": info.st_size,
                "modified_at": datetime.datetime.fromtimestamp(info.st_mtime).isoformat(timespec="seconds")}

    # ------------------------------------------------ loop
    def read_loop_lock_pid(self, work_dir):
        """PID written in the engine's `LOOP.lock` (0 = no lock, unreadable, or a reused pid). Whether it is alive is the process runner's call."""
        return loop_io.lock_owner_pid(loop_state.loop_path(work_dir, "LOOP.lock"))
