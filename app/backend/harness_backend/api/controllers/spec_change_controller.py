"""Spec-change routes (change a written definition with the AI): GET spec-change/<scope>, POST spec-change/<scope>/send,
POST spec-change/<scope>/reset, GET spec-change-job/<job>. Only translates HTTP <-> SpecChangeService; the refusals carry
`code` (spec_change_busy, loop_running). <scope> = a feature slug or `_project`."""
from ..router import Response
from .project_responses import respond_converting_errors

JOB_ACCEPTED_STATUS = 202


class SpecChangeController:
    def __init__(self, project_service, spec_change_service):
        self.project_service = project_service
        self.spec_change_service = spec_change_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/spec-change/{scope}", self.get_scope)
        router.add("POST", "/p/{project_id}/api/spec-change/{scope}/send", self.send_message)
        router.add("POST", "/p/{project_id}/api/spec-change/{scope}/reset", self.reset_conversation)
        router.add("GET", "/p/{project_id}/api/spec-change-job/{job_id}", self.get_job)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def get_scope(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.spec_change_service.get_scope(
            self.get_context(request), request.route_params["scope"])))

    def send_message(self, request):
        return respond_converting_errors(lambda: Response.json(JOB_ACCEPTED_STATUS, self.spec_change_service.send_message(
            self.get_context(request), request.route_params["scope"], request.body.get("text"))))

    def reset_conversation(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.spec_change_service.reset_conversation(
            self.get_context(request), request.route_params["scope"], request.body.get("confirm"))))

    def get_job(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.spec_change_service.get_job(
            self.get_context(request), request.route_params["job_id"])))
