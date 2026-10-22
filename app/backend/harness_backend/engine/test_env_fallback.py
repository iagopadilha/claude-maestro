"""Default `.specs/project/.env.test` of a project that has a test environment (TEST-ENVIRONMENT.md): the project file is born
already filled with the TEST values the user keeps at machine level, so the loop can work with that one file.

Sources, highest priority first (the same layers `credential_check.file_layers` reads, minus the project file itself):
`~/.loop-harness/.env`, the plugin's `shared/.env`, the user-level shared `.env`. Only TEST keys are copied (`TEST_*`, LOCAL_URL,
ROUTE_PREFIX, ADMIN_TOKEN and the owner's test mail destination); notification, SMTP and production keys never are.
Idempotent: only names without a value in the project file are appended, an existing line is never touched, the file is
created when missing and `.gitignore` gets the line that keeps it out of git. Comments name the ORIGIN of each value (a label),
never the value. Stdlib only; nothing here returns or logs a value.
"""
import json
import os

import credential_check
from harness_backend.config import app_config

ENV_TEST_IGNORE_LINE = ".specs/project/.env.test"
TEST_KEY_PREFIX = "TEST_"
TEST_KEYS = ("LOCAL_URL", "ROUTE_PREFIX", "ADMIN_TOKEN", "NOTIFY_EMAIL_TO")
# project name -> machine-level names that carry the same thing (the owner's own destination, never SMTP settings)
ALIASES = {"NOTIFY_EMAIL_TO": ("LOOP_NOTIFY_TO", "IDATA_NOTIFY_TO")}
DERIVED_LABEL = "default derived from the local panel"
HEADER = ("# .specs/project/.env.test: THIS project's test values. SECRET: ignored by git, never commit, never paste into a document.\n"
          "# Same names the project's TEST-ENVIRONMENT.md mentions as $NAME. This file wins over ~/.loop-harness/.env and plugin/shared/.env.\n"
          "# Only DEV and localhost values. Lists use `;` (write \\; for a literal ;).\n")
QUOTE_NEEDED_CHARACTERS = set("#&%{}?^ \t$`\"'\\")


def is_test_key(name):
    return name.startswith(TEST_KEY_PREFIX) or name in TEST_KEYS


def format_line(name, value):
    """`NAME=value`, single-quoted when the .env format needs it; None when the value cannot be written on one line."""
    if not value or any(character in value for character in "\r\n\0"):
        return None
    if value[:1] in ("'", '"') or any(character in QUOTE_NEEDED_CHARACTERS for character in value):
        if "'" not in value:
            return "%s='%s'" % (name, value)
        if '"' not in value:
            return '%s="%s"' % (name, value)
        return None
    return "%s=%s" % (name, value)


def panel_local_url():
    """URL of the local panel: the live app's registry (`app.json`), else the first port of the default range."""
    try:
        with open(os.path.join(app_config.get_user_data_dir(), "app.json"), encoding="utf-8") as handle:
            url = json.load(handle).get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url.rstrip("/")
    except (OSError, ValueError, AttributeError):
        pass
    return "http://localhost:%d" % app_config.DEFAULT_PORT_RANGE[0]


def fallback_candidates(root):
    """[(name, value, origin label)] of the TEST values found below the project file, one per name (the highest layer wins)."""
    found, seen = [], set()
    for layer in credential_check.file_layers(root)[1:]:
        for name, value in layer.values.items():
            if value and name not in seen and is_test_key(name):
                seen.add(name)
                found.append((name, value, layer.label))
    for name, sources in ALIASES.items():
        if name in seen:
            continue
        for layer in credential_check.file_layers(root)[1:]:
            source = next((candidate for candidate in sources if layer.values.get(candidate)), None)
            if source:
                seen.add(name)
                found.append((name, layer.values[source], "%s (%s)" % (layer.label, source)))
                break
    return found


def read_text(path):
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return handle.read()
    except OSError:
        return ""


def ensure_ignored(root):
    path = os.path.join(root, ".gitignore")
    text = read_text(path)
    if ENV_TEST_IGNORE_LINE in {line.strip() for line in text.splitlines()}:
        return False
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(("" if not text or text.endswith("\n") else "\n") + ENV_TEST_IGNORE_LINE + "\n")
    return True


def ensure_env_test(root):
    """Creates/completes `<root>/.specs/project/.env.test` from the fallback layers. Returns {"created": bool, "added": [names]}
    (names only). Never overwrites a name the file already defines; does nothing when there is nothing to add."""
    path = os.path.join(root, *credential_check.PROJECT_ENV_RELATIVE)
    existed = os.path.isfile(path)
    current = read_text(path)
    defined = {name for name, value in credential_check.parse_env_file(path).items() if value}
    additions = [(name, value, origin) for name, value, origin in fallback_candidates(root) if name not in defined]
    if "LOCAL_URL" not in defined and not any(name == "LOCAL_URL" for name, _, _ in additions):
        additions.append(("LOCAL_URL", panel_local_url(), DERIVED_LABEL + "; change it when the app under test runs elsewhere"))
    lines, added = [], []
    for name, value, origin in additions:
        line = format_line(name, value)
        if line:
            lines.append("# %s: from %s" % (name, origin))
            lines.append(line)
            added.append(name)
    if not added:
        return {"created": False, "added": []}
    os.makedirs(os.path.dirname(path), exist_ok=True)
    separator = "" if not current or current.endswith("\n") else "\n"
    prefix = "" if existed and current.strip() else HEADER
    block = "# --- defaults added by the harness (only names this file did not define) ---\n" + "\n".join(lines) + "\n"
    with open(path, "a", encoding="utf-8", newline="") as handle:
        handle.write(separator + prefix + ("\n" if current.strip() else "") + block)
    ensure_ignored(root)
    return {"created": not existed, "added": added}
