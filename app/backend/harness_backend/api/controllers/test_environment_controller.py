"""Panel-level routes of the Test environment tab (`/p/<project_id>/api/test-environment...`). Only translates HTTP <-> service.
Values travel only in the body of the PUT of values and are never echoed back."""
from ..router import Response
from .loop_commands_controller import PROJECT_ROUTE_PREFIX
from .project_responses import respond_converting_errors

TEST_ENVIRONMENT_ROUTE = PROJECT_ROUTE_PREFIX + "/test-environment"


class TestEnvironmentController:
    __test__ = False  # not a test class for pytest

    def __init__(self, project_service, test_environment_service):
        self.project_service = project_service
        self.test_environment_service = test_environment_service

    def register_routes(self, router):
        router.add("GET", TEST_ENVIRONMENT_ROUTE, self.get_test_environment)
        router.add("PUT", TEST_ENVIRONMENT_ROUTE + "/values", self.save_values)

    def respond_with_context(self, request, action_with_context):
        return respond_converting_errors(lambda: Response.json(200, action_with_context(
            self.project_service.get_project_context(request.route_params["project_id"]))))

    def get_test_environment(self, request):
        return self.respond_with_context(request, self.test_environment_service.get_test_environment)

    def save_values(self, request):
        body = request.body
        return self.respond_with_context(request, lambda context: self.test_environment_service.save_values(
            context, body.get("values"), body.get("target", "project")))
