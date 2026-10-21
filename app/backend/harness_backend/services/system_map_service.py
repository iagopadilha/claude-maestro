"""The system map of a confirmed monorepo: ONE job that writes the root `docs/codebase/SYSTEM.md` (design D21).

It runs as the last step of "Map all that are missing" (`attach_system_map` wires it on the subproject queue) and from its own
action. Job type `map`, prompt `/orchestrator map --system`, `claude` runs in the repository root with the hook profile `systemmap`
(only `docs/codebase/SYSTEM.md` may be written). After the job the app undoes every change outside `.specs` and that one file,
fails the job when `SYSTEM.md` was not written (or not touched), and writes the root `docs/codebase/README.md` index. It never writes
the root `CLAUDE.md`: a monorepo root has no `CODEBASE` block (the subprojects' context blocks point to `SYSTEM.md`).

Only a confirmed monorepo with at least two mapped, non-skipped subprojects gets it; a single project never does (SYS-07).
A failure of this job never undoes the subproject maps.
"""
import datetime
import os
import time

from ..domain.project_errors import InvalidRequestError, JobInProgressError
from ..repositories.specs_file_repository import run_hints
from .claude_job_service import JOB_MODEL_CEILING, JOB_TYPE_MAP, ClaudeJobCommand
from .map_job_runner import JOB_PERMISSION_MODE, MAP_GUARD_IGNORED_PATHS, MAP_GUARD_NOTE, MAP_GUARD_FILES_SHOWN
from .subprojects_service import MAP_RELATIVE_FOLDER, SubprojectsNotConfirmedError, subprojects_engine

SYSTEM_MAP_PROMPT = "/orchestrator map --system"
SYSTEM_HOOK_PROFILE = "systemmap"
SYSTEM_RELATIVE_PATH = "docs/codebase/SYSTEM.md"
MINIMUM_MAPPED_SUBPROJECTS = 2
SYSTEM_NOT_WRITTEN = ("The AI finished but did not write docs/codebase/SYSTEM.md, so the system map was not made. The subproject "
                      "maps are untouched.")
START_RETRIES = 100  # the previous job record says "finished" a moment before the job service frees the repository
START_RETRY_SECONDS = 0.03


class SystemMapService:
    def __init__(self, map_job_runner, subprojects_service, claude_job_service, ensure_repo_free_for_job, log):
        """`map_job_runner` supplies the claude client, the workspace guard and the short head reader (its own `finish` writes a
        `CODEBASE` block, so the job is built here). `ensure_repo_free_for_job(context)`: the refusals of the root map."""
        self.map_job_runner = map_job_runner
        self.subprojects_service = subprojects_service
        self.claude_job_service = claude_job_service
        self.ensure_repo_free_for_job = ensure_repo_free_for_job
        self.log = log
        self.context_block_service = None  # optional (`attach_context_block_service`)

    def attach_context_block_service(self, context_block_service):
        """After the system map every subproject's `maestro:context` block is refreshed (it points to `SYSTEM.md`); the root `CLAUDE.md`
        of the monorepo is never written."""
        self.context_block_service = context_block_service

    # ------------------------------------------------ status
    def status(self, context, repo_name=None):
        """`{exists, mapped_at, older_than_list, enabled, reason}`. `reason` says why the action is disabled ("" when enabled).
        `older_than_list`: `SYSTEM.md` was written before the subproject list was last confirmed (the list changed since)."""
        key, folder = self.subprojects_service.resolve_target(context, repo_name)
        entry = subprojects_engine.load(folder, key)["entry"]
        path = self.system_path(folder)
        exists = os.path.isfile(path)
        mapped_at = self.format_time(os.path.getmtime(path)) if exists else None
        older = bool(exists and entry and self.is_older_than_list(os.path.getmtime(path), entry))
        reason = self.refusal_reason(folder, entry)
        return {"exists": exists, "mapped_at": mapped_at, "older_than_list": older, "enabled": not reason, "reason": reason}

    @staticmethod
    def is_older_than_list(system_mtime, entry):
        confirmed_at = SystemMapService.parse_time(str(entry.get("confirmed_at") or ""))
        return confirmed_at is not None and system_mtime < confirmed_at

    @staticmethod
    def parse_time(text):
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=datetime.timezone.utc)
        return parsed.timestamp()

    @staticmethod
    def format_time(timestamp):
        return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def refusal_reason(self, folder, entry):
        """"" when the system map may run; else the sentence shown next to the disabled action."""
        if not entry or entry.get("monorepo") is not True:
            return "only a monorepo has a system map"
        if entry.get("confirmed") is not True:
            return "confirm the list of subprojects first"
        mapped = self.mapped_subprojects(folder, entry)
        if len(mapped) < MINIMUM_MAPPED_SUBPROJECTS:
            return "map at least %d subprojects first (mapped: %d)" % (MINIMUM_MAPPED_SUBPROJECTS, len(mapped))
        return ""

    @staticmethod
    def mapped_subprojects(folder, entry):
        mapped = []
        for project in subprojects_engine.entry_projects(entry):
            if project.get("skip") is True:
                continue
            path = subprojects_engine.normalize(project["path"])
            map_folder = os.path.join(folder, *path.split("/"), *MAP_RELATIVE_FOLDER.split("/"))
            if run_hints.list_map_documents(map_folder):
                mapped.append(path)
        return mapped

    # ------------------------------------------------ start (202 path)
    def start(self, context, budget_usd=None, repo_name=None):
        """Validates and starts the job; returns `{ok, job}` at once. No spend cap: `budget_usd` is ignored."""
        project_id = context.project.id
        self.log.info("map_requested", project=project_id, scope="system")
        try:
            key, folder = self.subprojects_service.resolve_target(context, repo_name)
            entry = subprojects_engine.load(folder, key)["entry"]
            self.check_allowed(folder, entry)
            self.ensure_repo_free_for_job(context)
            if self.claude_job_service.get_running_job(folder):
                raise JobInProgressError("a claude job is already running in this project: wait for it to finish")
            job = self.start_job(context, folder, repo_key=key)
        except Exception as refusal:
            self.log.info("map_refused", project=project_id, scope="system", reason=type(refusal).__name__,
                          message=str(refusal)[:200])
            raise
        return {"ok": True, "job": job}

    def check_allowed(self, folder, entry):
        if not entry or entry.get("monorepo") is not True:
            raise InvalidRequestError("only a monorepo has a system map")
        if entry.get("confirmed") is not True:
            raise SubprojectsNotConfirmedError()
        reason = self.refusal_reason(folder, entry)
        if reason:
            raise InvalidRequestError(reason)

    def start_job(self, context, folder, repo_key=None):
        project_id = context.project.id
        holder = {}
        runner = self.map_job_runner
        allowed_folders = (".specs", SYSTEM_RELATIVE_PATH)

        def before_start(update_progress):
            holder["snapshot"] = runner.workspace_file_guard.take_snapshot(folder, MAP_GUARD_IGNORED_PATHS)
            holder["written_before"] = self.read_system_stamp(folder)

        argv = runner.claude_client.build_command(JOB_MODEL_CEILING, None, no_mcp=True, prompt_argument=SYSTEM_MAP_PROMPT,
                                                  permission_mode=JOB_PERMISSION_MODE, hook_profile=SYSTEM_HOOK_PROFILE)
        command = ClaudeJobCommand(argv, work_dir=folder, before_start=before_start,
                                   finalize=lambda result: self.finish(project_id, folder, holder, allowed_folders, result,
                                                                       context=context, repo_key=repo_key))
        return self.claude_job_service.start_job(folder, JOB_TYPE_MAP, command)

    def finish(self, project_id, folder, holder, allowed_folders, result, context=None, repo_key=None):
        """Runs in the job thread: undoes changes outside `.specs` and `SYSTEM.md`, fails when `SYSTEM.md` was not written and
        writes the root index. The root `CLAUDE.md` is never written."""
        runner = self.map_job_runner
        report = runner.workspace_file_guard.revert_unexpected_changes(holder["snapshot"], allowed_folders)
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("map_guard_reverted", project=project_id, scope="system", reverted=report["reverted"][:50],
                          removed=report["removed"][:50], unrestorable=report["unrestorable"][:50])
        result = dict(result)
        if result.get("ok") and touched:
            result["reply"] = str(result.get("reply") or "") + MAP_GUARD_NOTE % ", ".join(touched[:MAP_GUARD_FILES_SHOWN])
        if result.get("ok"):
            stamp = self.read_system_stamp(folder)
            if stamp is None or stamp == holder.get("written_before"):
                result.update(ok=False, error=SYSTEM_NOT_WRITTEN)
        if result.get("ok"):  # the app (not the AI) writes the index; never the root CLAUDE.md
            try:
                run_hints.write_map_index(folder, runner.read_short_head(folder))
            except OSError as failure:
                self.log.warn("system_map_index_failed", project=project_id, message=str(failure)[:200])
            if self.context_block_service is not None:
                self.context_block_service.refresh_repo(folder, repo_key, subprojects_root=context.work_dir if context else folder)
        return result

    def system_path(self, folder):
        return os.path.join(folder, *SYSTEM_RELATIVE_PATH.split("/"))

    def read_system_stamp(self, folder):
        """(mtime_ns, size) of `SYSTEM.md`, None when missing: a job that left it as it was did not write it."""
        try:
            stat = os.stat(self.system_path(folder))
        except OSError:
            return None
        return (stat.st_mtime_ns, stat.st_size)

    # ------------------------------------------------ last step of the subproject queue
    def on_queue_finished(self, context, repo_name, folder, queue_state):
        """`SubprojectMapService.add_on_queue_finished` callback: starts the system map after a queue whose maps all succeeded and
        that was the "map all" call, and returns the started job (the queue follows it to its end). Returns None when the queue
        did not ask for it. Runs in the queue watcher thread, so it waits briefly for the repository to be free. A refusal raises
        (the queue records the reason); the subproject maps stay."""
        if queue_state.get("include_system") is not True:
            return None
        for _ in range(START_RETRIES):
            try:
                return self.start(context, repo_name=repo_name)["job"]
            except JobInProgressError:
                time.sleep(START_RETRY_SECONDS)
        raise JobInProgressError("the repository stayed busy")


def attach_system_map(subproject_map_service, system_map_service):
    """Wiring for the composition: the system map is the last step of a successful "map all" queue."""
    subproject_map_service.add_on_queue_finished(system_map_service.on_queue_finished)
