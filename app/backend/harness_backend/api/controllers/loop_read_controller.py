"""GET routes that read a project's loop (`/p/<project_id>/api/...`): health, state, history, agent log, turn and graph.

No token (read only). Unknown project = 404 `{"ok":false,"error":"project not found"}`.
"""
import os

from ..router import Response
from ...domain.project import ProjectNotFoundError
from .project_responses import respond_converting_errors

PROJECT_PREFIX = "/p/{project_id}/api"
PROJECT_NOT_FOUND_MESSAGE = "project not found"
TURN_WITHOUT_SNAPSHOT_MESSAGE = "turn without snapshot"
FEATURE_NOT_FOUND_MESSAGE = "feature not found"
HOST_HEADER = "host"


def get_header(request, name):
    for key, value in (request.headers or {}).items():
        if key.lower() == name:
            return value
    return ""


class LoopReadController:
    def __init__(self, project_service, loop_panel_service, agent_history_service, turn_service, log, error_center_service=None,
                 pipeline_service=None, feature_detail_service=None):
        self.feature_detail_service = feature_detail_service
        self.error_center_service = error_center_service
        self.pipeline_service = pipeline_service
        self.project_service = project_service
        self.loop_panel_service = loop_panel_service
        self.agent_history_service = agent_history_service
        self.turn_service = turn_service
        self.log = log

    def register_routes(self, router):
        router.add("GET", PROJECT_PREFIX + "/health", self.get_health)
        router.add("GET", PROJECT_PREFIX + "/state", self.get_state)
        router.add("GET", PROJECT_PREFIX + "/history", self.get_history)
        router.add("GET", PROJECT_PREFIX + "/agent/{agent_id*}/log", self.get_agent_log)  # raw id (may contain ':')
        router.add("GET", PROJECT_PREFIX + "/turn/{number}", self.get_turn)
        router.add("GET", PROJECT_PREFIX + "/graph", self.get_graph)
        router.add("GET", PROJECT_PREFIX + "/errors", self.get_errors)
        router.add("GET", PROJECT_PREFIX + "/pipeline", self.get_pipeline)
        router.add("GET", PROJECT_PREFIX + "/feature/{slug}", self.get_feature_detail)
        router.add("GET", PROJECT_PREFIX + "/codebase/tree", self.get_codebase_tree)
        router.add("GET", PROJECT_PREFIX + "/codebase/file", self.get_codebase_file)

    def get_codebase_tree(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.loop_panel_service.build_codebase_tree(context))

    def get_codebase_file(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        path = request.get_query("path")
        return respond_converting_errors(lambda: Response.json(200, self.loop_panel_service.read_codebase_file(context, path)))

    def get_errors(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.error_center_service.build_errors(context))

    def get_pipeline(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.pipeline_service.build_pipeline(context))

    def get_feature_detail(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        detail = self.feature_detail_service.build_feature_detail(context, request.route_params.get("slug", ""))
        if detail is None:
            return Response.error(404, FEATURE_NOT_FOUND_MESSAGE)
        return Response.json(200, detail)

    def get_context(self, request):
        """(context, None) or (None, 404 Response)."""
        project_id = request.route_params.get("project_id", "")
        try:
            return self.project_service.get_project_context(project_id), None
        except ProjectNotFoundError:
            return None, Response.error(404, PROJECT_NOT_FOUND_MESSAGE)

    @staticmethod
    def build_project_url(request, context):
        """URL of this project's panel from the Host already validated by the server (127.0.0.1:<port> or localhost:<port>)."""
        host = get_header(request, HOST_HEADER).strip()
        return "http://%s/p/%s/" % (host, context.project.id) if host else None

    def get_health(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, {"ok": True, "repo": context.repo_path, "work": context.work_dir, "pid": os.getpid()})

    def get_state(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.loop_panel_service.build_state(context, self.build_project_url(request, context)))

    def get_history(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.agent_history_service.build_history(context))

    def get_agent_log(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.agent_history_service.get_agent_log(context, request.route_params.get("agent_id", "")))

    def get_turn(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        number = request.route_params.get("number", "")
        detail = self.turn_service.get_turn_detail(context, int(number)) if number.isdigit() else None
        if not detail:
            return Response.error(404, TURN_WITHOUT_SNAPSHOT_MESSAGE)
        return Response.json(200, detail)

    def get_graph(self, request):
        context, error = self.get_context(request)
        if error:
            return error
        return Response.json(200, self.loop_panel_service.build_graph(context))
