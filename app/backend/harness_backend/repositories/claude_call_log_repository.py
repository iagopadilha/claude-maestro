"""Call log of the `claude -p` runs the panel itself starts (chat, map, roadmap, validation...): one JSON line per call in
`<work dir>/.specs/project/loop/internal/claude_calls.jsonl`. The file stays bounded: past `MAX_LINES` the oldest lines are dropped.
"""
import json
import os
import sys
import threading

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import loop_state  # noqa: E402

CALL_LOG_FILE_NAME = "claude_calls.jsonl"
MAX_LINES = 400  # kept after a trim
TRIM_AT_LINES = 500  # the file is rewritten when it grows past this
WRITE_LOCK = threading.Lock()


class ClaudeCallLogRepository:
    @staticmethod
    def get_path(work_dir):
        return os.path.join(loop_state.internal_dir(work_dir), CALL_LOG_FILE_NAME)

    def append_call(self, work_dir, entry):
        """Appends one call; trims the file when it is too long. Raises OSError when the folder cannot be written."""
        path = self.get_path(work_dir)
        with WRITE_LOCK:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            lines = self.read_lines(path)
            if len(lines) > TRIM_AT_LINES:
                temporary = path + ".tmp"
                with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
                    handle.write("\n".join(lines[-MAX_LINES:]) + "\n")
                os.replace(temporary, path)

    def read_calls(self, work_dir, limit=MAX_LINES):
        """The last `limit` calls, oldest first; unreadable lines and a missing file are skipped."""
        calls = []
        for line in self.read_lines(self.get_path(work_dir))[-limit:]:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict):
                calls.append(entry)
        return calls

    @staticmethod
    def read_lines(path):
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                return [line.strip() for line in handle if line.strip()]
        except OSError:
            return []
