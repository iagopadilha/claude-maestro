"""Planning readiness: the check the loop runs at start (`run_loop.py <repo> --check --planning`), run in the background while the
user plans, cached in `.specs/project/loop/internal/planning/readiness.json`.

    {state: ready|problems|stale|running|error, at, problems:[...], warnings:[...], uncommitted:[...], fingerprint, error?}

`uncommitted` (RD-01): one block per repo with uncommitted changes, `{kind: "uncommitted", repo, setup_files:[paths], other_changes:[paths]}`;
the check's text line for it is not repeated in `warnings`.

One run at a time per project (a second request waits for the first), 120 s timeout, never on the 2 s poll: `get` only reads the
cache. A cached `ready`/`problems` whose fingerprint (mtimes of ROADMAP, REPOS.json, TEST-ENVIRONMENT, .env.test, the machine and
shared `.env` and `~/.claude.json`) no longer matches is reported as `stale`. A check that could not run is `error` ("readiness
check did not run"), which keeps the approval blocked.
"""
import json
import os
import sys
import threading

from ..infrastructure.claude_client import redact_secrets
from ..infrastructure.process_runner import START_FAILURE_CODE, TIMEOUT_CODE
from ..repositories.loop_command_file_repository import now_iso
from ..repositories.specs_file_repository import loop_state
from .loop_control_service import parse_check_output

STATE_READY, STATE_PROBLEMS, STATE_STALE = "ready", "problems", "stale"
STATE_RUNNING, STATE_ERROR = "running", "error"
UNCOMMITTED_WARNING_PREFIX = "uncommitted changes in "  # the engine's text line of a block (setup_files.block_text)
READINESS_FILE_PARTS = ("planning", "readiness.json")
CHECK_TIMEOUT_SECONDS = 120
MESSAGE_LIMIT = 300
NOT_RUN_MESSAGE = "readiness check did not run"
FINGERPRINT_RELATIVE_PATHS = (".specs/project/ROADMAP.md", ".specs/project/REPOS.json", ".specs/project/TEST-ENVIRONMENT.md",
                              ".specs/project/.env.test")


def default_machine_paths():
    """Machine-level files that change what the check finds: machine and shared `.env`, `~/.claude.json` (MCP servers)."""
    paths = [os.path.join(os.path.expanduser("~"), ".claude.json")]
    try:
        from ..config import app_config
        paths.append(os.path.join(app_config.get_user_data_dir(), ".env"))
    except Exception:  # noqa: BLE001  (a missing layer only makes the fingerprint coarser)
        pass
    try:
        import credential_check  # noqa: E402  (engine folder is on sys.path through specs_file_repository)
        paths.append(credential_check.user_shared_env_path())
    except Exception:  # noqa: BLE001
        pass
    return paths


class ReadinessService:
    def __init__(self, process_runner, script_path_provider, log, machine_paths_provider=None, timeout_seconds=CHECK_TIMEOUT_SECONDS,
                 python=None, enforced=False):
        """`script_path_provider()` -> path of run_loop.py; `machine_paths_provider()` -> files added to the fingerprint (tests inject).
        `enforced`: whether the approval depends on this check (the roadmap service asks `enforced` before refusing)."""
        self.process_runner = process_runner
        self.script_path_provider = script_path_provider
        self.log = log
        self.machine_paths_provider = machine_paths_provider or default_machine_paths
        self.timeout_seconds = timeout_seconds
        self.python = python or sys.executable
        self.enforced = enforced
        self.state_lock = threading.Lock()
        self.pending = {}  # work_dir key -> number of runs scheduled or running
        self.run_locks = {}  # work_dir key -> Lock (one check at a time)

    # ------------------------------------------------ paths and fingerprint
    @staticmethod
    def key_of(context):
        return os.path.normcase(os.path.abspath(context.work_dir))

    @staticmethod
    def cache_path(context):
        return os.path.join(loop_state.internal_dir(context.work_dir), *READINESS_FILE_PARTS)

    def compute_fingerprint(self, context):
        paths = [os.path.join(context.work_dir, *relative.split("/")) for relative in FINGERPRINT_RELATIVE_PATHS]
        paths += list(self.machine_paths_provider())
        paths += self.subproject_paths(context)
        parts = []
        for path in paths:
            try:
                parts.append("%s=%d" % (os.path.basename(path), os.stat(path).st_mtime_ns))
            except OSError:
                parts.append("%s=-" % os.path.basename(path))
        parts.append("context=%s" % self.context_files_signature(context))
        return "|".join(parts)

    @staticmethod
    def subproject_paths(context):
        """SUBPROJECTS.json, the subprojects' CLAUDE.md files and the code inventories: what the subprojects checks read."""
        root = context.work_dir
        paths = [os.path.join(root, ".specs", "project", "SUBPROJECTS.json")]
        try:
            import subprojects  # noqa: E402  (engine folder is on sys.path through specs_file_repository)
            entry = subprojects.load(root)["entry"]
            for project in subprojects.entry_projects(entry):
                paths.append(os.path.join(root, *subprojects.normalize(project["path"]).split("/"), "CLAUDE.md"))
        except Exception:  # noqa: BLE001  (a missing layer only makes the fingerprint coarser)
            pass
        inventory_folder = os.path.join(root, ".specs", "project", "loop", "internal", "inventory")
        try:
            paths += [os.path.join(inventory_folder, name) for name in sorted(os.listdir(inventory_folder)) if name.endswith(".json")]
        except OSError:
            pass
        return paths

    @staticmethod
    def context_files_signature(context):
        """The uncommitted agent-context files (git porcelain paths and states): committing one makes the warning stale."""
        try:
            import context_files  # noqa: E402
            items = context_files.uncommitted_context(context.repo_path)
        except Exception:  # noqa: BLE001
            return "-"
        return ";".join("%s:%s" % (item["path"], item["state"]) for item in items) or "-"

    # ------------------------------------------------ read
    def get(self, context):
        """The cached result with the live state: running while a check is scheduled, stale when the fingerprint moved."""
        cached = self.read_cache(context)
        with self.state_lock:
            running = self.pending.get(self.key_of(context), 0) > 0
        if running:
            return dict(cached, state=STATE_RUNNING)
        if cached["state"] in (STATE_READY, STATE_PROBLEMS) and cached.get("fingerprint") != self.compute_fingerprint(context):
            return dict(cached, state=STATE_STALE)
        return cached

    def read_cache(self, context):
        try:
            with open(self.cache_path(context), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            data = {}
        if not isinstance(data, dict) or data.get("state") not in (STATE_READY, STATE_PROBLEMS, STATE_ERROR):
            return {"state": STATE_STALE, "at": None, "problems": [], "warnings": [], "uncommitted": [], "fingerprint": ""}
        return {"state": data["state"], "at": data.get("at"), "problems": list(data.get("problems") or []),
                "warnings": list(data.get("warnings") or []), "uncommitted": list(data.get("uncommitted") or []),
                "fingerprint": data.get("fingerprint") or "",
                "error": data.get("error") or ""}

    # ------------------------------------------------ run
    def schedule(self, context):
        """Starts a background check (waits behind a running one). Returns the thread (tests join it)."""
        key = self.key_of(context)
        with self.state_lock:
            self.pending[key] = self.pending.get(key, 0) + 1
            run_lock = self.run_locks.setdefault(key, threading.Lock())
        thread = threading.Thread(target=self.run_in_thread, args=(context, key, run_lock), name="readiness-" + key[-20:], daemon=True)
        thread.start()
        return thread

    def run(self, context, wait=False):
        thread = self.schedule(context)
        if wait:
            thread.join()
        return self.get(context)

    def run_in_thread(self, context, key, run_lock):
        try:
            with run_lock:
                result = self.execute_check(context)
                try:
                    os.makedirs(os.path.dirname(self.cache_path(context)), exist_ok=True)
                    with open(self.cache_path(context), "w", encoding="utf-8") as handle:
                        json.dump(result, handle, ensure_ascii=False, indent=2)
                except OSError as failure:
                    self.log.warn("readiness_cache_not_written", message=str(failure)[:200])
        except Exception as failure:  # noqa: BLE001  (the thread must never die silently with the item stuck on running)
            self.log.warn("readiness_check_crashed", message=str(failure)[:200])
        finally:
            with self.state_lock:
                self.pending[key] = max(0, self.pending.get(key, 1) - 1)

    def execute_check(self, context):
        fingerprint = self.compute_fingerprint(context)  # before the run: a change during it leaves the result stale
        argv = [self.python, self.script_path_provider(), context.repo_path, "--check", "--planning"]
        code, output, error = self.process_runner.run_and_wait(argv, context.repo_path, self.timeout_seconds)
        if code == TIMEOUT_CODE:
            return self.build_error("the check took longer than %d s" % self.timeout_seconds, fingerprint)
        if code == START_FAILURE_CODE and not output:
            return self.build_error(error, fingerprint)
        parsed = parse_check_output(output or "")
        if not parsed["ready"] and not parsed["problems"]:  # no verdict at all: the script failed before checking (bad flag, crash)
            last_lines = (error or output or "").strip().splitlines()
            return self.build_error(last_lines[-1] if last_lines else "no READY or PROBLEM line (code %s)" % code, fingerprint)
        problems = [redact_secrets(item, MESSAGE_LIMIT) for item in parsed["problems"]]
        warnings = [redact_secrets(item, MESSAGE_LIMIT) for item in parsed["warnings"] if not item.startswith(UNCOMMITTED_WARNING_PREFIX)]
        return {"state": STATE_PROBLEMS if problems else STATE_READY, "at": now_iso(), "problems": problems, "warnings": warnings,
                "uncommitted": self.read_uncommitted_blocks(context), "fingerprint": fingerprint, "error": ""}

    @staticmethod
    def read_uncommitted_blocks(context):
        """The structured twin of the engine's uncommitted line: one `{kind, repo, setup_files, other_changes}` per repo ([] on any failure)."""
        try:
            import project_repos  # noqa: E402  (engine folder is on sys.path through specs_file_repository)
            import setup_files  # noqa: E402
            repos = project_repos.load_repos(context.repo_path)
            return [dict(block, kind="uncommitted") for block in setup_files.uncommitted_blocks(repos, context.repo_path)]
        except Exception:  # noqa: BLE001
            return []

    @staticmethod
    def build_error(detail, fingerprint):
        return {"state": STATE_ERROR, "at": now_iso(), "problems": [], "warnings": [], "uncommitted": [], "fingerprint": fingerprint,
                "error": redact_secrets(detail, MESSAGE_LIMIT)}
