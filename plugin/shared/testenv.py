#!/usr/bin/env python3
"""Test credentials for every skill and the loop: one convention, values only in `.env` files, never in markdown. Stdlib only.

LAYERS (the first one that defines a key wins; a real environment variable beats all of them):
  1. a real environment variable
  2. <project>/.specs/project/.env.test       this project's own test values (gitignored)
  3. ~/.loop-harness/.env                     your machine (every project)
  4. <this folder>/.env                       the plugin's shared settings next to this script
  5. ~/.claude/skills/_shared/.env            the user-level shared settings (LOOP_USER_SHARED_DIR or config.json
                                              `user_shared_dir` in the data dir override the folder; LOOP_DATA_DIR moves 3)

TEST USERS are `;`-separated lists, aligned by position (write `\\;` for a literal `;` inside a value):
  TEST_USER_EMAILS=first@company.com;second@company.com
  TEST_USER_PASSWORDS=first-password;second-password
  TEST_USER_ROLES=customer;admin                (optional)
Any other list works the same way: `values("TEST_TENANTS")`. Each user is also exposed as plain variables, so a command never
needs a value written in a document:  TEST_USER_1_EMAIL, TEST_USER_1_PASSWORD, TEST_USER_1_ROLE, ... and TEST_USER_COUNT.

USAGE
  python testenv.py list  [--project DIR]            names and where each comes from; secret values always shown as ***
  python testenv.py check [--project DIR] [--names A,B]   exit 1 and list the names that are missing (default: every $NAME that
                                                          .specs/project/TEST-ENVIRONMENT.md mentions)
  python testenv.py exec  [--project DIR] 'curl -H "X-Token: $ADMIN_TOKEN" $URL'
                                                     runs the command with the variables filled in and replaces every secret
                                                     value in its output by ***. Use SINGLE quotes so your own shell does not
                                                     expand the $NAME before this script does.
Code that needs a value imports this module:  import testenv; testenv.load_layers(project)[0]["TEST_USER_1_EMAIL"]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import envload  # noqa: E402  (parse only)

PROJECT_ENV_RELATIVE = os.path.join(".specs", "project", ".env.test")
ENVIRONMENT_DOC_RELATIVE = os.path.join(".specs", "project", "TEST-ENVIRONMENT.md")
USER_DATA_DIR = os.path.join(os.path.expanduser("~"), ".loop-harness")
SHARED_DIR_VARIABLE = "LOOP_USER_SHARED_DIR"
DEFAULT_SHARED_PARTS = (".claude", "skills", "_shared")
LIST_SEPARATOR = ";"
ESCAPED_SEPARATOR = "\\;"
USER_FIELDS = (("EMAILS", "EMAIL"), ("PASSWORDS", "PASSWORD"), ("ROLES", "ROLE"))
SECRET_NAME = re.compile(r"(?i)(token|secret|passw|credential|private_key|api_?key|auth|pwd)")
MIN_SECRET_LENGTH = 4
MASK = "***"
NAME_REFERENCE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")
EXEC_TIMEOUT_SECONDS = 600


def split_values(text):
    """`a;b;c` -> ['a','b','c'] (blank items kept so lists stay aligned; `\\;` is a literal `;`)."""
    parts = (text or "").replace(ESCAPED_SEPARATOR, "\0").split(LIST_SEPARATOR) if text else []
    return [part.replace("\0", LIST_SEPARATOR).strip() for part in parts]


def layer_files(project=None):
    """(label, path) of the files to read, highest priority first."""
    data_dir = os.environ.get("LOOP_DATA_DIR") or USER_DATA_DIR
    files = []
    if project:
        files.append(("project .env.test", os.path.join(project, PROJECT_ENV_RELATIVE)))
    files.append(("user ~/.loop-harness/.env", os.path.join(data_dir, ".env")))
    files.append(("plugin shared/.env", os.path.join(HERE, ".env")))
    files.append(("user shared ~/.claude/skills/_shared/.env", os.path.join(user_shared_dir(data_dir), ".env")))
    return drop_repeated_paths(files)


def user_shared_dir(data_dir):
    """LOOP_USER_SHARED_DIR, then `user_shared_dir` of <data dir>/config.json, then ~/.claude/skills/_shared (as the app does)."""
    explicit = os.environ.get(SHARED_DIR_VARIABLE)
    if explicit:
        return explicit
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8-sig") as handle:
            configured = json.load(handle).get("user_shared_dir", "")
    except (OSError, ValueError, AttributeError):
        configured = ""
    if isinstance(configured, str) and configured.strip():
        return configured
    return os.path.join(os.path.expanduser("~"), *DEFAULT_SHARED_PARTS)


def drop_repeated_paths(files):
    """The same file under two labels is read once, under the higher-priority label."""
    seen, unique = set(), []
    for label, path in files:
        key = os.path.normcase(os.path.abspath(path))
        if key not in seen:
            seen.add(key)
            unique.append((label, path))
    return unique


def read_file(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return envload.parse(handle.read())
    except OSError:
        return {}


def add_test_users(values, sources):
    """Adds TEST_USER_<n>_EMAIL/PASSWORD/ROLE and TEST_USER_COUNT derived from the plural lists."""
    columns = {single: split_values(values.get("TEST_USER_" + plural, "")) for plural, single in USER_FIELDS}
    count = max((len(items) for items in columns.values()), default=0)
    for number in range(1, count + 1):
        for plural, single in USER_FIELDS:
            items = columns[single]
            if number <= len(items) and items[number - 1]:
                name = "TEST_USER_%d_%s" % (number, single)
                values[name] = items[number - 1]
                sources[name] = sources.get("TEST_USER_" + plural, "derived")
    values["TEST_USER_COUNT"] = str(count)
    sources["TEST_USER_COUNT"] = "derived"


def load_layers(project=None):
    """(values, sources): every key with the winning value and the label of the layer it came from."""
    values, sources = {}, {}
    for label, path in reversed(layer_files(project)):  # lowest priority first, so higher ones overwrite
        for key, value in read_file(path).items():
            if value:  # a blank value never hides a lower layer (same as the app)
                values[key], sources[key] = value, label
    for key, value in os.environ.items():
        if key in values or key.startswith(("TEST_", "LOOP_")):
            values[key], sources[key] = value, "environment"
    add_test_users(values, sources)
    return values, sources


def is_secret_name(name):
    return bool(SECRET_NAME.search(name))


def secret_values(values):
    return sorted({value for name, value in values.items() if is_secret_name(name) and len(value) >= MIN_SECRET_LENGTH},
                  key=len, reverse=True)


def redact(text, values):
    for secret in secret_values(values):
        text = text.replace(secret, MASK)
    return text


def referenced_names(project):
    """Every $NAME the project's TEST-ENVIRONMENT.md mentions."""
    try:
        with open(os.path.join(project, ENVIRONMENT_DOC_RELATIVE), encoding="utf-8") as handle:
            return sorted(set(NAME_REFERENCE.findall(handle.read())))
    except OSError:
        return []


def missing_names(project, names=None):
    values, _ = load_layers(project)
    wanted = names if names is not None else referenced_names(project)
    return [name for name in wanted if not values.get(name)]


def find_shell():
    for candidate in ("bash", "sh"):
        found = shutil.which(candidate)
        if found:
            return [found, "-c"]
    return [os.environ.get("COMSPEC", "cmd"), "/c"]


def command_list(project):
    values, sources = load_layers(project)
    for label, path in layer_files(project):
        print("%-28s %s %s" % (label, "found  " if os.path.isfile(path) else "missing", path))
    print()
    for name in sorted(values):
        if not name.startswith(("TEST_", "LOOP_SMTP", "LOOP_EMAIL", "LOOP_NOTIFY")) and sources[name] == "environment":
            continue
        shown = MASK if is_secret_name(name) and values[name] else values[name]
        print("  %-28s = %-32s (%s)" % (name, shown, sources[name]))
    return 0


def command_check(project, names):
    missing = missing_names(project, names)
    for name in missing:
        print("MISSING %s" % name)
    if not missing:
        print("ok: every name is defined")
    return 1 if missing else 0


def command_exec(project, command):
    values, _ = load_layers(project)
    environment = dict(os.environ)
    environment.update(values)
    done = subprocess.run(find_shell() + [command], env=environment, capture_output=True, text=True, errors="replace",
                          timeout=EXEC_TIMEOUT_SECONDS, cwd=project or None)
    sys.stdout.write(redact(done.stdout, values))
    sys.stderr.write(redact(done.stderr, values))
    return done.returncode


def main():
    parser = argparse.ArgumentParser(description="Test credentials: layers, test users, check and exec with redaction")
    parser.add_argument("action", choices=("list", "check", "exec"))
    parser.add_argument("command", nargs="?", default="", help="exec: the command line (single quotes)")
    parser.add_argument("--project", default="", help="project folder (default: none, user layers only)")
    parser.add_argument("--names", default="", help="check: comma-separated names to require")
    opts = parser.parse_args()
    project = os.path.abspath(opts.project) if opts.project else None
    if opts.action == "list":
        return command_list(project)
    if opts.action == "check":
        return command_check(project, [n.strip() for n in opts.names.split(",") if n.strip()] or None)
    if not opts.command:
        parser.error("exec needs the command line")
    return command_exec(project, opts.command)


if __name__ == "__main__":
    sys.exit(main())
