"""Route of "Commit setup files and push" (`POST /p/<project_id>/api/workspace/repos/<name>/commit-setup`). Only translates HTTP <-> SetupCommitService."""
from ..router import Response
from .loop_commands_controller import PROJECT_ROUTE_PREFIX
from .project_responses import respond_converting_errors


class SetupCommitController:
    def __init__(self, project_service, setup_commit_service):
        self.project_service = project_service
        self.setup_commit_service = setup_commit_service

    def register_routes(self, router):
        router.add("POST", PROJECT_ROUTE_PREFIX + "/workspace/repos/{name}/commit-setup", self.commit_setup)

    def commit_setup(self, request):
        def action():
            context = self.project_service.get_project_context(request.route_params["project_id"])
            return Response.json(200, self.setup_commit_service.commit_setup(context, request.route_params["name"], request.body.get("confirm_branch")))
        return respond_converting_errors(action)
