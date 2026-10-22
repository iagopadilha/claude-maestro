"""AI decisions routes of a project (`/p/<project_id>/api/decisions...`): the list (all or one turn), mark as reviewed and ask for a change.

The list is a plain read; the two POSTs need the token (checked by the server). An unknown decision id = 404.
"""
from ..router import Response
from ...services.decisions_service import DecisionNotFoundError
from .loop_commands_controller import PROJECT_ROUTE_PREFIX, respond_with_project_context

DECISIONS_ROUTE = PROJECT_ROUTE_PREFIX + "/decisions"
INVALID_TURN_MESSAGE = "turn must be a non-negative integer"


class DecisionsController:
    def __init__(self, project_service, decisions_service):
        self.project_service = project_service
        self.decisions_service = decisions_service

    def register_routes(self, router):
        router.add("GET", DECISIONS_ROUTE, self.list_decisions)
        router.add("POST", DECISIONS_ROUTE + "/{decision_id}/review", self.mark_reviewed)
        router.add("POST", DECISIONS_ROUTE + "/{decision_id}/change", self.request_change)

    def list_decisions(self, request):
        raw_turn = request.get_query("turn")
        turn = None
        if raw_turn != "":
            if not raw_turn.isdigit():
                return Response.error(400, INVALID_TURN_MESSAGE)
            turn = int(raw_turn)
        return respond_with_project_context(self.project_service, request,
                                            lambda context: Response.json(200, self.decisions_service.list_decisions(context, turn)))

    def mark_reviewed(self, request):
        return self.respond_for_decision(request, self.decisions_service.mark_reviewed)

    def request_change(self, request):
        return self.respond_for_decision(request, self.decisions_service.request_change)

    def respond_for_decision(self, request, action):
        decision_id = request.route_params["decision_id"]

        def run(context):
            try:
                return Response.json(200, action(context, decision_id))
            except DecisionNotFoundError as error:
                return Response.error(404, str(error))
        return respond_with_project_context(self.project_service, request, run)
