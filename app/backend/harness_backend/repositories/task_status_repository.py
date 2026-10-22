"""Read side of the task waves of a feature: tasks.md (titles, deps, files) joined with the status file written by `harness.py tasks set`.

The format and the wave math belong to the engine (`task_waves`); nothing of it is rewritten here.
"""
import os
from typing import Protocol

from .specs_file_repository import ENGINE_DIR  # noqa: F401  (also puts the engine folder on sys.path)
import task_waves  # noqa: E402

PENDING_STATUS = "pending"
SOURCE_TASKS_MARKDOWN = "tasks.md"
SOURCE_NONE = "none"


class ITaskStatusRepository(Protocol):
    def read_feature_tasks(self, work_dir, slug): ...


class TaskStatusRepository:
    def read_feature_tasks(self, work_dir, slug):
        """`{"source", "max_parallel", "tasks": [{id, title, status, wave, deps, files, parallel, note}], "waves": [{n, task_ids, split_by_limit}]}`.

        Tasks come in wave order. A missing or unreadable status file = every task `pending`; a feature without tasks.md = `source` "none"
        with empty `tasks` and `waves`."""
        max_parallel = task_waves.read_default_max_parallel(work_dir)
        parsed = task_waves.parse_tasks(self.read_tasks_markdown(work_dir, slug))
        if not parsed["tasks"]:
            return {"source": SOURCE_NONE, "max_parallel": max_parallel, "tasks": [], "waves": []}
        recorded = task_waves.read_status(work_dir, slug)["tasks"]
        planned_waves = self.build_planned_waves(parsed["tasks"], max_parallel)
        wave_numbers = {task_id: wave["n"] for wave in planned_waves for task_id in wave["task_ids"]}
        rows = []
        for position, task in enumerate(parsed["tasks"]):
            entry = recorded.get(task["id"]) if isinstance(recorded.get(task["id"]), dict) else {}
            status = entry.get("status") if entry.get("status") in task_waves.STATUS_VALUES else PENDING_STATUS
            wave = wave_numbers.get(task["id"]) or entry.get("wave")
            rows.append((wave if isinstance(wave, int) else len(parsed["tasks"]) + 1, position,
                         {"id": task["id"], "title": task["title"], "status": status, "wave": wave, "deps": list(task["deps"]),
                          "files": list(task["files"]), "parallel": bool(task["parallel"]), "note": str(entry.get("note") or "")}))
        rows.sort(key=lambda row: (row[0], row[1]))
        return {"source": SOURCE_TASKS_MARKDOWN, "max_parallel": max_parallel, "tasks": [row[2] for row in rows], "waves": planned_waves}

    @staticmethod
    def build_planned_waves(tasks, max_parallel):
        """`[{n, task_ids, split_by_limit}]` of the full plan (nothing done); [] when the engine reports a problem (unknown dependency, cycle).

        `split_by_limit` is true when, without the parallel limit, a task of the next wave would have run in the same wave as one of this wave."""
        limited = task_waves.build_waves(tasks, max_parallel)["waves"]
        if not limited:
            return []
        unlimited = task_waves.build_waves(tasks, len(tasks))["waves"]
        unlimited_number = {task_id: number for number, wave in enumerate(unlimited, 1) for task_id in wave}
        rows = []
        for index, wave in enumerate(limited):
            following = limited[index + 1] if index + 1 < len(limited) else []
            numbers_of_this_wave = {unlimited_number.get(task_id) for task_id in wave}
            split_by_limit = any(unlimited_number.get(task_id) in numbers_of_this_wave for task_id in following)
            rows.append({"n": index + 1, "task_ids": list(wave), "split_by_limit": split_by_limit})
        return rows

    @staticmethod
    def read_tasks_markdown(work_dir, slug):
        path = os.path.join(task_waves.loop_state.feature_dir(work_dir, slug), task_waves.TASKS_FILE_NAME)
        return task_waves.loop_state.read(path)
