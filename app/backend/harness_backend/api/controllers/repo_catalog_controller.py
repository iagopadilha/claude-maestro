"""Repo catalog routes (hub-level): GET /api/app/catalog, GET /api/app/catalog/sheet?path=, POST /api/app/catalog/scan,
GET /api/app/catalog/job/{job_id}. The scan is a background job; only the catalog file is ever written."""
from ..router import Response
from .project_responses import respond_converting_errors


class RepoCatalogController:
    def __init__(self, repo_catalog_service):
        self.repo_catalog_service = repo_catalog_service

    def register_routes(self, router):
        router.add("GET", "/api/app/catalog", self.get_index)
        router.add("GET", "/api/app/catalog/sheet", self.get_sheet)
        router.add("POST", "/api/app/catalog/scan", self.start_scan)
        router.add("GET", "/api/app/catalog/job/{job_id}", self.get_job)

    def get_index(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.repo_catalog_service.get_index()))

    def get_sheet(self, request):
        def read():
            sheet = self.repo_catalog_service.get_sheet(request.get_query("path"))
            return Response.json(200, sheet) if sheet else Response.error(404, "this repo has not been scanned")
        return respond_converting_errors(read)

    def start_scan(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.repo_catalog_service.start_scan(request.body.get("paths"))))

    def get_job(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.repo_catalog_service.get_job(request.route_params["job_id"])))
