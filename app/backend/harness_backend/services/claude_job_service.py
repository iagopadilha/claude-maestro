"""`claude -p` jobs launched by the app (generate/refine the roadmap, map the codebase, a planning chat reply): they run in a
thread, one per project at a time, and are written to `<repo>/.specs/project/loop/internal/jobs/<id>.json`.

The claude `session_id` stays in the file (so that `refine` continues the same conversation) and NEVER leaves through the
API. A job that was `running` when the app went down shows up as `error` (interrupted) when read.
"""
import datetime
import os
import re
import secrets
import threading
import time
from typing import Protocol

from ..domain.project_errors import JobInProgressError, JobNotFoundError
from ..repositories.specs_file_repository import loop_state
from ..infrastructure.claude_client import call_kind

JOB_TYPE_ROADMAP = "roadmap"
JOB_TYPE_ROADMAP_REFINE = "roadmap_refine"
JOB_TYPE_MAP = "map"
JOB_TYPE_PLANNING = "planning"
JOB_TYPE_WORKSPACE_CREATE = "workspace-create"  # not a claude run: a task (see start_task_job)
ROADMAP_JOB_TYPES = (JOB_TYPE_ROADMAP, JOB_TYPE_ROADMAP_REFINE)
JOB_STATE_RUNNING = "running"
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
JOB_MODEL_CEILING = "sonnet"  # model every map/validation job pins (RULES.md: no subagent above Sonnet), instead of the account default
PREPARING_SNAPSHOT_PROGRESS = "preparing the safety snapshot"
RECORD_READ_RETRIES = 5  # a job record that exists but cannot be read right now (being replaced) is read again
RECORD_READ_RETRY_SECONDS = 0.02
DEFAULT_JOB_TIMEOUT_SECONDS = 30 * 60
JOB_REPLY_LIMIT = 20000
JOB_ID_RANDOM_BYTES = 6  # bytes -> 12 hex characters
JOB_ID_PATTERN = re.compile(r"^j-[0-9a-f]{12}$")
JOBS_DIR_NAME = "jobs"
INTERRUPTED_JOB_MESSAGE = "interrupted: the app was shut down while the job was running"
PUBLIC_JOB_FIELDS = ("id", "type", "state", "started_at", "finished_at", "cost_usd", "reply", "error")
PROGRESS_JOB_FIELDS = ("progress",)  # only jobs with a preparation step (`before_start`) carry it
TASK_JOB_FIELDS = ("steps", "result")  # only task jobs carry them
CODE_JOB_FIELDS = ("code",)  # only a job that ended with a coded error (`ClaudeJobError`) carries it


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def build_jobs_dir(repo_path):
    return os.path.join(loop_state.internal_dir(repo_path), JOBS_DIR_NAME)


class IClaudeJobService(Protocol):
    def start_job(self, repo_path, job_type, command) -> dict: ...
    def list_jobs(self, repo_path) -> list: ...
    def get_job(self, repo_path, job_id) -> dict: ...
    def get_running_job(self, repo_path): ...
    def get_last_session(self, repo_path, job_types) -> str: ...
    def is_busy(self) -> bool: ...


class ClaudeJobError(Exception):
    """Raised by `before_start` to end the job as `error` with a stable `code` next to the message (the panel maps the code like the
    4xx the same refusal gave when it was answered synchronously)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class ClaudeJobCommand:
    """What to run: ready argv (built by the ClaudeClient) and the stdin text.

    `timeout_seconds` (optional) replaces the service timeout for this job. `finalize(result) -> result` (optional) runs in
    the job thread after claude ends and before the job is recorded as finished (e.g. the planning file guard and thread).
    `work_dir` (optional) is the folder claude runs in (the loop worktree); default = the repo path the job is registered under.
    `before_start(update_progress)` (optional) runs in the job thread BEFORE claude (e.g. the safety snapshot of a map job): the
    record says `progress: "preparing the safety snapshot"` meanwhile, `update_progress(text)` replaces that text, and an exception
    ends the job as `error` with its message (claude never starts and `finalize` does not run); a `ClaudeJobError` also puts its `code`
    in the record (and in the public job). `before_start` may RETURN a dict `{"argv": [...], "stdin_text": "..."}` (either key): those
    replace the command's argv / stdin, so the chat sends build the final command in the job thread (None = keep the command as is).
    `parallel_key` (optional, a normalized folder): the job may run next to other jobs of the SAME repository that also carry a
    `parallel_key`, as long as the keys differ (the subproject maps of a monorepo). Without it the job is exclusive both ways."""

    def __init__(self, argv, stdin_text="", resumed_session="", timeout_seconds=None, finalize=None, work_dir=None, before_start=None,
                 parallel_key=None, on_prepare_failed=None):
        self.parallel_key = parallel_key
        self.argv = list(argv)
        self.stdin_text = stdin_text
        self.resumed_session = resumed_session
        self.timeout_seconds = timeout_seconds
        self.finalize = finalize
        self.work_dir = work_dir
        self.before_start = before_start
        self.on_prepare_failed = on_prepare_failed  # optional `fn(error)`: runs in the job thread when `before_start` raised (undo what the request persisted)


class ClaudeJobService:
    def __init__(self, claude_client, specs_file_repository, log, timeout_seconds=DEFAULT_JOB_TIMEOUT_SECONDS):
        self.claude_client = claude_client
        self.specs_file_repository = specs_file_repository
        self.log = log
        self.timeout_seconds = timeout_seconds
        self.jobs_lock = threading.Lock()
        self.running_jobs = {}  # id -> repo path
        self.parallel_keys = {}  # id -> parallel key, only for the jobs that may run next to others (see ClaudeJobCommand)

    # ------------------------------------------------ launch
    def start_job(self, repo_path, job_type, command):
        """Writes the job as `running` and starts the thread. JobInProgressError when a job is already running in this repo."""
        record = {"id": "j-" + secrets.token_hex(JOB_ID_RANDOM_BYTES), "type": job_type, "state": JOB_STATE_RUNNING,
                  "started_at": now_iso(), "finished_at": None, "cost_usd": None, "reply": "", "error": "",
                  "dir": repo_path, "session_id": command.resumed_session,
                  "order": time.time_ns()}  # internal ordering tiebreak (several jobs in the same second); never leaves through the API
        if command.before_start is not None:
            record["progress"] = PREPARING_SNAPSHOT_PROGRESS
        with self.jobs_lock:
            if self.find_blocking_id(repo_path, command.parallel_key):
                raise JobInProgressError("a claude job is already running in this project: wait for it to finish")
            self.running_jobs[record["id"]] = repo_path
            if command.parallel_key:
                self.parallel_keys[record["id"]] = command.parallel_key
        try:
            self.write_record(repo_path, record)
            threading.Thread(target=self.run_job, args=(record, command), daemon=True,
                             name="claude-job-" + record["id"]).start()
        except Exception:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
                self.parallel_keys.pop(record["id"], None)
            raise
        self.log.info("claude_job_started", job=record["id"], type=job_type, repo=repo_path)
        return self.to_public(record)

    def start_task_job(self, repo_path, job_type, task):
        """Same registry and files as `start_job`, but the thread runs `task(add_step) -> result dict` instead of `claude -p`.

        `add_step(step)` appends a step (a dict) to the job record, written to disk so a poll sees it. `task` returns
        {"ok": bool, "result": any, "error": str}; an exception becomes an error job. JobInProgressError while any job
        (claude or task) runs in this repo."""
        record = {"id": "j-" + secrets.token_hex(JOB_ID_RANDOM_BYTES), "type": job_type, "state": JOB_STATE_RUNNING,
                  "started_at": now_iso(), "finished_at": None, "cost_usd": None, "reply": "", "error": "",
                  "dir": repo_path, "session_id": "", "steps": [], "result": None, "order": time.time_ns()}
        with self.jobs_lock:
            if self.find_running_id(repo_path):
                raise JobInProgressError("a job is already running in this project: wait for it to finish")
            self.running_jobs[record["id"]] = repo_path
        try:
            self.write_record(repo_path, record)
            threading.Thread(target=self.run_task_job, args=(record, task), daemon=True, name="task-job-" + record["id"]).start()
        except Exception:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
            raise
        self.log.info("task_job_started", job=record["id"], type=job_type, repo=repo_path)
        return self.to_public(record)

    def run_task_job(self, record, task):
        def add_step(step):
            with self.jobs_lock:
                record["steps"].append(step)
            self.try_write_record(record)

        try:
            outcome = task(add_step)
        except Exception as failure:  # a job never takes the app down
            self.log.error("task_job_failed", job=record["id"], type=type(failure).__name__, message=str(failure)[:200])
            outcome = {"ok": False, "result": None, "error": "internal failure: %s" % type(failure).__name__}
        record.update(state=JOB_STATE_OK if outcome.get("ok") else JOB_STATE_ERROR, finished_at=now_iso(),
                      error=str(outcome.get("error") or ""), result=outcome.get("result"))
        try:
            self.try_write_record(record)
        finally:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
        self.log.info("task_job_finished", job=record["id"], type=record["type"], state=record["state"])

    def try_write_record(self, record):
        try:
            self.write_record(record["dir"], record)
        except OSError as failure:
            self.log.error("claude_job_write_failed", job=record["id"], message=str(failure)[:200])

    def run_job(self, record, command):
        timeout_seconds = min(command.timeout_seconds or self.timeout_seconds, self.timeout_seconds)
        result = self.run_before_start(record, command)
        if result is None:
            try:
                with call_kind(record["type"]):
                    result = self.claude_client.run_prompt(command.argv, command.work_dir or record["dir"], command.stdin_text, timeout_seconds)
            except Exception as failure:  # a job never takes the app down
                result = {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "internal failure: %s" % type(failure).__name__}
            if command.finalize is not None:
                result = self.finalize_result(record, command, result)
        self.record_finished(record, result)

    def run_before_start(self, record, command):
        """None when claude may start; otherwise the error result of the failed preparation."""
        if command.before_start is None:
            return None
        try:
            prepared = command.before_start(lambda text: self.update_progress(record, text))
        except Exception as failure:  # the preparation failed: the job ends here with the reason visible in the record
            message = str(failure)[:500] or type(failure).__name__
            self.log.error("claude_job_before_start_failed", job=record["id"], type=type(failure).__name__, message=message[:200])
            result = {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": message}
            self.run_prepare_failed_hook(record, command, failure)
            if isinstance(failure, ClaudeJobError) and failure.code:
                result["code"] = str(failure.code)
            return result
        if isinstance(prepared, dict):
            if prepared.get("argv") is not None:
                command.argv = list(prepared["argv"])
            if prepared.get("stdin_text") is not None:
                command.stdin_text = prepared["stdin_text"]
        self.update_progress(record, "")
        return None

    def run_prepare_failed_hook(self, record, command, failure):
        if command.on_prepare_failed is None:
            return
        try:
            command.on_prepare_failed(failure)
        except Exception as hook_failure:  # the undo never keeps the job from ending
            self.log.error("claude_job_prepare_failed_hook_failed", job=record["id"], type=type(hook_failure).__name__, message=str(hook_failure)[:200])

    def update_progress(self, record, text):
        record["progress"] = str(text or "")
        self.try_write_record(record)

    def finalize_result(self, record, command, result):
        if command.finalize is not None:
            try:
                result = command.finalize(result)
            except Exception as failure:  # the finalization never leaves the job running forever
                self.log.error("claude_job_finalize_failed", job=record["id"], type=type(failure).__name__, message=str(failure)[:200])
                result = dict(result, ok=False, error="internal failure while finishing the job: %s" % type(failure).__name__)
        return result

    def record_finished(self, record, result):
        record.update(state=JOB_STATE_OK if result.get("ok") else JOB_STATE_ERROR, finished_at=now_iso(),
                      cost_usd=result.get("cost_usd"), reply=str(result.get("reply") or "")[:JOB_REPLY_LIMIT],
                      error=str(result.get("error") or ""), session_id=result.get("session_id") or record.get("session_id") or "")
        if result.get("code"):
            record["code"] = str(result["code"])
        try:
            self.write_record(record["dir"], record)
        except OSError as failure:
            self.log.error("claude_job_write_failed", job=record["id"], message=str(failure)[:200])
        finally:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
                self.parallel_keys.pop(record["id"], None)
        self.log.info("claude_job_finished", job=record["id"], type=record["type"], state=record["state"],
                      cost_usd=record["cost_usd"])

    # ------------------------------------------------ read
    def list_jobs(self, repo_path):
        records = [self.read_corrected_record(path) for path in self.specs_file_repository.list_json_files(build_jobs_dir(repo_path))]
        records = [record for record in records if record]
        records.sort(key=self.get_order_key, reverse=True)
        return [self.to_public(record) for record in records]

    def get_job(self, repo_path, job_id):
        record = self.read_record_by_id(repo_path, job_id)
        if not record:
            raise JobNotFoundError("job not found")
        return self.to_public(record)

    def get_running_job(self, repo_path):
        job_id = self.find_running_id(repo_path)
        record = self.read_record_by_id(repo_path, job_id) if job_id else None
        return self.to_public(record) if record else None

    def get_blocking_job(self, repo_path, parallel_key):
        """The running job of this repository that would stop a job with `parallel_key` from starting (None = it may start): any
        job without a parallel key, and the one with the same key."""
        job_id = self.find_blocking_id(repo_path, parallel_key)
        record = self.read_record_by_id(repo_path, job_id) if job_id else None
        return self.to_public(record) if record else None

    def get_last_session(self, repo_path, job_types):
        """session_id of the most recent job (finished or not) of one of `job_types` that has a session; '' when none."""
        records = [self.read_corrected_record(path) for path in self.specs_file_repository.list_json_files(build_jobs_dir(repo_path))]
        candidates = [record for record in records if record and record.get("type") in job_types and record.get("session_id")]
        candidates.sort(key=self.get_order_key, reverse=True)
        return str(candidates[0]["session_id"]) if candidates else ""

    def is_busy(self):
        """IBusyProvider: the app does not shut down for idleness while a claude job is running."""
        with self.jobs_lock:
            return bool(self.running_jobs)

    # ------------------------------------------------ support
    def find_running_id(self, repo_path):
        target = os.path.normcase(os.path.abspath(repo_path))
        for job_id, repo in list(self.running_jobs.items()):
            if os.path.normcase(os.path.abspath(repo)) == target:
                return job_id
        return ""

    def find_blocking_id(self, repo_path, parallel_key=None):
        """Id of a running job in `repo_path` that blocks a new job: without `parallel_key` any running job does; with one only the
        jobs that have no key or the same key. Called under `jobs_lock` by `start_job`."""
        target = os.path.normcase(os.path.abspath(repo_path))
        for job_id, repo in list(self.running_jobs.items()):
            if os.path.normcase(os.path.abspath(repo)) != target:
                continue
            running_key = self.parallel_keys.get(job_id)
            if not parallel_key or not running_key or running_key == parallel_key:
                return job_id
        return ""

    def read_record_by_id(self, repo_path, job_id):
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
            return None
        return self.read_corrected_record(os.path.join(build_jobs_dir(repo_path), job_id + ".json"))

    def read_corrected_record(self, path):
        record = self.specs_file_repository.read_json(path)
        for _ in range(RECORD_READ_RETRIES):  # Windows: a read can fail while the job thread replaces the file (progress updates)
            if record.get("id") or not os.path.exists(path):
                break
            time.sleep(RECORD_READ_RETRY_SECONDS)
            record = self.specs_file_repository.read_json(path)
        if not record.get("id"):
            return None
        if record.get("state") == JOB_STATE_RUNNING and record["id"] not in self.running_jobs:
            record = dict(record, state=JOB_STATE_ERROR, error=INTERRUPTED_JOB_MESSAGE)
        return record

    def write_record(self, repo_path, record):
        self.specs_file_repository.write_json_atomic(os.path.join(build_jobs_dir(repo_path), record["id"] + ".json"), record)

    @staticmethod
    def get_order_key(record):
        return str(record.get("started_at") or ""), record.get("order") or 0

    @staticmethod
    def to_public(record):
        fields = (PUBLIC_JOB_FIELDS + (TASK_JOB_FIELDS if "steps" in record else ()) + (PROGRESS_JOB_FIELDS if "progress" in record else ())
                  + (CODE_JOB_FIELDS if record.get("code") else ()))
        return {field: (list(record.get(field)) if field == "steps" else record.get(field)) for field in fields}
