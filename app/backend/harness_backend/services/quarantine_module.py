"""Wiring of the quarantine pieces (pause detail, summary, retry/skip/revert routes and the `error`/`quarantine` chat scopes).

Built AFTER the loop read and loop commands modules because it consumes their services (pipeline, error center, transcripts, loop control,
chat jobs). It also hands the chat service the provider of the server-built scope contexts.
"""
from ..api.controllers.quarantine_controller import QuarantineController
from ..infrastructure.claude_client import ClaudeClient
from ..infrastructure.git_client import GitClient
from ..infrastructure.process_runner import ProcessRunner
from ..repositories.quarantine_summary_repository import QuarantineSummaryRepository
from .chat_scope_context_service import ChatScopeContextService
from .quarantine_service import QuarantineService


class QuarantineModule:
    def __init__(self, quarantine_service, chat_scope_context_service, controllers, busy_providers):
        self.quarantine_service = quarantine_service
        self.chat_scope_context_service = chat_scope_context_service
        self.controllers = controllers
        self.busy_providers = busy_providers


def build_quarantine_module(config, log, project_service, loop_read_module, loop_commands_module, planning_requirement_service):
    process_runner = ProcessRunner()
    claude_client = ClaudeClient(process_runner, config.claude_binary)
    quarantine_service = QuarantineService(
        log, loop_read_module.loop_status_service, loop_read_module.loop_file_repository, loop_read_module.pipeline_service,
        loop_read_module.claude_transcript_repository, loop_commands_module.loop_control_service, GitClient(process_runner), claude_client,
        loop_commands_module.chat_service.chat_job_repository, QuarantineSummaryRepository())
    scope_context_service = ChatScopeContextService(loop_read_module.error_center_service, loop_read_module.pipeline_service, quarantine_service)
    loop_commands_module.chat_service.scope_context_provider = scope_context_service
    controllers = [QuarantineController(project_service, quarantine_service, planning_requirement_service,
                                        repo_usage_guard=getattr(loop_read_module.loop_status_service, "ensure_repos_free", None))]
    return QuarantineModule(quarantine_service, scope_context_service, controllers, [])
