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
"""
import glob
import os

SKILL_DIR_VARIABLE = "LOOP_SKILL_DIR"
PLUGIN_ROOT_VARIABLE = "CLAUDE_PLUGIN_ROOT"
PLUGIN_NAME = "loop-engineering-harness"
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
