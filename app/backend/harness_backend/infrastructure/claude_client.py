"""Client of `claude -p` (headless Claude Code): builds the argv, runs it with stdin, reads the stream-json output, environment
without secrets.

Who uses it: the read-only chat, the action router, the roadmap/mapping jobs. The loop itself stays in the engine
(`run_loop.py`).
"""
import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from typing import Protocol

from ..config import skill_locator
from ..engine import claude_binaries  # engine/ has no __init__.py: it imports as a namespace package (stdlib only modules)
from ..engine import cli_output
from ..engine import usage_windows
from .process_runner import TIMEOUT_CODE, TIMEOUT_MESSAGE

logger = logging.getLogger(__name__)

DEFAULT_BINARY_NAME = "claude"
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
SECRET_VARIABLE_NAME_PATTERN = re.compile(r"(?i)(token|secret|passw|credential|private_key|api_?key|auth)")
CLAUDE_VARIABLE_NAME_PATTERN = re.compile(r"^(ANTHROPIC_|CLAUDE_)")  # claude itself needs to authenticate
ALWAYS_REMOVED_VARIABLES = ("LOOP_MONITOR_URL",)
SECRET_TEXT_PATTERN = cli_output.SECRET_TEXT_PATTERN
SUSPICIOUS_LONG_TEXT_PATTERN = cli_output.SUSPICIOUS_LONG_TEXT_PATTERN
ERROR_MESSAGE_LIMIT = 300
LIMIT_CAUSE = "usage_limit"  # why run_once asks for the next command: the account ran out of usage
UNUSABLE_CAUSE = "account_unusable"  # ... or the account cannot be used at all (logged out, revoked token, no access)
DISABLED_PUSH_URL = "push-disabled-by-loop://use-delivery.py"
LIST_REMOTES_TIMEOUT_SECONDS = 30
REDACTION_MARK = cli_output.REDACTION_MARK
_call_context = threading.local()


@contextmanager
def call_kind(kind):
    """Names what the `run_prompt` calls made inside the block are for ("chat", "roadmap", "map"...): the call log shows it."""
    previous = getattr(_call_context, "kind", "")
    _call_context.kind = kind
    try:
        yield
    finally:
        _call_context.kind = previous


def redact_secrets(text, limit=None):
    """Removes what looks like a credential from the text before it leaves through the API (a claude error may quote a
    command with a token)."""
    return cli_output.redact(text, limit)


def build_push_disabled_variables(remote_names, base_env=None):
    """GIT_CONFIG_* that swap the pushurl of each remote for an invalid URL, adding to the existing GIT_CONFIG_COUNT."""
    base_env = os.environ if base_env is None else base_env
    try:
        count = int(base_env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    variables = {}
    for name in remote_names:
        variables["GIT_CONFIG_KEY_%d" % count] = "remote.%s.pushurl" % name
        variables["GIT_CONFIG_VALUE_%d" % count] = DISABLED_PUSH_URL
        count += 1
    variables["GIT_CONFIG_COUNT"] = str(count)
    return variables


class IClaudeClient(Protocol):
    def build_command(self, model, budget_usd, allowed_tools=None, disallowed_tools=None, no_mcp=True,
                      resume_session="", fork_session=False, prompt_argument="", permission_mode="", hook_profile="",
                      mcp_config_path="") -> list:
        """argv of `claude -p ... --output-format stream-json --verbose`."""

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None) -> dict:
        """{"ok", "reply", "cost_usd", "session_id", "error", "raw", "binary"}."""

    def build_push_disabled_env(self, folder) -> dict:
        """GIT_CONFIG_* variables that turn off the push of every remote of the folder (for `extra_env`)."""

    def build_env_without_secrets(self) -> dict:
        """os.environ without variables that look like secrets (keeps ANTHROPIC_*/CLAUDE_*)."""


class ClaudeClient:
    def __init__(self, process_runner, claude_binary="", guard_path="", fallback_binaries=(), call_recorder=None):
        """Empty `claude_binary` = `claude` from PATH; a name such as `cc` (a wrapper signed in with another account) is looked up
        on PATH and on the saved user PATH. A `.py` (the tests' fake claude) runs with `sys.executable`. `fallback_binaries` =
        the commands tried, in order, when the running one hits an account usage limit (engine/claude_binaries.py).
        `guard_path` = the orchestrator skill's guard.py (the app config's); empty = looked up through the skill locator.
        `call_recorder(folder, kind, result)` (optional) is told about every finished `run_prompt` (the call log)."""
        self.call_recorder = call_recorder
        self.process_runner = process_runner
        self.claude_binary = claude_binary
        self.guard_path = guard_path
        self.fallback_binaries = list(fallback_binaries or [])

    def get_binary(self):
        return claude_binaries.executable(self.claude_binary or DEFAULT_BINARY_NAME)

    def is_available(self):
        """True if the configured binary (or `claude`) is found."""
        return bool(claude_binaries.resolve(self.claude_binary or DEFAULT_BINARY_NAME))

    def locate_guard(self):
        """guard.py of the orchestrator skill ('' when it is not installed)."""
        if self.guard_path and os.path.isfile(self.guard_path):
            return self.guard_path
        return skill_locator.locate_guard(skill_locator.locate_orchestrator_skill_dir())

    def build_hook_settings(self, hook_profile):
        """`--settings` JSON that registers the guard as a PreToolUse hook (deterministic: blocks with exit 2 whatever the model
        tries) with `--profile <hook_profile>`; '' when no guard.py is found (the job's snapshot revert remains the net)."""
        guard = self.locate_guard()
        if not guard:
            logger.warning("guard.py not found: the `%s` job runs without the PreToolUse hook (only the snapshot revert confines it)", hook_profile)
            return ""
        command = 'python "%s" --profile %s' % (os.path.abspath(guard).replace("\\", "/"), hook_profile)
        return json.dumps({"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]}})

    def build_command(self, model, budget_usd, allowed_tools=None, disallowed_tools=None, no_mcp=True,
                      resume_session="", fork_session=False, prompt_argument="", permission_mode="", hook_profile="",
                      mcp_config_path=""):
        """argv without a shell. Without `prompt_argument`, the prompt goes through stdin in `run_prompt`.

        Order: base, -p [prompt], --output-format stream-json --verbose, --model, --max-budget-usd, --permission-mode, --allowedTools...,
        --disallowedTools..., --settings <hook json>, --strict-mcp-config --mcp-config {empty}, --resume <session> [--fork-session].
        `hook_profile` (e.g. "planning", "spec", "subprojects", "systemmap"; the name is passed through to guard.py as is) adds the guard.py PreToolUse hook with that profile through an inline `--settings`
        (nothing is written to the user's project). `mcp_config_path` (with `no_mcp`) replaces the empty config by that file, e.g. the
        read-only Grafana server of a project that opted in: still `--strict-mcp-config`, so no other MCP server joins it.
        """
        argv = claude_binaries.base_argv(self.claude_binary or DEFAULT_BINARY_NAME)
        argv.append("-p")
        if prompt_argument:
            argv.append(prompt_argument)
        argv += ["--output-format", "stream-json", "--verbose"]  # the stream carries the rate_limit_event (5-hour and weekly usage)
        if model:
            argv += ["--model", model]
        if budget_usd:
            argv += ["--max-budget-usd", str(budget_usd)]
        if permission_mode:
            argv += ["--permission-mode", permission_mode]
        if allowed_tools:
            argv += ["--allowedTools"] + list(allowed_tools)
        if disallowed_tools:
            argv += ["--disallowedTools"] + list(disallowed_tools)
        hook_settings = self.build_hook_settings(hook_profile) if hook_profile else ""
        if hook_settings:
            argv += ["--settings", hook_settings]
        if no_mcp:
            argv += ["--strict-mcp-config", "--mcp-config", mcp_config_path or EMPTY_MCP_CONFIG]
        if resume_session:
            argv += ["--resume", resume_session]
            if fork_session:
                argv.append("--fork-session")
        return argv

    def build_env_without_secrets(self):
        """Environment of `claude -p`: without the app/panel token and without variables that look like secrets (only
        claude's own stay)."""
        return {name: value for name, value in os.environ.items()
                if CLAUDE_VARIABLE_NAME_PATTERN.match(name)
                or not (SECRET_VARIABLE_NAME_PATTERN.search(name) or name in ALWAYS_REMOVED_VARIABLES)}

    def build_push_disabled_env(self, folder):
        """Variables that make the push impossible for the `claude -p` process (even through a guard gap: python -c,
        subshell...): an invalid `remote.<name>.pushurl` for EVERY remote of the folder, through GIT_CONFIG_COUNT/KEY_n/VALUE_n
        (nothing is written to the repo). Returns only the new variables, for `extra_env`."""
        code, output, _ = self.process_runner.run_and_wait(["git", "-C", folder, "remote"], folder, LIST_REMOTES_TIMEOUT_SECONDS)
        return build_push_disabled_variables((output or "").split() if code == 0 else [])

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None):
        """Runs the argv (no shell, environment without secrets, stdin = `stdin_text`) and parses the `--output-format stream-json` output (its last `result` event).
        When the command hits an account usage limit, or its account cannot be used (authentication or authorization failure), the
        same argv runs again with each fallback binary in turn.

        Always returns a dict (never raises):
          ok          True only with code 0, a JSON object and a false `is_error`
          reply       the JSON `result` ('' on error)
          cost_usd    `total_cost_usd` (or `cost_usd`) when numeric, otherwise None
          session_id  the JSON `session_id` ('' if missing) - used for `--resume` later
          error       '' on success; otherwise short redacted text ("timed out", last stderr line, the error `result`)
          raw         the JSON dict (or {} if the output was not JSON)
          binary      the command name that produced this result
          account     the `cc` account that ran it ('' for any other command)
        """
        names = claude_binaries.ordered(self.claude_binary, self.fallback_binaries)
        result = {}
        for position, name in enumerate(names):
            attempt = argv if position == 0 else claude_binaries.swap_binary(argv, names[0], name)
            result, switch_cause = self.run_once(attempt, folder, stdin_text, timeout_seconds, extra_env)
            result["binary"] = name
            result["account"] = result.pop("stderr_account", "")
            if not switch_cause or position == len(names) - 1:
                break
            logger.warning("`%s` %s: trying `%s`", name, "has an unusable account (%s)" % result["error"] if switch_cause == UNUSABLE_CAUSE
                           else "hit the account usage limit", names[position + 1])
        if self.call_recorder is not None:
            self.call_recorder(folder, getattr(_call_context, "kind", ""), result)
        return result

    def run_once(self, argv, folder, stdin_text, timeout_seconds, extra_env):
        """(result dict of `run_prompt`, why the next command should run: LIMIT_CAUSE for an account usage limit, UNUSABLE_CAUSE for an
        account that cannot be used (authentication or authorization failure), '' when the call worked or failed for another reason)."""
        argv, stdin_text = claude_binaries.prompt_through_stdin(argv, stdin_text or "")
        code, output, error = self.process_runner.run_and_wait(argv, folder, timeout_seconds, input_text=stdin_text,
                                                               env=self.build_env_without_secrets(), extra_env=extra_env)
        raw = self.parse_json_output(output)
        result = {"ok": False, "reply": "", "cost_usd": self.extract_cost(raw), "session_id": str(raw.get("session_id") or ""),
                  "error": "", "raw": raw, "stderr_account": claude_binaries.account_from_stderr(error)}
        if code == TIMEOUT_CODE:
            result["error"] = TIMEOUT_MESSAGE
            return result, ""
        if code != 0 or not raw or raw.get("is_error"):
            lines = [line for line in (error or output or "").strip().splitlines() if line.strip()]
            reason = raw.get("result") if raw.get("is_error") else None
            result["error"] = redact_secrets(reason or (lines[-1] if lines else "claude finished with an error"), ERROR_MESSAGE_LIMIT)
            failure_text = "\n".join((reason or "", cli_output.strip_rate_limit_events(output), error or ""))  # that event's name says "rate_limit"
            if claude_binaries.is_usage_limit(failure_text) or usage_windows.is_limited(raw.get("rate_limit")) or raw.get("api_error_status") == 429:
                return result, LIMIT_CAUSE
            unusable = claude_binaries.is_account_unusable(failure_text) or raw.get("api_error_status") in (401, 403)
            return result, UNUSABLE_CAUSE if unusable else ""
        result["ok"] = True
        result["reply"] = str(raw.get("result") or "")
        return result, ""

    @staticmethod
    def parse_json_output(output):
        """Normalised result event of the output (engine/cli_output.py: one object, an array of events, stream-json, noise
        before or after it); {} when nothing in it is a claude result."""
        data = cli_output.parse(output)
        return data if data.get("ok") else {}

    @staticmethod
    def extract_cost(raw):
        for key in ("total_cost_usd", "cost_usd"):
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return value
        return None
