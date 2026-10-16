"""Composition of the loop COMMANDS module (the only place in the module that instantiates concrete classes).

`build_loop_commands_module(...)` returns `LoopCommandsModule` with `controllers`, `busy_providers` (chat/router running,
relaunched loop alive) and the services (exposed for tests and diagnostics).
"""
from dataclasses import dataclass, field

from ..api.controllers.action_controller import ActionController
from ..api.controllers.chat_controller import ChatController
from ..api.controllers.loop_commands_controller import LoopCommandsController
from ..infrastructure.claude_client import ClaudeClient
from ..infrastructure.git_client import GitClient
from ..infrastructure.process_runner import ProcessRunner
from ..repositories.agent_transcript_repository import AgentTranscriptRepository
from ..repositories.answers_repository import AnswersRepository
from ..repositories.chat_repository import ChatJobRepository, ChatRepository
from ..repositories.loop_command_file_repository import LoopCommandFileRepository
from ..repositories.specs_file_repository import SpecsFileRepository
from ..repositories.whiteboard_repository import WhiteboardRepository
from .action_service import ActionService
from .chat_service import ChatService
from .engine_script_service import EngineScriptService
from .loop_control_service import LoopControlService
from .loop_worktree_service import LoopWorktreeService
from .planning_requirement_service import PlanningRequirementService
from .whiteboard_service import WhiteboardService


@dataclass
class LoopCommandsModule:
    loop_control_service: object
    chat_service: object
    action_service: object
    whiteboard_service: object
    loop_worktree_service: object
    controllers: list = field(default_factory=list)
    busy_providers: list = field(default_factory=list)


def build_loop_commands_module(config, log, project_service, loop_status_service, activity_monitor, get_app_url,
                               planning_requirement_service=None):
    """`activity_monitor` is not used here: busy state goes through `busy_providers` (the composition registers them).
    `planning_requirement_service` comes from the projects module (composition); alone (tests) the module builds its own."""
    process_runner = ProcessRunner()
    planning_requirement_service = planning_requirement_service or PlanningRequirementService(SpecsFileRepository(), process_runner)
    claude_client = ClaudeClient(process_runner, config.claude_binary)
    git_client = GitClient(process_runner)

    loop_command_file_repository = LoopCommandFileRepository()
    answers_repository = AnswersRepository()
    chat_repository = ChatRepository()
    chat_job_repository = ChatJobRepository()
    agent_transcript_repository = AgentTranscriptRepository(config.claude_transcripts_dir)
    whiteboard_repository = WhiteboardRepository()

    engine_script_service = EngineScriptService(config, process_runner)
    loop_control_service = LoopControlService(config, log, loop_status_service, process_runner, loop_command_file_repository,
                                              answers_repository, get_app_url)
    chat_service = ChatService(log, chat_repository, chat_job_repository, agent_transcript_repository, claude_client)
    loop_worktree_service = LoopWorktreeService(config, log, git_client, engine_script_service, loop_command_file_repository)
    action_service = ActionService(config, log, loop_status_service, loop_control_service, loop_command_file_repository,
                                   loop_worktree_service, engine_script_service, chat_job_repository, claude_client)
    whiteboard_service = WhiteboardService(log, whiteboard_repository)

    controllers = [LoopCommandsController(project_service, loop_control_service, whiteboard_service, planning_requirement_service),
                   ChatController(project_service, chat_service, planning_requirement_service),
                   ActionController(project_service, action_service, chat_service, planning_requirement_service)]
    return LoopCommandsModule(loop_control_service=loop_control_service, chat_service=chat_service, action_service=action_service,
                              whiteboard_service=whiteboard_service, loop_worktree_service=loop_worktree_service,
                              controllers=controllers, busy_providers=[chat_service, loop_control_service])
