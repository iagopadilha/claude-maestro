"""Single app configuration (paths, ports, times). Built once in the composition and injected where needed.

Precedence: command-line argument > environment variable > ~/.loop-harness/config.json > default.
"""
import json
import os
from dataclasses import dataclass, field

from . import skill_locator

APP_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
DEFAULT_DATA_DIR_NAME = ".loop-harness"
DEFAULT_PORT_RANGE = (47800, 47899)
DEFAULT_IDLE_HOURS = 2.0


def get_user_data_dir():
    """~/.loop-harness (or LOOP_DATA_DIR): config.json, projects.json, app.json, optional .env, logs/."""
    return os.path.abspath(os.environ.get(DATA_DIR_VARIABLE) or os.path.join(os.path.expanduser("~"), DEFAULT_DATA_DIR_NAME))


def read_user_config(data_dir):
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8") as handle:
            content = json.load(handle)
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


@dataclass
class AppConfig:
    app_root_dir: str
    user_data_dir: str
    orchestrator_skill_dir: str
    guard_path: str
    frontend_dir: str
    templates_dir: str
    engine_dir: str
    claude_transcripts_dir: str
    output_dir: str
    claude_binary: str = ""  # empty = `claude` from PATH; tests inject a fake .py
    port_range: tuple = DEFAULT_PORT_RANGE
    fixed_port: int = 0
    idle_hours: float = DEFAULT_IDLE_HOURS  # 0 = never shuts down on its own
    open_browser: bool = True
    engine_script_overrides: dict = field(default_factory=dict)  # tests only: {"run_loop": path, "delivery": ..., "loop_summary": ...}

    def engine_script_path(self, name):
        """Engine CLI script (`run_loop`, `delivery`, `loop_summary`, `loop_state`...), honoring a test override."""
        return self.engine_script_overrides.get(name) or os.path.join(self.engine_dir, name + ".py")

    @property
    def app_registry_path(self):
        """app.json: pid, port, url and administrative token of the live app (only the local user reads it)."""
        return os.path.join(self.user_data_dir, "app.json")

    @property
    def projects_registry_path(self):
        return os.path.join(self.user_data_dir, "projects.json")

    @property
    def logs_dir(self):
        return os.path.join(self.user_data_dir, "logs")


def load_app_config(**overrides):
    """Builds the AppConfig. `overrides` come from the command line (only non-empty values count)."""
    data_dir = overrides.pop("user_data_dir", "") or get_user_data_dir()
    user_config = read_user_config(data_dir)
    skill_dir = skill_locator.locate_orchestrator_skill_dir(user_config.get("orchestrator_skill_dir", ""))
    values = {
        "app_root_dir": APP_ROOT_DIR,
        "user_data_dir": data_dir,
        "orchestrator_skill_dir": skill_dir,
        "guard_path": skill_locator.locate_guard(skill_dir),
        "frontend_dir": os.path.join(APP_ROOT_DIR, "frontend"),
        "templates_dir": os.path.join(APP_ROOT_DIR, "templates"),
        "engine_dir": os.path.join(APP_ROOT_DIR, "backend", "harness_backend", "engine"),
        "claude_transcripts_dir": user_config.get("claude_transcripts_dir") or os.path.join(os.path.expanduser("~"), ".claude", "projects"),
        "output_dir": user_config.get("output_dir") or os.path.join(os.path.expanduser("~"), "Downloads"),
        "claude_binary": user_config.get("claude_binary", ""),
        "idle_hours": float(user_config.get("idle_hours", DEFAULT_IDLE_HOURS)),
    }
    for key, value in overrides.items():
        if value not in (None, "", {}):
            values[key] = value
    return AppConfig(**values)
