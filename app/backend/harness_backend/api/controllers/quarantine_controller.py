"""Quarantine routes of a project (`/p/<project_id>/api/quarantine/<slug>...`): the pause detail, its AI summary and the decisions
(retry with guidance, skip, revert uncommitted files). The detail is a plain read; every POST needs the token (checked by the server) and is
refused in planning mode (409 `planning_required`). Retry and skip relaunch the loop, so they also check that no other running loop uses
the project's repositories (409 `repo_in_use`)."""
from .loop_commands_controller import PROJECT_ROUTE_PREFIX, respond_with_project_context
from .project_responses import respond_converting_errors

QUARANTINE_ROUTE = PROJECT_ROUTE_PREFIX + "/quarantine/{slug}"


class QuarantineController:
    def __init__(self, project_service, quarantine_service, planning_requirement_service, repo_usage_guard=None):
        self.project_service = project_service
        self.quarantine_service = quarantine_service
        self.planning_requirement_service = planning_requirement_service
        self.repo_usage_guard = repo_usage_guard  # callable(context): raises RepoInUseError when another running loop uses a repo

    def register_routes(self, router):
        router.add("GET", QUARANTINE_ROUTE, self.get_detail)
        router.add("POST", QUARANTINE_ROUTE + "/summary", self.start_summary)
        router.add("POST", QUARANTINE_ROUTE + "/retry", self.retry_feature)
        router.add("POST", QUARANTINE_ROUTE + "/skip", self.skip_feature)
        router.add("POST", QUARANTINE_ROUTE + "/revert", self.revert_files)

    def respond_after_planning(self, request, action_with_context):
        return respond_with_project_context(self.project_service, request, action_with_context, self.planning_requirement_service)

    def respond_if_repos_free(self, action_with_context):
        def guarded(context):
            if self.repo_usage_guard is None:
                return action_with_context(context)
            return respond_converting_errors(lambda: (self.repo_usage_guard(context), action_with_context(context))[1])
        return guarded

    def get_detail(self, request):
        slug = request.route_params["slug"]
        return respond_with_project_context(self.project_service, request, lambda context: self.quarantine_service.get_detail(context, slug))

    def start_summary(self, request):
        slug = request.route_params["slug"]
        return self.respond_after_planning(request, lambda context: self.quarantine_service.start_summary(context, slug))

    def retry_feature(self, request):
        slug = request.route_params["slug"]
        return self.respond_after_planning(request, self.respond_if_repos_free(
            lambda context: self.quarantine_service.retry_feature(context, slug, request.body)))

    def skip_feature(self, request):
        slug = request.route_params["slug"]
        return self.respond_after_planning(request, self.respond_if_repos_free(
            lambda context: self.quarantine_service.skip_feature(context, slug)))

    def revert_files(self, request):
        slug = request.route_params["slug"]
        return self.respond_after_planning(request, lambda context: self.quarantine_service.revert_files(context, slug, request.body))
