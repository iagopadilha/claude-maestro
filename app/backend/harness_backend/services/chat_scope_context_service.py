"""Context of the chat scopes the panel's dialogs use (`error` and `quarantine`), built on the server from the same data the panel shows.

The chat never receives text from the browser as context: it only names an error id or a feature, and this service looks the facts up.
Everything is redacted before it reaches `claude -p`.
"""
from ..infrastructure.claude_client import redact_secrets
from .chat_service import ChatService
from .pipeline_service import STEPS

TURN_EVENTS_LIMIT = 40


class ChatScopeContextService:
    def __init__(self, error_center_service, pipeline_service, quarantine_service):
        self.error_center_service = error_center_service
        self.pipeline_service = pipeline_service
        self.quarantine_service = quarantine_service

    def build_error_context(self, context, error_id):
        """Title, detail, hint, turn and the events of that turn for one item of `GET errors`; None when the id is not in the list."""
        item = next((row for row in self.error_center_service.build_errors(context)["items"] if row["id"] == error_id), None)
        if item is None:
            return None
        lines = ["Error shown in the loop panel: %s" % item["title"], "Kind: %s. Severity: %s. When: %s. Turn: %s." % (
            item["kind"], item["severity"], item["when"] or "unknown", item["turn"] if item["turn"] is not None else "n/a"),
            "Detail:\n%s" % (item["detail"] or "(none)"), "What the panel suggests: %s" % (item["hint"] or "(nothing)")]
        if item["recover"]:
            lines.append("Recovery buttons offered: " + ", ".join(item["recover"]))
        events = self.list_turn_events(context, item["turn"])
        if events:
            lines.append("Events of turn %s:" % item["turn"])
            lines += events
        lines.append(ChatService.build_general_context(context.work_dir))
        return redact_secrets("\n".join(lines))

    def list_turn_events(self, context, turn_number):
        if turn_number is None:
            return []
        turn = next((row for row in self.pipeline_service.build_pipeline(context)["turns"] if row["n"] == turn_number), None)
        if turn is None:
            return []
        return ["- %s | %s | %s | %s" % (event["feature"], STEPS[event["step"]]["key"], event["status"], event["out"] or "(no text)")
                for event in turn["events"][:TURN_EVENTS_LIMIT]]

    def build_quarantine_context(self, context, slug):
        return self.quarantine_service.build_chat_context(context, slug)
