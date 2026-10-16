"""Free-form project board (whiteboard), persisted on the server with a version (optimistic control between tabs).

GET -> {"ok":true,"data":<object|null>,"version":N,"updated_at":"ISO|null"} (version 0 and data null when never saved).
POST {"data":<object>,"base_version":N} -> {"ok":true,"version":N+1,"updated_at":"ISO"}; a different base = 409 with the current one.
"""
import threading
from typing import Protocol

from ..repositories.loop_command_file_repository import now_iso

NEVER_SAVED_BOARD_VERSION = 0


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


class IWhiteboardService(Protocol):
    def get_board(self, context) -> tuple: ...
    def save_board(self, context, body) -> tuple: ...


class WhiteboardService:
    def __init__(self, log, whiteboard_repository):
        self.log = log
        self.whiteboard_repository = whiteboard_repository
        self.write_lock = threading.Lock()

    def read_current_version(self, board):
        return board["version"] if is_integer(board.get("version")) else NEVER_SAVED_BOARD_VERSION

    def get_board(self, context):
        board = self.whiteboard_repository.read_board(context.work_dir)
        data = board.get("data") if isinstance(board.get("data"), dict) else None
        return 200, {"ok": True, "data": data, "version": self.read_current_version(board), "updated_at": board.get("updated_at")}

    def save_board(self, context, body):
        data, base_version = body.get("data"), body.get("base_version")
        if not isinstance(data, dict):
            return 400, {"ok": False, "error": "data must be a JSON object"}
        if not is_integer(base_version):
            return 400, {"ok": False, "error": "base_version must be an integer"}
        with self.write_lock:
            current_version = self.read_current_version(self.whiteboard_repository.read_board(context.work_dir))
            if base_version != current_version:
                return 409, {"ok": False, "error": "the board changed in another tab (version %d); reload or overwrite" % current_version,
                             "version": current_version}
            board = {"data": data, "version": current_version + 1, "updated_at": now_iso()}
            self.whiteboard_repository.write_board(context.work_dir, board)
        self.log.info("whiteboard_saved", project=context.project.id, version=board["version"])
        return 200, {"ok": True, "version": board["version"], "updated_at": board["updated_at"]}
