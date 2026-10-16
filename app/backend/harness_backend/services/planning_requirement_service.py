"""Planning mode rule: a project whose roadmap was never approved AND that never ran a loop opens in planning mode.

While it is on, the loop commands, the chat, the Act mode, the whiteboard writes and the one-shot roadmap jobs are refused
with 409 `code:"planning_required"` (reads, structure, conformance, the map job, the planning endpoints and the approval
stay open). A project that already ran loops and lost the approval (rows changed) keeps the old behaviour (banner).

"Ran a loop" = any trace the engine leaves: the loop worktree (or a project opened on it), LOOP.json with a status,
`meta.json` with turns, a turn file or a live `LOOP.lock`. Everything is read from disk on each call (small files).
"""
import os
from typing import Protocol

from ..domain.project import LOOP_WORKTREE_SUFFIX
from ..domain.project_errors import PlanningRequiredError
from ..repositories.specs_file_repository import loop_state

PLANNING_REQUIRED_MESSAGE = ("planning mode: approve the roadmap in the planning screen first (the loop, the chat and the "
                             "actions open after the approval)")


class IPlanningRequirementService(Protocol):
    def is_planning_required(self, context) -> bool: ...
    def ensure_planning_done(self, context) -> None: ...
    def has_ever_run_loop(self, context) -> bool: ...


class PlanningRequirementService:
    def __init__(self, specs_file_repository, process_runner):
        self.specs_file_repository = specs_file_repository
        self.process_runner = process_runner

    def is_planning_required(self, context):
        if loop_state.roadmap_approval(context.work_dir)["approved"]:
            return False
        return not self.has_ever_run_loop(context)

    def ensure_planning_done(self, context):
        """Raises PlanningRequiredError while the project is in planning mode."""
        if self.is_planning_required(context):
            raise PlanningRequiredError(PLANNING_REQUIRED_MESSAGE)

    def has_ever_run_loop(self, context):
        work_dir = context.work_dir
        if os.path.normcase(os.path.abspath(work_dir)) != os.path.normcase(os.path.abspath(context.repo_path)):
            return True  # the loop worktree only exists because run_loop.py created it
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
