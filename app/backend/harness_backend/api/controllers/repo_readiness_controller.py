"""Routes of the per-repo readiness guard (hub-level, keyed by project id): the table, the Conformance line of one repo and its
codebase map job, and the Standards slots of a repo (`standards`, also for the project's own repo). Only translates HTTP <-> the services."""
from ..router import Response
from .project_responses import respond_converting_errors

READINESS_ROUTE = "/api/app/projects/{project_id}/repos-readiness"
REPO_ROUTE = "/api/app/projects/{project_id}/repos/{name}"
PRIMARY_STANDARDS_ROUTE = "/p/{project_id}/api/standards"
JOB_ACCEPTED_STATUS = 202


class RepoReadinessController:
    def __init__(self, project_service, repo_readiness_service, standards_service=None):
        self.project_service = project_service
        self.repo_readiness_service = repo_readiness_service
        self.standards_service = standards_service

    def register_routes(self, router):
        router.add("GET", READINESS_ROUTE, self.describe_repos)
        router.add("PUT", REPO_ROUTE + "/conformance", self.set_conformance)
        router.add("POST", REPO_ROUTE + "/map", self.map_repo)
        if self.standards_service is not None:
            router.add("GET", REPO_ROUTE + "/standards", self.get_standards)
            router.add("PUT", REPO_ROUTE + "/standards", self.set_standards)
            router.add("GET", PRIMARY_STANDARDS_ROUTE, self.get_standards)
            router.add("PUT", PRIMARY_STANDARDS_ROUTE, self.set_standards)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def describe_repos(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.repo_readiness_service.describe_repos(self.get_context(request))))

    def set_conformance(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.repo_readiness_service.set_conformance(
            self.get_context(request), request.route_params["name"], request.body.get("value"))))

    def map_repo(self, request):
        return respond_converting_errors(lambda: Response.json(JOB_ACCEPTED_STATUS, self.repo_readiness_service.map_repo(
            self.get_context(request), request.route_params["name"], request.body.get("budget_usd"))))

    def get_standards(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.standards_service.get_repo(
            self.get_context(request), request.route_params.get("name"))))

    def set_standards(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.standards_service.put_repo(
            self.get_context(request), request.route_params.get("name"), request.body.get("bindings"))))
