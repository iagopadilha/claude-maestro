"""Finds the `orchestrator` skill on this machine. The app is the runtime; the skill stays the brain (the `claude -p` prompt)
and owns the `guard.py` hook (the plugin's hooks/hooks.json points to it).

Lookup order (first folder with a SKILL.md wins):
  1. LOOP_SKILL_DIR (environment variable);
  2. `orchestrator_skill_dir` from the app's config.json (passed in by the caller);
  3. $CLAUDE_PLUGIN_ROOT/skills/orchestrator (when running inside the plugin);
  4. <repo root>/plugin/skills/orchestrator (the app lives next to the plugin in the same clone);
  5. the installed plugin cache: ~/.claude/plugins/cache/*/loop-engineering-harness/*/skills/orchestrator (newest first);
  6. ~/.claude/skills/orchestrator (a manual install).
Resolved on every call (tests switch the variables at run time).

The dev toolkit (plugin `claude-dev-toolkit`, the generic dev skills and their `shared/` scripts) is located by `find_toolkit_root`.
"""
import glob
import json
import os

SKILL_DIR_VARIABLE = "LOOP_SKILL_DIR"
PLUGIN_ROOT_VARIABLE = "CLAUDE_PLUGIN_ROOT"
PLUGIN_NAME = "loop-engineering-harness"
TOOLKIT_PLUGIN_NAME = "claude-dev-toolkit"
TOOLKIT_CONFIG_KEY = "dev_toolkit_dir"
DATA_DIR_VARIABLE = "LOOP_DATA_DIR"
CLAUDE_HOME_VARIABLE = "LOOP_CLAUDE_HOME"  # the user's ~/.claude (tests point it at a temp dir); default ~/.claude
DEFAULT_DATA_DIR_NAME = ".loop-harness"
REPO_ROOT_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))


def list_installed_plugin_skill_dirs():
    """Orchestrator skill folders of the installed plugin (Claude Code plugin cache), most recently changed first."""
    pattern = os.path.join(os.path.expanduser("~"), ".claude", "plugins", "cache", "*", PLUGIN_NAME, "*", "skills", "orchestrator")
    found = [path for path in glob.glob(pattern) if os.path.isdir(path)]
    return sorted(found, key=lambda path: os.path.getmtime(path), reverse=True)


def list_skill_dir_candidates(configured_dir=""):
    plugin_root = os.environ.get(PLUGIN_ROOT_VARIABLE, "")
    candidates = [os.environ.get(SKILL_DIR_VARIABLE, ""), configured_dir,
                  os.path.join(plugin_root, "skills", "orchestrator") if plugin_root else "",
                  os.path.join(REPO_ROOT_DIR, "plugin", "skills", "orchestrator")]
    candidates += list_installed_plugin_skill_dirs()
    candidates.append(os.path.join(os.path.expanduser("~"), ".claude", "skills", "orchestrator"))
    unique = []
    for path in candidates:
        if path and os.path.abspath(path) not in unique:
            unique.append(os.path.abspath(path))
    return unique


def locate_orchestrator_skill_dir(configured_dir=""):
    """First candidate folder that has a SKILL.md; '' when the skill is not installed."""
    for path in list_skill_dir_candidates(configured_dir):
        if os.path.isfile(os.path.join(path, "SKILL.md")):
            return path
    return ""


def locate_guard(skill_dir):
    """The skill's hooks/guard.py, or '' when it does not exist (the engine preflight reports it)."""
    path = os.path.join(skill_dir, "hooks", "guard.py") if skill_dir else ""
    return path if path and os.path.isfile(path) else ""


def plugin_root_of(skill_dir):
    """Plugin root of a skill folder (<plugin>/skills/orchestrator -> <plugin>), or '' without a skill folder."""
    return os.path.abspath(os.path.join(skill_dir, "..", "..")) if skill_dir else ""


# ---- dev toolkit (claude-dev-toolkit) ----------------------------------------------------------------------------------------------

def read_user_config():
    """`<LOOP_DATA_DIR or ~/.loop-harness>/config.json` as a dict ({} when missing or unreadable)."""
    data_dir = os.environ.get(DATA_DIR_VARIABLE) or os.path.join(os.path.expanduser("~"), DEFAULT_DATA_DIR_NAME)
    try:
        with open(os.path.join(data_dir, "config.json"), encoding="utf-8-sig") as handle:
            content = json.load(handle)
        return content if isinstance(content, dict) else {}
    except (OSError, ValueError):
        return {}


def toolkit_plugin_folder(path):
    """The folder holding `skills/` for a toolkit path: the path itself, or its `plugin/` child (a repo root). '' when neither has it."""
    if not path:
        return ""
    for candidate in (path, os.path.join(path, "plugin")):
        if os.path.isdir(os.path.join(candidate, "skills")):
            return os.path.abspath(candidate)
    return ""


def list_installed_toolkit_roots():
    """Toolkit plugin roots in the Claude Code plugin cache (`<claude home>/plugins/cache/*/claude-dev-toolkit/*/`, claude home = LOOP_CLAUDE_HOME
    or `~/.claude`), newest first. The version folder holds `skills/` directly or under `plugin/`."""
    claude_home = os.environ.get(CLAUDE_HOME_VARIABLE) or os.path.join(os.path.expanduser("~"), ".claude")
    pattern = os.path.join(glob.escape(claude_home), "plugins", "cache", "*", TOOLKIT_PLUGIN_NAME, "*")
    versions = sorted((path for path in glob.glob(pattern) if os.path.isdir(path)), key=lambda path: os.path.getmtime(path), reverse=True)
    return [root for root in (toolkit_plugin_folder(path) for path in versions) if root]


def find_toolkit_root(config=None):
    """Plugin root of claude-dev-toolkit (the folder holding `skills/` and `shared/`), or '' when it is not installed.

    Order: `dev_toolkit_dir` of config.json (a plugin folder or the repo root; `config` is that dict, read from the data dir when None),
    then the newest installed copy in the plugin cache, then the sibling clone `<repo root>/../claude-dev-toolkit/plugin`."""
    config = read_user_config() if config is None else config
    configured = config.get(TOOLKIT_CONFIG_KEY) if isinstance(config, dict) else ""
    found = toolkit_plugin_folder(os.path.expanduser(configured.strip())) if isinstance(configured, str) and configured.strip() else ""
    if found:
        return found
    installed = list_installed_toolkit_roots()
    if installed:
        return installed[0]
    return toolkit_plugin_folder(os.path.join(REPO_ROOT_DIR, "..", TOOLKIT_PLUGIN_NAME, "plugin"))
