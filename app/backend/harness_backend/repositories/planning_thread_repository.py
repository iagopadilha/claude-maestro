"""The planning conversation of a project: ONE thread in `<work>/.specs/project/loop/internal/planning/thread.json`
(atomic write, up to MAX_MESSAGES messages). Keeps the claude `session_id` (resumed on every message), which the API never
returns.
"""
import os
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, now_iso, read_json_object, write_json_atomic

PLANNING_FOLDER_NAME = "planning"
THREAD_FILE_NAME = "thread.json"
MAX_MESSAGES = 400


def create_empty_thread():
    return {"messages": [], "session_id": None, "created_at": now_iso(), "updated_at": None}


class IPlanningThreadRepository(Protocol):
    def read_thread(self, work_dir) -> dict: ...
    def write_thread(self, work_dir, thread) -> None: ...
    def get_planning_dir(self, work_dir) -> str: ...


class PlanningThreadRepository:
    def get_planning_dir(self, work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), PLANNING_FOLDER_NAME)

    def read_thread(self, work_dir):
        """The thread, or a new empty one when the file does not exist or does not have the expected format."""
        data = read_json_object(os.path.join(self.get_planning_dir(work_dir), THREAD_FILE_NAME))
        if not isinstance(data.get("messages"), list):
            return create_empty_thread()
        data["messages"] = [message for message in data["messages"] if isinstance(message, dict)]
        return data

    def write_thread(self, work_dir, thread):
        thread["messages"] = thread["messages"][-MAX_MESSAGES:]
        thread["updated_at"] = now_iso()
        write_json_atomic(os.path.join(self.get_planning_dir(work_dir), THREAD_FILE_NAME), thread)
