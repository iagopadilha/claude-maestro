"""Domain errors of the projects module. The controllers turn each type into an HTTP status (project_responses.py).

None of them knows HTTP: they say what happened, not how to answer.
"""


class InvalidPathError(ValueError):
    """A folder, name or path that does not work (does not exist, is not a folder, name with a separator...)."""


class ProjectAlreadyExistsError(ValueError):
    """Creating from scratch in a folder that already exists with content."""


class PathOutsideSpecsError(ValueError):
    """File path outside `.specs/` (`..`, absolute, symbolic link) or in a forbidden folder (`internal/`, `json/`)."""


class SpecFileNotFoundError(LookupError):
    """The requested file inside `.specs/` does not exist."""


class FileEditRefusedError(ValueError):
    """The file exists but the app cannot edit it (read-only by rule)."""


class FileVersionConflictError(ValueError):
    """The file changed since it was read (different sha1 version)."""

    def __init__(self, message, current_version):
        super().__init__(message)
        self.current_version = current_version


class LoopRunningError(RuntimeError):
    """The operation cannot happen while the loop is running in the project. `code` (optional) goes into the response."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class JobInProgressError(RuntimeError):
    """A claude job is already running in this project. `code` (optional) goes into the response."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class JobNotFoundError(LookupError):
    """Unknown (or invalid) job id in the project."""


class RoadmapSessionMissingError(LookupError):
    """Refining without a previous roadmap job with a claude session."""


class LoopWorktreeExistsError(RuntimeError):
    """Generating a roadmap in the repo while the loop worktree already exists (the real state lives in the worktree)."""


class InvalidRoadmapError(ValueError):
    """ROADMAP outside the grammar or without features (approving refuses it)."""


class InvalidRequestError(ValueError):
    """Incomplete or malformed request body (e.g. `confirm` other than true, empty goal)."""


class UnknownCredentialNameError(InvalidRequestError):
    """A test-environment value for a name TEST-ENVIRONMENT.md does not mention (400, `code` = unknown_name)."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


PLANNING_REQUIRED_CODE = "planning_required"
PLANNING_BUSY_CODE = "planning_busy"
PLANNING_PREREQUISITES_CODE = "planning_prerequisites"
PLANNING_GATES_CODE = "planning_gates"
LOOP_RUNNING_CODE = "loop_running"


class PlanningRequiredError(RuntimeError):
    """The project is in planning mode (roadmap never approved and no loop ever ran): the operation waits for the approval."""

    code = PLANNING_REQUIRED_CODE


class PlanningGatesError(ValueError):
    """Planning gates that are not ok. `gates` (the whole checklist) goes into the response so the front end shows why."""

    def __init__(self, message, gates, code):
        super().__init__(message)
        self.gates = gates
        self.code = code


class RepoRequestError(Exception):
    """Base of the multi-repo refusals. `code` (machine id) and `extras` (more response fields) go into the response."""

    def __init__(self, message, code=None, extras=None):
        super().__init__(message)
        self.code = code
        self.extras = dict(extras or {})


class InvalidRepoError(RepoRequestError, ValueError):
    """A repo that cannot be listed or a bad repo/copy value (not a folder, not git, detached HEAD, already listed...)."""


class RepoNotFoundError(RepoRequestError, LookupError):
    """Repo short name the project does not list."""


class RepoConflictError(RepoRequestError, RuntimeError):
    """Refused by state: a loop runs, the primary repo cannot be removed, a workspace job is running, the loop cannot close."""


class RepoInUseError(RepoConflictError):
    """The repo is used by the running loop of ANOTHER project. `extras` carries `repo` and `project` {id, name}."""
