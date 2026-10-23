"""Test package. The machine-level locations are redirected for every test run so no test ever reads the real user files: the
user-level shared `.env` (`LOOP_USER_SHARED_DIR`), the user's MCP definitions (`LOOP_CLAUDE_CONFIG_FILE`), the user's ~/.claude
(`LOOP_CLAUDE_HOME`: an empty folder, so the plugin cache holds no toolkit) and the sibling clone of the dev toolkit
(`<repo>/../claude-dev-toolkit`): `find_toolkit_root` gives '' instead of the real clone unless the test points `REPO_ROOT_DIR` at a
folder of its own or sets `dev_toolkit_dir` in a config.json. A test that needs the REAL toolkit opts in explicitly through
`skill_locator.find_toolkit_root.real` (see tests/engine/test_credential_testenv_parity.py)."""
import atexit
import os
import shutil
import tempfile

_ISOLATED_ROOT = tempfile.mkdtemp(prefix="loop-tests-user-files-")
atexit.register(shutil.rmtree, _ISOLATED_ROOT, True)
os.environ.setdefault("LOOP_USER_SHARED_DIR", os.path.join(_ISOLATED_ROOT, "user-shared"))
os.environ.setdefault("LOOP_CLAUDE_CONFIG_FILE", os.path.join(_ISOLATED_ROOT, "claude.json"))
os.environ.setdefault("LOOP_CLAUDE_HOME", os.path.join(_ISOLATED_ROOT, "claude-home"))  # the user's ~/.claude (skills, settings.json): runtime isolation reads it

from harness_backend.config import skill_locator  # noqa: E402  (after the environment above is in place)

_REAL_REPO_ROOT_DIR = skill_locator.REPO_ROOT_DIR
_REAL_SIBLING_TOOLKIT = skill_locator.toolkit_plugin_folder(
    os.path.join(_REAL_REPO_ROOT_DIR, "..", skill_locator.TOOLKIT_PLUGIN_NAME, "plugin"))
_real_find_toolkit_root = skill_locator.find_toolkit_root


def _find_toolkit_root_without_the_real_sibling(config=None):
    """The locator's answer, except that the machine's real sibling clone counts as 'not installed' (config.json and the plugin cache
    of the isolated LOOP_CLAUDE_HOME still win first, and a test that patched REPO_ROOT_DIR keeps its own sibling)."""
    found = _real_find_toolkit_root(config)
    is_real_sibling = bool(_REAL_SIBLING_TOOLKIT) and os.path.normcase(found) == os.path.normcase(_REAL_SIBLING_TOOLKIT)
    if found and is_real_sibling and skill_locator.REPO_ROOT_DIR == _REAL_REPO_ROOT_DIR:
        return ""
    return found


_find_toolkit_root_without_the_real_sibling.real = _real_find_toolkit_root  # explicit opt-in to the real clone
skill_locator.find_toolkit_root = _find_toolkit_root_without_the_real_sibling


def settings_text(argv):
    """Text of the `--settings` value of a claude argv: runtime isolation passes a file path (inline JSON breaks under a cmd.exe
    wrapper), the old argv passes the JSON itself."""
    value = argv[argv.index("--settings") + 1]
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as handle:
            return handle.read()
    return value
