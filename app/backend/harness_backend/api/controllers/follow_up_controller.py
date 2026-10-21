"""Follow-up routes of a project (`/p/<project_id>/api/follow-up...`): the draft and the last job, send (a `follow_up` claude job that
appends a stage to the ROADMAP) and run (a new loop round on the same worktree). Only translates HTTP <-> service.
POST requires the token (the server checks it). Errors: 400 invalid body, 409 loop or job running / no worktree / no stage."""
from ..router import Response
from .loop_commands_controller import PROJECT_ROUTE_PREFIX, respond_with_project_context
from .project_responses import respond_converting_errors


class FollowUpController:
    def __init__(self, project_service, follow_up_service, repo_usage_guard=None):
        self.project_service = project_service
        self.follow_up_service = follow_up_service
        self.repo_usage_guard = repo_usage_guard  # callable(context): raises RepoInUseError when another running loop uses a repo

    def register_routes(self, router):
        router.add("GET", PROJECT_ROUTE_PREFIX + "/follow-up", self.get_follow_up)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/follow-up/send", self.send_follow_up)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/follow-up/run", self.run_follow_up)

    def respond(self, request, action_with_context):
        return respond_with_project_context(self.project_service, request, lambda context: respond_converting_errors(lambda: action_with_context(context)))

    def get_follow_up(self, request):
        return self.respond(request, lambda context: Response.json(200, self.follow_up_service.get_follow_up(context)))

    def send_follow_up(self, request):
        return self.respond(request, lambda context: Response.json(202, self.follow_up_service.send_follow_up(context, request.body)))

    def run_follow_up(self, request):
        """The start of a round: the answer is the one `loop/start` gives (202 started, 400/409 refusals of the start itself)."""
        def run(context):
            if self.repo_usage_guard is not None:
                self.repo_usage_guard(context)
            return self.follow_up_service.run_follow_up(context)
        return self.respond(request, run)
