"""Per-project access choices of the Test environment tab: `<project>/.specs/project/ACCESS.json`.

    {"grafana_read": false, "shared_fallback": false}

- `grafana_read`: the planning AI and the loop's spec agents may look up sample data in Grafana/Loki (read only tools).
- `shared_fallback`: the project accepts the user's SHARED test account (plugin `shared/.env` and the user-level
  `~/.claude/skills/_shared/.env`) as a fallback for names the project does not define.

Both default to false. Reading is tolerant (missing, broken or odd-typed file = defaults); writing is atomic. Stdlib only.
"""
import json
import os
import tempfile

ACCESS_FILE_PARTS = (".specs", "project", "ACCESS.json")
GRAFANA_READ_KEY = "grafana_read"
SHARED_FALLBACK_KEY = "shared_fallback"
ACCESS_KEYS = (GRAFANA_READ_KEY, SHARED_FALLBACK_KEY)


def access_file_path(root):
    return os.path.join(root, *ACCESS_FILE_PARTS)


def default_access():
    return {key: False for key in ACCESS_KEYS}


def read_access(root):
    """{"grafana_read": bool, "shared_fallback": bool}; only a JSON `true` turns a key on."""
    access = default_access()
    try:
        with open(access_file_path(root), encoding="utf-8-sig") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return access
    if isinstance(content, dict):
        for key in ACCESS_KEYS:
            access[key] = content.get(key) is True
    return access


def write_access(root, access):
    """Atomic write of both keys (anything else is dropped). Returns what was written."""
    clean = {key: access.get(key) is True for key in ACCESS_KEYS}
    path = access_file_path(root)
    folder = os.path.dirname(path)
    os.makedirs(folder, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=".ACCESS.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(clean, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise
    return clean
