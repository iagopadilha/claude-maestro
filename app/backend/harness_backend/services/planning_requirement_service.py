"""Planning mode rule: a project whose roadmap was never approved AND that never ran a loop opens in planning mode.

While it is on, the loop commands, the chat, the Act mode, the whiteboard writes and the one-shot roadmap jobs are refused
with 409 `code:"planning_required"` (reads, structure, conformance, the map job, the planning endpoints and the approval
stay open). A project that already ran loops and lost the approval (rows changed) keeps the old behaviour (banner).

Reopen planning (POST planning/reopen) writes a marker (`PLANNING-REOPENED.json`) next to the approval: while the roadmap is not
approved AND the marker exists the project is in planning mode even though it ran loops before. The roadmap approval removes the
marker again (`clear_reopened`).

"Ran a loop" = any trace the engine leaves: the loop worktree (or a project opened on it), LOOP.json with a status,
`meta.json` with turns, a turn file or a live `LOOP.lock`. Everything is read from disk on each call (small files).
"""
import json
import os
from typing import Protocol

from ..domain.project import LOOP_WORKTREE_SUFFIX
from ..domain.project_errors import PlanningRequiredError
from ..repositories.specs_file_repository import loop_state

REOPENED_MARKER_NAME = "PLANNING-REOPENED.json"  # lives in project/loop/json/ (loop_state.loop_path rule for *.json)
PLANNING_REQUIRED_MESSAGE = ("planning mode: approve the roadmap in the planning screen first (the loop, the chat and the "
                             "actions open after the approval)")


class IPlanningRequirementService(Protocol):
    def is_planning_required(self, context) -> bool: ...
    def ensure_planning_done(self, context) -> None: ...
    def has_ever_run_loop(self, context) -> bool: ...
    def is_reopened(self, context) -> bool: ...
    def mark_reopened(self, context, now_iso, extra_work_dirs=()) -> None: ...
    def clear_reopened(self, context) -> None: ...


class PlanningRequirementService:
    def __init__(self, specs_file_repository, process_runner):
        self.specs_file_repository = specs_file_repository
        self.process_runner = process_runner

    def is_planning_required(self, context):
        if loop_state.roadmap_approval(context.work_dir)["approved"]:
            return False
        return self.is_reopened(context) or not self.has_ever_run_loop(context)

    # ------------------------------------------------ reopened planning marker
    def is_reopened(self, context):
        return os.path.isfile(loop_state.loop_path(context.work_dir, REOPENED_MARKER_NAME))

    def mark_reopened(self, context, now_iso, extra_work_dirs=()):
        """Marker in the current work dir, the repo and any extra folder (a worktree that could not be removed)."""
        for folder in dict.fromkeys([context.work_dir, context.repo_path, *extra_work_dirs]):
            if not os.path.isdir(folder):  # a removed worktree is never recreated by the marker
                continue
            path = loop_state.loop_path(folder, REOPENED_MARKER_NAME)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump({"at": now_iso}, handle)

    def clear_reopened(self, context):
        for folder in dict.fromkeys([context.work_dir, context.repo_path]):
            try:
                os.remove(loop_state.loop_path(folder, REOPENED_MARKER_NAME))
            except OSError:
                pass

    def ensure_planning_done(self, context):
        """Raises PlanningRequiredError while the project is in planning mode."""
        if self.is_planning_required(context):
            raise PlanningRequiredError(PLANNING_REQUIRED_MESSAGE)

    def has_ever_run_loop(self, context):
        work_dir = context.work_dir
        if os.path.basename(os.path.normpath(context.repo_path)).endswith(LOOP_WORKTREE_SUFFIX):
            return True  # the project was opened on the loop worktree itself
        loop_json = self.specs_file_repository.read_json(loop_state.state_file(work_dir, "LOOP.json"))
        if loop_json.get("status") in loop_state.LOOP_STATUS:
            return True
        turns = self.specs_file_repository.read_json(loop_state.loop_path(work_dir, "meta.json")).get("turns")
        if isinstance(turns, int) and not isinstance(turns, bool) and turns > 0:
            return True
        if self.specs_file_repository.list_json_files(loop_state.turns_dir(work_dir)):
            return True
        return self.process_runner.is_alive(self.specs_file_repository.read_loop_lock_pid(work_dir))
