"""Chat threads (read-only) and running jobs.

`ChatRepository`: one thread per file in `<work>/.specs/project/loop/internal/chat/<thread>.json` (atomic write, up to
200 messages; keeps the claude `session_id`, which the API never returns).
`ChatJobRepository`: chat and action router jobs, in the process memory (they vanish when the app restarts).
"""
import os
import re
import secrets
import threading
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, now_iso, read_json_object, write_json_atomic

THREADS_FOLDER_NAME = "chat"
THREAD_FILE_EXTENSION = ".json"
MAX_MESSAGES_PER_THREAD = 200
MAX_KEPT_JOBS = 100
THREAD_ID_PATTERN = re.compile(r"^t-[0-9a-f]{12}$")
THREAD_ID_PREFIX = "t-"
JOB_ID_PREFIX = "j-"
ID_RANDOM_BYTES = 6
JOB_STATE_RUNNING = "running"
THREAD_BUSY_REASON = "thread_busy"
TOTAL_LIMIT_REASON = "total_limit"
PROJECT_ID_INTERNAL_KEY = "_project_id"


def generate_thread_id():
    return THREAD_ID_PREFIX + secrets.token_hex(ID_RANDOM_BYTES)


class IChatRepository(Protocol):
    def read_thread(self, work_dir, thread_id): ...
    def write_thread(self, work_dir, thread) -> None: ...
    def list_threads(self, work_dir) -> list: ...


class ChatRepository:
    def get_threads_dir(self, work_dir):
        return os.path.join(get_loop_internal_dir(work_dir), THREADS_FOLDER_NAME)

    def get_thread_path(self, work_dir, thread_id):
        """None for an id outside the `t-<12 hex>` format (never builds a path from free text)."""
        if not isinstance(thread_id, str) or not THREAD_ID_PATTERN.fullmatch(thread_id):
            return None
        return os.path.join(self.get_threads_dir(work_dir), thread_id + THREAD_FILE_EXTENSION)

    def read_thread(self, work_dir, thread_id):
        """Thread dict, or None when it does not exist, the id is invalid or the file does not have the expected format."""
        path = self.get_thread_path(work_dir, thread_id)
        data = read_json_object(path) if path else {}
        if data.get("thread") != thread_id or not isinstance(data.get("messages"), list):
            return None
        return data

    def write_thread(self, work_dir, thread):
        thread["messages"] = thread["messages"][-MAX_MESSAGES_PER_THREAD:]
        thread["updated_at"] = now_iso()
        write_json_atomic(self.get_thread_path(work_dir, thread["thread"]), thread)

    def list_threads(self, work_dir):
        try:
            names = os.listdir(self.get_threads_dir(work_dir))
        except OSError:
            return []
        threads = []
        for name in names:
            if not name.endswith(THREAD_FILE_EXTENSION):
                continue
            thread = self.read_thread(work_dir, name[:-len(THREAD_FILE_EXTENSION)])
            if thread is not None:
                threads.append(thread)
        return threads


class IChatJobRepository(Protocol):
    def reserve_job(self, project_id, thread_id, total_limit, initial_fields=None) -> tuple: ...
    def finish_job(self, job_id, result) -> None: ...
    def get_job(self, job_id): ...
    def count_running_jobs(self) -> int: ...
    def has_running_job_in_thread(self, thread_id) -> bool: ...


class ChatJobRepository:
    """Jobs of every project of the app (the concurrency limit is the app's: each job is a `claude -p`)."""

    def __init__(self):
        self.lock = threading.Lock()
        self.jobs = {}

    def reserve_job(self, project_id, thread_id, total_limit, initial_fields=None):
        """Checks the limits and records the job as `running` in one step. (job_id, None) or (None, reason)."""
        with self.lock:
            running = [job for job in self.jobs.values() if job["state"] == JOB_STATE_RUNNING]
            if thread_id is not None and any(job["thread"] == thread_id for job in running):
                return None, THREAD_BUSY_REASON
            if len(running) >= total_limit:
                return None, TOTAL_LIMIT_REASON
            job_id = JOB_ID_PREFIX + secrets.token_hex(ID_RANDOM_BYTES)
            job = {"state": JOB_STATE_RUNNING, "reply": None, "cost_usd": None, "error": None, "thread": thread_id}
            job.update(initial_fields or {})
            job[PROJECT_ID_INTERNAL_KEY] = project_id
            self.jobs[job_id] = job
            self.discard_old_jobs()
            return job_id, None

    def discard_old_jobs(self):
        """Call with the lock held: keeps at most MAX_KEPT_JOBS, dropping the oldest finished ones."""
        while len(self.jobs) > MAX_KEPT_JOBS:
            oldest = next((key for key, job in self.jobs.items() if job["state"] != JOB_STATE_RUNNING), None)
            if oldest is None:
                return
            self.jobs.pop(oldest)

    def finish_job(self, job_id, result):
        with self.lock:
            project_id = (self.jobs.get(job_id) or {}).get(PROJECT_ID_INTERNAL_KEY)
            self.jobs[job_id] = dict(result, **{PROJECT_ID_INTERNAL_KEY: project_id})

    def get_job(self, job_id):
        """Copy of the job with `project_id` (internal field), or None."""
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            copy = {key: value for key, value in job.items() if key != PROJECT_ID_INTERNAL_KEY}
            copy["project_id"] = job.get(PROJECT_ID_INTERNAL_KEY)
            return copy

    def count_running_jobs(self):
        with self.lock:
            return sum(1 for job in self.jobs.values() if job["state"] == JOB_STATE_RUNNING)

    def has_running_job_in_thread(self, thread_id):
        with self.lock:
            return any(job["state"] == JOB_STATE_RUNNING and job["thread"] == thread_id for job in self.jobs.values())
