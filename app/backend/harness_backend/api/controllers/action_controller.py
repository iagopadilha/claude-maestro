"""Act mode routes of a project: GET actions, POST action/propose, GET action/job/<job> (alias of chat/job) and POST action/run.
The two POSTs are refused in planning mode (409 `planning_required`)."""
from .loop_commands_controller import PROJECT_ROUTE_PREFIX, respond_with_project_context


class ActionController:
    def __init__(self, project_service, action_service, chat_service, planning_requirement_service):
        self.project_service = project_service
        self.action_service = action_service
        self.chat_service = chat_service
        self.planning_requirement_service = planning_requirement_service

    def register_routes(self, router):
        router.add("GET", PROJECT_ROUTE_PREFIX + "/actions", self.list_actions)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/action/propose", self.propose_action)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/action/job/{job_id}", self.get_proposal_job)
        router.add("POST", PROJECT_ROUTE_PREFIX + "/action/run", self.run_action)

    def list_actions(self, request):
        return respond_with_project_context(self.project_service, request, self.action_service.list_available_actions)

    def propose_action(self, request):
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.action_service.propose_action_from_text(context, request.body),
                                            self.planning_requirement_service)

    def get_proposal_job(self, request):
        job_id = request.route_params["job_id"]
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.chat_service.get_project_job(context, job_id))

    def run_action(self, request):
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.action_service.run_confirmed_action(context, request.body),
                                            self.planning_requirement_service)
