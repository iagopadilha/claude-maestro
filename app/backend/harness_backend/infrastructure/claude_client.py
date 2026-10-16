"""Client of `claude -p` (headless Claude Code): builds the argv, runs it with stdin, reads the JSON output, environment
without secrets.

Who uses it: the read-only chat, the action router, the roadmap/mapping jobs. The loop itself stays in the engine
(`run_loop.py`).
"""
import os
import re
import shutil
import sys
from typing import Protocol

from ..engine import cli_output  # engine/ has no __init__.py: it imports as a namespace package (stdlib only module)
from .process_runner import TIMEOUT_CODE, TIMEOUT_MESSAGE

DEFAULT_BINARY_NAME = "claude"
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'
SECRET_VARIABLE_NAME_PATTERN = re.compile(r"(?i)(token|secret|passw|credential|private_key|api_?key|auth)")
CLAUDE_VARIABLE_NAME_PATTERN = re.compile(r"^(ANTHROPIC_|CLAUDE_)")  # claude itself needs to authenticate
ALWAYS_REMOVED_VARIABLES = ("LOOP_MONITOR_URL",)
SECRET_TEXT_PATTERN = cli_output.SECRET_TEXT_PATTERN
SUSPICIOUS_LONG_TEXT_PATTERN = cli_output.SUSPICIOUS_LONG_TEXT_PATTERN
ERROR_MESSAGE_LIMIT = 300
DISABLED_PUSH_URL = "push-disabled-by-loop://use-delivery.py"
LIST_REMOTES_TIMEOUT_SECONDS = 30
REDACTION_MARK = cli_output.REDACTION_MARK


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
                      resume_session="", fork_session=False, prompt_argument="", permission_mode="") -> list:
        """argv of `claude -p ... --output-format json`."""

    def run_prompt(self, argv, folder, stdin_text, timeout_seconds, extra_env=None) -> dict:
        """{"ok", "reply", "cost_usd", "session_id", "error", "raw"}."""

    def build_push_disabled_env(self, folder) -> dict:
        """GIT_CONFIG_* variables that turn off the push of every remote of the folder (for `extra_env`)."""

    def build_env_without_secrets(self) -> dict:
        """os.environ without variables that look like secrets (keeps ANTHROPIC_*/CLAUDE_*)."""


class ClaudeClient:
    def __init__(self, process_runner, claude_binary=""):
        """Empty `claude_binary` = `claude` from PATH. A `.py` (the tests' fake claude) runs with `sys.executable`."""
        self.process_runner = process_runner
        self.claude_binary = claude_binary

    def get_binary(self):
        return self.claude_binary or shutil.which(DEFAULT_BINARY_NAME) or DEFAULT_BINARY_NAME

    def is_available(self):
        """True if the configured binary exists or `claude` is on the PATH."""
        if self.claude_binary:
            return os.path.isfile(self.claude_binary) or bool(shutil.which(self.claude_binary))
        return bool(shutil.which(DEFAULT_BINARY_NAME))

    def build_command(self, model, budget_usd, allowed_tools=None, disallowed_tools=None, no_mcp=True,
                      resume_session="", fork_session=False, prompt_argument="", permission_mode=""):
        """argv without a shell. Without `prompt_argument`, the prompt goes through stdin in `run_prompt`.

        Order: base, -p [prompt], --output-format json, --model, --max-budget-usd, --permission-mode, --allowedTools...,
        --disallowedTools..., --strict-mcp-config --mcp-config {empty}, --resume <session> [--fork-session].
        """
        binary = self.get_binary()
        argv = [sys.executable, binary] if binary.endswith(".py") else [binary]
        argv.append("-p")
        if prompt_argument:
            argv.append(prompt_argument)
        argv += ["--output-format", "json"]
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
        if no_mcp:
            argv += ["--strict-mcp-config", "--mcp-config", EMPTY_MCP_CONFIG]
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
        """Runs the argv (no shell, environment without secrets, stdin = `stdin_text`) and parses the `--output-format json` JSON.

        Always returns a dict (never raises):
          ok          True only with code 0, a JSON object and a false `is_error`
          reply       the JSON `result` ('' on error)
          cost_usd    `total_cost_usd` (or `cost_usd`) when numeric, otherwise None
          session_id  the JSON `session_id` ('' if missing) - used for `--resume` later
          error       '' on success; otherwise short redacted text ("timed out", last stderr line, the error `result`)
          raw         the JSON dict (or {} if the output was not JSON)
        """
        code, output, error = self.process_runner.run_and_wait(argv, folder, timeout_seconds, input_text=stdin_text or "",
                                                               env=self.build_env_without_secrets(), extra_env=extra_env)
        raw = self.parse_json_output(output)
        result = {"ok": False, "reply": "", "cost_usd": self.extract_cost(raw), "session_id": str(raw.get("session_id") or ""),
                  "error": "", "raw": raw}
        if code == TIMEOUT_CODE:
            result["error"] = TIMEOUT_MESSAGE
            return result
        if code != 0 or not raw or raw.get("is_error"):
            lines = [line for line in (error or output or "").strip().splitlines() if line.strip()]
            reason = raw.get("result") if raw.get("is_error") else None
            result["error"] = redact_secrets(reason or (lines[-1] if lines else "claude finished with an error"), ERROR_MESSAGE_LIMIT)
            return result
        result["ok"] = True
        result["reply"] = str(raw.get("result") or "")
        return result

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
