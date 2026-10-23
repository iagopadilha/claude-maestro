"""Composition root: the ONLY place that instantiates concrete classes and wires the dependencies (constructor injection).

Each module exposes a `build_*_module` function that receives the dependencies and returns an object with:
  - `controllers`: list of objects with `register_routes(router)`
  - `busy_providers`: list of IBusyProvider (they prevent the idle shutdown)
  - the services other modules consume (named in docs/ARCHITECTURE.md)
"""
import datetime
import json
import os
import threading
import webbrowser

from .api.router import Router
from .api.http_server import AppHttpServer
from .config.app_config import load_app_config
from .infrastructure.structured_log import StructuredLog
from .infrastructure.activity_monitor import ActivityMonitor
from .repositories.app_config_repository import AppConfigRepository
from .repositories.loop_file_repository import LoopFileRepository
from .api.controllers.app_controller import AppController
from .api.controllers.app_settings_controller import AppSettingsController
from .domain.project import ProjectContext
from .services.app_diagnostics_service import build_app_diagnostics_service
from .services.app_settings_service import AppSettingsService
from .services.claude_account_service import build_shared_claude_account_service
from .services.claude_call_log_service import build_shared_claude_call_log_service
from .services.decisions_service import DecisionsService
from .services.projects_module import build_projects_module
from .services.loop_read_module import build_loop_read_module
from .services.loop_commands_module import build_loop_commands_module
from .services.multi_repo_module import build_multi_repo_module
from .services.quarantine_module import build_quarantine_module


class AssembledApplication:
    def __init__(self, config, log, server, activity_monitor, projects_module, loop_read_module, loop_commands_module,
                 multi_repo_module=None):
        self.config = config
        self.log = log
        self.server = server
        self.activity_monitor = activity_monitor
        self.projects_module = projects_module
        self.loop_read_module = loop_read_module
        self.loop_commands_module = loop_commands_module
        self.multi_repo_module = multi_repo_module

    def get_app_url(self):
        return self.server.url or ""

    def get_project_url(self, project_id):
        return "%sp/%s/" % (self.get_app_url(), project_id)


def list_usage_folders(project_service):
    """Work folder and repo folder of the most recently opened project: where the loop wrote the usage of each command ([] if none)."""
    projects = project_service.list_projects()
    if not projects:
        return []
    context = ProjectContext(max(projects, key=lambda project: str(project.last_opened_at or "")))
    return list(dict.fromkeys([context.work_dir, context.repo_path]))


def build_application(config=None, **config_overrides):
    config = config or load_app_config(**config_overrides)
    log = StructuredLog(config.logs_dir)
    router = Router()
    server_holder = {}
    activity_monitor = ActivityMonitor(config.idle_hours, on_idle=lambda: server_holder["server"].request_shutdown())
    server = AppHttpServer(router, config.frontend_dir, activity_monitor, log)
    server_holder["server"] = server

    def get_app_url():
        return server.url or ""

    projects_module = build_projects_module(config, log, get_app_url)
    decisions_service = DecisionsService()  # one instance: the loop panel, the decisions routes and the wrap-up share it
    loop_read_module = build_loop_read_module(config, log, projects_module.project_service, activity_monitor,
                                              projects_module.planning_requirement_service, decisions_service=decisions_service,
                                              claude_account_service=build_shared_claude_account_service(config),
                                              claude_call_log_service=build_shared_claude_call_log_service(config))
    loop_commands_module = build_loop_commands_module(config, log, projects_module.project_service,
                                                      loop_read_module.loop_status_service, activity_monitor, get_app_url,
                                                      projects_module.planning_requirement_service)
    multi_repo_module = build_multi_repo_module(config, log, projects_module.project_service, loop_read_module.loop_status_service,
                                                loop_commands_module.loop_worktree_service, projects_module.claude_job_service,
                                                loop_commands_module.loop_control_service,
                                                specs_structure_service=projects_module.project_progress_service.specs_structure_service,
                                                roadmap_service=projects_module.roadmap_service,
                                                project_progress_service=projects_module.project_progress_service,
                                                planning_requirement_service=projects_module.planning_requirement_service,
                                                planning_repos_service=projects_module.planning_repos_service,
                                                on_plan_changed=projects_module.on_plan_changed,
                                                subprojects_service=projects_module.subprojects_service,
                                                context_block_service=projects_module.context_block_service,
                                                decisions_service=decisions_service)
    # the validation service resolves a secondary repository (`?repo=`) through the registry the multi-repo module built
    # (subprojects_service gets it from build_multi_repo_module)
    projects_module.subproject_validation_service.repo_registry_service = multi_repo_module.repo_registry_service
    quarantine_module = build_quarantine_module(config, log, projects_module.project_service, loop_read_module, loop_commands_module,
                                                projects_module.planning_requirement_service)
    app_controller = AppController(config, log, server, activity_monitor, projects_module.project_service,
                                   app_diagnostics_service=build_app_diagnostics_service(
                                       config, lambda: list_usage_folders(projects_module.project_service)))
    app_settings_controller = AppSettingsController(log, AppSettingsService(config, AppConfigRepository(config.user_data_dir),
                                                                           projects_module.project_service, LoopFileRepository()))

    for module in (projects_module, loop_read_module, loop_commands_module, multi_repo_module, quarantine_module):
        for controller in module.controllers:
            controller.register_routes(router)
        for provider in module.busy_providers:
            activity_monitor.add_busy_provider(provider)
    app_controller.register_routes(router)
    app_settings_controller.register_routes(router)
    return AssembledApplication(config, log, server, activity_monitor, projects_module, loop_read_module, loop_commands_module,
                                multi_repo_module)


def write_app_registry(application):
    """app.json: how the engine (monitor_proc) and the skill find the live app and register a project in it."""
    path = application.config.app_registry_path
    os.makedirs(os.path.dirname(path), exist_ok=True)
    registry = {"pid": os.getpid(), "port": application.server.port, "url": application.server.url, "token": application.server.token,
                "started_at": datetime.datetime.now().isoformat(timespec="seconds"), "app_root_dir": application.config.app_root_dir}
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(registry, handle, ensure_ascii=False, indent=2)
    os.replace(temporary, path)


def delete_app_registry(application):
    path = application.config.app_registry_path
    try:
        with open(path, encoding="utf-8") as handle:
            if json.load(handle).get("pid") != os.getpid():
                return
        os.remove(path)
    except (OSError, ValueError):
        pass


def start_application(application, initial_project_path=""):
    """Opens the port, registers the app, opens the initial project (if any), prints APP_URL/MONITOR_URL and serves until stopped."""
    app_url = application.server.open_port(application.config.port_range, application.config.fixed_port)
    write_app_registry(application)
    initial_url = app_url
    if initial_project_path:
        project = application.projects_module.project_service.open_existing_project(initial_project_path)
        initial_url = application.get_project_url(project.id)
    print("MONITOR_URL=" + initial_url, flush=True)
    print("APP_URL=" + app_url, flush=True)
    application.log.info("app_started", url=app_url, pid=os.getpid())
    application.activity_monitor.watch_idle_in_background()
    if application.config.open_browser:
        threading.Thread(target=webbrowser.open, args=(initial_url,), daemon=True).start()
    try:
        application.server.serve_until_stopped()
    finally:
        application.projects_module.project_service.release_projects_on_shutdown()
        delete_app_registry(application)
        application.log.info("app_stopped", pid=os.getpid())
