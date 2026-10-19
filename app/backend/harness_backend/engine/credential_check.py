"""Preflight checks of the test credentials convention (the same one as the plugin's `shared/testenv.py`): TEST-ENVIRONMENT.md
holds NAMES only; the values live in `.env` files git ignores. Pure functions plus one file read; stdlib only.

Names are resolved in the SAME order as the skills' `testenv.py` (first wins): a real environment variable, `<project>/.specs/project/.env.test`,
`~/.loop-harness/.env`, the plugin's `shared/.env`, the user-level shared `~/.claude/skills/_shared/.env`. The last two are the SHARED
fallback (the user's generic DEV account): they count ONLY when the project accepted them (`.specs/project/ACCESS.json`
`shared_fallback: true`); a name found only there without acceptance has the source `shared-blocked` and stops the run like a missing one.
Test users are `;`-separated lists (TEST_USER_EMAILS / TEST_USER_PASSWORDS / TEST_USER_ROLES) that also become TEST_USER_<n>_EMAIL /
_PASSWORD / _ROLE. Nothing here returns or logs a value: callers get names, sources and labels.
"""
import os
import re

from app_paths import envload
from harness_backend.config import app_config
import access_settings

PROJECT_ENV_RELATIVE = (".specs", "project", ".env.test")
SHARED_DIR_VARIABLE = "LOOP_USER_SHARED_DIR"  # tests point the user-level shared folder at a temporary one
DEFAULT_SHARED_PARTS = (".claude", "skills", "_shared")
ENVIRONMENT_LABEL = "environment variable"
PROJECT_LABEL = "project .env.test"
MACHINE_LABEL = "~/.loop-harness/.env"
PLUGIN_SHARED_LABEL = "plugin shared/.env"
USER_SHARED_LABEL = "~/.claude/skills/_shared/.env"
LIST_SEPARATOR = ";"
ESCAPED_SEPARATOR = "\\;"
USER_COLUMNS = (("EMAILS", "EMAIL"), ("PASSWORDS", "PASSWORD"), ("ROLES", "ROLE"))
NAME_REFERENCE = re.compile(r"\$\{?([A-Z][A-Z0-9_]{2,})\}?")
SECRET_NAME_WORDS = r"(?:password|passwd|senha|secret|token|api[_-]?key|apikey|bearer)"
# `password: abc123`, `token = abc123`, `Bearer eyJ...`: a value written after a secret-looking word, not a $NAME or ***
PLAINTEXT_SECRET = re.compile(
    r"(?i)\b" + SECRET_NAME_WORDS + r"\b[`*\s]*([:=|])[`*\s]*([^\s|`*]{4,})|\bbearer\s+([A-Za-z0-9._~+/=-]{12,})")
HARMLESS_VALUE = re.compile(r"^(\$|\*{3}|FILL_IN|<|\(|none$|name$|value$)", re.I)
MAX_REPORTED_NAMES = 8
PLACEHOLDER_WORDS = frozenset({"NAME", "NAMES"})  # literal words of the template prose (case-sensitive), never credential names
SECRET_NAME = re.compile(r"(?i)(token|secret|passw|credential|private_key|api_?key|auth|pwd)")  # same rule as testenv.py: shown as ***


def split_values(text):
    parts = text.replace(ESCAPED_SEPARATOR, "\0").split(LIST_SEPARATOR) if text else []
    return [part.replace("\0", LIST_SEPARATOR).strip() for part in parts]


def parse_env_file(path):
    """{name: value} of one `.env` style file ({} when it does not exist). Values never leave the caller."""
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8-sig") as handle:
            return envload.parse(handle.read())
    except OSError:
        return {}


def read_project_values(root):
    return parse_env_file(os.path.join(root, *PROJECT_ENV_RELATIVE))


def user_shared_dir():
    """Folder of the user-level shared `.env` (layer 4 of the skills' testenv.py): LOOP_USER_SHARED_DIR (tests), then
    `user_shared_dir` of `<data dir>/config.json`, then the default `~/.claude/skills/_shared`."""
    explicit = os.environ.get(SHARED_DIR_VARIABLE)
    if explicit:
        return explicit
    configured = app_config.read_user_config(app_config.get_user_data_dir()).get("user_shared_dir", "")
    return configured if isinstance(configured, str) and configured.strip() else os.path.join(os.path.expanduser("~"), *DEFAULT_SHARED_PARTS)


def user_shared_env_path():
    return os.path.join(user_shared_dir(), ".env")


class Layer:
    """One `.env` file of the stack. `shared` layers only count when the project accepted the shared fallback."""

    def __init__(self, source, label, path, shared):
        self.source, self.label, self.path, self.shared = source, label, path, shared
        self.values = parse_env_file(path)

    @property
    def found(self):
        return os.path.isfile(self.path) if self.path else False


def file_layers(root):
    """Layers below the real environment, highest priority first: project .env.test, ~/.loop-harness/.env, plugin shared/.env,
    user-level shared .env (the last two are `shared`)."""
    candidates = envload.candidates()
    machine_path = candidates[0] if candidates else ""
    plugin_path = candidates[1] if len(candidates) > 1 and not envload.get_explicit_file() else ""
    layers = [Layer("project", PROJECT_LABEL, os.path.join(root, *PROJECT_ENV_RELATIVE), False),
              Layer("machine", MACHINE_LABEL, machine_path, False)]
    if plugin_path:
        layers.append(Layer("shared", PLUGIN_SHARED_LABEL, plugin_path, True))
    layers.append(Layer("shared", USER_SHARED_LABEL, user_shared_env_path(), True))
    return layers


def add_test_users(values, origins):
    """TEST_USER_<n>_EMAIL/PASSWORD/ROLE and TEST_USER_COUNT derived from the `;` lists (origin = the list's origin)."""
    columns = {single: split_values(values.get("TEST_USER_" + plural, "")) for plural, single in USER_COLUMNS}
    count = max((len(items) for items in columns.values()), default=0)
    for number in range(1, count + 1):
        for plural, single in USER_COLUMNS:
            items = columns[single]
            if number <= len(items) and items[number - 1]:
                name = "TEST_USER_%d_%s" % (number, single)
                values[name] = items[number - 1]
                origins[name] = origins.get("TEST_USER_" + plural)
    values["TEST_USER_COUNT"] = str(count)
    origins["TEST_USER_COUNT"] = origins.get("TEST_USER_EMAILS")


def real_environment(layers):
    """Real environment variables with a value. `envload.load()` copies `.env` files into os.environ (without overwriting), so a
    value identical to a FILE layer's is that file's value, not a variable the user set: it keeps the file's layer (and, for a
    shared file, its acceptance rule) instead of jumping to the top."""
    file_values = [layer.values for layer in layers if layer.source != "project"]
    return {name: value for name, value in os.environ.items()
            if value and not any(values.get(name) == value for values in file_values)}


def stack_values(layers, environment):
    """(values, origins) of a stack of layers (highest priority first) under the real environment. origin = (source, label)."""
    values, origins = {}, {}
    for layer in reversed(layers):  # lowest first, so higher layers overwrite; a blank value never hides a lower one
        for name, value in layer.values.items():
            if value:
                values[name], origins[name] = value, (layer.source, layer.label)
    for name, value in environment.items():
        values[name], origins[name] = value, ("env", ENVIRONMENT_LABEL)
    add_test_users(values, origins)
    return values, origins


class Resolution:
    """Where every name comes from for one project. `lookup(name)` -> (source, label); never exposes a value."""

    def __init__(self, root, accept_shared=None):
        self.layers = file_layers(root)
        self.accept_shared = access_settings.read_access(root)[access_settings.SHARED_FALLBACK_KEY] if accept_shared is None else bool(accept_shared)
        environment = real_environment(self.layers)
        counted = [layer for layer in self.layers if self.accept_shared or not layer.shared]
        self.values, self.origins = stack_values(counted, environment)
        self.shared_values, self.shared_origins = stack_values([layer for layer in self.layers if layer.shared], {})

    def lookup(self, name):
        if self.values.get(name) and self.origins.get(name):
            return self.origins[name]
        if not self.accept_shared and self.shared_values.get(name):
            return ("shared-blocked", self.shared_origins[name][1] if self.shared_origins.get(name) else "")
        return ("missing", "")

    def names_defined(self):
        return {name for name, value in self.values.items() if value}

    def shared_files_found(self):
        return [layer.label for layer in self.layers if layer.shared and layer.found]


def defined_names(root):
    """Names that have a non-empty value in some counted layer, including the derived TEST_USER_<n>_* names."""
    return Resolution(root).names_defined()


def is_secret_name(name):
    return bool(SECRET_NAME.search(name))


def mentioned_names(document_text):
    """Every `$NAME` the document mentions, except the documentation placeholders `$NAME` / `$NAMES` of the template header."""
    return sorted(set(NAME_REFERENCE.findall(document_text)) - PLACEHOLDER_WORDS)


def names_by_source(root, document_text):
    """{"missing": [...], "shared-blocked": [...]} of the names the document mentions (the ones that stop a run)."""
    resolution = Resolution(root)
    stopped = {"missing": [], "shared-blocked": []}
    for name in mentioned_names(document_text):
        source = resolution.lookup(name)[0]
        if source in stopped:
            stopped[source].append(name)
    return stopped


def undefined_names(root, document_text):
    """Mentioned names with no value anywhere (a name only the not-accepted shared fallback defines is `blocked_names`)."""
    return names_by_source(root, document_text)["missing"]


def blocked_names(root, document_text):
    return names_by_source(root, document_text)["shared-blocked"]


def plaintext_secret_lines(document_text):
    """1-based line numbers where a secret-looking word is followed by a literal value (never the value itself)."""
    found = []
    for number, line in enumerate(document_text.splitlines(), 1):
        for match in PLAINTEXT_SECRET.finditer(line):
            value = match.group(2) or match.group(3) or ""
            if match.group(1) == "|" and not any(char.isdigit() for char in value):
                continue  # a table cell next to a secret-looking word ("| Access token | obtained from login |") is prose, not a value
            if value and not HARMLESS_VALUE.match(value):
                found.append(number)
                break
    return found


def credential_problems(root, document_text):
    """Problems for the preflight (empty = fine). Reports names and line numbers, never a value."""
    problems = []
    lines = plaintext_secret_lines(document_text)
    if lines:
        problems.append(".specs/project/TEST-ENVIRONMENT.md has a plaintext secret on line(s) %s: write only the $NAME and put the "
                        "value in .specs/project/.env.test (or ~/.loop-harness/.env), which git ignores" % ", ".join(map(str, lines[:MAX_REPORTED_NAMES])))
    stopped = names_by_source(root, document_text)
    if stopped["missing"]:
        problems.append("TEST-ENVIRONMENT.md mentions names no .env defines: %s (define it in .specs/project/.env.test or ~/.loop-harness/.env)"
                        % ", ".join("$" + name for name in stopped["missing"][:MAX_REPORTED_NAMES]))
    if stopped["shared-blocked"]:
        problems.append("TEST-ENVIRONMENT.md uses names only the shared fallback account defines: %s (accept the shared fallback in the Test "
                        "environment tab, or define it in .specs/project/.env.test)"
                        % ", ".join("$" + name for name in stopped["shared-blocked"][:MAX_REPORTED_NAMES]))
    return problems
