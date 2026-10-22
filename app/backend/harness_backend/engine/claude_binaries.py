#!/usr/bin/env python3
"""Which `claude` command a headless call runs, and the fallback when an account hits its usage limit (standard library only).

Used by the engine (`run_loop`, imported as a script sibling) and by the app (`infrastructure/claude_client.ClaudeClient`), so the
loop turns and the panel jobs pick the command the same way.

The order comes from config.json: `claude_binary` first (empty = `claude`), then `claude_fallback_binaries` (a list or one name).
A name like `cc` may be a wrapper script (`cc.cmd`) that signs `claude` in with another account. When the running command
fails with an account usage limit, the caller moves to the next one; an `overloaded` API (529) is not an account limit and
never switches.

A Windows batch wrapper (`.cmd`/`.bat`) goes through cmd.exe, which cuts an argument at its first line break and expands
`%NAME%`: the prompt of such a call always goes through stdin (`claude -p` with no prompt argument reads it from there).

Which account runs: `<command> auth status` (JSON, no secret: method, e-mail, organization, plan) and, for the `cc` account
picker in headless mode, its `[cc] conta: <name>` stderr line.
"""
import datetime
import json
import os
import re
import shutil
import subprocess
import sys

DEFAULT_BINARY_NAME = "claude"
BATCH_EXTENSIONS = (".cmd", ".bat")
PROMPT_FLAG = "-p"
# account-side limits: switching to another account helps. `overloaded` (API capacity) is left out on purpose.
USAGE_LIMIT_PATTERN = re.compile(r"usage limit|limit reached|rate.?limit|too many requests|\b429\b|quota", re.I)
CC_ACCOUNT_PATTERN = re.compile(r"^\[cc\] conta: *(\S+)", re.M)  # printed on stderr by `cc` (claude-shared/cc.mjs) in headless mode
AUTH_STATUS_TIMEOUT_SECONDS = 90
METHOD_LABELS = {"claude.ai": "login", "oauth_token": "token", "api_key": "API key", "apiKey": "API key"}


def user_path_entries():
    """PATH folders saved for the user and the machine in the Windows registry ([] elsewhere or on error): a process started
    before the user added a folder (an editor, the panel) still finds a wrapper such as `cc` there."""
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []
    entries = []
    for hive, key in ((winreg.HKEY_CURRENT_USER, r"Environment"),
                      (winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "Path")
        except OSError:
            continue
        entries += [os.path.expandvars(item) for item in str(value).split(";") if item.strip()]
    return entries


def resolve(name):
    """Full path of `name` (a path, or a command looked up on PATH and then on the registry PATH); '' when not found."""
    if not name:
        return ""
    if os.path.isfile(name):
        return name
    found = shutil.which(name)
    if found:
        return found
    saved = os.pathsep.join(user_path_entries())
    return (shutil.which(name, path=saved) or "") if saved else ""


def ordered(primary, fallbacks=()):
    """Command names in the order they are tried: the primary (empty = `claude`), then each fallback found on this machine,
    without repeats. A fallback that is not installed is skipped (another machine may not have `cc`): it never blocks a call."""
    names = []
    for position, name in enumerate([primary or DEFAULT_BINARY_NAME] + [item for item in (fallbacks or []) if isinstance(item, str)]):
        name = name.strip()
        if name and name not in names and (position == 0 or resolve(name)):
            names.append(name)
    return names


def read_fallbacks(value):
    """`claude_fallback_binaries` of config.json: a list of names or one name; anything else = none."""
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        return [item.strip() for item in value if isinstance(item, str) and item.strip()]
    return []


def executable(name):
    """What goes in argv[0]: the resolved path, else the name as given (the OS then reports it missing)."""
    return resolve(name) or name


def base_argv(name):
    """Start of the argv: a `.py` (the tests' fake claude) runs with this Python."""
    binary = executable(name)
    return [sys.executable, binary] if binary.endswith(".py") else [binary]


def is_batch(argv):
    return bool(argv) and argv[0].lower().endswith(BATCH_EXTENSIONS)


def prompt_through_stdin(argv, stdin_text=""):
    """(argv, stdin_text) safe for the command: for a batch wrapper the prompt argument right after `-p` moves to stdin
    (joined after any stdin text already there). Anything else comes back unchanged."""
    argv = list(argv)
    if not is_batch(argv) or PROMPT_FLAG not in argv:
        return argv, stdin_text
    index = argv.index(PROMPT_FLAG) + 1
    if index >= len(argv) or argv[index].startswith("--"):
        return argv, stdin_text
    prompt = argv.pop(index)
    return argv, (prompt + "\n\n" + stdin_text) if stdin_text else prompt


def swap_binary(argv, old_name, new_name):
    """The same argv with the base of `old_name` replaced by the base of `new_name` (unchanged when it does not start with it)."""
    old = base_argv(old_name)
    if list(argv[:len(old)]) != old:
        return list(argv)
    return base_argv(new_name) + list(argv[len(old):])


def is_usage_limit(text):
    """True when the output names an account limit (switching account helps); False for anything else, `overloaded` included."""
    return bool(USAGE_LIMIT_PATTERN.search(text or ""))


CC_RESET_PATTERN = re.compile(r"^\[cc\] reinicia: *(\S+)", re.M)  # `cc` with no free account: when the first one frees up (ISO, UTC)
EPOCH_RESET_PATTERN = re.compile(r"limit reached\|(\d{9,11})", re.I)  # `claude`: "Claude AI usage limit reached|<unix seconds>"


def reset_time(text):
    """Local naive datetime of the limit reset named in the output (`[cc] reinicia: <ISO>` or `limit reached|<epoch>`); None when absent."""
    text = text or ""
    found = CC_RESET_PATTERN.search(text)
    if found:
        try:
            moment = datetime.datetime.fromisoformat(found.group(1).replace("Z", "+00:00"))
        except ValueError:
            moment = None
        if moment:
            return moment.astimezone().replace(tzinfo=None) if moment.tzinfo else moment
    found = EPOCH_RESET_PATTERN.search(text)
    return datetime.datetime.fromtimestamp(int(found.group(1))) if found else None


def account_from_stderr(text):
    """Account name the `cc` picker chose (its `[cc] conta: <name>` line); '' for any other command."""
    found = CC_ACCOUNT_PATTERN.search(text or "")
    return found.group(1) if found else ""


def auth_status(name):
    """{"method", "email", "org", "plan", "account", "error"} of `<name> auth status` (no secret in it); {} for a test `.py` fake.
    Never raises: a missing command or an unreadable answer comes back as `error`."""
    argv = base_argv(name)
    if argv[0] == sys.executable:
        return {}
    try:
        done = subprocess.run(argv + ["auth", "status"], stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=AUTH_STATUS_TIMEOUT_SECONDS)
    except (OSError, subprocess.SubprocessError) as error:
        return {"error": str(error)[:200]}
    start = done.stdout.find("{")
    try:
        data = json.loads(done.stdout[start:]) if start >= 0 else {}
    except ValueError:
        data = {}
    if not isinstance(data, dict) or not data:
        return {"error": "`%s auth status` gave no JSON (code %s)" % (name, done.returncode)}
    method = str(data.get("authMethod") or "")
    return {"method": METHOD_LABELS.get(method, method or "?"), "email": str(data.get("email") or ""), "org": str(data.get("orgName") or ""),
            "plan": str(data.get("subscriptionType") or ""), "account": account_from_stderr(done.stderr),
            "logged_in": bool(data.get("loggedIn")), "error": ""}


def account_label(auth, account=""):
    """Short text of the account: "login desenv10@x.com (iData, team)", "token, cc account claude"; '' when nothing is known."""
    auth = auth or {}
    account = account or auth.get("account") or ""
    if auth.get("error") and not account:
        return ""
    who = auth.get("email") or ("cc account %s" % account if account else "")
    extra = ", ".join(item for item in (auth.get("org"), auth.get("plan")) if item)
    text = ", ".join(item for item in (auth.get("method"), who) if item)
    return text + (" (%s)" % extra if extra else "")


def describe(primary, fallbacks=()):
    """One row per configured command, missing ones included: {"name", "role", "path", "found", "auth", "label"}.
    Runs `auth status` of every command found (the `cc` picker queries each account's usage: a few seconds)."""
    names = []
    for name in [primary or DEFAULT_BINARY_NAME] + read_fallbacks(list(fallbacks or [])):
        if name not in names:
            names.append(name)
    rows = []
    for position, name in enumerate(names):
        path = resolve(name)
        auth = auth_status(name) if path else {}
        rows.append({"name": name, "role": "main" if position == 0 else "fallback", "path": path, "found": bool(path), "auth": auth,
                     "label": account_label(auth)})
    return rows
