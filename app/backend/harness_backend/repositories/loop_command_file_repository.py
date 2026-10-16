"""Loop control files that the commands module reads and writes inside the `work_dir` of ONE project.

Reads: `LOOP-CMD.json` (command to relaunch), `meta.json`, ROADMAP (open features). Writes: `PAUSE`, `meta.json`
(pr/push after a delivery), `internal/actions.log`. The `.specs/project/loop/` layout comes from `engine/loop_state.py`
(imported from the engine, never rewritten here).
"""
import datetime
import json
import os
import sys
from typing import Protocol

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import loop_state  # noqa: E402

LOOP_COMMAND_FILE_NAME = "LOOP-CMD.json"
META_FILE_NAME = "meta.json"
PAUSE_FILE_NAME = "PAUSE"
RELAUNCH_LOG_FILE_NAME = "monitor-relaunch.log"
ACTIONS_LOG_FILE_NAME = "actions.log"
OPEN_FEATURE_STATUSES = ("pending", "speccing", "specced", "approved", "building")


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def read_json_object(path):
    """JSON object of the file; missing, unreadable or not an object = {}."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_json_atomic(path, data):
    """Writes to a temporary file and swaps it in with `os.replace` (a reader never sees a half-written file)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def get_loop_internal_dir(work_dir):
    """`.specs/project/loop/internal` (chat, whiteboard, actions.log)."""
    return loop_state.internal_dir(work_dir)


class ILoopCommandFileRepository(Protocol):
    def read_loop_command(self, work_dir) -> dict: ...
    def get_relaunch_log_path(self, work_dir) -> str: ...
    def create_pause_file(self, work_dir) -> None: ...
    def read_meta(self, work_dir) -> dict: ...
    def write_delivery_to_meta(self, work_dir, push, pr, pr_status) -> None: ...
    def record_executed_action(self, work_dir, action, ok, summary) -> None: ...
    def list_open_features(self, work_dir) -> list: ...


class LoopCommandFileRepository:
    def read_loop_command(self, work_dir):
        """`LOOP-CMD.json` written by run_loop.py: {"argv":[...],"cwd":"..."}; missing = {}."""
        return read_json_object(loop_state.loop_path(work_dir, LOOP_COMMAND_FILE_NAME))

    def get_relaunch_log_path(self, work_dir):
        return loop_state.loop_path(work_dir, RELAUNCH_LOG_FILE_NAME)

    def create_pause_file(self, work_dir):
        """Empty `PAUSE` file: run_loop.py checks it at the top of every turn and stops with `awaiting_user`."""
        path = loop_state.loop_path(work_dir, PAUSE_FILE_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        open(path, "a").close()

    def read_meta(self, work_dir):
        return read_json_object(loop_state.loop_path(work_dir, META_FILE_NAME))

    def write_delivery_to_meta(self, work_dir, push, pr, pr_status):
        """After a delivery: pr/pr_status/push in `meta.json` (as run_loop does) so the state shows the link."""
        path = loop_state.loop_path(work_dir, META_FILE_NAME)
        meta = read_json_object(path)
        meta["push"], meta["pr"], meta["pr_status"] = push, pr, pr_status
        write_json_atomic(path, meta)

    def record_executed_action(self, work_dir, action, ok, summary):
        """One JSON line in `internal/actions.log` ({"action","t","ok","summary"}). Deleted worktree: does not recreate it."""
        if not os.path.isdir(work_dir):
            return
        path = loop_state.loop_path(work_dir, ACTIONS_LOG_FILE_NAME)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        line = json.dumps({"action": action, "t": now_iso(), "ok": ok, "summary": summary}, ensure_ascii=False)
        with open(path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(line + "\n")

    def list_open_features(self, work_dir):
        """ROADMAP slugs whose status is still open (pending..building), in file order."""
        text = loop_state.read(os.path.join(work_dir, ".specs", "project", "ROADMAP.md"))
        features, _ = loop_state.parse_roadmap(text)
        return [slug for slug, fields in features.items() if fields.get("status") in OPEN_FEATURE_STATUSES]
