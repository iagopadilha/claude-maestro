"""`claude -p` jobs launched by the app (generate/refine the roadmap, map the codebase): they run in a thread, one per
project at a time, and are written to `<repo>/.specs/project/loop/internal/jobs/<id>.json`.

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

JOB_TYPE_ROADMAP = "roadmap"
JOB_TYPE_ROADMAP_REFINE = "roadmap_refine"
JOB_TYPE_MAP = "map"
ROADMAP_JOB_TYPES = (JOB_TYPE_ROADMAP, JOB_TYPE_ROADMAP_REFINE)
JOB_STATE_RUNNING = "running"
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
DEFAULT_JOB_TIMEOUT_SECONDS = 30 * 60
JOB_REPLY_LIMIT = 20000
JOB_ID_RANDOM_BYTES = 6  # bytes -> 12 hex characters
JOB_ID_PATTERN = re.compile(r"^j-[0-9a-f]{12}$")
JOBS_DIR_NAME = "jobs"
INTERRUPTED_JOB_MESSAGE = "interrupted: the app was shut down while the job was running"
PUBLIC_JOB_FIELDS = ("id", "type", "state", "started_at", "finished_at", "cost_usd", "reply", "error")


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


class ClaudeJobCommand:
    """What to run: ready argv (built by the ClaudeClient) and the stdin text."""

    def __init__(self, argv, stdin_text="", resumed_session=""):
        self.argv = list(argv)
        self.stdin_text = stdin_text
        self.resumed_session = resumed_session


class ClaudeJobService:
    def __init__(self, claude_client, specs_file_repository, log, timeout_seconds=DEFAULT_JOB_TIMEOUT_SECONDS):
        self.claude_client = claude_client
        self.specs_file_repository = specs_file_repository
        self.log = log
        self.timeout_seconds = timeout_seconds
        self.jobs_lock = threading.Lock()
        self.running_jobs = {}  # id -> repo path

    # ------------------------------------------------ launch
    def start_job(self, repo_path, job_type, command):
        """Writes the job as `running` and starts the thread. JobInProgressError when a job is already running in this repo."""
        record = {"id": "j-" + secrets.token_hex(JOB_ID_RANDOM_BYTES), "type": job_type, "state": JOB_STATE_RUNNING,
                  "started_at": now_iso(), "finished_at": None, "cost_usd": None, "reply": "", "error": "",
                  "dir": repo_path, "session_id": command.resumed_session,
                  "order": time.time_ns()}  # internal ordering tiebreak (several jobs in the same second); never leaves through the API
        with self.jobs_lock:
            if self.find_running_id(repo_path):
                raise JobInProgressError("a claude job is already running in this project: wait for it to finish")
            self.running_jobs[record["id"]] = repo_path
        try:
            self.write_record(repo_path, record)
            threading.Thread(target=self.run_job, args=(record, command), daemon=True,
                             name="claude-job-" + record["id"]).start()
        except Exception:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
            raise
        self.log.info("claude_job_started", job=record["id"], type=job_type, repo=repo_path)
        return self.to_public(record)

    def run_job(self, record, command):
        try:
            result = self.claude_client.run_prompt(command.argv, record["dir"], command.stdin_text, self.timeout_seconds)
        except Exception as failure:  # a job never takes the app down
            result = {"ok": False, "reply": "", "cost_usd": None, "session_id": "", "error": "internal failure: %s" % type(failure).__name__}
        record.update(state=JOB_STATE_OK if result.get("ok") else JOB_STATE_ERROR, finished_at=now_iso(),
                      cost_usd=result.get("cost_usd"), reply=str(result.get("reply") or "")[:JOB_REPLY_LIMIT],
                      error=str(result.get("error") or ""), session_id=result.get("session_id") or record.get("session_id") or "")
        try:
            self.write_record(record["dir"], record)
        except OSError as failure:
            self.log.error("claude_job_write_failed", job=record["id"], message=str(failure)[:200])
        finally:
            with self.jobs_lock:
                self.running_jobs.pop(record["id"], None)
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

    def read_record_by_id(self, repo_path, job_id):
        if not isinstance(job_id, str) or not JOB_ID_PATTERN.match(job_id):
            return None
        return self.read_corrected_record(os.path.join(build_jobs_dir(repo_path), job_id + ".json"))

    def read_corrected_record(self, path):
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
        return {field: record.get(field) for field in PUBLIC_JOB_FIELDS}
