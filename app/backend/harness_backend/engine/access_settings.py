"""Access of a project is COMPUTED now (see `mcp_access` for the MCP servers and `credential_check` for the `.env` layers): there is
no per-project toggle any more. `<project>/.specs/project/ACCESS.json` (`{"grafana_read": ..., "shared_fallback": ...}`) is a legacy
file: its keys are ignored and `migrate_access_file` drops them (an existing `false` is migrated to the new default: always on).

`read_access` stays only for the callers that still index the old keys: it returns the effective values, always True (Grafana read and
the shared `.env` layers are on by default), whatever the file says. Stdlib only.
"""
import json
import os

ACCESS_FILE_PARTS = (".specs", "project", "ACCESS.json")
GRAFANA_READ_KEY = "grafana_read"
SHARED_FALLBACK_KEY = "shared_fallback"
LEGACY_KEYS = (GRAFANA_READ_KEY, SHARED_FALLBACK_KEY)
ACCESS_KEYS = LEGACY_KEYS


def access_file_path(root):
    return os.path.join(root, *ACCESS_FILE_PARTS)


def default_access():
    return {key: True for key in ACCESS_KEYS}


def read_access(root):
    """Effective access: every key True (the file is ignored). Kept so old callers keep working."""
    return default_access()


def migrate_access_file(root):
    """Drops the legacy keys from `ACCESS.json`; the file is removed when nothing else is left. Returns True when it changed anything.
    Tolerant: a missing, broken or odd file is left alone (it is ignored anyway)."""
    path = access_file_path(root)
    try:
        with open(path, encoding="utf-8-sig") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return False
    if not isinstance(content, dict) or not any(key in content for key in LEGACY_KEYS):
        return False
    remaining = {key: value for key, value in content.items() if key not in LEGACY_KEYS}
    try:
        if remaining:
            temporary = path + ".tmp"
            with open(temporary, "w", encoding="utf-8", newline="\n") as stream:
                json.dump(remaining, stream, indent=2)
                stream.write("\n")
            os.replace(temporary, path)
        else:
            os.remove(path)
    except OSError:
        return False
    return True
