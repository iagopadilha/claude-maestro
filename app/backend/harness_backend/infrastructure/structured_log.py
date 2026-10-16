"""Structured log (one JSON per line) in ~/.loop-harness/logs/app.jsonl. Never writes a token, password or .env body."""
import datetime
import json
import os
import re
import threading

SECRET_PATTERN = re.compile(r"(?i)(token|password|secret|api[_-]?key|authorization)([\"'\s:=]+)([^\s\"',}]+)")
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024


def redact_secrets(text):
    return SECRET_PATTERN.sub(lambda found: found.group(1) + found.group(2) + "***", str(text))


class StructuredLog:
    def __init__(self, logs_dir, file_name="app.jsonl", mirror_to_console=False):
        self.file_path = os.path.join(logs_dir, file_name) if logs_dir else ""
        self.mirror_to_console = mirror_to_console
        self.write_lock = threading.Lock()

    def record(self, level, event, **fields):
        entry = {"t": datetime.datetime.now().isoformat(timespec="seconds"), "level": level, "event": event}
        entry.update({key: redact_secrets(value) if isinstance(value, str) else value for key, value in fields.items()})
        line = json.dumps(entry, ensure_ascii=False, default=str)
        if self.mirror_to_console:
            print(line, flush=True)
        if not self.file_path:
            return
        with self.write_lock:
            try:
                os.makedirs(os.path.dirname(self.file_path), exist_ok=True)
                if os.path.exists(self.file_path) and os.path.getsize(self.file_path) > MAX_FILE_SIZE_BYTES:
                    os.replace(self.file_path, self.file_path + ".1")
                with open(self.file_path, "a", encoding="utf-8") as handle:
                    handle.write(line + "\n")
            except OSError:
                pass  # the log never breaks the app

    def info(self, event, **fields):
        self.record("info", event, **fields)

    def warn(self, event, **fields):
        self.record("warning", event, **fields)

    def error(self, event, **fields):
        self.record("error", event, **fields)
