"""Assembly of the loop read module (the only place of this module that instantiates concrete classes)."""
from ..api.controllers.loop_read_controller import LoopReadController
from ..repositories.claude_transcript_repository import ClaudeTranscriptRepository
from ..repositories.loop_file_repository import LoopFileRepository
from .agent_history_service import AgentHistoryService
from .loop_panel_service import LoopPanelService
from .loop_status_service import LoopStatusService
from .roadmap_view_service import RoadmapViewService
from .turn_service import TurnService


class LoopReadModule:
    def __init__(self, loop_status_service, loop_panel_service, agent_history_service, turn_service, claude_transcript_repository, controllers,
                 busy_providers):
        self.loop_status_service = loop_status_service
        self.loop_panel_service = loop_panel_service
        self.agent_history_service = agent_history_service
        self.turn_service = turn_service
        self.claude_transcript_repository = claude_transcript_repository
        self.controllers = controllers
        self.busy_providers = busy_providers


def build_loop_read_module(config, log, project_service, activity_monitor):
    loop_file_repository = LoopFileRepository()
    claude_transcript_repository = ClaudeTranscriptRepository(config.claude_transcripts_dir, log)
    loop_status_service = LoopStatusService(loop_file_repository, project_service, log)
    roadmap_view_service = RoadmapViewService(loop_file_repository)
    turn_service = TurnService(loop_file_repository, roadmap_view_service)
    agent_history_service = AgentHistoryService(loop_file_repository, claude_transcript_repository, log)
    loop_panel_service = LoopPanelService(loop_status_service, loop_file_repository, roadmap_view_service, turn_service,
                                          agent_history_service, activity_monitor)
    controller = LoopReadController(project_service, loop_panel_service, agent_history_service, turn_service, log)
    return LoopReadModule(loop_status_service, loop_panel_service, agent_history_service, turn_service, claude_transcript_repository,
                          [controller], [loop_status_service])
