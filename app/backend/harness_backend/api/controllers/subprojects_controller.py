"""Routes of the monorepo subprojects (per project, optional `?repo=<name>` for a secondary repository): the list and its detection,
the AI validation, the per-subproject Conformance and Standards slots, the map queue and the system map. Only translates HTTP <-> the services."""
from ..router import Response
from .project_responses import respond_converting_errors

JOB_ACCEPTED_STATUS = 202
SUBPROJECTS_ROUTE = "/p/{project_id}/api/subprojects"
JOB_TYPE_SUBPROJECTS = "subprojects"


class SubprojectsController:
    def __init__(self, project_service, subprojects_service, validation_service, map_service, system_map_service, claude_job_service, standards_service=None):
        self.standards_service = standards_service
        self.project_service = project_service
        self.subprojects_service = subprojects_service
        self.validation_service = validation_service
        self.map_service = map_service
        self.system_map_service = system_map_service
        self.claude_job_service = claude_job_service

    def register_routes(self, router):
        router.add("GET", SUBPROJECTS_ROUTE, self.describe)
        router.add("PUT", SUBPROJECTS_ROUTE, self.update_list)
        router.add("POST", SUBPROJECTS_ROUTE + "/detect", self.detect)
        router.add("POST", SUBPROJECTS_ROUTE + "/validate", self.validate)
        router.add("POST", SUBPROJECTS_ROUTE + "/conformance", self.set_conformance)
        if self.standards_service is not None:
            router.add("GET", SUBPROJECTS_ROUTE + "/standards", self.get_standards)
            router.add("PUT", SUBPROJECTS_ROUTE + "/standards", self.set_standards)
        router.add("POST", SUBPROJECTS_ROUTE + "/map", self.map_subprojects)
        router.add("POST", SUBPROJECTS_ROUTE + "/system-map", self.map_system)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    @staticmethod
    def get_repo_name(request):
        return request.get_query("repo") or None

    def describe(self, request):
        def action():
            context = self.get_context(request)
            repo_name = self.get_repo_name(request)
            description = self.subprojects_service.describe(context, repo_name)
            description["system"] = self.system_map_service.status(context, repo_name)
            description["validation_job"] = self.find_validation_job(context)
            return Response.json(200, description)
        return respond_converting_errors(action)

    def find_validation_job(self, context):
        """The running validation job, else the last one of type `subprojects` (None when there is none)."""
        jobs = [job for job in self.claude_job_service.list_jobs(context.repo_path) if job.get("type") == JOB_TYPE_SUBPROJECTS]
        running = [job for job in jobs if job.get("state") == "running"]
        return (running or jobs or [None])[0]

    def detect(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.subprojects_service.detect_again(
            self.get_context(request), self.get_repo_name(request), force=request.body.get("force") is True)))

    def update_list(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.subprojects_service.update_list(
            self.get_context(request), request.body, self.get_repo_name(request))))

    def validate(self, request):
        def action():
            job = self.validation_service.start(self.get_context(request), repo_name=self.get_repo_name(request))
            return Response.json(JOB_ACCEPTED_STATUS, {"ok": True, "job": job})
        return respond_converting_errors(action)

    def set_conformance(self, request):
        def action():
            context, repo_name, body = self.get_context(request), self.get_repo_name(request), request.body
            if body.get("all") is True:
                return Response.json(200, self.subprojects_service.set_conformance_all(context, repo_name))
            return Response.json(200, self.subprojects_service.set_conformance(context, body.get("path"), body.get("value"), repo_name))
        return respond_converting_errors(action)

    def get_standards(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.standards_service.get_subproject(
            self.get_context(request), request.get_query("path"), self.get_repo_name(request))))

    def set_standards(self, request):
        body = request.body
        return respond_converting_errors(lambda: Response.json(200, self.standards_service.put_subproject(
            self.get_context(request), body.get("path"), body.get("bindings"), self.get_repo_name(request))))

    def map_subprojects(self, request):
        def action():
            body = request.body
            started = self.map_service.start(self.get_context(request), paths=body.get("paths"),
                                             repo_name=self.get_repo_name(request))
            return Response.json(JOB_ACCEPTED_STATUS, {"ok": True, "job": started["job"], "queue": started["queue"]})
        return respond_converting_errors(action)

    def map_system(self, request):
        def action():
            started = self.system_map_service.start(self.get_context(request), repo_name=self.get_repo_name(request))
            return Response.json(JOB_ACCEPTED_STATUS, {"ok": True, "job": started["job"]})
        return respond_converting_errors(action)
