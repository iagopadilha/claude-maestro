"""App domain entities: Project (a folder opened in the panel) and ProjectContext (where the loop data lives)."""
import os
from dataclasses import dataclass, field

LOOP_WORKTREE_SUFFIX = "-worktree-loop"


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
class ProjectContext:
    """What a service needs to read/write the files of ONE project.

    `work_dir` is re-evaluated on every call: the loop worktree may be created after the project was opened.
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
        return worktree if os.path.isdir(worktree) else self.project.repo_path

    @property
    def specs_dir(self):
        return os.path.join(self.work_dir, ".specs")


class ProjectNotFoundError(LookupError):
    """Project id the registry does not know (becomes 404 in the API)."""
