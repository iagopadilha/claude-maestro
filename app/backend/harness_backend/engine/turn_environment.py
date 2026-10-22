#!/usr/bin/env python3
"""The environment a loop turn (`claude -p`) starts with, minus the secrets that only came from the app `.env` (standard library only).

Used by the engine (`run_loop.push_lock_env`, imported as a script sibling, like `claude_binaries` and `usage_windows`).

Why: the engine loads `<LOOP_DATA_DIR or ~/.loop-harness>/.env` into its own process (`envload`, notice channels, git identity...). Every child
process inherits it, so a turn could read the mail password or a webhook address with a plain `echo`. The turn does not need them: notices are
sent by the engine, outside the turn.

A variable is left out only when ALL of these hold:
  - its value is the one an app `.env` file gives it (a real variable the user had with a different value stays: it is not ours to hide);
  - its name looks like a secret (PASSWORD, PASSWD, SECRET, TOKEN, API_KEY, APIKEY, PRIVATE_KEY, WEBHOOK, CREDENTIAL);
  - it does not start with TEST_USER_ (the generic test user the test conventions use) nor GUARD_ (the guard's settings reach the turn as is);
  - the project's `.specs/project/TEST-ENVIRONMENT.md` does not reference it as `$NAME` or `${NAME}` (those names keep working).

`build(environ, cwd, env_paths)` returns {"env": the dict for the turn, "removed": sorted NAMES}. Values are never returned in `removed`, and
never printed. Any internal error returns the full environment (the turn must not break over this filter) with an `error` text.
"""
import os
import re

SECRET_NAME_PARTS = ("PASSWORD", "PASSWD", "SECRET", "TOKEN", "API_KEY", "APIKEY", "PRIVATE_KEY", "WEBHOOK", "CREDENTIAL")
KEPT_PREFIXES = ("TEST_USER_", "GUARD_")  # the test user conventions, and the guard's own settings (GUARD_SECRET_PATHS holds paths, not a secret)
TEST_ENVIRONMENT_FILE = os.path.join(".specs", "project", "TEST-ENVIRONMENT.md")
NAME_REFERENCE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def looks_secret(name):
    upper = name.upper()
    return any(part in upper for part in SECRET_NAME_PARTS)


def parse_env_text(text):
    """`KEY=value` lines of an app .env (same format as envload: whole-line comments, optional `export`, quoted values read literally)."""
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


def values_from_env_files(env_paths):
    """{name: value} the app .env files give. The first file that defines a name wins, like envload."""
    found = {}
    for path in env_paths or []:
        if not path or not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8-sig") as handle:
                parsed = parse_env_text(handle.read())
        except OSError:
            continue
        for name, value in parsed.items():
            found.setdefault(name, value)
    return found


def names_referenced_by_project(cwd):
    """Names the project's TEST-ENVIRONMENT.md mentions as `$NAME` or `${NAME}` (empty when there is no such file)."""
    path = os.path.join(os.path.abspath(cwd), TEST_ENVIRONMENT_FILE)
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return set(NAME_REFERENCE.findall(handle.read()))
    except OSError:
        return set()


def build(environ, cwd, env_paths):
    """{"env": copy of `environ` without the app-.env secrets, "removed": sorted names left out, "error": text (only when the filter failed)}."""
    full = dict(environ)
    try:
        from_files = values_from_env_files(env_paths)
        referenced = names_referenced_by_project(cwd)
        removed = sorted(
            name for name, value in full.items()
            if name in from_files and from_files[name] == value
            and looks_secret(name) and not name.startswith(KEPT_PREFIXES) and name not in referenced)
        env = {name: value for name, value in full.items() if name not in removed}
        return {"env": env, "removed": removed}
    except Exception as error:  # the filter must never stop a turn: pass everything and say so once
        return {"env": full, "removed": [], "error": "%s: %s" % (type(error).__name__, error)}
