"""Project board (whiteboard): `<work>/.specs/project/loop/internal/whiteboard.json`, atomic write.

Opaque content for the back: {"data": <front object>, "version": N, "updated_at": "ISO"}.
"""
import os
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, read_json_object, write_json_atomic

WHITEBOARD_FILE_NAME = "whiteboard.json"


class IWhiteboardRepository(Protocol):
    def read_board(self, work_dir) -> dict: ...
    def write_board(self, work_dir, board) -> None: ...


class WhiteboardRepository:
    def get_board_path(self, work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), WHITEBOARD_FILE_NAME)

    def read_board(self, work_dir):
        """{} when it was never saved (or the file is unreadable)."""
        return read_json_object(self.get_board_path(work_dir))

    def write_board(self, work_dir, board):
        write_json_atomic(self.get_board_path(work_dir), board)
