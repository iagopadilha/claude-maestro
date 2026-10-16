"""Planning mode routes: GET planning, POST planning/send, GET planning/job/<job>, POST planning/reset. Only translates
HTTP <-> PlanningService; the refusals carry `code` (planning_busy, planning_prerequisites, loop_running)."""
from ..router import Response
from .project_responses import respond_converting_errors

JOB_ACCEPTED_STATUS = 202


class PlanningController:
    def __init__(self, project_service, planning_service):
        self.project_service = project_service
        self.planning_service = planning_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/planning", self.get_planning)
        router.add("POST", "/p/{project_id}/api/planning/send", self.send_message)
        router.add("GET", "/p/{project_id}/api/planning/job/{job_id}", self.get_job)
        router.add("POST", "/p/{project_id}/api/planning/reset", self.reset_conversation)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def get_planning(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_service.get_planning(self.get_context(request))))

    def send_message(self, request):
        return respond_converting_errors(lambda: Response.json(JOB_ACCEPTED_STATUS, self.planning_service.send_message(
            self.get_context(request), request.body.get("text"))))

    def get_job(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_service.get_planning_job(
            self.get_context(request), request.route_params["job_id"])))

    def reset_conversation(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_service.reset_conversation(
            self.get_context(request), request.body.get("confirm"))))
