"""App domain entities: Project (a folder opened in the panel) and ProjectContext (where the loop data lives)."""
import json
import os
from dataclasses import dataclass, field

LOOP_WORKTREE_SUFFIX = "-worktree-loop"


LOOP_STATE_FOLDER_PARTS = (".specs", "project", "loop")


def has_loop_started(worktree):
    """True once a loop REALLY ran in this worktree: LOOP.json, a lock file, a turn record or `turns` > 0 in meta.json.
    A bare worktree (created by Create workspace, or by the loop before its first turn) has only meta.json with turns 0."""
    loop_folder = os.path.join(worktree, *LOOP_STATE_FOLDER_PARTS)
    if os.path.isfile(os.path.join(loop_folder, "json", "LOOP.json")) or os.path.isfile(os.path.join(loop_folder, "internal", "LOOP.lock")):
        return True
    try:
        if any(name.endswith(".json") for name in os.listdir(os.path.join(loop_folder, "internal", "turns"))):
            return True
    except OSError:
        pass
    try:
        with open(os.path.join(loop_folder, "json", "meta.json"), encoding="utf-8") as handle:
            turns = json.load(handle).get("turns")
    except (OSError, ValueError, AttributeError):
        return False
    return isinstance(turns, int) and not isinstance(turns, bool) and turns > 0


@dataclass
class Project:
    """A project folder known to the app. `id` is stable (short hash of the normalized path)."""

    id: str
    name: str
    repo_path: str
    opened_at: str = ""
    last_opened_at: str = ""
    favorite: bool = False

    def loop_worktree_path(self):
        """Sibling worktree created by the engine (`../<name>-worktree-loop`)."""
        return os.path.join(os.path.dirname(self.repo_path), os.path.basename(self.repo_path) + LOOP_WORKTREE_SUFFIX)


@dataclass
class RepoRef:
    """One git repository of a project. The PRIMARY repo holds `.specs/` and is where the loop runs.

    `worktree_path` empty = the default sibling folder (`../<folder>-worktree-loop`), same as a single-repo project today.
    `copy` lists git-ignored files (relative to the repo) copied into the worktree when it is created.
    """

    name: str
    path: str
    primary: bool = False
    worktree_path: str = ""
    copy: list = field(default_factory=list)
    setup: str = ""  # one-time command run in this repo's worktree after it is created (empty = auto-detected by the engine)
    copy_configured: bool = False  # False = REPOS.json had no `copy` key: the engine copies the suggested local files; True with [] = none

    def loop_worktree_path(self):
        return self.worktree_path or os.path.join(os.path.dirname(self.path), os.path.basename(self.path) + LOOP_WORKTREE_SUFFIX)


@dataclass
class ProjectContext:
    """What a service needs to read/write the files of ONE project.

    `work_dir` is re-evaluated on every call: the loop worktree only becomes the work dir once a loop really ran in it (a bare
    worktree, e.g. from Create workspace, changes nothing: planning keeps reading and writing the primary repo's `.specs/`).
    Without a worktree, the data lives in the repo itself (status `no_loop`).
    """

    project: Project
    fixed_work_dir: str = ""
    extras: dict = field(default_factory=dict)

    @property
    def repo_path(self):
        return self.project.repo_path

    @property
    def work_dir(self):
        if self.fixed_work_dir:
            return self.fixed_work_dir
        worktree = self.project.loop_worktree_path()
        return worktree if os.path.isdir(worktree) and has_loop_started(worktree) else self.project.repo_path

    @property
    def specs_dir(self):
        return os.path.join(self.work_dir, ".specs")


class ProjectNotFoundError(LookupError):
    """Project id the registry does not know (becomes 404 in the API)."""
