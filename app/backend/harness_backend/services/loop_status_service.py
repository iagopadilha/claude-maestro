"""Loop status of a project and app occupancy (a loop running in some project).

Derivation rules (docs/API.md): `LOOP.lock` with a live PID = `running`; no lock and LOOP.json at `continue` = `stopped`
(the script died), or `error` when `meta.json` recorded an error stop after LOOP.json; no LOOP.json = `no_loop`.
"""
import os
import re
import sys
from typing import Protocol

from ..domain.project import ProjectNotFoundError
from ..domain.project_errors import RepoInUseError
from ..repositories.loop_file_repository import loop_state

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import project_repos  # noqa: E402

STATUS_RUNNING = "running"
STATUS_STOPPED = "stopped"
STATUS_ERROR = "error"
STATUS_NO_LOOP = "no_loop"
STATUS_CONTINUE = "continue"
STOPPED_BY_ERROR = "error"
STATUS_AWAITING_USER = "awaiting_user"
REPO_IN_USE_CODE = "repo_in_use"
STAGE_TITLE_FORMAT = "Stage %s"


def normalize_repo_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def to_text_list(value):
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [str(value)] if isinstance(value, str) and value.strip() else []


class ILoopStatusService(Protocol):
    def get_loop_summary(self, context) -> dict:
        """{"status","running","pid","json","meta","reason"}."""

    def is_any_loop_running(self) -> bool:
        """True if some registered project has a `LOOP.lock` with a live PID."""

    def ensure_repos_free(self, context) -> None:
        """RepoInUseError when a repo of this project is used by the running loop of ANOTHER project."""

    def list_running_loops(self) -> list:
        """One item per project whose `LOOP.lock` PID is alive (the shape of `GET /api/app/overview`)."""


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

    # ------------------------------------------------ repos in use (several projects, several repos)
    def list_used_repo_paths(self, context):
        """{normalized path: repo name} of everything this project's loop uses or listed: its repos and `meta.repos`."""
        used = {normalize_repo_path(repo.path): repo.name for repo in project_repos.load_repos(context.repo_path)}
        used.setdefault(normalize_repo_path(context.repo_path), os.path.basename(os.path.normpath(context.repo_path)))
        meta_repos = self.loop_file_repository.read_meta(context.work_dir).get("repos")
        for name, item in (meta_repos.items() if isinstance(meta_repos, dict) else []):
            if isinstance(item, dict) and item.get("path"):
                used.setdefault(normalize_repo_path(item["path"]), str(name))
        return used

    def find_repo_in_use(self, context):
        """(repo name, other project) of the first repo of this project that another RUNNING loop uses; None when free."""
        mine = {normalize_repo_path(repo.path): repo.name for repo in project_repos.load_repos(context.repo_path)}
        mine.setdefault(normalize_repo_path(context.repo_path), os.path.basename(os.path.normpath(context.repo_path)))
        for project in self.project_service.list_projects():
            if project.id == context.project.id:
                continue
            try:
                other = self.project_service.get_project_context(project.id)
            except ProjectNotFoundError:
                continue
            if not self.is_loop_running(other):
                continue
            for path in self.list_used_repo_paths(other):
                if path in mine:
                    return mine[path], project
        return None

    def ensure_repos_free(self, context):
        found = self.find_repo_in_use(context)
        if found is None:
            return
        repo_name, project = found
        raise RepoInUseError("Repo %s is in use by the running loop of project '%s'." % (repo_name, project.name), code=REPO_IN_USE_CODE,
                             extras={"repo": repo_name, "project": {"id": project.id, "name": project.name}})

    # ------------------------------------------------ overview of the running loops
    def list_running_loops(self):
        loops = []
        for project in self.project_service.list_projects():
            try:
                context = self.project_service.get_project_context(project.id)
            except ProjectNotFoundError:
                continue
            if self.is_loop_running(context):
                loops.append(self.build_running_loop_item(project, context))
        return loops

    def build_running_loop_item(self, project, context):
        work_dir = context.work_dir
        loop_json = self.loop_file_repository.read_loop_json(work_dir)
        meta = self.loop_file_repository.read_meta(work_dir)
        awaiting = loop_json.get("status") == STATUS_AWAITING_USER
        features = to_text_list(loop_json.get("feature"))
        meta_repos = meta.get("repos")
        repo_names = list(meta_repos) if isinstance(meta_repos, dict) and meta_repos else [repo.name for repo in project_repos.load_repos(context.repo_path)[:1]]
        question = None
        if awaiting:
            question = {"reason": loop_json.get("reason"), "options": to_text_list(loop_json.get("options")), "since": loop_json.get("at")}
        cost = meta.get("cost_usd")
        return {"project_id": project.id, "name": project.name, "url": "/p/%s/" % project.id, "running": True,
                "status": STATUS_AWAITING_USER if awaiting else STATUS_RUNNING, "stage": self.describe_stage(work_dir, features),
                "feature": features, "turn": self.compute_running_turn(work_dir, loop_json, meta),
                "cost_usd": cost if isinstance(cost, (int, float)) and not isinstance(cost, bool) else None,
                "repos": repo_names, "question": question}

    def describe_stage(self, work_dir, features):
        """`Stage 2: API` of the first running feature (its ROADMAP stage label + the matching `## Stage` title); None when unknown."""
        parsed, _ = loop_state.parse_roadmap(self.loop_file_repository.read_roadmap(work_dir))
        label = next((parsed[slug].get("stage") for slug in features if slug in parsed and parsed[slug].get("stage")), "")
        if not label:
            return None
        text = self.loop_file_repository.read_roadmap(work_dir)
        for line in text.splitlines():
            found = re.match(r"^#{1,6}\s+(Stage\s+%s\b.*?)\s*#*\s*$" % re.escape(label), line.strip(), re.I)
            if found:
                return found.group(1).strip()
        return STAGE_TITLE_FORMAT % label

    def compute_running_turn(self, work_dir, loop_json, meta):
        """The turn in progress: the next one after the last one done (same rule as the panel while running)."""
        numbers = self.loop_file_repository.list_turn_numbers(work_dir)
        done = [value for value in (meta.get("turns"), loop_json.get("turn")) if isinstance(value, (int, float)) and not isinstance(value, bool)]
        return int(max(done + numbers[-1:] + [0])) + 1
