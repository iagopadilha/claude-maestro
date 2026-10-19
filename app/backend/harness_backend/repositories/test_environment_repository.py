"""Files behind the Test environment tab: `.specs/project/TEST-ENVIRONMENT.md` (names only), `.specs/project/.env.test` (the
project's test VALUES), the user-level shared `.env` and `.specs/project/ACCESS.json`. Values only ever flow INTO a file here;
nothing in this class returns one, and no error message carries one. `.env.test` and the shared file are written only through
this class (never by the planning AI).

Writing a `.env` keeps every line that is not a `KEY=value` of a name being saved byte for byte (comments, blank lines, other
keys, the file's line endings and BOM): only the matching line is replaced (the last one, which is the one the parser reads) and
new names are appended. The write is atomic (temporary file in the same folder, then replace).
"""
import os
import re
import tempfile

from ..domain.project_errors import InvalidRequestError
from .specs_file_repository import ENGINE_DIR  # noqa: F401  (puts the engine folder on sys.path)

import access_settings  # noqa: E402
import credential_check  # noqa: E402

DOCUMENT_RELATIVE = (".specs", "project", "TEST-ENVIRONMENT.md")
ENV_TEST_RELATIVE = credential_check.PROJECT_ENV_RELATIVE
QUOTE_NEEDED = re.compile(r"[\s#&%{}?^]")
ENV_LINE_KEY = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")
VALUE_LIMIT_CHARACTERS = 500
BYTE_ORDER_MARK = "﻿"


def format_env_line(name, value):
    """`NAME=value`, quoted when the .env format needs it (a quoted value is read literally). The refusals never quote the value."""
    if not isinstance(value, str) or not value.strip():
        raise InvalidRequestError("the value of %s is empty" % name)
    if "\n" in value or "\r" in value or "\0" in value:
        raise InvalidRequestError("the value of %s has a line break: values are a single line" % name)
    if len(value) > VALUE_LIMIT_CHARACTERS:
        raise InvalidRequestError("the value of %s is longer than %d characters" % (name, VALUE_LIMIT_CHARACTERS))
    if value[:1] in ("'", '"') or QUOTE_NEEDED.search(value):
        if "'" not in value:
            return "%s='%s'" % (name, value)
        if '"' not in value:
            return '%s="%s"' % (name, value)
        raise InvalidRequestError("the value of %s has spaces or symbols and both kinds of quote: it cannot be stored" % name)
    return "%s=%s" % (name, value)


def detect_line_ending(text):
    for ending in ("\r\n", "\n", "\r"):
        if ending in text:
            return ending
    return "\n"


def replace_env_lines(text, lines_by_name):
    """`text` with the last `NAME=...` line of each name replaced and the missing names appended; everything else is untouched."""
    lines = text.splitlines(keepends=True)
    ending = detect_line_ending(text)
    last_index = {}
    for index, raw in enumerate(lines):
        found = ENV_LINE_KEY.match(raw.lstrip(BYTE_ORDER_MARK))
        if found and found.group(1) in lines_by_name:
            last_index[found.group(1)] = index
    appended = []
    for name, new_line in lines_by_name.items():
        if name in last_index:
            raw = lines[last_index[name]]
            body = raw.rstrip("\r\n")
            lines[last_index[name]] = (BYTE_ORDER_MARK if body.startswith(BYTE_ORDER_MARK) else "") + new_line + raw[len(body):]
        else:
            appended.append(new_line)
    result = "".join(lines)
    if appended:
        if result and not result.endswith(("\n", "\r")):
            result += ending
        result += "".join(line + ending for line in appended)
    return result


class TestEnvironmentRepository:
    __test__ = False  # not a test class for pytest

    def __init__(self, specs_file_repository):
        self.specs_file_repository = specs_file_repository

    def read_document(self, work_dir):
        """Text of TEST-ENVIRONMENT.md or None."""
        return self.specs_file_repository.read_text(os.path.join(work_dir, *DOCUMENT_RELATIVE))

    def write_project_values(self, work_dir, values):
        """Writes only `.specs/project/.env.test` (created when missing). `values` = {NAME: value}."""
        self.write_env_values(os.path.join(work_dir, *ENV_TEST_RELATIVE), values)

    def write_shared_values(self, values):
        """Writes only the user-level shared `.env` (its folder and file are created when missing)."""
        self.write_env_values(credential_check.user_shared_env_path(), values)

    @staticmethod
    def write_env_values(path, values):
        lines = {name: format_env_line(name, value) for name, value in values.items()}  # validates every value before touching the file
        try:
            with open(path, "rb") as handle:
                current = handle.read().decode("utf-8")
        except FileNotFoundError:
            current = ""
        except UnicodeDecodeError:
            raise InvalidRequestError("the env file is not UTF-8 text: fix it by hand first")
        folder = os.path.dirname(path)
        os.makedirs(folder, exist_ok=True)
        handle, temporary = tempfile.mkstemp(prefix=".env.", suffix=".tmp", dir=folder)  # owner-only permission on the temporary file
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(replace_env_lines(current, lines).encode("utf-8"))
            os.replace(temporary, path)
        except BaseException:
            try:
                os.remove(temporary)
            except OSError:
                pass
            raise

    @staticmethod
    def read_access(work_dir):
        return access_settings.read_access(work_dir)

    @staticmethod
    def write_access(work_dir, access):
        return access_settings.write_access(work_dir, access)
