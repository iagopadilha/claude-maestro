"""Bridge between the engine and the app package. The engine runs as a loose script (`python engine/run_loop.py`) and also
imported (`import run_loop` with engine/ on sys.path); in both cases it must find `harness_backend` (folder `backend`) to use
the app configuration: the skill (brain + owner of the guard) and the `.env`.

Skill folder precedence: see config/skill_locator.py (LOOP_SKILL_DIR > `orchestrator_skill_dir` in the app's config.json >
the plugin next to the app or installed in the Claude Code plugin cache). Resolved on every call (tests switch the variable
at run time).
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(os.path.dirname(ENGINE_DIR))
APP_ROOT_DIR = os.path.dirname(BACKEND_DIR)

for _folder in (BACKEND_DIR, ENGINE_DIR):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)

from harness_backend.config import app_config, skill_locator  # noqa: E402
from harness_backend.config import envload  # noqa: E402,F401  (re-exported: notify/delivery/run_loop use app_paths.envload)


def _configured_skill_dir():
    return app_config.read_user_config(app_config.get_user_data_dir()).get("orchestrator_skill_dir", "")


def default_output_dir():
    """The app's output folder (`output_dir` in config.json, default ~/Downloads): where summaries and backups go, never the repo."""
    configured = app_config.read_user_config(app_config.get_user_data_dir()).get("output_dir", "")
    return configured or os.path.join(os.path.expanduser("~"), "Downloads")


def locate_skill_dir():
    """Orchestrator skill folder (with SKILL.md) or '' when it is not installed."""
    return skill_locator.locate_orchestrator_skill_dir(_configured_skill_dir())


def locate_guard():
    """The skill's hooks/guard.py, or '' when it does not exist."""
    return skill_locator.locate_guard(locate_skill_dir())


def describe_guard_search_paths():
    return ", ".join(os.path.join(folder, "hooks", "guard.py") for folder in skill_locator.list_skill_dir_candidates(_configured_skill_dir()))
