"""Reads and writes `config.json` of the app data folder (`~/.loop-harness`, or `LOOP_DATA_DIR`) for the Settings screen.

Only the keys asked for change: every other key (including the ones this app does not know) is kept as it is. The write is atomic
(temp file in the same folder, then `os.replace`) and UTF-8. A file that exists but is not a JSON object is never overwritten.
"""
import json
import os
import threading

from ..domain.app_settings import ConfigFileUnreadableError

CONFIG_FILE_NAME = "config.json"
WRITE_LOCK = threading.Lock()


class AppConfigRepository:
    def __init__(self, data_dir):
        self.data_dir = data_dir

    @property
    def path(self):
        return os.path.join(self.data_dir, CONFIG_FILE_NAME)

    def read_all(self):
        """The whole file as a dict; {} when it is missing or empty. Raises ConfigFileUnreadableError when it holds something else."""
        try:
            with open(self.path, encoding="utf-8-sig") as handle:
                text = handle.read()
        except FileNotFoundError:
            return {}
        except OSError as error:
            raise ConfigFileUnreadableError("config.json cannot be read: %s" % error)
        if not text.strip():
            return {}
        try:
            content = json.loads(text)
        except ValueError:
            content = None
        if not isinstance(content, dict):
            raise ConfigFileUnreadableError("config.json is not a JSON object: fix or remove it, then save again")
        return content

    def read_all_or_empty(self):
        """Same as `read_all` for display: an unreadable file shows the defaults."""
        try:
            return self.read_all()
        except ConfigFileUnreadableError:
            return {}

    def set_value(self, key, value):
        with WRITE_LOCK:
            content = self.read_all()
            content[key] = value
            self.write_all(content)

    def remove_key(self, key):
        """Drops the key (back to the default); a file that never had it is not touched."""
        with WRITE_LOCK:
            content = self.read_all()
            if key not in content:
                return False
            del content[key]
            self.write_all(content)
            return True

    def write_all(self, content):
        os.makedirs(self.data_dir, exist_ok=True)
        temporary = "%s.tmp-%d" % (self.path, os.getpid())
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(content, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, self.path)
