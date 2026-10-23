#!/usr/bin/env python3
"""The `claude -p` call of one turn: argv (model, MCP, isolation, disallowed tools), environment with the push lock, run and parse.

Moved out of `run_loop.py` as it was.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprocess

import app_paths  # noqa: E402  (harness_backend package on sys.path; skill and guard locations)
import claude_binaries  # noqa: E402  (which `claude` command runs, prompt via stdin for a .cmd wrapper)
import cli_output  # noqa: E402  (one tolerant parser of the `claude -p` JSON output, shared with the app)
import mcp_access  # noqa: E402  (read-only MCP servers of the turn, computed from ~/.claude.json: no per-project toggle)
import runtime_isolation  # noqa: E402  (`--setting-sources project,local` + skills/agents bundle + inline guard hook; kill switch loop_isolation)
import turn_environment  # noqa: E402  (the turn's environment without the secrets that only came from the app .env)
from claude_accounts import current_claude  # noqa: E402
from loop_io import IS_WIN, git, kill_tree  # noqa: E402
from harness_backend.config import app_config  # noqa: E402

APP_ROOT_DIR = app_paths.APP_ROOT_DIR


DEFAULT_ORCHESTRATOR_MODEL = "opus"


DISABLED_PUSH_URL = "push-disabled-by-loop://use-delivery.py"


# Command line of `claude -p`: what the loop never needs is refused by the CLI itself, not only by the guard hook.
# No allow-list on purpose: under --permission-mode auto it would only pre-approve, and a narrow one would stop a normal turn.
LOOP_DISALLOWED_TOOLS = ["WebFetch", "WebSearch", "NotebookEdit", "Bash(ssh *)", "Bash(scp *)", "Bash(sftp *)"]


EMPTY_MCP_CONFIG = '{"mcpServers":{}}'  # no MCP server in the headless turn (any MCP denial already stops the loop as risky)


def mcp_read_arguments():
    """(mcp config, allowed tools, denied tools) of the turn, computed by `mcp_access.build_access`: every known server the user's
    `~/.claude.json` defines (grafana, mcp-mysql-dev, mcp-mysql-prod) with its read tools only. No server = the empty strict config
    (every other MCP stays out; a missing server is just absent, never a warning)."""
    access = mcp_access.build_access(app_config.get_user_data_dir())
    if not access["config_path"]:
        return EMPTY_MCP_CONFIG, [], []
    return access["config_path"], list(access["allowed_tools"]), list(access["denied_tools"])


def model_arguments(opts):
    """`--model <value>` of every orchestrator turn (normal, correction and resume); an empty option sends no flag."""
    model = str(getattr(opts, "orchestrator_model", DEFAULT_ORCHESTRATOR_MODEL) or "").strip()
    return ["--model", model] if model else []


def isolation_arguments(opts):
    """Isolation flags of every turn (normal, correction, resume): `--setting-sources project,local`, `--add-dir=<bundle>` and the guard hook
    (`--profile auto`) through inline `--settings`. [] when `loop_isolation` is false in config.json: the argv is then exactly the old one.
    The bundle is rebuilt only when its sources changed (signature compare, 30 s memo): cheap per turn."""
    config = runtime_isolation.read_config()
    if not runtime_isolation.isolation_enabled(config):
        return []
    prepared = runtime_isolation.prepare(getattr(opts, "repo", None), config, runtime_isolation.plugin_root())
    guard = app_paths.locate_guard()
    return runtime_isolation.isolation_argv(prepared["bundle"]["path"], runtime_isolation.guard_settings(guard, "auto"))


def isolation_environment(cwd):
    """LOOP_ISOLATED and LOOP_SKILLS_FILE of the turn environment ({} when isolation is off)."""
    config = runtime_isolation.read_config()
    if not runtime_isolation.isolation_enabled(config):
        return {}
    allowlist = runtime_isolation.skills_allowlist(cwd, config)
    return runtime_isolation.isolation_env(runtime_isolation.write_allowlist_file(runtime_isolation.data_dir(), allowlist))


def claude_command(opts, prompt):
    base = claude_binaries.base_argv(current_claude(opts))
    # T23: each non-primary worktree is granted as `--add-dir=<path>` (the `=` form: the flag is variadic and would swallow what follows)
    # D4: each `reference` repo (read only, no worktree) is granted the same way, as its own folder
    extra_dirs = ["--add-dir=%s" % path for path in (getattr(opts, "extra_worktrees", None) or []) + (getattr(opts, "reference_dirs", None) or [])]
    mcp_config, allowed_tools, denied_tools = mcp_read_arguments()
    allowed = ["--allowedTools"] + allowed_tools if allowed_tools else []
    return base + [
        "-p", prompt, "--output-format", "stream-json", "--verbose", "--permission-mode", opts.permission_mode,
        "--permission-prompts", "none", "--max-budget-usd", str(opts.budget),
    ] + model_arguments(opts) + extra_dirs + isolation_arguments(opts) + [
        "--strict-mcp-config", "--mcp-config", mcp_config,
    ] + allowed + [
        "--disallowedTools", ",".join(LOOP_DISALLOWED_TOOLS + denied_tools),  # last and as ONE argument: the option is variadic
    ]


def loop_json_correction_prompt(turn, branch):
    """Prompt of the single correction turn (same session, --resume): only write LOOP.json, never redo or start work."""
    return ("/orchestrator continue --headless --turn %d --branch %s --finish-only. Your previous reply ended turn %d WITHOUT writing LOOP.json "
            "(e.g. you stopped to wait for background subagents). Do not start new work and do not wait for background tasks: if a subagent is "
            "still unfinished, record that in the reason and use status continue. Write STATE.md if needed and, as the last action, run: "
            "python \"<app>/harness.py\" state loop <root> --status <continue|awaiting_user|done|error> --turn %d --reason \"...\" --next \"...\"."
            % (turn, branch, turn, turn))


TURN_ENVIRONMENT_REPORTED = []  # one entry once the run has printed its turn-environment line (first turn only)


def turn_environment_for(cwd):
    """The process environment minus the secret-looking variables that came from the app .env (see turn_environment.py). The first turn of
    a run prints their NAMES (never a value); an internal error passes the full environment and prints one warning."""
    result = turn_environment.build(os.environ, cwd, app_paths.envload.candidates())
    if not TURN_ENVIRONMENT_REPORTED:
        TURN_ENVIRONMENT_REPORTED.append(True)
        if result.get("error"):
            print("[loop] WARNING: turn environment filter failed, the turn gets the full environment (%s)" % result["error"])
        elif result["removed"]:
            print("[loop] turn environment: %d secret-looking variable(s) from the app .env left out (%s)" % (len(result["removed"]), ", ".join(result["removed"])))
    return result["env"]


def push_lock_env(cwd):
    """Environment of `claude -p`: push is impossible even through a guard gap (python -c, subshell...). Disables the pushurl of EVERY remote
    for that process only (GIT_CONFIG_*, nothing is written to the repo); the end delivery is done by this script, outside that environment."""
    env = turn_environment_for(cwd)
    env["LOOP_HEADLESS"] = "1"
    env.update(isolation_environment(cwd))
    env.setdefault("CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS", "0")  # wait for the background subagents (the verifier) instead of killing them 10 min after the last turn; run_turn's timeout still bounds it
    env.setdefault("LOOP_APP_HOME", APP_ROOT_DIR)  # the headless turn finds the app without depending on config.json
    specs_dir = os.path.join(os.path.abspath(cwd), ".specs")  # cwd = the PRIMARY worktree (run_turn is called with `work`)
    if os.path.isdir(specs_dir):
        env["LOOP_SPECS_DIR"] = specs_dir  # the guard finds SCOPE.json even when its cwd is a second worktree
    _, remotes, _ = git(cwd, "remote")
    names = remotes.split()
    try:
        count = int(env.get("GIT_CONFIG_COUNT", "0"))
    except ValueError:
        count = 0
    for name in names:
        env["GIT_CONFIG_KEY_%d" % count] = "remote.%s.pushurl" % name
        env["GIT_CONFIG_VALUE_%d" % count] = DISABLED_PUSH_URL
        count += 1
    env["GIT_CONFIG_COUNT"] = str(count)
    return env


def run_turn(command, cwd, timeout_min):
    command, stdin_text = claude_binaries.prompt_through_stdin(command)  # a .cmd wrapper (`cc`) would cut the prompt at a line break
    flags = {"stdin": subprocess.PIPE if stdin_text else subprocess.DEVNULL, "stdout": subprocess.PIPE, "stderr": subprocess.PIPE, "text": True, "encoding": "utf-8", "errors": "replace", "cwd": cwd,
             "env": push_lock_env(cwd)}
    if IS_WIN:
        flags["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    proc = subprocess.Popen(command, **flags)
    try:
        out, err = proc.communicate(input=stdin_text or None, timeout=timeout_min * 60)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        kill_tree(proc.pid)
        out, err = proc.communicate()
        return proc.returncode if proc.returncode is not None else -1, out or "", err or "", True


def parse_result(text):
    """Normalised result of a turn (cli_output.parse): {"ok": True, "result", "is_error", "total_cost_usd", ...} or
    {"ok": False, "reason": "unrecognized_output", "detail", "sample"} when the CLI printed nothing the loop can read."""
    return cli_output.parse(text)


def unrecognized_output(data):
    return data.get("ok") is False and data.get("reason") == cli_output.UNRECOGNIZED
