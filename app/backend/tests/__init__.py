"""Test package. Two machine-level locations are redirected for every test run so no test ever reads the real user files: the
user-level shared `.env` (`LOOP_USER_SHARED_DIR`) and the user's MCP definitions (`LOOP_CLAUDE_CONFIG_FILE`)."""
import atexit
import os
import shutil
import tempfile

_ISOLATED_ROOT = tempfile.mkdtemp(prefix="loop-tests-user-files-")
atexit.register(shutil.rmtree, _ISOLATED_ROOT, True)
os.environ.setdefault("LOOP_USER_SHARED_DIR", os.path.join(_ISOLATED_ROOT, "user-shared"))
os.environ.setdefault("LOOP_CLAUDE_CONFIG_FILE", os.path.join(_ISOLATED_ROOT, "claude.json"))
os.environ.setdefault("LOOP_CLAUDE_HOME", os.path.join(_ISOLATED_ROOT, "claude-home"))  # the user's ~/.claude (skills, settings.json): runtime isolation reads it


def settings_text(argv):
    """Text of the `--settings` value of a claude argv: runtime isolation passes a file path (inline JSON breaks under a cmd.exe
    wrapper), the old argv passes the JSON itself."""
    value = argv[argv.index("--settings") + 1]
    if os.path.isfile(value):
        with open(value, encoding="utf-8") as handle:
            return handle.read()
    return value
