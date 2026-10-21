"""One map job per subproject of a confirmed monorepo, ALL AT ONCE (installer, INS-04/05).

Every selected subproject gets its own `claude` job (Sonnet, no spend cap), running in `<repo>/<path>` with the prompt
`/orchestrator map --subproject <path>`. The one-job-per-repository rule is relaxed only here: each job carries a `parallel_key` (its
subproject folder), so map jobs of different subproject folders run next to each other while any other kind of job still refuses (and is
refused by) a running map. At most `max_parallel_maps` run at the same time; the rest stay `queued` and start as the others end.
The state is a file (`.specs/project/loop/internal/subprojects/map-queue.json`):

    {"state": "idle"|"running"|"done"|"stopped",
     "items": {path: {"state": "queued"|"running"|"done"|"failed", "started_at", "finished_at", "error", "job_id", "documents": count of files in <path>/docs/codebase}},
     "system": {"state": "idle"|"waiting"|"running"|"done"|"failed", "error": "", "job_id"},
     "started_at": iso, "include_system": bool}

`include_system` is True for the "map all that are missing" call (no explicit paths) and for a retry of such a stopped run: when every
item is `done` the system map starts by itself (the callback of `SystemMapService.on_queue_finished`, wired with `attach_system_map`,
returns its job and the queue follows it to the end). A failed item never stops the others; the run ends `stopped` and a new `start`
retries the failed (and never started) items, keeping the finished ones. After an app restart a `running` file with no job alive is read
as `stopped`: the items that were running become `failed` ("interrupted") and show Retry.

The public payload (`public_queue`) also keeps the keys of the old sequential queue (`queue`, `current`, `done`, `failed`, `budget_usd`).

Extension points (optional, a failing callback is logged and never breaks the queue):
  `add_extra_before_start(callback)`   callback(context, repo_name, folder, project_path, update_progress), runs in the job thread
                                       after the safety snapshot (the code inventory)
  `add_prompt_extension(callback)`     callback(context, repo_name, folder, project_path) -> text appended to the prompt ("" = none)
  `add_on_queue_finished(callback)`    callback(context, repo_name, folder, queue_state) -> job dict | None, when every item succeeded
                                       (the system map starts here); an exception is recorded in `system.error`
"""
import os
import threading
import time

from ..domain.project_errors import InvalidPathError, InvalidRequestError, JobInProgressError
from ..repositories.specs_file_repository import run_hints
from ..repositories.subprojects_file_repository import SubprojectsFileError
from .claude_job_service import JOB_MODEL_CEILING, JOB_STATE_RUNNING, now_iso
from .subprojects_service import MAP_RELATIVE_FOLDER, SubprojectsNotConfirmedError, subprojects_engine

SUBPROJECT_MAP_PROMPT = "/orchestrator map --subproject %s"
DEFAULT_MAX_PARALLEL_MAPS = 6
QUEUE_IDLE = "idle"
QUEUE_RUNNING = "running"
QUEUE_DONE = "done"
QUEUE_STOPPED = "stopped"
ITEM_QUEUED = "queued"
ITEM_RUNNING = "running"
ITEM_DONE = "done"
ITEM_FAILED = "failed"
SYSTEM_IDLE = "idle"
SYSTEM_WAITING = "waiting"
SYSTEM_RUNNING = "running"
SYSTEM_DONE = "done"
SYSTEM_FAILED = "failed"
INTERRUPTED_REASON = "interrupted: the app was shut down while the job was running"
WATCH_INTERVAL_SECONDS = 0.1
PROBE_PARALLEL_KEY = "\0probe"  # no job has it: `get_blocking_job` then answers only for the jobs that cannot run in parallel
ITEM_FIELDS = ("state", "started_at", "finished_at", "error", "job_id", "documents")


class SubprojectMapService:
    def __init__(self, map_job_runner, subprojects_file_repository, subprojects_service, claude_job_service, ensure_repo_free_for_job,
                 log, max_parallel_maps=DEFAULT_MAX_PARALLEL_MAPS):
        """`ensure_repo_free_for_job(context)`: the refusals of the root map (loop running, worktree exists, job running)."""
        self.map_job_runner = map_job_runner
        self.subprojects_file_repository = subprojects_file_repository
        self.subprojects_service = subprojects_service
        self.claude_job_service = claude_job_service
        self.ensure_repo_free_for_job = ensure_repo_free_for_job
        self.log = log
        self.max_parallel_maps = max(1, int(max_parallel_maps or DEFAULT_MAX_PARALLEL_MAPS))
        self.queue_lock = threading.RLock()
        self.active_folders = set()  # folders whose queue has a watcher thread alive in this process
        self.extra_before_start_callbacks = []
        self.prompt_extension_callbacks = []
        self.queue_finished_callbacks = []
        self.context_block_service = None  # optional (`attach_context_block_service`)

    def attach_context_block_service(self, context_block_service):
        """After each successful subproject map job that subproject's `CLAUDE.md` gets (or refreshes) the `maestro:context` block."""
        self.context_block_service = context_block_service

    def add_extra_before_start(self, callback):
        self.extra_before_start_callbacks.append(callback)

    def add_prompt_extension(self, callback):
        self.prompt_extension_callbacks.append(callback)

    def add_on_queue_finished(self, callback):
        self.queue_finished_callbacks.append(callback)

    # ------------------------------------------------ start (202 path)
    def start(self, context, paths=None, budget_usd=None, repo_name=None):
        """Validates, writes the queue, starts every job (up to `max_parallel_maps`) and returns `{ok, job, queue}` at once.
        `budget_usd` is accepted and ignored: the setup jobs have no spend cap."""
        project_id = context.project.id
        self.log.info("map_requested", project=project_id, scope="subprojects", paths=list(paths) if paths else None)
        try:
            key, folder = self.subprojects_service.resolve_target(context, repo_name)
            self.ensure_free_for_map_jobs(context)
            projects = self.read_confirmed_projects(context, key)
            previous = self.read_queue_state(context, repo_name)
            if previous and previous.get("state") == QUEUE_RUNNING:
                raise JobInProgressError("the subprojects are already being mapped: wait for them to finish")
            selected = self.select_paths(folder, projects, paths, previous)
            for path in selected:
                if self.claude_job_service.get_blocking_job(folder, self.parallel_key(folder, path)):
                    raise JobInProgressError("a claude job is already running in this project: wait for it to finish")
        except Exception as refusal:
            self.log.info("map_refused", project=project_id, scope="subprojects", reason=type(refusal).__name__,
                          message=str(refusal)[:200])
            raise
        stopped = previous if previous and previous.get("state") == QUEUE_STOPPED else None
        include_system = paths is None or bool(stopped and stopped.get("include_system") is True)
        with self.queue_lock:
            items = {path: dict(item) for path, item in ((stopped or {}).get("items") or {}).items() if item.get("state") == ITEM_DONE}
            for path in selected:
                items[path] = self.new_item()
            state = {"state": QUEUE_RUNNING, "items": items, "started_at": now_iso(), "include_system": include_system,
                     "system": {"state": SYSTEM_WAITING if include_system else SYSTEM_IDLE, "error": ""}}
            self.active_folders.add(self.folder_key(folder))
            try:
                jobs = self.start_ready_items(context, key, folder, state, raise_on_first_failure=True)
            except Exception:
                self.active_folders.discard(self.folder_key(folder))
                raise
            self.write_queue(folder, state)
        return {"ok": True, "job": jobs[0], "queue": self.public_queue(state)}

    def ensure_free_for_map_jobs(self, context):
        """The root map's refusals (loop running, worktree exists), except "a job is running" when every running job is itself a
        parallel subproject map (those are checked per folder by `start`)."""
        try:
            self.ensure_repo_free_for_job(context)
        except JobInProgressError:
            if self.claude_job_service.get_blocking_job(context.repo_path, PROBE_PARALLEL_KEY):
                raise

    @staticmethod
    def new_item():
        return {"state": ITEM_QUEUED, "started_at": None, "finished_at": None, "error": "", "job_id": "", "documents": 0}

    def read_confirmed_projects(self, context, key):
        entry = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(entry, SubprojectsFileError) or not isinstance(entry, dict) \
                or entry.get("monorepo") is not True or entry.get("confirmed") is not True:
            raise SubprojectsNotConfirmedError()
        return {subprojects_engine.normalize(project["path"]): project for project in subprojects_engine.entry_projects(entry)}

    def select_paths(self, folder, projects, requested, previous_state):
        """Explicit paths must be listed, not skipped and exist on disk (else the request is refused). Without paths: the failed and
        not-started items of a stopped queue, else every non-skipped project with code and no map yet (missing folders are left out)."""
        if requested is not None:
            if not isinstance(requested, (list, tuple)) or not requested or not all(isinstance(path, str) for path in requested):
                raise InvalidRequestError("paths must be a non-empty list of subproject paths")
            selected = []
            for raw_path in requested:
                path = subprojects_engine.normalize(raw_path)
                project = projects.get(path)
                if project is None or project.get("skip") is True:
                    raise InvalidRequestError("%s is not a mappable subproject of the confirmed list" % raw_path)
                if not os.path.isdir(self.project_folder(folder, path)):
                    raise InvalidPathError("the subproject folder does not exist: %s" % path)
                if path not in selected:
                    selected.append(path)
            return selected
        stopped = previous_state if previous_state and previous_state.get("state") == QUEUE_STOPPED else None
        if stopped:
            candidates = [path for path, item in stopped["items"].items() if item.get("state") in (ITEM_FAILED, ITEM_QUEUED)]
        else:
            candidates = [path for path, project in projects.items() if project.get("has_code") is True
                          and not run_hints.list_map_documents(self.map_folder(folder, path))]
        selected = []
        for path in candidates:
            project = projects.get(path)
            if project is None or project.get("skip") is True or not os.path.isdir(self.project_folder(folder, path)):
                continue
            if path not in selected:
                selected.append(path)
        if not selected:
            raise InvalidRequestError("no subproject to map: every non-skipped project with code already has its map")
        return selected

    # ------------------------------------------------ queue execution
    def start_ready_items(self, context, key, folder, state, raise_on_first_failure=False):
        """Starts queued items while fewer than `max_parallel_maps` run (under `queue_lock`). Returns the started jobs. An item that
        cannot start becomes `failed` (the first one raises instead when `raise_on_first_failure` and nothing started yet)."""
        started = []
        running = sum(1 for item in state["items"].values() if item["state"] == ITEM_RUNNING)
        for path, item in state["items"].items():
            if running >= self.max_parallel_maps:
                break
            if item["state"] != ITEM_QUEUED:
                continue
            try:
                started.append(self.start_item(context, key, folder, state, path))
                running += 1
            except Exception as failure:
                if raise_on_first_failure and not started:
                    raise
                self.log.warn("subproject_map_next_failed", path=path, type=type(failure).__name__, message=str(failure)[:200])
                item.update(state=ITEM_FAILED, finished_at=now_iso(),
                            error="could not start: %s" % (str(failure)[:300] or type(failure).__name__))
        return started

    def start_item(self, context, key, folder, state, path):
        """Starts the job of one item and its watcher thread. Returns the public job record."""
        prompt = SUBPROJECT_MAP_PROMPT % path + self.build_prompt_extension(context, key, folder, path)
        job = self.map_job_runner.start(
            repo_path=folder, project_id=context.project.id, budget_usd=None, prompt=prompt,
            work_dir=self.project_folder(folder, path), allowed_folders=self.allowed_folders(state, path),
            map_root=path, model=JOB_MODEL_CEILING, hook_profile="spec",
            extra_before_start=self.build_extra_before_start(context, key, folder, path),
            parallel_key=self.parallel_key(folder, path))
        state["items"][path].update(state=ITEM_RUNNING, started_at=now_iso(), finished_at=None, error="", job_id=job["id"], documents=0)
        threading.Thread(target=self.watch_job, args=(context, key, folder, path, job["id"]), daemon=True,
                         name="subproject-map-" + job["id"]).start()
        return job

    @staticmethod
    def allowed_folders(state, path):
        """What the guard of one job leaves alone. Jobs of the same run share the repository, so each guard also leaves the map folder
        and the `CLAUDE.md` of the other subprojects of the run alone (their jobs and the app write there meanwhile)."""
        siblings = [other for other in state["items"] if other != path]
        return (".specs", "%s/%s" % (path, MAP_RELATIVE_FOLDER)) + tuple(
            entry for other in siblings for entry in ("%s/%s" % (other, MAP_RELATIVE_FOLDER), "%s/CLAUDE.md" % other))

    def watch_job(self, context, key, folder, path, job_id):
        """Job thread of one item: waits for its job to end, records the outcome, starts the next queued item and, when this was
        the last one and everything succeeded, runs the system map."""
        try:
            job = self.wait_for_job(folder, job_id)
            if job.get("state") == "ok" and self.context_block_service is not None:
                self.context_block_service.refresh_folder(folder, key, path, subprojects_root=context.work_dir)
            documents = run_hints.list_map_documents(self.map_folder(folder, path))
            run_system = False
            with self.queue_lock:
                state = self.read_queue_file(folder)
                item = state["items"].get(path) if isinstance(state, dict) else None
                if item is None or item.get("job_id") != job_id:
                    return
                if job.get("state") == "ok":
                    item.update(state=ITEM_DONE, finished_at=now_iso(), error="", documents=len(documents))
                else:
                    item.update(state=ITEM_FAILED, finished_at=now_iso(), documents=len(documents),
                                error=str(job.get("error") or "the map job failed")[:500])
                    self.log.warn("subproject_map_failed", path=path, reason=item["error"][:200])
                self.start_ready_items(context, key, folder, state)
                run_system = self.claim_system_step(state)
                state["state"] = self.compute_state(state)
                self.write_queue(folder, state)
                snapshot = self.public_queue(state)
            if run_system:
                self.run_system_step(context, key, folder, snapshot)
        except Exception as failure:  # a watcher never takes the app down: the item is left failed with the reason
            self.log.error("subproject_map_watch_failed", path=path, type=type(failure).__name__, message=str(failure)[:200])
            self.fail_item(folder, path, job_id, "internal failure: %s" % type(failure).__name__)
        finally:
            with self.queue_lock:
                state = self.read_queue_file(folder)
                if not (isinstance(state, dict) and state.get("state") == QUEUE_RUNNING):
                    self.active_folders.discard(self.folder_key(folder))

    def wait_for_job(self, folder, job_id):
        while True:
            job = self.claude_job_service.get_job(folder, job_id)
            if job.get("state") != JOB_STATE_RUNNING:
                return job
            time.sleep(WATCH_INTERVAL_SECONDS)

    @staticmethod
    def compute_state(state):
        """`running` while any item is queued or running or the system map is waiting/running; else `stopped` when something failed
        (an item or the system map), else `done`."""
        items = list(state["items"].values())
        if any(item["state"] in (ITEM_QUEUED, ITEM_RUNNING) for item in items) \
                or state["system"]["state"] in (SYSTEM_WAITING, SYSTEM_RUNNING):
            return QUEUE_RUNNING
        if any(item["state"] == ITEM_FAILED for item in items) or state["system"]["state"] == SYSTEM_FAILED:
            return QUEUE_STOPPED
        return QUEUE_DONE

    @staticmethod
    def claim_system_step(state):
        """Under `queue_lock`, once no item is queued or running: True (exactly once) when the system map must start now; a run with a
        failure never starts it (the system map goes back to idle)."""
        if state["system"]["state"] != SYSTEM_WAITING:
            return False
        items = list(state["items"].values())
        if any(item["state"] in (ITEM_QUEUED, ITEM_RUNNING) for item in items):
            return False
        if any(item["state"] == ITEM_FAILED for item in items):
            state["system"]["state"] = SYSTEM_IDLE
            return False
        state["system"]["state"] = SYSTEM_RUNNING
        return True

    def run_system_step(self, context, key, folder, snapshot):
        """The system map after the last map: the callbacks start it and return its job, the queue then waits for that job to end."""
        job, refusal, failure = None, "", ""
        for callback in list(self.queue_finished_callbacks):
            try:
                started = callback(context, key, folder, snapshot)
                job = job or started
            except InvalidRequestError as invalid:  # e.g. fewer than two mapped subprojects: not a failure, the system map is not made
                refusal = str(invalid)[:300]
            except Exception as error:  # the subproject maps stay: the reason is shown next to the system map
                failure = str(error)[:300] or type(error).__name__
                self.log.warn("subproject_map_finished_callback_failed", type=type(error).__name__, message=str(error)[:200])
        if job is None:
            self.finish_system_step(folder, SYSTEM_FAILED if failure else SYSTEM_IDLE, failure or refusal)
            return
        with self.queue_lock:
            state = self.read_queue_file(folder)
            if isinstance(state, dict):
                state["system"]["job_id"] = job["id"]
                self.write_queue(folder, state)
        ended = self.wait_for_job(folder, job["id"])
        if ended.get("state") == "ok":
            self.finish_system_step(folder, SYSTEM_DONE, "")
        else:
            self.finish_system_step(folder, SYSTEM_FAILED, str(ended.get("error") or "the system map failed")[:500])

    def finish_system_step(self, folder, system_state, error):
        with self.queue_lock:
            state = self.read_queue_file(folder)
            if not isinstance(state, dict):
                return
            state["system"].update(state=system_state, error=error)
            state["state"] = self.compute_state(state)
            self.write_queue(folder, state)

    def fail_item(self, folder, path, job_id, reason):
        with self.queue_lock:
            state = self.read_queue_file(folder)
            item = state["items"].get(path) if isinstance(state, dict) else None
            if item is None or item.get("job_id") != job_id:
                return
            item.update(state=ITEM_FAILED, finished_at=now_iso(), error=reason)
            state["state"] = self.compute_state(state)
            try:
                self.write_queue(folder, state)
            except OSError:
                pass

    def build_prompt_extension(self, context, key, folder, path):
        text = ""
        for callback in list(self.prompt_extension_callbacks):
            try:
                text += str(callback(context, key, folder, path) or "")
            except Exception as failure:
                self.log.warn("subproject_map_prompt_extension_failed", path=path, type=type(failure).__name__)
        return text

    def build_extra_before_start(self, context, key, folder, path):
        if not self.extra_before_start_callbacks:
            return None

        def run_callbacks(update_progress):
            for callback in list(self.extra_before_start_callbacks):
                callback(context, key, folder, path, update_progress)

        return run_callbacks

    # ------------------------------------------------ read (describe) and restart recovery
    def read_queue(self, context, repo_name=None):
        """The public queue for the table and the installer (the idle payload when there never was one). Marks the items an app restart
        left running as failed "interrupted"; the documents of a running item are read live from its map folder."""
        state = self.read_queue_state(context, repo_name)
        if state is None:
            return self.public_queue({"state": QUEUE_IDLE, "items": {}, "system": {"state": SYSTEM_IDLE, "error": ""}})
        _, folder = self.subprojects_service.resolve_target(context, repo_name)
        for path, item in state["items"].items():
            if item["state"] == ITEM_RUNNING:
                item["documents"] = len(run_hints.list_map_documents(self.map_folder(folder, path)))
        return self.public_queue(state)

    def read_queue_state(self, context, repo_name=None):
        key, folder = self.subprojects_service.resolve_target(context, repo_name)
        with self.queue_lock:
            state = self.read_queue_file(folder)
            if not isinstance(state, dict):
                return None
            if state.get("state") == QUEUE_RUNNING and not self.is_queue_alive(folder):
                self.mark_interrupted(folder, state)
            return state

    def is_queue_alive(self, folder):
        return self.folder_key(folder) in self.active_folders or bool(self.claude_job_service.get_running_job(folder))

    def mark_interrupted(self, folder, state):
        """Per item: the running ones become failed (Retry); the never started stay queued (a retry runs them); a waiting system map
        goes back to idle and a running one is failed."""
        for item in state["items"].values():
            if item["state"] == ITEM_RUNNING:
                item.update(state=ITEM_FAILED, finished_at=now_iso(), error=INTERRUPTED_REASON)
        if state["system"]["state"] == SYSTEM_RUNNING:
            state["system"].update(state=SYSTEM_FAILED, error=INTERRUPTED_REASON)
        elif state["system"]["state"] == SYSTEM_WAITING:
            state["system"]["state"] = SYSTEM_IDLE
        has_queued = any(item["state"] == ITEM_QUEUED for item in state["items"].values())
        state["state"] = QUEUE_STOPPED if has_queued else self.compute_state(state)
        try:
            self.write_queue(folder, state)
        except OSError as failure:
            self.log.warn("subproject_map_queue_write_failed", type=type(failure).__name__, message=str(failure)[:200])

    @staticmethod
    def public_queue(state):
        if not isinstance(state, dict):
            return None
        items = {path: {field: item.get(field) for field in ITEM_FIELDS}
                 for path, item in (state.get("items") or {}).items()}
        running = [path for path, item in items.items() if item["state"] == ITEM_RUNNING]
        return {"state": state.get("state", QUEUE_IDLE), "items": items,
                "system": {"state": state["system"].get("state", SYSTEM_IDLE), "error": state["system"].get("error", "")},
                "started_at": state.get("started_at", ""), "include_system": state.get("include_system") is True,
                # keys of the old sequential queue, kept for compatibility
                "queue": [path for path, item in items.items() if item["state"] == ITEM_QUEUED],
                "current": running[0] if running else None,
                "done": [path for path, item in items.items() if item["state"] == ITEM_DONE],
                "failed": {path: item["error"] for path, item in items.items() if item["state"] == ITEM_FAILED},
                "budget_usd": None}

    # ------------------------------------------------ files
    def read_queue_file(self, folder):
        state = self.subprojects_file_repository.read_map_queue(folder)
        if not isinstance(state, dict):
            return None
        if not isinstance(state.get("items"), dict):  # the file of the old sequential queue: converted to the item shape
            state = self.convert_legacy_queue(state)
        state.setdefault("state", QUEUE_IDLE)
        if not isinstance(state.get("system"), dict):
            state["system"] = {"state": SYSTEM_IDLE, "error": ""}
        state["system"].setdefault("state", SYSTEM_IDLE)
        state["system"].setdefault("error", "")
        for item in state["items"].values():
            for field, default in (("state", ITEM_QUEUED), ("started_at", None), ("finished_at", None), ("error", ""), ("job_id", ""),
                                   ("documents", 0)):
                item.setdefault(field, default)
        return state

    @staticmethod
    def convert_legacy_queue(old):
        items = {path: dict(SubprojectMapService.new_item(), state=ITEM_DONE) for path in old.get("done") or []}
        for path, reason in (old.get("failed") or {}).items():
            items[path] = dict(SubprojectMapService.new_item(), state=ITEM_FAILED, error=str(reason))
        if old.get("current"):
            items[old["current"]] = dict(SubprojectMapService.new_item(), state=ITEM_RUNNING)
        for path in old.get("queue") or []:
            items[path] = SubprojectMapService.new_item()
        legacy_state = old.get("state")
        return {"state": QUEUE_DONE if legacy_state == "finished" else legacy_state or QUEUE_IDLE, "items": items,
                "system": {"state": SYSTEM_IDLE, "error": ""}, "started_at": old.get("started_at", ""),
                "include_system": old.get("include_system") is True}

    def write_queue(self, folder, state):
        self.subprojects_file_repository.write_map_queue(folder, state)

    @staticmethod
    def project_folder(folder, path):
        return os.path.join(folder, *path.split("/"))

    def map_folder(self, folder, path):
        return os.path.join(self.project_folder(folder, path), *MAP_RELATIVE_FOLDER.split("/"))

    def parallel_key(self, folder, path):
        return self.folder_key(self.project_folder(folder, path))

    @staticmethod
    def folder_key(folder):
        return os.path.normcase(os.path.abspath(folder))
