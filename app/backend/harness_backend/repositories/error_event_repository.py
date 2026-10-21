"""Durable store of the error center's events: `<work>/.specs/project/loop/internal/events/errors.jsonl`, one JSON object per line.

Append only: a record is never rewritten or removed, so the history survives a rotated or truncated `guard.log` and a launch log that
starts over. A record is `{"id", "kind", ...}`; a later record with the same `id` only fills fields (a non-null value wins over
nothing, `null` never erases), which is how an event whose turn became known later is updated. One write per batch, so a crash leaves
at most one broken last line (skipped on read, and a missing final newline is repaired before the next append).
"""
import json
import os
import threading

from ..repositories.loop_file_repository import loop_state, read_json_lines

EVENTS_FOLDER_NAME = "events"
ERRORS_FILE_NAME = "errors.jsonl"


class ErrorEventRepository:
    def __init__(self):
        self.lock = threading.Lock()
        self.cache = {}

    def get_path(self, work_dir):
        return os.path.join(loop_state.loop_path(work_dir, EVENTS_FOLDER_NAME), ERRORS_FILE_NAME)

    def read_events(self, work_dir):
        """Events by first appearance, later records merged in (see module docstring). Missing file = []."""
        path = self.get_path(work_dir)
        try:
            info = os.stat(path)
        except OSError:
            return []
        stamp = (info.st_mtime_ns, info.st_size)
        with self.lock:
            stored = self.cache.get(path)
            if stored and stored[0] == stamp:
                return [dict(event) for event in stored[1].values()]
        merged = {}
        for record in read_json_lines(path):
            self.merge_record(merged, record)
        with self.lock:
            self.cache[path] = (stamp, merged)
        return [dict(event) for event in merged.values()]

    @staticmethod
    def merge_record(merged, record):
        event_id = record.get("id")
        if not isinstance(event_id, str) or not event_id:
            return
        known = merged.setdefault(event_id, {"id": event_id})
        known.update({key: value for key, value in record.items() if value is not None or key not in known})

    def append_events(self, work_dir, records):
        """Appends the records in ONE write. Returns False (and writes nothing) when the folder is not writable."""
        if not records:
            return True
        path = self.get_path(work_dir)
        payload = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records).encode("utf-8")
        with self.lock:
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                if os.path.getsize(path) > 0 if os.path.exists(path) else False:
                    with open(path, "rb") as handle:
                        handle.seek(-1, os.SEEK_END)
                        if handle.read(1) != b"\n":
                            payload = b"\n" + payload
                with open(path, "ab") as handle:
                    handle.write(payload)
                self.cache.pop(path, None)
                return True
            except OSError:
                return False
