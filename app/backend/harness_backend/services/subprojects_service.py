"""Subprojects of a monorepo: the stored list (`.specs/project/SUBPROJECTS.json`), its edition and the Conformance of each one.

The file holds only what a person or the AI decided (list, skip, name, stack, confirmation). Everything else is derived from disk on
every read: the `Conformance:` line of `<path>/CLAUDE.md`, the map documents of `<path>/docs/codebase` and the leftovers of the old
single-project flow in the root. Nothing is mapped or written before the user confirms the list; the root `CLAUDE.md` of a confirmed
monorepo is never written; the only exception is the removal of the Conformance block an earlier version of the app left there
(between its markers, when the list is confirmed; text outside the markers is never touched).

The folder read and written is the work folder: the loop worktree when it exists, else the repository (like `repo_readiness_service`).
`SUBPROJECTS.json` is keyed by repository name, so a repository of `REPOS.json` can be a monorepo too (`repo_name`, default = primary).

Extension points for later tasks (all optional, a failing callback is logged and never breaks the operation):
  `add_after_list_confirmed(callback)`       callback(context, repo_name, folder, entry)  after Confirm list stored the entry
  `add_after_conformance_written(callback)`  callback(context, repo_name, folder, project_path, value)  after a Conformance write
`describe` builds each row in `build_project_row` and the whole answer in `build_description`: other tasks extend those two.
"""
import datetime
import os
import re
import sys
from typing import Protocol

from ..domain.project_errors import InvalidPathError, InvalidRequestError, LoopRunningError, RepoConflictError
from ..repositories.specs_file_repository import run_hints  # also puts the engine folder on sys.path
from ..repositories.subprojects_file_repository import SubprojectsFileError
from .code_inventory_service import inventory_name

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprojects as subprojects_engine  # noqa: E402

ALLOWED_STACKS = ("dotnet", "nestjs", "react", "node", "python", "go", "java", "rust", "php", "ruby", "unknown")
VALIDATED_BY_SCRIPT = "script"
VALIDATED_BY_USER = "user"
SUBPROJECTS_NOT_CONFIRMED_CODE = "subprojects_not_confirmed"
SUBPROJECTS_NOT_CONFIRMED_MESSAGE = "confirm the list of subprojects first"
LIST_CONFIRMED_CODE = "list_confirmed"
LOOP_RUNNING_CODE = "loop_running"
ROOT_CONFORMANCE_BEGIN = "<!-- CONFORMANCE:BEGIN -->"  # the markers of the block the app itself wrote (engine/classify_structure.py)
ROOT_CONFORMANCE_END = "<!-- CONFORMANCE:END -->"
ROOT_CONFORMANCE_BLOCK_PATTERN = re.compile(re.escape(ROOT_CONFORMANCE_BEGIN) + r".*?" + re.escape(ROOT_CONFORMANCE_END) + r"\n?", re.S)
MAP_RELATIVE_FOLDER = "docs/codebase"
MISSING_CONFORMANCE = "conformance"
MISSING_MAP = "map"
MISSING_FOLDER = "folder"


class SubprojectsNotConfirmedError(InvalidRequestError):
    """A subproject action before the list is confirmed. The controller answers 422 (it must be listed before InvalidRequestError)."""

    def __init__(self, message=SUBPROJECTS_NOT_CONFIRMED_MESSAGE):
        super().__init__(message)
        self.code = SUBPROJECTS_NOT_CONFIRMED_CODE


class ISubprojectsService(Protocol):
    def describe(self, context, repo_name=None) -> dict: ...
    def detect_again(self, context, repo_name=None, force=False) -> dict: ...
    def update_list(self, context, payload, repo_name=None) -> dict: ...
    def set_conformance(self, context, path, value, repo_name=None) -> dict: ...
    def set_conformance_all(self, context, repo_name=None) -> dict: ...


def now_text():
    return datetime.datetime.now().astimezone().isoformat(timespec="seconds")


class SubprojectsService:
    def __init__(self, subprojects_file_repository, specs_structure_service, repo_registry_service, loop_status_service, log,
                 after_list_confirmed=None, after_conformance_written=None, code_inventory_service=None, map_queue_reader=None):
        self.subprojects_file_repository = subprojects_file_repository
        self.specs_structure_service = specs_structure_service
        self.repo_registry_service = repo_registry_service
        self.loop_status_service = loop_status_service
        self.log = log
        self.after_list_confirmed_callbacks = list(after_list_confirmed or [])
        self.after_conformance_written_callbacks = list(after_conformance_written or [])
        self.code_inventory_service = code_inventory_service  # optional: giant file counts of the last inventory per row
        self.map_queue_reader = map_queue_reader  # optional callable(context, repo_name) -> queue dict | None (SubprojectMapService.read_queue)

    def add_after_list_confirmed(self, callback):
        self.after_list_confirmed_callbacks.append(callback)

    def add_after_conformance_written(self, callback):
        self.after_conformance_written_callbacks.append(callback)

    def attach_repo_registry_service(self, repo_registry_service):
        """The registry that resolves `?repo=<name>` to a secondary repo folder (built after the projects module, so set later)."""
        self.repo_registry_service = repo_registry_service

    def attach_context_block_service(self, context_block_service):
        """Confirm list rewrites the `maestro:context` block of every subproject; a Conformance write rewrites that subproject's."""
        self.add_after_list_confirmed(
            lambda context, key, folder, entry: context_block_service.refresh_repo(folder, key, subprojects_root=context.work_dir))
        self.add_after_conformance_written(
            lambda context, key, folder, path, value: context_block_service.refresh_folder(folder, key, path,
                                                                                           subprojects_root=context.work_dir))

    # ------------------------------------------------ target (which repository, which folders)
    def resolve_target(self, context, repo_name=None):
        """(key in SUBPROJECTS.json, work folder of the repository). The file itself always lives in the primary's work dir."""
        primary_name = os.path.basename(os.path.normpath(context.repo_path))
        if not repo_name or repo_name == primary_name:
            return primary_name, context.work_dir
        repo = self.repo_registry_service.resolve(context, repo_name)
        if repo.primary:
            return primary_name, context.work_dir
        worktree = repo.loop_worktree_path()
        return repo.name, worktree if os.path.isdir(worktree) else repo.path

    def ensure_loop_stopped(self, context):
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("a loop is running in this project: stop it first", code=LOOP_RUNNING_CODE)

    # ------------------------------------------------ read
    def describe(self, context, repo_name=None):
        """The table of the design (see `build_description`). Detects and stores the list when the repository has no entry yet
        (DET-01). Never raises for a bad file: `error` is filled and the repository is treated as a single project."""
        key, folder = self.resolve_target(context, repo_name)
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(stored, SubprojectsFileError):
            return self.build_description(key, folder, None, error=stored.error)
        if stored is None and os.path.isdir(folder):
            stored = self.detect_and_store(context, key, folder)
        return self.attach_map_queue(self.build_description(key, folder, stored), context, repo_name)

    def attach_map_queue(self, description, context, repo_name):
        """Fills `queue` and, for the path being mapped now, the row's `map.running` / `map.job` (no reader or a failing one = unchanged)."""
        if self.map_queue_reader is None:
            return description
        try:
            queue = self.map_queue_reader(context, repo_name)
        except Exception as failure:  # the queue is a nicety of the table: never take it down
            self.log.warn("subprojects_queue_unreadable", type=type(failure).__name__, message=str(failure)[:200])
            return description
        description["queue"] = queue
        description["map_queue"] = queue  # the name of the installer contract: same payload
        items = queue.get("items") if isinstance(queue, dict) else None
        legacy_current = queue.get("current") if isinstance(queue, dict) and queue.get("state") == "running" else None
        for row in description["projects"]:
            item = (items or {}).get(row["path"])
            if (isinstance(item, dict) and item.get("state") == "running") or row["path"] == legacy_current:
                row["map"]["running"] = True
                row["map"]["job"] = {"path": row["path"], "state": "running", "queue_state": queue.get("state")}
        return description

    def build_description(self, key, folder, entry, error=""):
        entry = entry if isinstance(entry, dict) else None
        monorepo = bool(entry and entry.get("monorepo") is True)
        confirmed = bool(entry and entry.get("confirmed") is True)
        projects = [self.build_project_row(folder, project) for project in subprojects_engine.entry_projects(entry)] if monorepo else []
        confirmed_monorepo = monorepo and confirmed
        return {"ok": True, "repo": key, "monorepo": monorepo, "confirmed": confirmed,
                "validated_by": (entry or {}).get("validated_by", ""), "validated_at": (entry or {}).get("validated_at", ""),
                "confirmed_at": (entry or {}).get("confirmed_at", ""), "reason": (entry or {}).get("reason", ""),
                "projects": projects, "rejected": list((entry or {}).get("rejected") or []), "notes": (entry or {}).get("notes") or [],
                "differences": list((entry or {}).get("differences") or []),  # what the AI validation changed (VAL-03)
                "validation_error": (entry or {}).get("validation_error", ""),
                "root_conformance_leftover": confirmed_monorepo and self.root_has_conformance(folder),
                "root_map_leftover": confirmed_monorepo and self.root_has_old_map(folder),
                "error": error, "job": None, "queue": None, "map_queue": None}

    def build_project_row(self, folder, project):
        path = subprojects_engine.normalize(project["path"])
        project_folder = os.path.join(folder, *path.split("/"))
        exists = os.path.isdir(project_folder)
        skip = project.get("skip") is True
        has_code = project.get("has_code") is True
        conformance = self.specs_structure_service.get_conformance(project_folder) if exists else None
        suggestion = None
        if exists and not conformance and not skip:
            try:
                suggestion = self.specs_structure_service.suggest_conformance(project_folder)
            except Exception as failure:  # a hint only: it never takes the table down
                self.log.warn("subproject_conformance_suggestion_failed", path=path, type=type(failure).__name__)
        map_folder = os.path.join(project_folder, *MAP_RELATIVE_FOLDER.split("/"))
        map_files = run_hints.list_map_documents(map_folder) if exists else []
        missing = []
        if not skip:
            if not exists:
                missing.append(MISSING_FOLDER)
            else:
                if not conformance:
                    missing.append(MISSING_CONFORMANCE)
                if has_code and not map_files:
                    missing.append(MISSING_MAP)
        giants = self.read_giants(folder, path)
        return {"path": path, "name": project.get("name") or path, "stack": project.get("stack") or "unknown",
                "skill": project.get("skill") or "", "skip": skip, "reason": project.get("reason") or "", "has_code": has_code,
                "warning": project.get("warning") or "", "exists": exists, "conformance": conformance, "suggestion": suggestion,
                "map": {"files": map_files, "folder": "%s/%s" % (path, MAP_RELATIVE_FOLDER),
                        "commit": run_hints.read_mapped_commit(map_folder) if exists else None, "running": False, "job": None},
                "giant_files": giants["count"], "giant": giants["top"], "missing": missing, "ready": skip or (exists and not missing)}

    def read_giants(self, folder, path):
        """{count, top (5 x {path, lines})} from the last inventory of the subproject; zeros when there is none or no service."""
        empty = {"count": 0, "top": []}
        if self.code_inventory_service is None:
            return empty
        try:
            name = inventory_name(path)
            return {"count": self.code_inventory_service.giant_count(folder, name), "top": self.code_inventory_service.giant_top(folder, name)}
        except Exception as failure:  # a hint of the table only
            self.log.warn("subproject_inventory_unreadable", path=path, type=type(failure).__name__)
            return empty

    @staticmethod
    def root_has_old_map(folder):
        """Old single-project flow left map documents in the root `docs/codebase`: anything but `SYSTEM.md` (the monorepo's own
        system map) and the `README.md` index (never listed as a document)."""
        documents = run_hints.list_map_documents(os.path.join(folder, *MAP_RELATIVE_FOLDER.split("/")))
        return any(name != run_hints.SYSTEM_DOCUMENT_NAME for name in documents)

    def root_has_conformance(self, folder):
        """True while the root CLAUDE.md still has the app-written block between its markers (it is removed on confirm)."""
        text = self.specs_structure_service.specs_file_repository.read_text(os.path.join(folder, "CLAUDE.md")) or ""
        return bool(ROOT_CONFORMANCE_BLOCK_PATTERN.search(text))

    def remove_root_conformance_block(self, folder):
        """Removes the app-written Conformance block (markers included) from the root CLAUDE.md of a monorepo; everything outside
        the markers stays as it was (only the blank line the app added before the block goes with it). True when it was removed."""
        repository = self.specs_structure_service.specs_file_repository
        path = os.path.join(folder, "CLAUDE.md")
        text = repository.read_text(path)
        match = ROOT_CONFORMANCE_BLOCK_PATTERN.search(text) if text else None
        if match is None:
            return False
        before, after = text[:match.start()], text[match.end():]
        if after.strip("\r\n") == "":
            before, after = before.rstrip("\r\n"), ""
            before += "\n" if before else ""
        elif before.endswith("\n\n") and after.startswith("\n"):
            after = after[1:]
        repository.write_text_atomic(path, before + after)
        return True

    # ------------------------------------------------ detection
    def detect_and_store(self, context, key, folder):
        """Runs the script detection and stores it unconfirmed (also for a non-monorepo: `monorepo:false, confirmed:false`)."""
        result = subprojects_engine.detect(folder)
        entry = {"monorepo": bool(result["monorepo"]), "confirmed": False, "validated_by": VALIDATED_BY_SCRIPT,
                 "validated_at": now_text(), "reason": result.get("reason", ""), "projects": list(result["projects"]),
                 "rejected": [], "notes": list(result.get("warnings") or [])}
        if not os.path.isdir(os.path.join(context.work_dir, ".specs")):  # a read never creates .specs/: that is the structure step's job
            return entry
        try:
            self.subprojects_file_repository.write_repo_entry(context.work_dir, key, entry)
        except OSError as failure:
            self.log.warn("subprojects_store_failed", repo=key, type=type(failure).__name__)
        self.log.info("subprojects_detected", repo=key, monorepo=entry["monorepo"], projects=len(entry["projects"]))
        return entry

    def detect_again(self, context, repo_name=None, force=False):
        """Runs the detection again. Refused (409) when the list is confirmed, unless `force`."""
        key, folder = self.resolve_target(context, repo_name)
        if not os.path.isdir(folder):
            raise InvalidPathError("the repo folder does not exist: %s" % folder)
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(stored, dict) and stored.get("confirmed") is True and not force:
            raise RepoConflictError("the list is confirmed: detect again only with force", code=LIST_CONFIRMED_CODE)
        self.ensure_loop_stopped(context)
        entry = self.detect_and_store(context, key, folder)
        return self.build_description(key, folder, entry)

    # ------------------------------------------------ edit / confirm
    def update_list(self, context, payload, repo_name=None):
        """Edits the stored list. `payload`: {monorepo?, projects?:[{path, name?, stack?, skip?}], confirmed?}.
        `projects` is the whole list: a listed existing path keeps its fields and takes the given ones, a new path is validated and
        added, an unlisted one is removed. `monorepo:false` = "Not a monorepo" (stored with confirmed:true). Returns the description
        plus `warnings` (e.g. a folder with its own .git)."""
        if not isinstance(payload, dict):
            raise InvalidRequestError("the body must be a JSON object")
        key, folder = self.resolve_target(context, repo_name)
        if not os.path.isdir(folder):
            raise InvalidPathError("the repo folder does not exist: %s" % folder)
        self.ensure_loop_stopped(context)
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(stored, SubprojectsFileError):
            stored = None  # unusable file: the edit starts from a fresh detection and replaces it
        entry = dict(stored) if isinstance(stored, dict) else self.detect_and_store(context, key, folder)
        warnings = []
        if "projects" in payload:
            entry["projects"] = self.apply_project_edits(folder, subprojects_engine.entry_projects(entry), payload["projects"], warnings)
            entry["validated_by"], entry["validated_at"] = VALIDATED_BY_USER, now_text()
        if "monorepo" in payload:
            if not isinstance(payload["monorepo"], bool):
                raise InvalidRequestError("`monorepo` must be true or false")
            entry["monorepo"] = payload["monorepo"]
            if payload["monorepo"] is False:
                entry["confirmed"], entry["confirmed_at"] = True, now_text()  # "Not a monorepo": the user's word, never overridden
        if "confirmed" in payload:
            if not isinstance(payload["confirmed"], bool):
                raise InvalidRequestError("`confirmed` must be true or false")
            if payload["confirmed"]:
                self.validate_confirmation(folder, entry)
                entry["confirmed"], entry["confirmed_at"] = True, now_text()
            else:
                entry["confirmed"] = False
                entry.pop("confirmed_at", None)
        self.store_updated_entry(context, key, folder, entry)
        if payload.get("confirmed") is True and entry.get("monorepo") is True:
            self.remove_legacy_root_conformance(folder)
        return dict(self.build_description(key, folder, entry), warnings=warnings)

    def remove_legacy_root_conformance(self, folder):
        """The confirmation of a monorepo list cleans the block an earlier version wrote in the root CLAUDE.md (never asks)."""
        try:
            if self.remove_root_conformance_block(folder):
                self.log.info("root_conformance_block_removed", folder=folder)
        except OSError as failure:
            self.log.warn("root_conformance_block_not_removed", type=type(failure).__name__, message=str(failure)[:200])

    def store_updated_entry(self, context, key, folder, entry):
        self.subprojects_file_repository.write_repo_entry(context.work_dir, key, entry)
        self.log.info("subprojects_list_updated", repo=key, monorepo=entry.get("monorepo"), confirmed=entry.get("confirmed"),
                      projects=len(subprojects_engine.entry_projects(entry)))
        if entry.get("monorepo") is True and entry.get("confirmed") is True:
            self.run_callbacks(self.after_list_confirmed_callbacks, context, key, folder, entry)

    @staticmethod
    def validate_confirmation(folder, entry):
        if entry.get("monorepo") is not True:
            return
        active = [project for project in subprojects_engine.entry_projects(entry) if project.get("skip") is not True]
        if not active:
            raise InvalidRequestError("confirm needs at least one subproject that is not skipped (or choose Not a monorepo)")
        for project in active:
            if not os.path.isdir(os.path.join(folder, *subprojects_engine.normalize(project["path"]).split("/"))):
                raise InvalidRequestError("the folder of %s does not exist: remove it or skip it" % project["path"])

    def apply_project_edits(self, folder, current, requested, warnings):
        if not isinstance(requested, list):
            raise InvalidRequestError("`projects` must be a list")
        by_path = {subprojects_engine.normalize(project["path"]): project for project in current}
        result, seen = [], []
        for item in requested:
            if not isinstance(item, dict):
                raise InvalidRequestError("each project must be an object with a `path`")
            is_new = subprojects_engine.normalize(item.get("path")) not in by_path
            path = self.validate_path(folder, item.get("path"), is_new)
            for other in seen:
                if path == other or path.startswith(other + "/") or other.startswith(path + "/"):
                    raise InvalidRequestError("%s is duplicated or nested in %s" % (path, other))
            seen.append(path)
            project = dict(by_path.get(path) or self.build_new_project(folder, path, [entry["name"] for entry in result]))
            if "warning" in project:
                warnings.append("%s: %s" % (path, project["warning"]))
            self.apply_fields(project, item)
            result.append(project)
        names = [project["name"] for project in result]
        if len(set(names)) != len(names):
            raise InvalidRequestError("two subprojects have the same name")
        return result

    @staticmethod
    def apply_fields(project, item):
        if "name" in item:
            name = str(item["name"] or "").strip()
            if not name:
                raise InvalidRequestError("the name of %s cannot be empty" % project["path"])
            project["name"] = name
        if "stack" in item:
            if item["stack"] not in ALLOWED_STACKS:
                raise InvalidRequestError("stack must be one of: %s" % ", ".join(ALLOWED_STACKS))
            project["stack"] = item["stack"]
            project["skill"] = subprojects_engine.SKILL_BY_STACK.get(item["stack"], "")
        if "skip" in item:
            if not isinstance(item["skip"], bool):
                raise InvalidRequestError("`skip` must be true or false")
            project["skip"] = item["skip"]
        if "reason" in item:
            project["reason"] = str(item["reason"] or "")

    @staticmethod
    def validate_path(folder, raw_path, is_new):
        """Normalized relative path, refused when empty, absolute, leaving the repository (`..`) or (for a new one) not a folder."""
        text = str(raw_path or "").strip().replace("\\", "/")
        if not text or text.startswith("/") or re.match(r"^[A-Za-z]:", text):
            raise InvalidRequestError("the path must be relative to the repository: %r" % text)
        path = subprojects_engine.normalize(text)
        if not path or any(part in ("..", ".", "") for part in path.split("/")):
            raise InvalidRequestError("the path cannot leave the repository: %s" % path)
        target = os.path.realpath(os.path.join(folder, *path.split("/")))
        root = os.path.realpath(folder)
        if os.path.commonpath([root, target]) != root or target == root:
            raise InvalidRequestError("the path is outside the repository: %s" % path)
        if is_new and not os.path.isdir(target):
            raise InvalidRequestError("the folder does not exist: %s" % path)
        return path

    @staticmethod
    def build_new_project(folder, path, taken_names):
        project_folder = os.path.join(folder, *path.split("/"))
        stack = subprojects_engine.infer_stack(project_folder)
        name, counter = path.replace("/", "-"), 2
        unique = name
        while unique in taken_names:
            unique, counter = "%s-%d" % (name, counter), counter + 1
        project = {"path": path, "name": unique, "stack": stack, "skill": subprojects_engine.SKILL_BY_STACK.get(stack, ""),
                   "skip": False, "reason": "", "has_code": subprojects_engine.has_code_files(project_folder)}
        if os.path.exists(os.path.join(project_folder, ".git")):
            project["warning"] = "has its own .git (a separate repository inside the folder)"
        return project

    # ------------------------------------------------ Conformance per subproject
    def set_conformance(self, context, path, value, repo_name=None):
        """Writes the Conformance block in `<work folder>/<path>/CLAUDE.md`. A line that already exists counts as set and is kept.
        Returns {ok, path, conformance, written}."""
        key, folder, entry = self.require_confirmed(context, repo_name)
        self.ensure_loop_stopped(context)
        validated = self.specs_structure_service.validate_conformance(value)
        project = self.find_active_project(entry, path)
        return dict(self.write_project_conformance(context, key, folder, project, validated), ok=True)

    def set_conformance_all(self, context, repo_name=None):
        """Each non-skipped subproject without a Conformance gets ITS OWN suggestion. Returns {ok, written:[{path,value}], kept:[{path,value}]}."""
        key, folder, entry = self.require_confirmed(context, repo_name)
        self.ensure_loop_stopped(context)
        written, kept = [], []
        for project in subprojects_engine.entry_projects(entry):
            if project.get("skip") is True:
                continue
            project_folder = os.path.join(folder, *subprojects_engine.normalize(project["path"]).split("/"))
            if not os.path.isdir(project_folder):
                continue
            existing = self.specs_structure_service.get_conformance(project_folder)
            if existing:
                kept.append({"path": project["path"], "value": existing})
                continue
            suggestion = self.specs_structure_service.suggest_conformance(project_folder)["suggestion"]
            result = self.write_project_conformance(context, key, folder, project, suggestion)
            written.append({"path": result["path"], "value": result["conformance"]})
        return {"ok": True, "written": written, "kept": kept}

    def write_project_conformance(self, context, key, folder, project, value):
        path = subprojects_engine.normalize(project["path"])
        project_folder = os.path.join(folder, *path.split("/"))
        if not os.path.isdir(project_folder):
            raise InvalidPathError("the subproject folder does not exist: %s" % path)
        self.specs_structure_service.ensure_specs_in_gitignore(folder)  # the repo root: `.specs/` always out of git once the Conformance is defined
        existing = self.specs_structure_service.get_conformance(project_folder)
        if existing:
            return {"path": path, "conformance": existing, "written": False}
        written = self.specs_structure_service.write_conformance(project_folder, value)
        self.log.info("subproject_conformance_set", repo=key, path=path, value=written)
        self.run_callbacks(self.after_conformance_written_callbacks, context, key, folder, path, written)
        return {"path": path, "conformance": written, "written": True}

    def require_confirmed(self, context, repo_name):
        key, folder = self.resolve_target(context, repo_name)
        entry = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if not isinstance(entry, dict) or entry.get("monorepo") is not True or entry.get("confirmed") is not True:
            raise SubprojectsNotConfirmedError()
        return key, folder, entry

    @staticmethod
    def find_active_project(entry, path):
        normalized = subprojects_engine.normalize(str(path or "").replace("\\", "/"))
        for project in subprojects_engine.entry_projects(entry):
            if subprojects_engine.normalize(project["path"]) == normalized:
                if project.get("skip") is True:
                    raise InvalidRequestError("%s is skipped: it needs no Conformance" % normalized)
                return project
        raise InvalidRequestError("unknown subproject: %s" % normalized)

    # ------------------------------------------------ support
    def run_callbacks(self, callbacks, *arguments):
        for callback in callbacks:
            try:
                callback(*arguments)
            except Exception as failure:  # an extension never breaks the operation that triggered it
                self.log.warn("subprojects_callback_failed", callback=getattr(callback, "__name__", "callback"), type=type(failure).__name__)
