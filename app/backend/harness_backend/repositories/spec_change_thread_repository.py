"""The spec-change conversations of a project: ONE thread per scope (a feature slug or `_project`) in
`<work>/.specs/project/loop/internal/spec_change/<scope>.json` (atomic write, up to MAX_MESSAGES messages). Keeps the claude
`session_id` of the scope (resumed on every message), which the API never returns.
"""
import os
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, now_iso, read_json_object, write_json_atomic

SPEC_CHANGE_FOLDER_NAME = "spec_change"
MAX_MESSAGES = 200


def create_empty_thread():
    return {"messages": [], "session_id": None, "created_at": now_iso(), "updated_at": None}


class ISpecChangeThreadRepository(Protocol):
    def read_thread(self, work_dir, scope) -> dict: ...
    def write_thread(self, work_dir, scope, thread) -> None: ...


class SpecChangeThreadRepository:
    def get_thread_path(self, work_dir, scope):
        return os.path.join(get_loop_internal_dir(work_dir), SPEC_CHANGE_FOLDER_NAME, scope + ".json")

    def read_thread(self, work_dir, scope):
        """The thread, or a new empty one when the file does not exist or does not have the expected format."""
        data = read_json_object(self.get_thread_path(work_dir, scope))
        if not isinstance(data.get("messages"), list):
            return create_empty_thread()
        data["messages"] = [message for message in data["messages"] if isinstance(message, dict)]
        return data

    def write_thread(self, work_dir, scope, thread):
        thread["messages"] = thread["messages"][-MAX_MESSAGES:]
        thread["updated_at"] = now_iso()
        write_json_atomic(self.get_thread_path(work_dir, scope), thread)
