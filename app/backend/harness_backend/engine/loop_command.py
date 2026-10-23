"""Validation and rebuild of the saved relaunch command (`.specs/project/loop/json/LOOP-CMD.json`).

Why: that file sits in a folder a headless Claude turn can write, and the user's next Answer / Resume / Instruction click relaunches
the loop from it. Running its `argv` as-is would let a prompt-injected agent run any program with the user's rights. So the file is
never executed: `rebuild_loop_command` takes ONLY known run_loop.py options (type checked) out of it and builds the command again
as `[this interpreter, the app's run_loop script, the project repo, <known options>]`. Anything else raises
`LoopCommandInvalidError` and nothing starts.

Dropped on purpose (never carried over from the file): `--answer`/`--option`/`--leftovers` (the app adds its own), `--claude` and
`--claude-fallback` (the app injects the binaries of its own config: a saved one would be an arbitrary executable), `--setup` (a shell
command; it only runs when a worktree is created, and a relaunch continues an existing one) and `--summary-dir` (an arbitrary folder).
Stdlib only; `ALLOWED_FLAGS` mirrors `loop_cli.build_parser()` (a test keeps the two in step).
"""
import math
import os
import re
import sys

LOOP_COMMAND_INVALID_CODE = "loop_command_invalid"
MAX_WHOLE_NUMBER = 1000000
MAX_WAVE = 64
MAX_AMOUNT = 1000000.0
INTERPRETER_NAME = re.compile(r"^(python|pythonw|py)(\d+(\.\d+)*)?(\.exe)?$", re.IGNORECASE)
WHOLE_NUMBER = re.compile(r"^\d{1,7}$")
AMOUNT = re.compile(r"^\d{1,7}(\.\d{1,6})?$")
BRANCH_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,79}$")
GIT_USER_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._@+-]{0,79}$")
MODEL_NAME = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,79})?$")  # empty = no --model flag in the turns
PERMISSION_MODES = ("auto", "acceptEdits", "dontAsk")

KIND_SWITCH, KIND_WHOLE, KIND_AMOUNT, KIND_TEXT, KIND_CHOICE = "switch", "whole", "amount", "text", "choice"
ALLOWED_FLAGS = {
    "--e2e": (KIND_SWITCH,), "--push": (KIND_SWITCH,), "--no-push": (KIND_SWITCH,), "--continue-on-blocked": (KIND_SWITCH,),
    "--pause-on-blocked": (KIND_SWITCH,), "--allow-silent": (KIND_SWITCH,), "--no-auto-answer": (KIND_SWITCH,), "--no-monitor": (KIND_SWITCH,),
    "--wave": (KIND_WHOLE, 1, MAX_WAVE), "--timeout": (KIND_WHOLE, 1, MAX_WHOLE_NUMBER), "--max-stall": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER),
    "--limit-wait": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER), "--limit-retries": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER),
    "--gate-tiebreaks": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER), "--max-auto-answers": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER),
    "--auto-answers-per-feature": (KIND_WHOLE, 0, MAX_WHOLE_NUMBER),
    "--budget": (KIND_AMOUNT,), "--max-cost": (KIND_AMOUNT,),
    "--branch": (KIND_TEXT, BRANCH_NAME), "--base-branch": (KIND_TEXT, BRANCH_NAME), "--git-user": (KIND_TEXT, GIT_USER_NAME),
    "--orchestrator-model": (KIND_TEXT, MODEL_NAME),
    "--permission-mode": (KIND_CHOICE, PERMISSION_MODES),
}
DROPPED_FLAGS_WITH_VALUE = ("--answer", "--option", "--leftovers", "--claude", "--claude-fallback", "--setup", "--summary-dir")


class LoopCommandInvalidError(ValueError):
    """The saved command is not something the app may relaunch (the message is shown to the user)."""


def normalize_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def is_same_path(first, second):
    try:
        return normalize_path(first) == normalize_path(second)
    except (OSError, ValueError):
        return False


def check_interpreter(token, interpreter):
    """argv[0] is only a tamper signal (the command is rebuilt with `interpreter`): this interpreter, or a python-looking name."""
    if not isinstance(token, str) or not token:
        raise LoopCommandInvalidError("the saved command does not start with a Python interpreter")
    if is_same_path(token, interpreter) or INTERPRETER_NAME.match(os.path.basename(token.replace("\\", "/"))):
        return
    raise LoopCommandInvalidError("the saved command starts with %r, not a Python interpreter" % token[:80])


def check_script(token, run_script):
    """argv[1] must be the app's run_loop script (by file name: another checkout may have recorded it)."""
    name = os.path.normcase(os.path.basename(str(token).replace("\\", "/")))
    if not name or name != os.path.normcase(os.path.basename(run_script)):
        raise LoopCommandInvalidError("the saved command runs %r, not %s" % (str(token)[:80], os.path.basename(run_script)))


def convert_value(name, spec, raw):
    """The checked text of an option value; raises LoopCommandInvalidError."""
    kind = spec[0]
    ok = False
    if kind == KIND_WHOLE:
        ok = bool(WHOLE_NUMBER.match(raw)) and spec[1] <= int(raw) <= spec[2]
    elif kind == KIND_AMOUNT:
        ok = bool(AMOUNT.match(raw)) and math.isfinite(float(raw)) and float(raw) <= MAX_AMOUNT
    elif kind == KIND_TEXT:
        ok = bool(spec[1].match(raw)) and ".." not in raw
    elif kind == KIND_CHOICE:
        ok = raw in spec[1]
    if not ok:
        raise LoopCommandInvalidError("the saved command has an invalid value for %s: %r" % (name, raw[:80]))
    return raw


def read_option_value(tokens, index, inline_value, name):
    """(value, next index) of an option: `--name=value` or `--name value`."""
    if inline_value is not None:
        return inline_value, index + 1
    if index + 1 >= len(tokens) or not isinstance(tokens[index + 1], str):
        raise LoopCommandInvalidError("the saved command has %s without a value" % name)
    return tokens[index + 1], index + 2


def parse_saved_options(tokens):
    """(positionals, kept argv options, dropped option names). Unknown option or a bad value = LoopCommandInvalidError."""
    positionals, kept, dropped = [], [], []
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-") or token == "-":
            positionals.append(token)
            index += 1
            continue
        name, equals, inline = token.partition("=")
        inline_value = inline if equals else None
        if name in DROPPED_FLAGS_WITH_VALUE:
            _, index = read_option_value(tokens, index, inline_value, name)
            dropped.append(name)
            continue
        spec = ALLOWED_FLAGS.get(name)
        if spec is None:
            raise LoopCommandInvalidError("the saved command has an option the app does not relaunch with: %s" % token[:80])
        if spec[0] == KIND_SWITCH:
            if equals:
                raise LoopCommandInvalidError("the saved command gives a value to %s" % name)
            kept.append(name)
            index += 1
            continue
        raw, index = read_option_value(tokens, index, inline_value, name)
        kept += [name, convert_value(name, spec, raw)]
    return positionals, kept, dropped


def resolve_working_folder(saved_cwd, project_repo, allowed_folders):
    """The folder the process starts in: the project repo when the file has none, else the SAME folder as the repo or one of its loop
    worktrees (a subfolder is not accepted: the process looks programs up from its folder)."""
    if saved_cwd is None or saved_cwd == "":
        return project_repo
    if isinstance(saved_cwd, str) and any(is_same_path(saved_cwd, folder) for folder in allowed_folders):
        return os.path.abspath(saved_cwd)
    raise LoopCommandInvalidError("the saved command starts in %r, which is not this project's repository or loop worktree" % str(saved_cwd)[:120])


def rebuild_loop_command(saved, project_repo, allowed_folders, run_script, interpreter=None):
    """The command to relaunch, built from the saved `{"argv", "cwd"}` and the app's own values.

    Returns `{"argv": [interpreter, run_script, project_repo, *known options], "cwd": folder, "dropped": [option names]}`.
    Raises LoopCommandInvalidError for a foreign interpreter or script, an unknown option, a bad value, another repository
    or a folder outside the project."""
    interpreter = interpreter or sys.executable
    argv = saved.get("argv") if isinstance(saved, dict) else None
    if not isinstance(argv, list) or len(argv) < 2 or not all(isinstance(item, str) for item in argv):
        raise LoopCommandInvalidError("the saved command is not a list of text arguments")
    check_interpreter(argv[0], interpreter)
    check_script(argv[1], run_script)
    positionals, kept, dropped = parse_saved_options(argv[2:])
    if len(positionals) != 1 or not is_same_path(positionals[0], project_repo):
        raise LoopCommandInvalidError("the saved command is for another repository than this project's")
    folder = resolve_working_folder(saved.get("cwd"), project_repo, allowed_folders)
    return {"argv": [interpreter, run_script, project_repo] + kept, "cwd": folder, "dropped": dropped}
