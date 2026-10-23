#!/usr/bin/env python3
"""Loads variables from a `.env` file (notifications, git, PR, panel) without any library. Same format and API as the
plugin's `shared/envload.py`, so the engine reads settings the same way (`envload.load()`, `envload.get()`, `envload.get_list()`).

Lookup order:
  1. LOOP_ENV_FILE (explicit file): ONLY that file is read (tests isolate the environment this way).
  2. `<app data folder>/.env` (LOOP_DATA_DIR or ~/.loop-harness).
  3. `<plugin root>/shared/.env` (plugin root = the orchestrator skill folder /../..), the plugin's own settings file.
Without an explicit file, items 2 and 3 are merged: the first one that defines a key wins; a real environment variable
beats them all (it is never overwritten).

These are the APP's own settings (notify, panel, git, PR). Loading copies them into os.environ, so the module records every
key it set (`LOADED_VALUES`): `is_loaded_value(name, value)` tells the credential layers that an os.environ entry is a copy of
an app `.env`, not a variable the user set, so it never counts as the real-environment credential layer (the toolkit's
`shared/.env` and the other credential files are read as layers by `engine/credential_check.py`, never through here).

Format: `KEY=value`, one per line; comments only on whole lines starting with `#`; a value in single or double quotes is
read literally (quote it when it holds # & % { } ? ^). The `.env` is a secret: never commit it.
"""
import json
import os

try:
    from . import skill_locator
except ImportError:  # loaded as a loose script (python envload.py)
    import skill_locator

EXPLICIT_FILE_VARIABLE = "LOOP_ENV_FILE"
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
DEFAULT_DATA_DIR_NAME = ".loop-harness"
LOADED_VALUES = {}  # name -> value this module copied into os.environ (a name the real environment already had is never recorded)
SECRET_WORDS = ("TOKEN", "SECRET", "PASSWORD", "KEY")


def get_explicit_file():
    return os.environ.get(EXPLICIT_FILE_VARIABLE, "")


def _data_dir():
    return os.environ.get(DATA_DIR_VARIABLE) or os.path.join(os.path.expanduser("~"), DEFAULT_DATA_DIR_NAME)


def _configured_skill_dir(data_dir):
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8") as handle:
            content = json.load(handle)
        return content.get("orchestrator_skill_dir", "") if isinstance(content, dict) else ""
    except (OSError, ValueError):
        return ""


def candidates():
    """Candidate .env files, in priority order."""
    explicit = get_explicit_file()
    if explicit:
        return [explicit]
    data_dir = _data_dir()
    found = [os.path.join(data_dir, ".env")]
    plugin_root = skill_locator.plugin_root_of(skill_locator.locate_orchestrator_skill_dir(_configured_skill_dir(data_dir)))
    if plugin_root:
        found.append(os.path.join(plugin_root, "shared", ".env"))
    return found


def parse(text):
    values = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        if key:
            values[key] = value
    return values


def load_all():
    """Loads every existing candidate (setdefault). Returns the list of files read."""
    used = []
    for path in candidates():
        if path and os.path.isfile(path):
            try:
                with open(path, encoding="utf-8-sig") as handle:
                    for key, value in parse(handle.read()).items():
                        if key not in os.environ:
                            os.environ[key] = value
                            LOADED_VALUES[key] = value
                used.append(path)
            except OSError:
                continue
    return used


def load():
    """Returns the path of the first .env used (or ''), filling os.environ only where the variable does not exist yet."""
    used = load_all()
    return used[0] if used else ""


def is_loaded_value(name, value):
    """True when os.environ[name] == `value` is the copy this module made from a `.env` file (the user did not set it)."""
    return name in LOADED_VALUES and LOADED_VALUES[name] == value


def get(name, default=""):
    load()
    return os.environ.get(name, default)


def get_list(name):
    return [item.strip() for item in get(name).split(",") if item.strip()]


def is_secret_name(name):
    return any(word in name.upper() for word in SECRET_WORDS)


if __name__ == "__main__":
    used = load_all()
    print("files:", ", ".join(used) or "(no .env found)")
    for key in sorted(k for k in os.environ if k.startswith(("LOOP_", "GUARD_"))):
        print(" ", key, "=", "<set>" if is_secret_name(key) and os.environ[key] else os.environ[key])
