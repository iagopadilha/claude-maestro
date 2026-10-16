"""Projects known to the app (IProjectService): list, open an existing folder, create from scratch, forget.

When a project is opened with the app up, a compatible `MONITOR.json` is written to the `work` dir (loop worktree or repo):
the engine (`monitor_proc`) and the skill find the panel through it. It is only written when `.specs/` already exists
(opening a folder creates nothing in it) and it never overwrites the one of another live process. On shutdown, only the
ones this process wrote are deleted.
"""
import datetime
import os
import re
import threading
import urllib.parse
from typing import Protocol

from ..domain.project import Project, ProjectContext, ProjectNotFoundError
from ..domain.project_errors import InvalidPathError, ProjectAlreadyExistsError
from ..repositories.project_repository import compute_project_id
from ..repositories.specs_file_repository import loop_state

MONITOR_FILE_NAME = "MONITOR.json"
PROJECT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$")
PROJECT_NAME_MAX_LENGTH = 100
WINDOWS_RESERVED_NAMES = {"con", "prn", "aux", "nul"} | {"com%d" % number for number in range(1, 10)} | {"lpt%d" % number for number in range(1, 10)}
INITIAL_COMMIT_MESSAGE = "chore: initial project structure (loop-harness)"


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def build_relative_project_url(project_id):
    return "/p/%s/" % project_id


class IProjectService(Protocol):
    def list_projects(self) -> list: ...
    def get_project(self, project_id) -> Project: ...
    def get_project_context(self, project_id) -> ProjectContext: ...
    def open_existing_project(self, path) -> Project: ...
    def create_project_from_scratch(self, parent_path, name, init_git=True, conformance=None) -> Project: ...
    def forget_project(self, project_id) -> None: ...
    def release_projects_on_shutdown(self) -> None: ...


class ProjectService:
    def __init__(self, project_repository, specs_structure_service, specs_file_repository, git_client, process_runner, log,
                 get_app_url):
        self.project_repository = project_repository
        self.specs_structure_service = specs_structure_service
        self.specs_file_repository = specs_file_repository
        self.git_client = git_client
        self.process_runner = process_runner
        self.log = log
        self.get_app_url = get_app_url
        self.started_at = now_iso()
        self.monitor_lock = threading.Lock()
        self.written_monitors = {}  # MONITOR.json path -> project id
        self.registered_work_dirs = {}  # project id -> work dir where the MONITOR.json was written

    # ------------------------------------------------ read
    def list_projects(self):
        """Favorites first, then the most recent access."""
        projects = self.project_repository.list_projects()
        return sorted(projects, key=lambda project: (bool(project.favorite), str(project.last_opened_at or "")), reverse=True)

    def get_project(self, project_id):
        project = self.project_repository.get_project(project_id)
        if project is None:
            raise ProjectNotFoundError(project_id)
        return project

    def get_project_context(self, project_id):
        """Context with a re-evaluated `work_dir`; if the loop worktree was born (or `.specs/` appeared) after opening, the
        MONITOR.json starts to exist in the new work dir as well."""
        context = ProjectContext(self.get_project(project_id))
        if self.registered_work_dirs.get(project_id) != context.work_dir:
            self.register_monitor_in_work_dir(context)
        return context

    # ------------------------------------------------ open / create / forget
    def open_existing_project(self, path):
        """Registers (or updates `last_opened_at`) and writes the MONITOR.json. Idempotent: same folder = same id."""
        folder = self.validate_existing_folder(path)
        project_id = compute_project_id(folder)
        now = now_iso()
        existing = self.project_repository.get_project(project_id)
        if existing:
            existing.last_opened_at = now
            project = existing
        else:
            project = Project(id=project_id, name=os.path.basename(os.path.normpath(folder)) or folder, repo_path=folder,
                              opened_at=now, last_opened_at=now)
        self.project_repository.write_project(project)
        self.register_monitor_in_work_dir(ProjectContext(project))
        self.log.info("project_opened", project=project.id, path=folder, new=existing is None)
        return project

    def create_project_from_scratch(self, parent_path, name, init_git=True, conformance=None):
        validated_name = self.validate_project_name(name)
        parent_dir = self.validate_existing_folder(parent_path)
        destination = os.path.join(parent_dir, validated_name)
        if os.path.exists(destination) and (not os.path.isdir(destination) or os.listdir(destination)):
            raise ProjectAlreadyExistsError("%s already exists with content: choose another name or use Open folder" % destination)
        os.makedirs(destination, exist_ok=True)
        self.specs_structure_service.initialize_structure(destination, conformance, validated_name)
        if init_git:
            ok, error = self.git_client.init_repo_with_initial_commit(destination, INITIAL_COMMIT_MESSAGE)
            if not ok:  # the folder and .specs already exist: the project opens and the checklist shows git as pending
                self.log.warn("project_git_init_failed", path=destination, error=error)
        self.log.info("project_created", path=destination, git=bool(init_git))
        return self.open_existing_project(destination)

    def forget_project(self, project_id):
        """Only removes it from the registry (and releases this process's MONITOR.json); never deletes a project file."""
        self.get_project(project_id)
        self.release_monitors(lambda owner: owner == project_id)
        self.registered_work_dirs.pop(project_id, None)
        self.project_repository.remove_project(project_id)
        self.log.info("project_forgotten", project=project_id)

    def release_projects_on_shutdown(self):
        self.release_monitors(lambda owner: True)

    # ------------------------------------------------ MONITOR.json
    def register_monitor_in_work_dir(self, context):
        app_url = self.get_app_url() or ""
        work = context.work_dir
        if not app_url or not os.path.isdir(os.path.join(work, ".specs")):
            return
        path = loop_state.loop_path(work, MONITOR_FILE_NAME)
        with self.monitor_lock:
            existing = self.specs_file_repository.read_json(path)
            existing_pid = existing.get("pid")
            if existing and existing_pid != os.getpid() and self.process_runner.is_alive(existing_pid):
                self.log.warn("monitor_json_owned_by_other_process", path=path, pid=existing_pid)
                self.registered_work_dirs[context.project.id] = work
                return
            data = {"pid": os.getpid(), "port": urllib.parse.urlsplit(app_url).port,
                    "url": urllib.parse.urljoin(app_url, build_relative_project_url(context.project.id)),
                    "repo": context.repo_path, "work": work, "started_at": self.started_at}
            try:
                self.specs_file_repository.write_json_atomic(path, data)
            except OSError as failure:
                self.log.warn("monitor_json_not_written", path=path, message=str(failure)[:200])
                return
            self.written_monitors[path] = context.project.id
            self.registered_work_dirs[context.project.id] = work

    def release_monitors(self, belongs):
        with self.monitor_lock:
            for path, owner in list(self.written_monitors.items()):
                if not belongs(owner):
                    continue
                if self.specs_file_repository.read_json(path).get("pid") == os.getpid():
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                self.written_monitors.pop(path, None)

    # ------------------------------------------------ validation
    @staticmethod
    def validate_existing_folder(path):
        if not isinstance(path, str) or not path.strip():
            raise InvalidPathError("empty path")
        folder = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isdir(folder):
            raise InvalidPathError("not an existing folder: %s" % folder)
        return folder

    @staticmethod
    def validate_project_name(name):
        text = name.strip() if isinstance(name, str) else ""
        if not text or len(text) > PROJECT_NAME_MAX_LENGTH or not PROJECT_NAME_PATTERN.match(text) or text.endswith((".", " ")):
            raise InvalidPathError("invalid name: use letters, digits, space, dot, - and _ (no slash), starting with a letter or digit")
        if text.split(".")[0].lower() in WINDOWS_RESERVED_NAMES:
            raise InvalidPathError("name reserved by Windows: %s" % text)
        return text
