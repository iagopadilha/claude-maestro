"""Assembly of the loop read module (the only place of this module that instantiates concrete classes)."""
from ..api.controllers.decisions_controller import DecisionsController
from ..api.controllers.loop_read_controller import LoopReadController
from ..repositories.claude_transcript_repository import ClaudeTranscriptRepository
from ..repositories.error_event_repository import ErrorEventRepository
from ..infrastructure.process_runner import ProcessRunner
from ..repositories.loop_file_repository import LoopFileRepository
from ..repositories.specs_file_repository import SpecsFileRepository
from ..repositories.task_status_repository import TaskStatusRepository
from .agent_history_service import AgentHistoryService
from .decisions_service import DecisionsService
from .error_center_service import ErrorCenterService
from .guard_event_service import GuardEventService
from .loop_panel_service import LoopPanelService
from .loop_status_service import LoopStatusService
from .pipeline_service import PipelineService
from .planning_requirement_service import PlanningRequirementService
from .roadmap_view_service import RoadmapViewService
from .turn_service import TurnService


class LoopReadModule:
    def __init__(self, loop_status_service, loop_panel_service, agent_history_service, turn_service, claude_transcript_repository, controllers,
                 busy_providers, loop_file_repository=None, pipeline_service=None, error_center_service=None, decisions_service=None):
        self.decisions_service = decisions_service
        self.loop_file_repository = loop_file_repository
        self.pipeline_service = pipeline_service
        self.error_center_service = error_center_service
        self.loop_status_service = loop_status_service
        self.loop_panel_service = loop_panel_service
        self.agent_history_service = agent_history_service
        self.turn_service = turn_service
        self.claude_transcript_repository = claude_transcript_repository
        self.controllers = controllers
        self.busy_providers = busy_providers


def build_loop_read_module(config, log, project_service, activity_monitor, planning_requirement_service=None, decisions_service=None):
    """`planning_requirement_service` comes from the projects module (composition); alone (tests) the module builds its own.
    `decisions_service` is the ONE instance the composition shares with the wrap-up; alone (tests) the module builds its own."""
    decisions_service = decisions_service or DecisionsService()
    planning_requirement_service = planning_requirement_service or PlanningRequirementService(SpecsFileRepository(), ProcessRunner())
    loop_file_repository = LoopFileRepository()
    claude_transcript_repository = ClaudeTranscriptRepository(config.claude_transcripts_dir, log)
    loop_status_service = LoopStatusService(loop_file_repository, project_service, log)
    roadmap_view_service = RoadmapViewService(loop_file_repository)
    turn_service = TurnService(loop_file_repository, roadmap_view_service)
    agent_history_service = AgentHistoryService(loop_file_repository, claude_transcript_repository, log)
    loop_panel_service = LoopPanelService(loop_status_service, loop_file_repository, roadmap_view_service, turn_service,
                                          agent_history_service, activity_monitor, GuardEventService(loop_file_repository),
                                          planning_requirement_service, decisions_service=decisions_service,
                                          task_status_repository=TaskStatusRepository())
    error_center_service = ErrorCenterService(loop_status_service, loop_file_repository, ErrorEventRepository())
    pipeline_service = PipelineService(loop_status_service, loop_file_repository, roadmap_view_service, agent_history_service, log)
    controller = LoopReadController(project_service, loop_panel_service, agent_history_service, turn_service, log, error_center_service,
                                    pipeline_service)
    return LoopReadModule(loop_status_service, loop_panel_service, agent_history_service, turn_service, claude_transcript_repository,
                          [controller, DecisionsController(project_service, decisions_service)], [loop_status_service], loop_file_repository, pipeline_service, error_center_service, decisions_service)
