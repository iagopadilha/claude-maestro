"""Loop status of a project and app occupancy (a loop running in some project).

Derivation rules (docs/API.md): `LOOP.lock` with a live PID = `running`; no lock and LOOP.json at `continue` = `stopped`
(the script died), or `error` when `meta.json` recorded an error stop after LOOP.json; no LOOP.json = `no_loop`.
"""
from typing import Protocol

from ..domain.project import ProjectNotFoundError
from ..repositories.loop_file_repository import loop_state

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
STATUS_NO_LOOP = "no_loop"
STATUS_CONTINUE = "continue"
STOPPED_BY_ERROR = "error"


class ILoopStatusService(Protocol):
    def get_loop_summary(self, context) -> dict:
        """{"status","running","pid","json","meta","reason"}."""

    def is_any_loop_running(self) -> bool:
        """True if some registered project has a `LOOP.lock` with a live PID."""


class LoopStatusService:
    def __init__(self, loop_file_repository, project_service, log):
        self.loop_file_repository = loop_file_repository
        self.project_service = project_service
        self.log = log

    def get_loop_summary(self, context):
        work_dir = context.work_dir
        loop_json = self.loop_file_repository.read_loop_json(work_dir)
        meta = self.loop_file_repository.read_meta(work_dir)
        pid = self.loop_file_repository.read_lock_pid(work_dir)
        running = self.loop_file_repository.is_process_alive(pid)
        status = loop_json.get("status") if loop_json.get("status") in loop_state.LOOP_STATUS else None
        reason = loop_json.get("reason")
        if running:
            status = STATUS_RUNNING
        elif status == STATUS_CONTINUE:
            status = STATUS_STOPPED  # the script died in the middle of the loop
            # finish(error) writes only meta.json; when it is newer than LOOP.json, it says why the loop stopped
            if meta.get("stopped") == STOPPED_BY_ERROR and str(meta.get("finished_at") or "") >= str(loop_json.get("at") or ""):
                status, reason = STATUS_ERROR, meta.get("reason")
        elif status is None:
            status = STATUS_NO_LOOP
        return {"status": status, "running": running, "pid": pid if running else None, "json": loop_json, "meta": meta, "reason": reason}

    def is_loop_running(self, context):
        """Only the lock (cheap): used by the occupancy sweep."""
        pid = self.loop_file_repository.read_lock_pid(context.work_dir)
        return self.loop_file_repository.is_process_alive(pid)

    def is_any_loop_running(self):
        try:
            projects = list(self.project_service.list_projects())
        except Exception as failure:  # unreadable registry: when in doubt the activity monitor does not shut down
            self.log.warn("loop_read_list_projects_failed", type=type(failure).__name__)
            raise
        for project in projects:
            try:
                context = self.project_service.get_project_context(project.id)
            except ProjectNotFoundError:
                continue
            if self.is_loop_running(context):
                return True
        return False

    def is_busy(self):
        """IBusyProvider: the app does not shut down for idleness while a loop runs in some project."""
        return self.is_any_loop_running()
