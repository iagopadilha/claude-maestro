"""Routes of the `.specs/` view: /p/<id>/api/specs/tree and specs/file (GET with a version, PUT with a version conflict check)."""
from ..router import Response
from .project_responses import respond_converting_errors

WRITE_FILE_BODY_LIMIT_BYTES = 2 * 1024 * 1024  # a large spec fits; the service still limits the text to 1 M characters


class SpecsController:
    def __init__(self, project_service, spec_file_service):
        self.project_service = project_service
        self.spec_file_service = spec_file_service

    def register_routes(self, router):
        router.add("GET", "/p/{project_id}/api/specs/tree", self.get_tree)
        router.add("GET", "/p/{project_id}/api/specs/file", self.read_file)
        router.add("PUT", "/p/{project_id}/api/specs/file", self.write_file, body_limit_bytes=WRITE_FILE_BODY_LIMIT_BYTES)

    def get_context(self, request):
        return self.project_service.get_project_context(request.route_params["project_id"])

    def get_tree(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.spec_file_service.build_specs_tree(self.get_context(request))))

    def read_file(self, request):
        return respond_converting_errors(lambda: Response.json(200, self.spec_file_service.read_spec_file(
            self.get_context(request), request.get_query("path"))))

    def write_file(self, request):
        body = request.body
        return respond_converting_errors(lambda: Response.json(200, dict(self.spec_file_service.write_spec_file(
            self.get_context(request), body.get("path"), body.get("content"), body.get("version")), ok=True)))
