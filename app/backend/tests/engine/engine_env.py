"""Isolated environment of the engine tests (import BEFORE the engine).

- Engine (`backend/harness_backend/engine`) and `backend` on sys.path (`import run_loop`, `from harness_backend...`).
- Temporary LOOP_DATA_DIR: nothing reads or writes the real ~/.loop-harness (config.json, app.json, .env).
- LOOP_SKILL_DIR -> temporary FAKE plugin (<tmp>/fake-plugin/skills/orchestrator with SKILL.md + hooks/guard.py COPIED from the
  public plugin in this repo, read-only, and <tmp>/fake-plugin/hooks/hooks.json pointing at it with ${CLAUDE_PLUGIN_ROOT}).
  The real plugin is never touched. Without the plugin guard, GUARD_AVAILABLE=False and the tests that depend on it skip.
Idempotent: the first module that imports it prepares; the others reuse it (same process).
"""
import atexit
import json
import os
import shutil
import sys
import tempfile

ENGINE_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(os.path.dirname(ENGINE_TESTS_DIR))
APP_ROOT_DIR = os.path.dirname(BACKEND_DIR)
REPO_ROOT_DIR = os.path.dirname(APP_ROOT_DIR)
ENGINE_DIR = os.path.join(BACKEND_DIR, "harness_backend", "engine")
PLUGIN_SKILL_DIR = os.path.join(REPO_ROOT_DIR, "plugin", "skills", "orchestrator")
GUARD_REAL = os.path.join(PLUGIN_SKILL_DIR, "hooks", "guard.py")
PREPARED_MARK = "LOOP_TEST_ENV_PREPARED"
HOOKS_JSON = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [
    {"type": "command", "command": "python \"${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py\" --profile auto"}]}]}}

for _folder in (BACKEND_DIR, ENGINE_DIR):
    if _folder not in sys.path:
        sys.path.insert(0, _folder)


def create_fake_skill(skill_dir, copy_guard=True):
    """Minimal plugin around `skill_dir` (= <plugin>/skills/orchestrator): SKILL.md + hooks/guard.py (copy of the public plugin
    guard, when it exists) and <plugin>/hooks/hooks.json. Returns the guard path ('' without a guard)."""
    os.makedirs(os.path.join(skill_dir, "hooks"), exist_ok=True)
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as handle:
        handle.write("---\nname: orchestrator\n---\nfake skill of the tests\n")
    plugin_hooks = os.path.join(skill_dir, "..", "..", "hooks")
    os.makedirs(plugin_hooks, exist_ok=True)
    with open(os.path.join(plugin_hooks, "hooks.json"), "w", encoding="utf-8") as handle:
        json.dump(HOOKS_JSON, handle)
    if copy_guard and os.path.isfile(GUARD_REAL):
        target = os.path.join(skill_dir, "hooks", "guard.py")
        shutil.copyfile(GUARD_REAL, target)
        return target
    return ""


def prepare_isolated_environment():
    if os.environ.get(PREPARED_MARK):
        return
    root = tempfile.mkdtemp(prefix="loop-engine-test-")
    atexit.register(shutil.rmtree, root, True)
    skill = os.path.join(root, "fake-plugin", "skills", "orchestrator")
    create_fake_skill(skill)
    os.environ["LOOP_SKILL_DIR"] = skill
    os.environ["LOOP_DATA_DIR"] = os.path.join(root, "app-data")
    os.environ["LOOP_USER_SHARED_DIR"] = os.path.join(root, "user-shared")  # the user-level shared .env (never the real ~/.claude)
    os.environ["LOOP_CLAUDE_CONFIG_FILE"] = os.path.join(root, "claude.json")  # the user's MCP definitions (never the real ~/.claude.json)
    os.environ[PREPARED_MARK] = root


prepare_isolated_environment()
GUARD = os.path.join(os.environ["LOOP_SKILL_DIR"], "hooks", "guard.py")
GUARD_AVAILABLE = os.path.isfile(GUARD)
NO_GUARD_REASON = "the public plugin guard (plugin/skills/orchestrator/hooks/guard.py) is not in this clone: restore it or set LOOP_SKILL_DIR"
if not GUARD_AVAILABLE:
    sys.stderr.write("[engine tests] WARNING: %s; the tests that go through the preflight or the guard will fail%s" % (NO_GUARD_REASON, os.linesep))
