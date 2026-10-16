"""Conversion of the projects module's domain errors into HTTP responses, in one place for its three controllers."""
from ...domain.project import ProjectNotFoundError
from ...domain.project_errors import (FileEditRefusedError, FileVersionConflictError, InvalidPathError, InvalidRequestError,
                                      InvalidRoadmapError, JobInProgressError, JobNotFoundError, LoopRunningError,
                                      LoopWorktreeExistsError, PathOutsideSpecsError, ProjectAlreadyExistsError,
                                      RoadmapSessionMissingError, SpecFileNotFoundError)
from ..router import Response

PROJECT_NOT_FOUND_MESSAGE = "project not found"
STATUS_BY_ERROR = (
    (FileVersionConflictError, 409),
    (InvalidPathError, 400),
    (InvalidRequestError, 400),
    (PathOutsideSpecsError, 400),
    (FileEditRefusedError, 400),
    (SpecFileNotFoundError, 404),
    (JobNotFoundError, 404),
    (LoopRunningError, 409),
    (JobInProgressError, 409),
    (RoadmapSessionMissingError, 409),
    (LoopWorktreeExistsError, 409),
    (InvalidRoadmapError, 409),
    (ProjectAlreadyExistsError, 409),
)


def respond_converting_errors(action):
    """Runs `action()` and returns its Response; a known domain error becomes the right status; anything else goes up (the
    server's 500)."""
    try:
        return action()
    except ProjectNotFoundError:
        return Response.error(404, PROJECT_NOT_FOUND_MESSAGE)
    except FileVersionConflictError as error:
        return Response.error(409, str(error), current_version=error.current_version)
    except tuple(error_type for error_type, _ in STATUS_BY_ERROR) as error:
        status = next(code for error_type, code in STATUS_BY_ERROR if isinstance(error, error_type))
        return Response.error(status, str(error))


def project_to_dict(project):
    return {"id": project.id, "name": project.name, "repo_path": project.repo_path, "opened_at": project.opened_at,
            "last_opened_at": project.last_opened_at, "favorite": bool(project.favorite), "url": "/p/%s/" % project.id}
