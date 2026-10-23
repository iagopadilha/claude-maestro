"""Test environment tab: which `$NAME` the project's TEST-ENVIRONMENT.md needs, where each one is defined (never its value), saving
values (project `.env.test` or the user-level shared `.env`), and the computed `access` card (credential layers + MCP read servers).

Layers, same order as the toolkit's `testenv.py`: real environment variable > project `.specs/project/.env.test` > `~/.loop-harness/.env` >
toolkit `shared/.env` > user-level shared `.env` (only when `user_shared_dir` is set in config.json). Every layer is always read (no
toggle); a name that only a shared file defines has the source `shared`. MCP access is computed from `~/.claude.json` (`mcp_access`), read tools only.

Secrets rule: values are written, never read back into a response, a log line, a file the AI reads or an error message.
"""
import json
import os
import threading
from typing import Protocol

from ..domain.project_errors import InvalidRequestError, UnknownCredentialNameError
from ..repositories.specs_file_repository import ENGINE_DIR  # noqa: F401  (puts the engine folder on sys.path)
from .planning_gates import list_fill_in_lines

import credential_check  # noqa: E402
import mcp_access  # noqa: E402

TARGET_PROJECT = "project"
TARGET_SHARED = "shared"
TARGETS = (TARGET_PROJECT, TARGET_SHARED)
SOURCES_THAT_COUNT = ("env", "project", "machine", "shared")
CREDENTIAL_NAMES_PARTS = (".specs", "project", "loop", "internal", "credential-names.json")
UNKNOWN_NAME_CODE = "unknown_name"
MAX_VALUES_PER_REQUEST = 100


class ITestEnvironmentService(Protocol):
    def get_test_environment(self, context) -> dict: ...
    def save_values(self, context, values, target) -> dict: ...
    def write_credential_names_file(self, context) -> str: ...


class TestEnvironmentService:
    __test__ = False  # not a test class for pytest

    def __init__(self, test_environment_repository, specs_structure_service, log):
        self.test_environment_repository = test_environment_repository
        self.specs_structure_service = specs_structure_service
        self.log = log
        self.write_lock = threading.Lock()
        self.after_change = None  # fn(context), set by the projects module: capabilities block + readiness check after new values

    # ------------------------------------------------ read
    def describe_names(self, context):
        """([{name, source, set, secret, path_label}], resolution): one row per `$NAME` the document mentions (names inside comments
        too, like the preflight). Never a value."""
        text = self.test_environment_repository.read_document(context.work_dir) or ""
        resolution = credential_check.Resolution(context.work_dir)
        rows = []
        for name in credential_check.mentioned_names(text):
            source, label = resolution.lookup(name)
            rows.append({"name": name, "source": source, "set": source in SOURCES_THAT_COUNT,
                         "secret": credential_check.is_secret_name(name), "path_label": label})
        return rows, resolution, text

    def get_test_environment(self, context):
        rows, resolution, text = self.describe_names(context)
        fill_in = list_fill_in_lines(text)
        ready = not fill_in and all(row["source"] in SOURCES_THAT_COUNT for row in rows)
        try:
            self.test_environment_repository.migrate_legacy_access(context.work_dir)  # legacy ACCESS.json keys are dropped
        except OSError:
            pass
        return {"ok": True, "ready": ready, "names": rows, "fill_in": fill_in,
                "access": self.describe_access(resolution, rows),
                "shared_paths": resolution.shared_files_found()}

    @staticmethod
    def describe_access(resolution, rows):
        """The Access card: `credential_layers` [{name: project|machine|shared, label, path, exists, names: [names of the document each
        layer defines, with their source resolved by the highest layer]}] and `mcp` [{server, tools, configured}]. Never a value."""
        document_names = [row["name"] for row in rows]
        layers = resolution.layer_report(document_names)
        winner = {row["name"]: row["source"] for row in rows}
        for layer in layers:
            layer["names"] = [{"name": name, "source": winner.get(name, "missing"), "winning": winner.get(name) == layer["name"]}
                              for name in layer["names"]]
        return {"credential_layers": layers, "mcp": mcp_access.describe_servers()}

    # ------------------------------------------------ values
    def save_values(self, context, values, target):
        if target not in TARGETS:
            raise InvalidRequestError("target must be 'project' or 'shared'")
        if not isinstance(values, dict) or not values:
            raise InvalidRequestError("values must be an object {NAME: value} with at least one name")
        if len(values) > MAX_VALUES_PER_REQUEST:
            raise InvalidRequestError("too many values in one request (at most %d)" % MAX_VALUES_PER_REQUEST)
        known = set(credential_check.mentioned_names(self.test_environment_repository.read_document(context.work_dir) or ""))
        unknown = sorted(str(name) for name in values if name not in known)
        if unknown:
            raise UnknownCredentialNameError("not a name of TEST-ENVIRONMENT.md: %s" % ", ".join(unknown[:8]), UNKNOWN_NAME_CODE)
        with self.write_lock:
            if target == TARGET_PROJECT:
                self.test_environment_repository.write_project_values(context.work_dir, values)
                self.specs_structure_service.ensure_env_files_in_gitignore(context.repo_path)  # keeps .env.test out of git
            else:
                if not credential_check.user_shared_env_path():
                    raise InvalidRequestError("no user-level shared folder: set `user_shared_dir` in the app's config.json first")
                self.test_environment_repository.write_shared_values(values)
        saved = list(values)
        self.log.info("test_environment_values_saved", project=context.project.id, target=target, names=saved)  # names only
        if self.after_change is not None:
            self.after_change(context)
        return {"ok": True, "saved": saved}

    # ------------------------------------------------ planning AI
    def write_credential_names_file(self, context):
        """`.specs/project/loop/internal/credential-names.json`: [{name, source, set}] WITHOUT values, for the planning AI to read
        (it must never ask for or write a value). Not an editable path of the planning chat. Returns the file path."""
        rows, _, _ = self.describe_names(context)
        path = os.path.join(context.work_dir, *CREDENTIAL_NAMES_PARTS)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        content = [{"name": row["name"], "source": row["source"], "set": row["set"]} for row in rows]
        temporary = path + ".tmp"
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(content, handle, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
        return path
