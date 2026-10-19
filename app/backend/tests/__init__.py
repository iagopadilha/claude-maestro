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
