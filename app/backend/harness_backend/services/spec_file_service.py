"""View and edit of the `.md` files of `.specs/` (grouped tree, read with a version, write with a version conflict check).

Edit rules (docs/ARCHITECTURE.md): editable `features/**`, `codebase/**`, `project/PROJECT.md`, `FLOWS.md`, `RULES.md` and
`TEST-ENVIRONMENT.md`; `ROADMAP.md` and `STATE.md` only with the loop stopped; `loop/json` and `loop/internal` never
(not even read). Every path goes through the repository's safe resolver (no `..`, absolute path or symbolic link).
"""
import os
from typing import Protocol

from ..domain.project_errors import (FileEditRefusedError, FileVersionConflictError, InvalidRequestError, LoopRunningError,
                                     PathOutsideSpecsError, SpecFileNotFoundError)
from ..repositories.specs_file_repository import compute_bytes_version, loop_state, split_relative_path

EDITABLE_EXTENSION = ".md"
LOOP_DIR_RELATIVE = "project/" + loop_state.LOOP_FOLDER_NAME
TRASH_DIR_RELATIVE = ".trash"  # `.specs/.trash/`: what "Reset specs" moved out; never listed or read
FORBIDDEN_DIRS = (LOOP_DIR_RELATIVE + "/" + loop_state.JSON_FOLDER_NAME, LOOP_DIR_RELATIVE + "/" + loop_state.INTERNAL_FOLDER_NAME,
                  TRASH_DIR_RELATIVE)
EDITABLE_PROJECT_FILES = ("project/PROJECT.md", "project/FLOWS.md", "project/RULES.md", "project/TEST-ENVIRONMENT.md")
EDITABLE_WITH_LOOP_STOPPED_FILES = ("project/ROADMAP.md", "project/STATE.md")
FEATURE_FILE_MIN_DEPTH = 3  # features/<slug>/<file>.md
LOOP_ROOT_FILE_DEPTH = 3  # project/loop/<file>.md
CONTENT_CHARACTER_LIMIT = 1024 * 1024
READ_ONLY_REASON = "read-only: written by the loop/orchestrator"
LOOP_RUNNING_REASON = "loop running: ROADMAP.md and STATE.md can only be edited with the loop stopped"


def evaluate_edit(relative, loop_running):
    """(editable, reason) for a normalized path (`a/b.md`) relative to `.specs/`."""
    parts = relative.split("/")
    if parts[0] == "features" and len(parts) >= FEATURE_FILE_MIN_DEPTH:
        return True, ""
    if parts[0] == "codebase" and len(parts) >= 2:
        return True, ""
    if relative in EDITABLE_PROJECT_FILES:
        return True, ""
    if relative in EDITABLE_WITH_LOOP_STOPPED_FILES:
        return (False, LOOP_RUNNING_REASON) if loop_running else (True, "")
    return False, READ_ONLY_REASON


def is_in_forbidden_dir(relative):
    """Case-insensitive: on Windows `Project/Loop/JSON` is the same folder."""
    text = relative.lower()
    return any(text == folder.lower() or text.startswith(folder.lower() + "/") for folder in FORBIDDEN_DIRS)


class ISpecFileService(Protocol):
    def build_specs_tree(self, context) -> dict: ...
    def read_spec_file(self, context, path) -> dict: ...
    def write_spec_file(self, context, path, content, version) -> dict: ...


class SpecFileService:
    def __init__(self, specs_file_repository, project_progress_service, log):
        self.specs_file_repository = specs_file_repository
        self.project_progress_service = project_progress_service
        self.log = log

    # ------------------------------------------------ tree
    def build_specs_tree(self, context):
        specs_dir = context.specs_dir
        loop_running = self.project_progress_service.is_loop_running(context)
        relatives = self.specs_file_repository.list_markdown_files(specs_dir, FORBIDDEN_DIRS)
        roadmap_text = self.specs_file_repository.read_text(os.path.join(specs_dir, "project", "ROADMAP.md")) or ""
        roadmap_features, _ = loop_state.parse_roadmap(roadmap_text)
        groups = {"project": [], "codebase": [], "loop": [], "other": []}
        files_by_feature = {}
        for relative in relatives:
            item = self.build_file_item(specs_dir, relative, loop_running)
            parts = relative.split("/")
            if parts[0] == "features" and len(parts) >= FEATURE_FILE_MIN_DEPTH:
                files_by_feature.setdefault(parts[1], []).append(item)
            elif relative.startswith(LOOP_DIR_RELATIVE + "/"):
                if len(parts) == LOOP_ROOT_FILE_DEPTH:  # only the .md files at the loop root (ANSWERS, VALIDATE, DEPENDENCIES, GRAPH)
                    groups["loop"].append(item)
            elif parts[0] == "project":
                groups["project"].append(item)
            elif parts[0] == "codebase":
                groups["codebase"].append(item)
            else:
                groups["other"].append(item)
        return {"exists": os.path.isdir(specs_dir), "specs_dir": specs_dir, "loop_running": loop_running,
                "project": groups["project"], "codebase": groups["codebase"],
                "features": self.build_features(roadmap_features, files_by_feature),
                "loop": groups["loop"], "other": groups["other"]}

    @staticmethod
    def build_features(roadmap_features, files_by_feature):
        """ROADMAP features (in its order) and then the folders without a ROADMAP line (material ready to adopt)."""
        features = []
        for slug, fields in roadmap_features.items():
            features.append({"slug": slug, "status": fields.get("status"), "stage": fields.get("stage", ""), "in_roadmap": True,
                             "files": files_by_feature.get(slug, [])})
        for slug in sorted(set(files_by_feature) - set(roadmap_features)):
            features.append({"slug": slug, "status": None, "stage": "", "in_roadmap": False, "files": files_by_feature[slug]})
        return features

    def build_file_item(self, specs_dir, relative, loop_running):
        editable, reason = evaluate_edit(relative, loop_running)
        item = {"path": relative, "name": relative.rsplit("/", 1)[-1], "editable": editable, "not_editable_reason": reason or None}
        item.update(self.specs_file_repository.get_file_info(os.path.join(specs_dir, *relative.split("/"))))
        return item

    # ------------------------------------------------ read and write
    def read_spec_file(self, context, path):
        relative, target = self.resolve_markdown(context, path)
        content = self.specs_file_repository.read_bytes(target)
        if content is None or not os.path.isfile(target):
            raise SpecFileNotFoundError("file not found in .specs/: %s" % relative)
        editable, reason = evaluate_edit(relative, self.project_progress_service.is_loop_running(context))
        return {"path": relative, "text": content.decode("utf-8", "replace"), "version": compute_bytes_version(content),
                "editable": editable, "not_editable_reason": reason or None}

    def write_spec_file(self, context, path, content, version):
        if not isinstance(content, str):
            raise InvalidRequestError("content must be text")
        if len(content) > CONTENT_CHARACTER_LIMIT:
            raise InvalidRequestError("content longer than %d characters" % CONTENT_CHARACTER_LIMIT)
        relative, target = self.resolve_markdown(context, path)
        editable, reason = evaluate_edit(relative, self.project_progress_service.is_loop_running(context))
        if not editable:
            raise LoopRunningError(reason) if reason == LOOP_RUNNING_REASON else FileEditRefusedError("%s: %s" % (relative, reason))
        current = self.specs_file_repository.read_bytes(target)
        self.check_version(relative, current, version)
        self.specs_file_repository.write_text_atomic(target, content)
        written = self.specs_file_repository.read_bytes(target) or b""
        self.log.info("spec_file_written", project=context.project.id, path=relative, created=current is None)
        return {"path": relative, "version": compute_bytes_version(written), "created": current is None}

    @staticmethod
    def check_version(relative, current, version):
        """An existing file requires the version that was read (sha1); a new file requires an empty version."""
        if current is None:
            if version not in (None, ""):
                raise FileVersionConflictError("the file %s no longer exists (it was deleted after it was read)" % relative, None)
            return
        current_version = compute_bytes_version(current)
        if version != current_version:
            raise FileVersionConflictError("the file %s changed since it was read: reload before saving" % relative, current_version)

    def resolve_markdown(self, context, path):
        """(normalized relative path, absolute path) of a `.md` outside the forbidden folders, or PathOutsideSpecsError."""
        relative = "/".join(split_relative_path(path))
        if is_in_forbidden_dir(relative):
            raise PathOutsideSpecsError("loop/json and loop/internal are not accessible here")
        if not relative.lower().endswith(EDITABLE_EXTENSION):
            raise PathOutsideSpecsError("only .md files of .specs/")
        return relative, self.specs_file_repository.resolve_path_inside_specs(context.specs_dir, relative)
