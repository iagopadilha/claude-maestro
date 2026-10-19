"""Reopen planning routes: GET planning/reopen-check (read only) and POST planning/reopen. Only HTTP <-> PlanningReopenService."""
from ..router import Response
from .project_responses import respond_converting_errors


class PlanningReopenController:
    def __init__(self, project_service, planning_reopen_service):
        self.project_service = project_service
        self.planning_reopen_service = planning_reopen_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/planning/reopen-check", self.check_reopen)
        router.add("POST", "/p/{project_id}/api/planning/reopen", self.reopen)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def check_reopen(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_reopen_service.check_reopen(self.get_context(request))))

    def reopen(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_reopen_service.reopen(self.get_context(request), request.body)))
