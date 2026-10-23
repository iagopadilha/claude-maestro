"""Routes of the Settings screen: `GET /api/app/settings` and `POST /api/app/settings` (`{key, value, confirm: true}`; or
`{key, reset: true, confirm: true}` to drop the key and go back to the default). The POST needs the token (Router default) and `confirm: true`.
"""
from ...domain.app_settings import ConfigFileUnreadableError, InvalidSettingError
from ..router import Response

SETTINGS_ROUTE = "/api/app/settings"


class AppSettingsController:
    def __init__(self, log, app_settings_service):
        self.log = log
        self.app_settings_service = app_settings_service

    def register_routes(self, router):
        router.add("GET", SETTINGS_ROUTE, self.get_settings)
        router.add("POST", SETTINGS_ROUTE, self.save_setting)

    def get_settings(self, request):
        return Response.json(200, self.app_settings_service.get_settings())

    def save_setting(self, request):
        body = request.body
        if body.get("confirm") is not True:
            return Response.error(400, "confirm must be true")
        key = body.get("key")
        wants_reset = body.get("reset") is True
        if not wants_reset and "value" not in body:
            return Response.error(400, "value is required (or reset: true)")
        try:
            item = (self.app_settings_service.reset_setting(key) if wants_reset
                    else self.app_settings_service.update_setting(key, body["value"]))
        except InvalidSettingError as error:
            return Response.error(400, str(error))
        except ConfigFileUnreadableError as error:
            return Response.error(409, str(error))
        self.log.info("app_setting_saved", key=key, reset=wants_reset)
        return Response.json(200, {"ok": True, "setting": item})
