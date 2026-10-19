"""Loop command routes of a project (`/p/<project_id>/api/...`): answer, resume, note, pause, loop/check, loop/start,
instruction, whiteboard and the legacy shutdown (a no-op: shutting down belongs to the app). Same bodies and responses as
the legacy panel (docs/API.md). POST requires the token (the server checks it). In planning mode every POST here except
the legacy shutdown is refused with 409 `code:"planning_required"`."""
from ...domain.project import ProjectNotFoundError
from ...domain.project_errors import PLANNING_REQUIRED_CODE
from ...services.planning_requirement_service import PLANNING_REQUIRED_MESSAGE
from ..router import Response
from .project_responses import respond_converting_errors

PROJECT_ROUTE_PREFIX = "/p/{project_id}/api"
WHITEBOARD_BODY_LIMIT_BYTES = 8 * 1024 * 1024
PROJECT_NOT_FOUND_ERROR = "project not found"


def respond_with_project_context(project_service, request, action_with_context, planning_requirement_service=None):
    """Resolves the route's project and calls `action_with_context(context)`; an unknown project = 404. With
    `planning_requirement_service`, a project in planning mode = 409 `code:"planning_required"` (nothing runs)."""
    try:
        context = project_service.get_project_context(request.route_params["project_id"])
    except ProjectNotFoundError:
        return Response.error(404, PROJECT_NOT_FOUND_ERROR)
    if planning_requirement_service is not None and planning_requirement_service.is_planning_required(context):
        return Response.error(409, PLANNING_REQUIRED_MESSAGE, code=PLANNING_REQUIRED_CODE)
    return action_with_context(context)


class LoopCommandsController:
    def __init__(self, project_service, loop_control_service, whiteboard_service, planning_requirement_service,
                 repo_usage_guard=None):
        self.repo_usage_guard = repo_usage_guard  # callable(context): raises RepoInUseError when another running loop uses a repo
        self.project_service = project_service
        self.loop_control_service = loop_control_service
        self.whiteboard_service = whiteboard_service
        self.planning_requirement_service = planning_requirement_service

    def respond_after_planning(self, request, action_with_context):
        """Loop commands and whiteboard writes: refused in planning mode."""
        return respond_with_project_context(self.project_service, request, action_with_context, self.planning_requirement_service)

    def register_routes(self, router):
        post_routes = (("answer", self.answer_question), ("resume", self.resume_loop), ("note", self.write_note),
                       ("pause", self.request_pause), ("loop/check", self.check_preflight), ("loop/start", self.start_loop),
                       ("instruction", self.send_instruction), ("shutdown", self.ignore_legacy_shutdown))
        for path, handler in post_routes:
            router.add("POST", "%s/%s" % (PROJECT_ROUTE_PREFIX, path), handler)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/whiteboard", self.get_whiteboard)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/whiteboard", self.save_whiteboard, body_limit_bytes=WHITEBOARD_BODY_LIMIT_BYTES)

    def respond_if_repos_free(self, action_with_context):
        """Start-like commands (new loop, resume, answer = relaunch): refused with 409 `repo_in_use` when another project's
        running loop uses one of this project's repos. No guard wired = no check (single-project tests)."""
        def guarded(context):
            if self.repo_usage_guard is None:
                return action_with_context(context)
            return respond_converting_errors(lambda: (self.repo_usage_guard(context), action_with_context(context))[1])
        return guarded

    def answer_question(self, request):
        return self.respond_after_planning(request, self.respond_if_repos_free(
            lambda context: self.loop_control_service.answer_loop_question(context, request.body)))

    def resume_loop(self, request):
        return self.respond_after_planning(request, self.respond_if_repos_free(self.loop_control_service.resume_loop))

    def write_note(self, request):
        return self.respond_after_planning(request, lambda context: self.loop_control_service.write_user_note(context, request.body))

    def request_pause(self, request):
        return self.respond_after_planning(request, self.loop_control_service.request_loop_pause)

    def check_preflight(self, request):
        return self.respond_after_planning(request, lambda context: self.loop_control_service.check_preflight(context, request.body))

    def start_loop(self, request):
        return self.respond_after_planning(request, self.respond_if_repos_free(
            lambda context: self.loop_control_service.start_new_loop(context, request.body)))

    def send_instruction(self, request):
        return self.respond_after_planning(request, lambda context: self.loop_control_service.send_instruction_to_loop(context, request.body))

    def ignore_legacy_shutdown(self, request):
        """Compatibility: the old front/engine called the panel's /api/shutdown; in the app, shutting down is `/api/app/shutdown`."""
        return respond_with_project_context(self.project_service, request, lambda context: (200, {"ok": True}))

    def get_whiteboard(self, request):
        return respond_with_project_context(self.project_service, request, self.whiteboard_service.get_board)

    def save_whiteboard(self, request):
        return self.respond_after_planning(request, lambda context: self.whiteboard_service.save_board(context, request.body))
