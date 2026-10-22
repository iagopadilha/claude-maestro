"""Read side of the task waves of a feature: tasks.md titles joined with the status file written by `harness.py tasks set`.

The format and the wave math belong to the engine (`task_waves`); nothing of it is rewritten here.
"""
import os
from typing import Protocol

from .specs_file_repository import ENGINE_DIR  # noqa: F401  (also puts the engine folder on sys.path)
import task_waves  # noqa: E402

PENDING_STATUS = "pending"


class ITaskStatusRepository(Protocol):
    def read_feature_tasks(self, work_dir, slug): ...


class TaskStatusRepository:
    def read_feature_tasks(self, work_dir, slug):
        """`{"tasks": [{id, title, status, wave, note}], "max_parallel": n}` in wave order.

        A missing or unreadable status file = every task `pending`; a feature without tasks.md = an empty list."""
        max_parallel = task_waves.read_default_max_parallel(work_dir)
        parsed = task_waves.parse_tasks(self.read_tasks_markdown(work_dir, slug))
        if not parsed["tasks"]:
            return {"tasks": [], "max_parallel": max_parallel}
        recorded = task_waves.read_status(work_dir, slug)["tasks"]
        wave_numbers = task_waves.plan_wave_numbers(parsed["tasks"], max_parallel)
        rows = []
        for position, task in enumerate(parsed["tasks"]):
            entry = recorded.get(task["id"]) if isinstance(recorded.get(task["id"]), dict) else {}
            status = entry.get("status") if entry.get("status") in task_waves.STATUS_VALUES else PENDING_STATUS
            wave = wave_numbers.get(task["id"]) or entry.get("wave")
            rows.append((wave if isinstance(wave, int) else len(parsed["tasks"]) + 1, position,
                         {"id": task["id"], "title": task["title"], "status": status, "wave": wave, "note": str(entry.get("note") or "")}))
        rows.sort(key=lambda row: (row[0], row[1]))
        return {"tasks": [row[2] for row in rows], "max_parallel": max_parallel}

    @staticmethod
    def read_tasks_markdown(work_dir, slug):
        path = os.path.join(task_waves.loop_state.feature_dir(work_dir, slug), task_waves.TASKS_FILE_NAME)
        return task_waves.loop_state.read(path)
