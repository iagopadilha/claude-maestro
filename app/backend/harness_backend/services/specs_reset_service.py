"""Reset specs: wipes the planning/loop state of a project so new features start from zero, WITHOUT deleting anything.

Everything that is reset is MOVED to `<work>/.specs/.trash/<YYYYMMDD-HHMMSS>/` keeping the paths relative to `.specs/`
(so the user can put it back by hand). Then the files that have an app template (ROADMAP, FLOWS, STATE) are born again
from `templates/specs` and `features/` is recreated empty. Which paths move and which stay is declared in the two
constants below (`RESET_MOVED_PATHS`, `RESET_KEPT_PATHS`) so the policy is easy to read and change.

The specs that are reset are ALWAYS the primary checkout's (`context.repo_path`), even when a loop already ran and the work dir is the loop
worktree: right after the reset, the loop worktree(s) of the project are DELETED too (folder + local `loop/*` branch, see
`LoopWorktreeRemovalService`; no merge, no commit, no push, no backup), so the project goes back to planning on the primary's `.specs/`.
A worktree that cannot be removed is reported in `worktree_failures` and never aborts the reset.

Safety: refused while a loop or a claude job runs in the project; only paths under `<primary>/.specs/` are touched; a symbolic
link (or junction) is never followed and never moved (it stays where it is); `.specs/.trash/` is git-ignored and the app never
reads it as part of the plan or the loop (see `TRASH_RELATIVE_DIR` in specs_structure_service and FORBIDDEN_DIRS in
spec_file_service). Layout: the current one (`project/loop/{json,internal}`); old flat layouts are migrated by the engine
(`loop_state.migrate_layout`) before the loop runs, they are not handled here.
"""
import datetime
import os
import shutil
import threading
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, PLANNING_BUSY_CODE, InvalidRequestError, JobInProgressError,
                                     LoopRunningError, PathOutsideSpecsError)
from ..repositories.specs_file_repository import is_inside_folder, is_link_or_junction, loop_state
from .specs_structure_service import TRASH_RELATIVE_DIR

# MOVED to the trash (relative to the work dir). `.specs/project/loop` moves whole, except what is in the kept list below.
RESET_MOVED_PATHS = (
    ".specs/features",              # recreated empty
    ".specs/project/ROADMAP.md",    # recreated from the template
    ".specs/project/FLOWS.md",      # recreated from the template
    ".specs/project/STATE.md",      # recreated from the template
    ".specs/project/phases",
    ".specs/project/archive",
    ".specs/project/loop",          # approval, answers, graph, turns, snapshots, chat, jobs, planning thread, logs, LOOP.json...
)
# KEPT, never touched (relative to the work dir). Paths that do not exist are simply left out of the response.
RESET_KEPT_PATHS = (
    ".specs/project/RULES.md",
    ".specs/project/PROJECT.md",
    ".specs/project/TEST-ENVIRONMENT.md",
    ".specs/project/REPOS.json",    # the repo list of a multi-repo project: a reset never forgets it
    ".specs/project/.env.test",
    ".specs/project/ACCESS.json",   # the user's Test environment choices (Grafana read, shared fallback): a reset never forgets them
    "docs/codebase",
    ".specs/codebase",              # legacy location of the codebase map
    "CLAUDE.md",
)
# Files of the loop folder that stay (resolved with loop_state.loop_path, so the folder layout lives in one place).
KEPT_LOOP_FILE_NAMES = ("rules.tests.json", "PERMISSIONS.json", "SCOPE.json", "last-run.json")  # last-run.json: how to re-attach after Close loop
# Written by the app itself and deleted by it on shutdown: neither moved nor listed.
LEFT_ALONE_LOOP_FILE_NAMES = ("MONITOR.json",)
RECREATED_TEMPLATE_FILES = ("ROADMAP.md", "FLOWS.md", "STATE.md")
FEATURES_RELATIVE_DIR = ".specs/features"
TRASH_STAMP_FORMAT = "%Y%m%d-%H%M%S"


def to_posix(path):
    return path.replace("\\", "/")


def normalize(path):
    return os.path.normcase(os.path.abspath(path))


def contains_files(folder):
    """True when the folder has at least one file (or link) somewhere inside; folders of empty folders are not content."""
    for _, _, names in os.walk(folder, followlinks=False):
        if names:
            return True
    return False


class ISpecsResetService(Protocol):
    def reset_specs(self, context, confirm) -> dict: ...
    def preview_reset(self, context) -> dict: ...


class SpecsResetService:
    def __init__(self, specs_file_repository, specs_structure_service, project_progress_service, claude_job_service, log,
                 loop_worktree_removal_service=None):
        self.loop_worktree_removal_service = loop_worktree_removal_service
        self.specs_file_repository = specs_file_repository
        self.specs_structure_service = specs_structure_service
        self.project_progress_service = project_progress_service
        self.claude_job_service = claude_job_service
        self.log = log
        self.reset_lock = threading.Lock()

    def reset_specs(self, context, confirm):
        """Moves the planning/loop state of the primary checkout to the trash, recreates the templates, then deletes the loop worktrees.
        Returns {"ok", "moved", "trash", "kept", "worktrees_removed", "worktree_failures"}. `trash` is None when there was nothing to move
        (and nothing was created)."""
        self.ensure_confirmed(context, confirm)
        with self.reset_lock:
            self.ensure_can_reset(context)
            work = context.repo_path  # the primary's plan, never the loop worktree's (that one is deleted below)
            moved, trash_relative = self.reset_specs_folder(context, work)
            worktrees = self.remove_loop_worktrees(context)
        self.log.info("specs_reset", project=context.project.id, moved=len(moved), trash=trash_relative,
                      worktrees_removed=len(worktrees["worktrees_removed"]), worktree_failures=len(worktrees["worktree_failures"]))
        return dict({"ok": True, "moved": moved, "trash": trash_relative, "kept": self.list_kept(work)}, **worktrees)

    def preview_reset(self, context):
        """Read only, for the confirmation: the loop worktrees the reset would delete (path, branch, unpushed commits or None)
        and the ones it would leave alone. `{"ok", "worktrees": [...], "skipped": [...]}`."""
        if self.loop_worktree_removal_service is None:
            return {"ok": True, "worktrees": [], "skipped": []}
        return dict(self.loop_worktree_removal_service.preview_removable_worktrees(context), ok=True)

    def reset_specs_folder(self, context, work):
        """(moved, trash relative path) of the `.specs/` reset of `work`; ([], None) when there is no `.specs/` or nothing to move."""
        specs_dir = os.path.join(work, ".specs")
        if not os.path.isdir(specs_dir):
            return [], None
        if is_link_or_junction(specs_dir):
            raise PathOutsideSpecsError(".specs/ is a symbolic link (or junction): refusing to reset outside the project")
        templates = self.build_templates(work, context.project.name)
        sources = self.collect_sources(work, specs_dir, templates)
        moved, trash_relative = [], None
        if sources:
            trash_dir = self.create_trash_dir(specs_dir)
            trash_relative = to_posix(os.path.relpath(trash_dir, work))
            moved = self.move_to_trash(work, specs_dir, trash_dir, sources)
        self.recreate_state(work, templates)
        return moved, trash_relative

    def remove_loop_worktrees(self, context):
        if self.loop_worktree_removal_service is None:
            return {"worktrees_removed": [], "worktree_failures": []}
        return self.loop_worktree_removal_service.remove_loop_worktrees(context)

    # ------------------------------------------------ guards
    @staticmethod
    def ensure_confirmed(context, confirm):
        if not isinstance(confirm, str) or confirm.strip() != context.project.name:
            raise InvalidRequestError("confirm must be the project name (%s)" % context.project.name)

    def ensure_can_reset(self, context):
        if self.project_progress_service.is_loop_running(context):
            raise LoopRunningError("loop running: stop the loop before resetting the specs", code=LOOP_RUNNING_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job (planning reply, roadmap or map) is running: wait for it to finish",
                                     code=PLANNING_BUSY_CODE)

    # ------------------------------------------------ plan
    def build_templates(self, work, project_name):
        """{absolute path: text} of the files the reset recreates, from the same templates as the structure initialization."""
        project_dir = os.path.join(work, ".specs", "project")
        built = self.specs_structure_service.build_template_files(work, project_name)
        return {os.path.join(project_dir, name): built[os.path.join(project_dir, name)] for name in RECREATED_TEMPLATE_FILES
                if os.path.join(project_dir, name) in built}

    def list_protected_paths(self, work):
        """Absolute paths the reset must never move: the kept list plus the loop files that stay or belong to the app."""
        protected = [os.path.join(work, *relative.split("/")) for relative in RESET_KEPT_PATHS]
        protected += [loop_state.loop_path(work, name) for name in KEPT_LOOP_FILE_NAMES + LEFT_ALONE_LOOP_FILE_NAMES]
        return {normalize(path) for path in protected}

    def collect_sources(self, work, specs_dir, templates):
        protected = self.list_protected_paths(work)
        normalized_templates = {normalize(path): text for path, text in templates.items()}
        sources = []
        for relative in RESET_MOVED_PATHS:
            self.collect(os.path.join(work, *relative.split("/")), specs_dir, protected, normalized_templates, sources)
        return sources

    def collect(self, path, specs_dir, protected, normalized_templates, sources):
        """Adds to `sources` what must move: the whole entry, or (when something protected lives inside it) its children."""
        if not os.path.lexists(path) or normalize(path) in protected:
            return
        if is_link_or_junction(path) or not is_inside_folder(specs_dir, path):
            self.log.warn("specs_reset_skipped_link", path=path)
            return
        if os.path.isdir(path):
            prefix = normalize(path) + os.sep
            if any(item.startswith(prefix) for item in protected):
                for name in sorted(os.listdir(path)):
                    self.collect(os.path.join(path, name), specs_dir, protected, normalized_templates, sources)
            elif contains_files(path):
                sources.append(path)
            return
        template = normalized_templates.get(normalize(path))
        if template is not None and self.specs_file_repository.read_text(path) == template:
            return  # already the pristine template: nothing to clean (keeps the reset idempotent)
        sources.append(path)

    # ------------------------------------------------ act
    def create_trash_dir(self, specs_dir):
        stamp = datetime.datetime.now().strftime(TRASH_STAMP_FORMAT)
        trash_root = os.path.join(specs_dir, os.path.basename(TRASH_RELATIVE_DIR))
        if is_link_or_junction(trash_root):
            raise PathOutsideSpecsError(".specs/.trash is a symbolic link (or junction): refusing to move files into it")
        candidate, counter = os.path.join(trash_root, stamp), 1
        while os.path.exists(candidate):  # two resets in the same second
            counter += 1
            candidate = os.path.join(trash_root, "%s-%d" % (stamp, counter))
        os.makedirs(candidate)
        return candidate

    def move_to_trash(self, work, specs_dir, trash_dir, sources):
        moved = []
        for source in sources:
            destination = os.path.join(trash_dir, os.path.relpath(source, specs_dir))
            try:
                os.makedirs(os.path.dirname(destination), exist_ok=True)
                shutil.move(source, destination)
            except OSError as failure:  # a locked file on Windows: what moved so far is in the trash and logged
                self.log.error("specs_reset_move_failed", path=source, message=str(failure)[:200], moved=len(moved))
                raise
            moved.append(to_posix(os.path.relpath(source, work)))
        return moved

    def recreate_state(self, work, templates):
        """Born clean again: templates where they exist, `features/` empty, the loop folders the engine expects."""
        os.makedirs(os.path.join(work, *FEATURES_RELATIVE_DIR.split("/")), exist_ok=True)
        for folder in self.specs_structure_service.list_structure_folders(work):
            os.makedirs(folder, exist_ok=True)
        for path, text in templates.items():
            self.specs_file_repository.create_file_if_missing(path, text)
        self.specs_structure_service.ensure_specs_trash_in_gitignore(work)

    def list_kept(self, work):
        kept = [os.path.join(work, *relative.split("/")) for relative in RESET_KEPT_PATHS]
        kept += [loop_state.loop_path(work, name) for name in KEPT_LOOP_FILE_NAMES]
        return [to_posix(os.path.relpath(path, work)) for path in kept if os.path.lexists(path)]
