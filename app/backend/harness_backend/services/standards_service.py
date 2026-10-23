"""Standards slots of a repository or of one subproject: the `## Standards` section of a `CLAUDE.md` (STD-04).

A generic dev skill only knows the slot name; the company skill that fills it is bound here, in the project. This service holds the logic
of the `standards` routes, mirroring the Conformance ones: the same target resolution (primary or secondary repo, loop worktree when
there is one), the same refusal while a loop runs (409 `loop_running`) and, by rule R-03, no section at the root of a monorepo (it lives
in each subproject's `CLAUDE.md`). The file format is owned by `engine/standards.py`; nothing here parses or renders it.

  `get(folder)`                                           {"bindings", "notes", "slots"} of `<folder>/CLAUDE.md`
  `put(context, folder, bindings)`                        validates, refuses while a loop runs, writes, returns `get(folder)`
  `get_repo` / `put_repo(context, repo_name, ...)`        the repository's root (`repo_name` None = the project's own repo)
  `get_subproject` / `put_subproject(context, path, ...)` one subproject of a confirmed monorepo (`repo_name` as above)
"""
import os
import sys
from typing import Protocol

from ..domain.project_errors import InvalidPathError, InvalidRepoError, InvalidRequestError, RepoConflictError

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import standards as standards_engine  # noqa: E402
import subprojects as subprojects_engine  # noqa: E402

INVALID_STANDARDS_CODE = "invalid_standards"
MONOREPO_ROOT_STANDARDS_CODE = "monorepo_root_standards"
MONOREPO_ROOT_STANDARDS_MESSAGE = "this repository is a monorepo: bind the Standards slots per subproject"
FOLDER_MISSING_CODE = "folder_missing"


class InvalidStandardsError(InvalidRequestError):
    """An unknown slot, a value that is not a skill name or a body that is not a mapping (400, `code` = invalid_standards)."""

    def __init__(self, message, code=INVALID_STANDARDS_CODE):
        super().__init__(message)
        self.code = code


class IStandardsService(Protocol):
    def get(self, folder) -> dict: ...
    def put(self, context, folder, bindings) -> dict: ...
    def get_repo(self, context, repo_name=None) -> dict: ...
    def put_repo(self, context, repo_name, bindings) -> dict: ...
    def get_subproject(self, context, path, repo_name=None) -> dict: ...
    def put_subproject(self, context, path, bindings, repo_name=None) -> dict: ...


class StandardsService:
    def __init__(self, subprojects_service, log):
        self.subprojects_service = subprojects_service  # target resolution, the confirmed list and the loop-running check, as Conformance
        self.log = log

    # ------------------------------------------------ folder level
    def get(self, folder):
        found = standards_engine.read(folder)
        return {"bindings": found["bindings"], "notes": found["notes"], "slots": list(standards_engine.SLOTS)}

    def put(self, context, folder, bindings):
        """Replaces the bindings of `<folder>/CLAUDE.md` (an empty value clears its slot; an empty mapping removes the section)."""
        validated = self.validate(bindings)
        self.subprojects_service.ensure_loop_stopped(context)
        if not os.path.isdir(folder):
            raise InvalidRepoError("the folder does not exist: %s" % folder, code=FOLDER_MISSING_CODE)
        standards_engine.write(folder, validated)
        self.log.info("standards_set", folder=folder, slots=sorted(validated))
        return self.get(folder)

    @staticmethod
    def validate(bindings):
        """The mapping without its empty values; InvalidStandardsError (400, invalid_standards) for anything else the engine refuses."""
        if not isinstance(bindings, dict):
            raise InvalidStandardsError("bindings must be an object {slot: skill}")
        try:
            standards_engine.validate(bindings)
        except ValueError as error:
            raise InvalidStandardsError(str(error)) from error
        return {slot: str(value).strip() for slot, value in bindings.items() if str(value or "").strip()}

    # ------------------------------------------------ repository root
    def resolve_repo(self, context, repo_name):
        key, folder = self.subprojects_service.resolve_target(context, repo_name)
        return key, folder

    def is_confirmed_monorepo(self, context, key):
        entry = self.subprojects_service.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        return isinstance(entry, dict) and entry.get("monorepo") is True and entry.get("confirmed") is True

    def get_repo(self, context, repo_name=None):
        _, folder = self.resolve_repo(context, repo_name)
        return self.get(folder)

    def put_repo(self, context, repo_name, bindings):
        key, folder = self.resolve_repo(context, repo_name)
        if self.is_confirmed_monorepo(context, key):  # rule R-03: the section lives in each subproject, never in the root CLAUDE.md
            raise RepoConflictError(MONOREPO_ROOT_STANDARDS_MESSAGE, code=MONOREPO_ROOT_STANDARDS_CODE)
        return self.put(context, folder, bindings)

    # ------------------------------------------------ one subproject
    def resolve_subproject(self, context, path, repo_name):
        key, folder, entry = self.subprojects_service.require_confirmed(context, repo_name)
        project = self.subprojects_service.find_active_project(entry, path)
        relative = subprojects_engine.normalize(project["path"])
        project_folder = os.path.join(folder, *relative.split("/"))
        if not os.path.isdir(project_folder):
            raise InvalidPathError("the subproject folder does not exist: %s" % relative)
        return project_folder

    def get_subproject(self, context, path, repo_name=None):
        return self.get(self.resolve_subproject(context, path, repo_name))

    def put_subproject(self, context, path, bindings, repo_name=None):
        return self.put(context, self.resolve_subproject(context, path, repo_name), bindings)
