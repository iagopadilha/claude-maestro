"""Routes of the app hub (`/api/app/*`): health, info (diagnostics), test notification and shutdown.

The POSTs require the token (Router default). `info` never returns a variable value: only channel names and whether they
are configured.
"""
import os

from ...services.app_diagnostics_service import build_app_diagnostics_service
from ..router import Response

APP_VERSION = "0.2.0"
CHANNELS_KEY = "channels"


class AppController:
    def __init__(self, config, log, server, activity_monitor, project_service, app_diagnostics_service=None):
        self.config = config
        self.log = log
        self.server = server
        self.activity_monitor = activity_monitor
        self.project_service = project_service
        self.app_diagnostics_service = app_diagnostics_service or build_app_diagnostics_service(config)

    def register_routes(self, router):
        router.add("GET", "/api/app/health", self.get_health)
        router.add("GET", "/api/app/info", self.get_diagnostics)
        router.add("POST", "/api/app/notify/test", self.send_test_notification)
        router.add("POST", "/api/app/shutdown", self.shut_down_app)

    def get_health(self, request):
        return Response.json(200, {"ok": True, "pid": os.getpid(), "version": APP_VERSION, "url": self.server.url or ""})

    def get_diagnostics(self, request):
        diagnostics = self.app_diagnostics_service.build_diagnostics()
        diagnostics.update(ok=True, version=APP_VERSION, pid=os.getpid(), url=self.server.url or "",
                           seconds_until_shutdown=self.activity_monitor.seconds_until_shutdown(),
                           open_projects=len(self.project_service.list_projects()))
        return Response.json(200, diagnostics)

    def send_test_notification(self, request):
        channels = request.body.get(CHANNELS_KEY)
        if channels is not None and not (isinstance(channels, list) and all(isinstance(channel, str) for channel in channels)):
            return Response.error(400, "channels must be a list of names")
        result = self.app_diagnostics_service.send_test_notification(channels)
        self.log.info("test_notification_sent", ok=result["ok"], channels=",".join(result["results"]))
        if result["error"] and not result["results"]:
            return Response.json(400, result)
        return Response.json(200, result)

    def shut_down_app(self, request):
        self.log.info("shutdown_requested", origin="api")
        self.server.request_shutdown()
        return Response.json(200, {"ok": True})
