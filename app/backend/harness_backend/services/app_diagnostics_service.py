"""App diagnostics (GET /api/app/info and `harness.py doctor`) and the test notification (POST /api/app/notify/test).

Never returns an environment variable value nor `.env` content: only channel names and whether they are configured.
"""
import importlib
import json
import os
import re
import sys

from ..config import app_config, envload, skill_locator
from ..engine import cli_output
from ..infrastructure.claude_client import redact_secrets
from ..repositories.claude_call_log_repository import ClaudeCallLogRepository
from ..repositories.loop_file_repository import LoopFileRepository
from .claude_account_service import build_shared_claude_account_service, command_name

GUARD_GIT_PUSH_INPUT = json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}})
GUARD_BLOCKED_CODE = 2
GUARD_TIMEOUT_SECONDS = 30
CLAUDE_VERSION_TIMEOUT_SECONDS = 30
VERSION_OUTPUT_LIMIT = 120
TEST_NOTIFICATION_TIMEOUT_SECONDS = 120
DEFAULT_ENABLED_CHANNELS = ("console", "toast", "file")
CHANNEL_DETAIL_LIMIT = 200
RECENT_TURNS_FOR_USAGE = 5
TOOLKIT_INSTALL_LINES = ("/plugin marketplace add iagopadilha/claude-dev-toolkit",
                         "/plugin install claude-dev-toolkit@claude-dev-toolkit")
NOTIFY_RESULT_LINE_PATTERN = re.compile(r"^(\S+)\s+((?:ok|FAILED|SKIPPED).*)$")


def load_engine_module(engine_dir, name):
    """Imports an engine script (the engine is not a package: it runs as a loose script and imports its neighbors through sys.path)."""
    if engine_dir not in sys.path:
        sys.path.insert(0, engine_dir)
    return importlib.import_module(name)


class AppDiagnosticsService:
    def __init__(self, config, process_runner, claude_client, loop_file_repository=None, claude_call_log_repository=None,
                 project_folders_provider=None):
        """`project_folders_provider()` -> the folders whose loop files tell the usage (None = no project: \"usage unknown\")."""
        self.project_folders_provider = project_folders_provider
        self.config = config
        self.loop_file_repository = loop_file_repository or LoopFileRepository()
        self.claude_call_log_repository = claude_call_log_repository or ClaudeCallLogRepository()
        self.process_runner = process_runner
        self.claude_client = claude_client

    def check_guard_blocks_git_push(self):
        """(found, blocks) running the real guard with `git push` in the executor profile (expected: code 2)."""
        guard_path = self.config.guard_path
        if not guard_path or not os.path.isfile(guard_path):
            return False, False
        code, _, _ = self.process_runner.run_and_wait([sys.executable, guard_path, "--profile", "executor"],
                                                      os.path.dirname(guard_path), GUARD_TIMEOUT_SECONDS,
                                                      input_text=GUARD_GIT_PUSH_INPUT)
        return True, code == GUARD_BLOCKED_CODE

    def read_claude_version(self):
        """`claude --version` -> cli_output.describe_version(...) plus the redacted first output line. Never raises and never
        fails the diagnostics: an old or unreadable version is only a warning."""
        output = ""
        if self.claude_client.is_available():
            binary = self.claude_client.get_binary()
            argv = [sys.executable, binary, "--version"] if binary.endswith(".py") else [binary, "--version"]
            code, stdout, stderr = self.process_runner.run_and_wait(argv, self.config.app_root_dir, CLAUDE_VERSION_TIMEOUT_SECONDS)
            lines = [line.strip() for line in ((stdout or "") + "\n" + (stderr or "")).splitlines() if line.strip()]
            output = lines[0] if lines and code == 0 else ""
        info = cli_output.describe_version(output)
        info["output"] = redact_secrets(output, VERSION_OUTPUT_LIMIT)
        return info

    def list_notify_channels(self):
        """[{"name", "enabled", "required", "configured"}] - only names and booleans. LOOP_NOTIFY_REQUIRED unset = none required."""
        notify = load_engine_module(self.config.engine_dir, "notify")
        envload.load()
        enabled = envload.get_list("LOOP_NOTIFY_CHANNELS") or list(DEFAULT_ENABLED_CHANNELS)
        required = envload.get_list("LOOP_NOTIFY_REQUIRED")
        return [{"name": name, "enabled": name in enabled, "required": name in required, "configured": not notify.missing_config(name)}
                for name in notify.CHANNELS]

    def describe_claude_commands(self):
        """Main and fallback `claude` commands: found or not, auth method and account (`auth status`, never a secret)."""
        return build_shared_claude_account_service(self.config).get_rows(wait=True)  # cached a few minutes, shared with the loop panel

    @staticmethod
    def attach_usage(command_rows, usage_view):
        """Each command row gets `usage`: the usage-view rows of that command (one per cc account), [] when none."""
        return [dict(row, usage=[item for item in usage_view if item["command"] == command_name(row.get("name"))]) for row in command_rows]

    def build_claude_usage(self, project_folders=()):
        """5-hour and weekly usage per command/account, from `meta.json`, the newest turns and the call log of the given project folders
        (the loop's work folder and the repo). No query of its own; without a project the configured commands show "usage unknown"."""
        meta, turns, calls = {}, [], []
        for folder in project_folders:
            meta = meta or self.loop_file_repository.read_meta(folder)
            for number in self.loop_file_repository.list_turn_numbers(folder)[-RECENT_TURNS_FOR_USAGE:]:
                turns.append(self.loop_file_repository.read_turn(folder, number))
            calls += self.claude_call_log_repository.read_calls(folder)
        calls.sort(key=lambda entry: str(entry.get("at") or ""), reverse=True)
        return build_shared_claude_account_service(self.config).build_usage_view(meta, turns, calls, wait=True)

    def find_toolkit(self):
        """Plugin root of claude-dev-toolkit ('' when not found): `dev_toolkit_dir` of config.json, the plugin cache, the sibling clone."""
        return skill_locator.find_toolkit_root(app_config.read_user_config(self.config.user_data_dir))

    def check_standards_bindings(self, project_folders, toolkit_root):
        """[{"folder", "slot", "skill", "resolved"}] for every Standards binding of the given projects (root and subprojects), resolved the way
        the runtime bundle does (user skills, toolkit, Maestro plugin, installed plugins). Never raises."""
        standards = load_engine_module(self.config.engine_dir, "standards")
        isolation = load_engine_module(self.config.engine_dir, "runtime_isolation")
        plugin_root = skill_locator.plugin_root_of(self.config.orchestrator_skill_dir)
        others = isolation.installed_plugin_skill_dirs()
        rows = []
        for folder in project_folders:
            for entry in standards.project_bindings(folder):
                for slot, value in entry["bindings"].items():
                    name = standards.skill_name(value)
                    found, _ = isolation.resolve_skill_sources(plugin_root, isolation.user_skills_dir(), [name], toolkit_root, others)
                    rows.append({"folder": os.path.join(folder, entry["folder"]) if entry["folder"] != "." else folder,
                                 "slot": slot, "skill": value, "resolved": name in found})
        return rows

    def build_diagnostics(self, project_folders=None):
        """Dictionary of /api/app/info (without `seconds_until_shutdown`, which belongs to the server process).
        `project_folders` = the folders whose loop files tell the usage of each command (none = \"usage unknown\")."""
        guard_found, guard_blocks = self.check_guard_blocks_git_push()
        if project_folders is None:
            project_folders = self.project_folders_provider() if self.project_folders_provider else []
        usage_view = self.build_claude_usage(project_folders)
        channels = self.list_notify_channels()
        problems = []
        if not self.config.orchestrator_skill_dir:
            problems.append("orchestrator skill not found (LOOP_SKILL_DIR, config.json orchestrator_skill_dir or the plugin next to the app)")
        if not guard_found:
            problems.append("the skill's hooks/guard.py was not found")
        elif not guard_blocks:
            problems.append("guard.py did not block `git push` (expected code 2): do not run the loop unsupervised")
        toolkit_root = self.find_toolkit()
        if not toolkit_root:
            problems.append("claude-dev-toolkit not found (the dev skills and their shared scripts). In Claude Code run: %s"
                            % " ; ".join(TOOLKIT_INSTALL_LINES))
        warnings = []
        standards_rows = self.check_standards_bindings(project_folders, toolkit_root) if project_folders else []
        warnings += ["standards: slot `%s` of %s is bound to `%s`, which is not installed (looked in ~/.claude/skills, the toolkit, the "
                     "Maestro plugin and the installed plugins)" % (row["slot"], row["folder"], row["skill"])
                     for row in standards_rows if not row["resolved"]]
        claude_on_path = self.claude_client.is_available()
        version = self.read_claude_version()
        if not claude_on_path:
            problems.append("`claude` command not found on the PATH")
        elif version["warning"]:
            warnings.append(version["warning"])
        problems += ["required notify channel %s is not configured" % channel["name"] for channel in channels if channel["required"] and not channel["configured"]]
        return {
            "app_root_dir": self.config.app_root_dir,
            "data_dir": self.config.user_data_dir,
            "skill_dir": self.config.orchestrator_skill_dir,
            "toolkit": {"path": toolkit_root, "found": bool(toolkit_root)},
            "standards": standards_rows,
            "guard": {"path": self.config.guard_path, "found": guard_found, "blocks_git_push": guard_blocks},
            "claude": {"on_path": claude_on_path, "binary_configured": bool(self.config.claude_binary), "version": version["version"],
                       "version_output": version["output"], "min_tested_version": version["min_tested_version"],
                       "older_than_tested": version["older_than_tested"]},
            "claude_commands": self.attach_usage(self.describe_claude_commands(), usage_view),
            "claude_usage": usage_view,
            "notify_channels": channels,
            "env_loaded": bool(envload.load_all()),
            "problems": problems,
            "warnings": warnings,
        }

    def send_test_notification(self, channels=None):
        """`notify.py --test [--channels a,b]`. Returns {"ok", "results": {channel: short redacted text}, "error"}."""
        valid_names = set(load_engine_module(self.config.engine_dir, "notify").CHANNELS)
        channels = [channel for channel in (channels or []) if isinstance(channel, str)]
        unknown = [channel for channel in channels if channel not in valid_names]
        if unknown:
            return {"ok": False, "results": {}, "error": "unknown channel: %s" % ", ".join(unknown)}
        argv = [sys.executable, self.config.engine_script_path("notify"), "--test"] + (["--channels", ",".join(channels)] if channels else [])
        code, output, error = self.process_runner.run_and_wait(argv, self.config.engine_dir, TEST_NOTIFICATION_TIMEOUT_SECONDS)
        results = {}
        for line in (output or "").splitlines():
            found = NOTIFY_RESULT_LINE_PATTERN.match(line.strip())
            if found and found.group(1) in valid_names:
                results[found.group(1)] = redact_secrets(found.group(2), CHANNEL_DETAIL_LIMIT)
        error_message = "" if code == 0 else redact_secrets((error or "").strip().splitlines()[-1] if (error or "").strip() else "some channel failed", CHANNEL_DETAIL_LIMIT)
        return {"ok": code == 0, "results": results, "error": error_message}


def build_app_diagnostics_service(config, project_folders_provider=None):
    """Factory used when the composition does not inject the service (AppController with the fixed 5-argument signature)."""
    from ..infrastructure.claude_client import ClaudeClient
    from ..infrastructure.process_runner import ProcessRunner
    process_runner = ProcessRunner()
    return AppDiagnosticsService(config, process_runner, ClaudeClient(process_runner, config.claude_binary, fallback_binaries=config.claude_fallback_binaries),
                                  project_folders_provider=project_folders_provider)
