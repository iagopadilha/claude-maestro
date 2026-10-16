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
from .api.controllers.app_controller import AppController
from .services.app_diagnostics_service import build_app_diagnostics_service
from .services.projects_module import build_projects_module
from .services.loop_read_module import build_loop_read_module
from .services.loop_commands_module import build_loop_commands_module


class AssembledApplication:
    def __init__(self, config, log, server, activity_monitor, projects_module, loop_read_module, loop_commands_module):
        self.config = config
        self.log = log
        self.server = server
        self.activity_monitor = activity_monitor
        self.projects_module = projects_module
        self.loop_read_module = loop_read_module
        self.loop_commands_module = loop_commands_module

    def get_app_url(self):
        return self.server.url or ""

    def get_project_url(self, project_id):
        return "%sp/%s/" % (self.get_app_url(), project_id)


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
    loop_read_module = build_loop_read_module(config, log, projects_module.project_service, activity_monitor)
    loop_commands_module = build_loop_commands_module(config, log, projects_module.project_service,
                                                      loop_read_module.loop_status_service, activity_monitor, get_app_url)
    app_controller = AppController(config, log, server, activity_monitor, projects_module.project_service,
                                   app_diagnostics_service=build_app_diagnostics_service(config))

    for module in (projects_module, loop_read_module, loop_commands_module):
        for controller in module.controllers:
            controller.register_routes(router)
        for provider in module.busy_providers:
            activity_monitor.add_busy_provider(provider)
    app_controller.register_routes(router)
    return AssembledApplication(config, log, server, activity_monitor, projects_module, loop_read_module, loop_commands_module)


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
