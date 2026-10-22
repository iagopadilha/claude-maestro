"""Planning mode routes: GET planning, POST planning/send, GET planning/job/<job>, POST planning/reset, the spec import
(POST planning/import/preview, POST planning/import, GET planning/imports, POST planning/import/<id>/send) and the readiness check (GET planning/readiness,
POST planning/readiness/run). Only translates HTTP <-> the services; the refusals carry `code` (planning_busy, planning_prerequisites, loop_running)."""
from ..router import Response
from .project_responses import respond_converting_errors

JOB_ACCEPTED_STATUS = 202


class PlanningController:
    def __init__(self, project_service, planning_service, planning_import_service=None, readiness_service=None):
        self.project_service = project_service
        self.planning_service = planning_service
        self.planning_import_service = planning_import_service
        self.readiness_service = readiness_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/planning", self.get_planning)
        router.add("POST", "/p/{project_id}/api/planning/send", self.send_message)
        router.add("GET", "/p/{project_id}/api/planning/job/{job_id}", self.get_job)
        router.add("POST", "/p/{project_id}/api/planning/reset", self.reset_conversation)
        if self.planning_import_service:
            router.add("POST", "/p/{project_id}/api/planning/import/preview", self.preview_import)
            router.add("POST", "/p/{project_id}/api/planning/import", self.import_spec)
            router.add("GET", "/p/{project_id}/api/planning/imports", self.list_imports)
            router.add("POST", "/p/{project_id}/api/planning/import/{import_id}/send", self.send_import)
        if self.readiness_service:
            router.add("GET", "/p/{project_id}/api/planning/readiness", self.get_readiness)
            router.add("POST", "/p/{project_id}/api/planning/readiness/run", self.run_readiness)

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

    def preview_import(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_import_service.preview_import(
            self.get_context(request), request.body)))

    def import_spec(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_import_service.import_spec(
            self.get_context(request), request.body)))

    def list_imports(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.planning_import_service.list_imports(self.get_context(request))))

    def send_import(self, request):
        return respond_converting_errors(lambda: Response.json(JOB_ACCEPTED_STATUS, self.planning_import_service.send_import(
            self.get_context(request), request.route_params["import_id"])))

    def get_readiness(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.readiness_service.get(self.get_context(request))))

    def run_readiness(self, request):
        """Starts the check in the background (202) and returns the live state (`running`); poll GET planning/readiness."""
        return respond_converting_errors(lambda: Response.json(JOB_ACCEPTED_STATUS, self.readiness_service.run(self.get_context(request))))
