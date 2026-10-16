"""Registry of the projects known to the app (`projects.json` in ~/.loop-harness or LOOP_DATA_DIR).

Atomic write (temporary file + os.replace) guarded by a lock: several requests may open a project at the same time.
The project id is stable: short hash of the normalized path (same folder = same id, in any session).
"""
import dataclasses
import hashlib
import json
import os
import threading
from typing import Protocol

from ..domain.project import Project

PROJECT_ID_LENGTH = 12
REGISTRY_FORMAT_VERSION = 1


def normalize_project_path(path):
    """Absolute path, without symbolic links and with the system's case rules (on Windows `C:\\A` == `c:\\a`)."""
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def compute_project_id(path):
    return hashlib.sha1(normalize_project_path(path).encode("utf-8")).hexdigest()[:PROJECT_ID_LENGTH]


class IProjectRepository(Protocol):
    def list_projects(self) -> list: ...
    def get_project(self, project_id): ...
    def write_project(self, project) -> None: ...
    def remove_project(self, project_id) -> bool: ...


class ProjectRepository:
    def __init__(self, registry_path):
        self.registry_path = registry_path
        self.registry_lock = threading.RLock()

    def list_projects(self):
        with self.registry_lock:
            return [self.to_project(item) for item in self.read_registry_items()]

    def get_project(self, project_id):
        """Project or None."""
        for project in self.list_projects():
            if project.id == project_id:
                return project
        return None

    def write_project(self, project):
        """Inserts or replaces by id (keeps the position of an existing one)."""
        with self.registry_lock:
            items = self.read_registry_items()
            new_item = dataclasses.asdict(project)
            for position, item in enumerate(items):
                if item.get("id") == project.id:
                    items[position] = new_item
                    break
            else:
                items.append(new_item)
            self.write_registry_items(items)

    def remove_project(self, project_id):
        with self.registry_lock:
            items = self.read_registry_items()
            remaining = [item for item in items if item.get("id") != project_id]
            if len(remaining) == len(items):
                return False
            self.write_registry_items(remaining)
            return True

    def read_registry_items(self):
        try:
            with open(self.registry_path, encoding="utf-8") as handle:
                content = json.load(handle)
        except (OSError, ValueError):
            return []
        items = content.get("projects") if isinstance(content, dict) else None
        return [item for item in items if isinstance(item, dict) and item.get("id") and item.get("repo_path")] if isinstance(items, list) else []

    def write_registry_items(self, items):
        os.makedirs(os.path.dirname(self.registry_path) or ".", exist_ok=True)
        temporary = "%s.%d.tmp" % (self.registry_path, threading.get_ident())
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"version": REGISTRY_FORMAT_VERSION, "projects": items}, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, self.registry_path)

    @staticmethod
    def to_project(item):
        fields = {field.name for field in dataclasses.fields(Project)}
        return Project(**{key: value for key, value in item.items() if key in fields})
