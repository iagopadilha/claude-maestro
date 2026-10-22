"""The follow-up (wrap-up) conversations of a project: ONE thread per round in
`<work>/.specs/project/loop/internal/follow-up/round-<n>.json` (atomic write). Keeps the claude `session_id` of the round
(resumed on every message), which the API never returns.

Thread keys: round, session_id, spent_usd, summary_note, baseline {roadmap, flows}, messages [], closed_at.
"""
import os
import re
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, read_json_object, write_json_atomic

FOLLOW_UP_FOLDER_NAME = "follow-up"
MAX_MESSAGES = 200
ROUND_FILE_PATTERN = re.compile(r"^round-(\d+)\.json$")


def create_empty_thread(round_number):
    return {
        "round": round_number,
        "session_id": None,
        "spent_usd": 0.0,
        "summary_note": "",
        "baseline": {"roadmap": "", "flows": []},
        "messages": [],
        "closed_at": None,
    }


class IFollowUpThreadRepository(Protocol):
    def read_thread(self, work_dir, round_number) -> dict: ...
    def write_thread(self, work_dir, round_number, thread) -> None: ...
    def current_round(self, work_dir, roadmap_text) -> int: ...


class FollowUpThreadRepository:
    def get_folder_path(self, work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), FOLLOW_UP_FOLDER_NAME)

    def get_thread_path(self, work_dir, round_number):
        return os.path.join(self.get_folder_path(work_dir), "round-" + str(int(round_number)) + ".json")

    def read_thread(self, work_dir, round_number):
        """The thread of the round, or a new empty one when the file does not exist or does not have the expected format."""
        data = read_json_object(self.get_thread_path(work_dir, round_number))
        if not isinstance(data.get("messages"), list):
            return create_empty_thread(round_number)
        data["messages"] = [message for message in data["messages"] if isinstance(message, dict)]
        data.setdefault("round", round_number)
        return data

    def write_thread(self, work_dir, round_number, thread):
        thread["messages"] = thread.get("messages", [])[-MAX_MESSAGES:]
        thread["round"] = round_number
        write_json_atomic(self.get_thread_path(work_dir, round_number), thread)

    def current_round(self, work_dir, roadmap_text):
        """The round whose stage has not run yet: 1 + the number of closed rounds (round files with `closed_at` set).
        Nothing on disk = round 1. `roadmap_text` is accepted for callers that already hold it; the count comes from the files."""
        folder = self.get_folder_path(work_dir)
        try:
            names = os.listdir(folder)
        except OSError:
            return 1
        closed_rounds = 0
        for name in names:
            match = ROUND_FILE_PATTERN.match(name)
            if match and read_json_object(os.path.join(folder, name)).get("closed_at"):
                closed_rounds += 1
        return closed_rounds + 1
