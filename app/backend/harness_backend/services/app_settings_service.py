"""The Settings screen of the app (GET/POST /api/app/settings): the list of settings with their current value and default, the
validated write to `config.json`, the caveman availability and the caveman cost comparison over the turns already recorded.

Read, decide, write stay apart: `build_item` and `build_caveman_comparison` only read; `update_setting` validates (domain), then
asks the repository to write. Nothing here starts a process.
"""
import os

from ..config import app_config
from ..domain import app_settings
from ..domain.project import ProjectContext
from ..engine import claude_binaries
from ..engine import runtime_isolation

MIN_TURNS_PER_SIDE = 3  # CAV-04: fewer turns on either side is "not enough data yet"
COST_DIGITS = 4
TURN_COST_KEY = "total_cost_usd"
TURN_CAVEMAN_KEY = "caveman"
CAVEMAN_KEY = runtime_isolation.CAVEMAN_KEY


class AppSettingsService:
    def __init__(self, config, config_repository, project_service, loop_file_repository, read_user_settings=None):
        """`read_user_settings()` -> the user's Claude settings dict (default: `runtime_isolation.read_user_settings`, which honors
        LOOP_CLAUDE_HOME); tests inject a fake."""
        self.config = config
        self.config_repository = config_repository
        self.project_service = project_service
        self.loop_file_repository = loop_file_repository
        self.read_user_settings = read_user_settings or runtime_isolation.read_user_settings

    # ---- read ------------------------------------------------------------------------------------------------------------------

    def get_settings(self):
        stored = self.config_repository.read_all_or_empty()
        user_settings = self.read_user_settings()
        return {"ok": True,
                "settings": [self.build_item(definition, stored, user_settings) for definition in app_settings.SETTING_CATALOG],
                "info": self.build_info_rows(),
                "caveman": self.build_caveman_comparison()}

    def is_caveman_installed(self, user_settings):
        return bool(runtime_isolation.find_caveman_hooks(user_settings))

    def read_effective_value(self, definition, stored):
        """The value the app really uses for this key: the stored one when the code accepts it, else the default (never an error)."""
        key = definition.key
        notes = []
        if key == "loop_isolation":
            return runtime_isolation.isolation_enabled(stored)
        if key == CAVEMAN_KEY:
            return stored.get(key, False) is True
        if key == "loop_extra_skills":
            extra = stored.get(key)
            return [str(item).strip() for item in extra if str(item).strip()] if isinstance(extra, list) else []
        if key == "orchestrator_model":
            return app_config.read_orchestrator_model(stored)
        if key == "claude_binary":
            value = stored.get(key, "")
            return value if isinstance(value, str) else ""
        if key == "claude_fallback_binaries":
            return claude_binaries.read_fallbacks(stored.get(key))
        if key == "idle_hours":
            value = stored.get(key, definition.default)
            return value if app_settings.is_finite_number(value) and value >= 0 else definition.default
        if key == "output_dir":
            value = stored.get(key)
            return value if isinstance(value, str) and value else app_settings.get_default_value(definition)
        return app_config.read_positive_threshold(stored, key, definition.default, notes)

    def build_item(self, definition, stored, user_settings):
        value = self.read_effective_value(definition, stored)
        item = {"key": definition.key, "label": definition.label, "description": definition.description, "type": definition.type,
                "value": value, "default": app_settings.get_default_value(definition), "restart_required": definition.restart_required,
                "customized": definition.key in stored}
        if definition.type == app_settings.CHOICE:
            choices = [{"value": choice, "label": label} for choice, label in definition.choices]
            if value not in [choice["value"] for choice in choices]:
                choices.append({"value": value, "label": value})  # a model id written by hand stays selectable
            item["choices"] = choices
        if definition.key == CAVEMAN_KEY:
            installed = self.is_caveman_installed(user_settings)
            item["available"] = installed
            item["note"] = self.describe_caveman_note(installed, runtime_isolation.isolation_enabled(stored))
        return item

    @staticmethod
    def describe_caveman_note(installed, isolation_on):
        if not installed:
            return app_settings.CAVEMAN_NOT_INSTALLED_NOTE
        if not isolation_on:
            return "Has no effect while isolation is off: your own settings then load as they are."
        return ""

    def build_info_rows(self):
        port_range = "%d-%d" % tuple(self.config.port_range)
        skill_dir = self.config.orchestrator_skill_dir
        plugin_root = os.path.abspath(os.path.join(skill_dir, "..", "..")) if skill_dir else ""
        return [{"key": "data_dir", "label": "Data folder", "value": self.config.user_data_dir},
                {"key": "plugin_root", "label": "Plugin folder", "value": plugin_root or "not found"},
                {"key": "guard_path", "label": "Guard hook", "value": self.config.guard_path or "not found"},
                {"key": "port_range", "label": "Port range", "value": str(self.config.fixed_port) if self.config.fixed_port else port_range}]

    def list_recorded_turns(self):
        """[{caveman: bool, cost_usd: float}] of every turn log of every project (the work folder and the repo folder, once each).
        A turn log with no `caveman` key was written before the option existed (and before runtime isolation): it is left out, so both
        sides compare turns of the same app version."""
        turns, folders = [], set()
        for project in self.project_service.list_projects():
            context = ProjectContext(project)
            for folder in dict.fromkeys([context.work_dir, context.repo_path]):
                key = os.path.normcase(os.path.abspath(folder))
                if key in folders:
                    continue
                folders.add(key)
                for number in self.loop_file_repository.list_turn_numbers(folder):
                    record = self.loop_file_repository.read_turn(folder, number)
                    cost = record.get(TURN_COST_KEY) if isinstance(record, dict) else None
                    flag = record.get(TURN_CAVEMAN_KEY) if isinstance(record, dict) else None
                    if isinstance(flag, bool) and isinstance(cost, (int, float)) and not isinstance(cost, bool):
                        turns.append({"caveman": flag, "cost_usd": float(cost)})
        return turns

    def build_caveman_comparison(self):
        turns = self.list_recorded_turns()
        sides = {name: [turn["cost_usd"] for turn in turns if turn["caveman"] is (name == "on")] for name in ("on", "off")}
        summary = {name: {"turns": len(costs), "average_cost_usd": round(sum(costs) / len(costs), COST_DIGITS) if costs else None}
                   for name, costs in sides.items()}
        enough = all(len(costs) >= MIN_TURNS_PER_SIDE for costs in sides.values())
        return {"recommendation": app_settings.CAVEMAN_RECOMMENDATION, "on": summary["on"], "off": summary["off"], "enough_data": enough,
                "message": "" if enough else "Not enough data yet (%d on / %d off)" % (len(sides["on"]), len(sides["off"]))}

    # ---- write -----------------------------------------------------------------------------------------------------------------

    def update_setting(self, key, value):
        """Validates and saves one value; returns the updated item. Raises InvalidSettingError (400), ConfigFileUnreadableError (409)."""
        definition = app_settings.find_definition(key)
        stored_value = app_settings.validate_setting_value(definition, value)
        if key == CAVEMAN_KEY and stored_value is True and not self.is_caveman_installed(self.read_user_settings()):
            raise app_settings.SettingUnavailableError(app_settings.CAVEMAN_NOT_INSTALLED_NOTE)
        self.config_repository.set_value(key, stored_value)
        return self.read_item(definition)

    def reset_setting(self, key):
        """Removes the key from config.json (back to the default) and returns the item."""
        definition = app_settings.find_definition(key)
        self.config_repository.remove_key(key)
        return self.read_item(definition)

    def read_item(self, definition):
        return self.build_item(definition, self.config_repository.read_all_or_empty(), self.read_user_settings())
