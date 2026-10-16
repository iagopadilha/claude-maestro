"""Read-only chat routes of a project: chat/send, chat/job/<job>, chat/threads and chat/thread/<thread>."""
from .loop_commands_controller import PROJECT_ROUTE_PREFIX, respond_with_project_context


def parse_turn_from_query(text):
    return int(text) if text.isdigit() else None


class ChatController:
    def __init__(self, project_service, chat_service):
        self.project_service = project_service
        self.chat_service = chat_service

    def register_routes(self, router):
        router.add("POST", PROJECT_ROUTE_PREFIX + "/chat/send", self.send_question)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/chat/job/{job_id}", self.get_job)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/chat/threads", self.list_threads)
        router.add("GET", PROJECT_ROUTE_PREFIX + "/chat/thread/{thread_id}", self.get_thread)

    def send_question(self, request):
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.chat_service.send_question(context, request.body))

    def get_job(self, request):
        job_id = request.route_params["job_id"]
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.chat_service.get_project_job(context, job_id))

    def list_threads(self, request):
        agent_id = request.query.get("agent_id", [None])[0]
        turn = parse_turn_from_query(request.get_query("turn"))
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.chat_service.list_project_threads(context, agent_id, turn))

    def get_thread(self, request):
        thread_id = request.route_params["thread_id"]
        return respond_with_project_context(self.project_service, request,
                                            lambda context: self.chat_service.get_project_thread(context, thread_id))
