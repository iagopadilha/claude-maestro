#!/usr/bin/env python3
"""Which `claude` command and account a turn runs on: fallback order, unusable accounts, account labels and usage windows kept in meta.json.

Moved out of `run_loop.py` as it was. The two functions that send a notice (`notify_claude_change`, `alert_main_weekly_limit`) stay in
`run_loop.py` because they call its `notify_wait`, which tests replace.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import datetime
import re

import cli_output  # noqa: E402  (one tolerant parser of the `claude -p` JSON output, shared with the app)
import claude_binaries  # noqa: E402  (which `claude` command runs, the fallback on an account limit)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in the app .env)
import usage_windows  # noqa: E402  (5-hour / weekly usage windows read from the rate_limit_event of the turn output)


def claude_names(opts):
    """Commands of the turns, in fallback order: --claude (default `claude`), then each --claude-fallback."""
    return claude_binaries.ordered(opts.claude, getattr(opts, "claude_fallback", None) or [])


def current_claude(opts):
    names = claude_names(opts)
    return names[min(getattr(opts, "claude_index", 0), len(names) - 1)]


def unusable_names(opts):
    """Commands whose account turned out unusable in this run (they are never picked again in it)."""
    return [item["command"] for item in getattr(opts, "unusable_commands", [])]


def first_usable_index(opts):
    """Position of the first command that is not unusable (0 = the main command); 0 when every one is."""
    blocked = unusable_names(opts)
    return next((index for index, name in enumerate(claude_names(opts)) if name not in blocked), 0)


def switch_claude(opts):
    """Moves to the next fallback command that is not unusable (sticky until the next wait); False when there is none left."""
    names, blocked = claude_names(opts), unusable_names(opts)
    for index in range(getattr(opts, "claude_index", 0) + 1, len(names)):
        if names[index] not in blocked:
            opts.claude_index = index
            return True
    return False


def mark_account_unusable(opts, name, account, reason, turn):
    """Remembers that the command's account cannot be used in this run and returns the record kept in meta.json
    ({command, account, reason, turn}; the reason is redacted and short). The `cc` account picker cannot yet leave one account out,
    so an unusable `cc` account makes the whole `cc` command unusable for the run (excluding just that account is planned)."""
    record = {"command": name, "account": account, "reason": cli_output.redact(reason, claude_binaries.UNUSABLE_REASON_LIMIT), "turn": turn}
    opts.__dict__.setdefault("unusable_commands", []).append(record)
    opts.unusable_notice = record
    return record


def unusable_stop_message(opts):
    """Reason of the final error when every command has an unusable account: each command, its account and why, and what to do."""
    rows = ["`%s` (%s): %s" % (item["command"], item["account"] or "account unknown", item["reason"] or "no reason in the output")
            for item in getattr(opts, "unusable_commands", [])]
    return ("no account can be used: %s. Waiting does not fix a login. Sign in again (`claude` then /login, or `claude setup-token`), "
            "ask the admin to enable Claude access for the organization, or switch to another account (`--claude-fallback`), then run again."
            % "; ".join(rows))


def claude_account(opts, name, stderr):
    """Account text of a turn: the `cc` picker names it on stderr; otherwise `<name> auth status`, asked once per command."""
    cache = opts.__dict__.setdefault("claude_auth", {})
    if name not in cache:
        cache[name] = claude_binaries.auth_status(name)
    return claude_binaries.account_label(cache[name], claude_binaries.account_from_stderr(stderr))


LOCAL_NOTICE_CHANNELS = ("console", "toast", "file")  # channels that never leave this machine


def local_notice_channels():
    """The configured notice channels that stay on this machine (console, toast, file), in the configured order."""
    configured = notify.envload.get_list("LOOP_NOTIFY_CHANNELS") or list(LOCAL_NOTICE_CHANNELS)
    return [name for name in configured if name in LOCAL_NOTICE_CHANNELS]


# how each sign-in method of `claude auth status` is billed, in plain words (a sign-in token is still the subscription)
SIGN_IN_WORDS = {"login": "subscription, browser login", "token": "subscription, sign-in token", "API key": "API key, billed per token"}


def command_base_name(name):
    """`C:/bin/claude.exe` -> `claude`, `cc.cmd` -> `cc`."""
    return os.path.splitext(os.path.basename(name or ""))[0]


def split_account_label(account):
    """(method, who, organization and plan) of a claude_binaries.account_label text such as
    "login, dev@x.com (iData, team)" or "token, cc account claude2"; missing parts come back as ''."""
    text = (account or "").strip()
    extra = ""
    found = re.search(r"\s*\(([^)]*)\)\s*$", text)
    if found:
        extra, text = found.group(1), text[:found.start()]
    method, _, who = text.partition(", ")
    if method not in SIGN_IN_WORDS:
        method, who = "", text
    return method, who.strip(), extra


AUTH_METHOD_CODES = {"login": "login", "token": "token", "API key": "api_key"}  # label word -> the code kept in the turn log


CC_ACCOUNT_IN_LABEL = re.compile(r"cc account +(\S+)")


def claude_auth_fields(account):
    """Structured sign-in of a turn, taken from its account label (never a secret): {"auth_method": "login"|"token"|"api_key"|"",
    "auth_email", "auth_plan", "cc_account"}. "login, dev@x.com (iData, team)" -> login, dev@x.com, team, ''."""
    method, who, extra = split_account_label(account)
    parts = [part.strip() for part in extra.split(",")] + ["", ""]
    cc_found = CC_ACCOUNT_IN_LABEL.search(who)
    return {"auth_method": AUTH_METHOD_CODES.get(method, ""), "auth_email": who if "@" in who else "",
            "auth_plan": parts[1], "cc_account": cc_found.group(1) if cc_found else ""}


def describe_claude_used(name, account, is_main):
    """Body text of one command and its account: "the main command `claude` (dev@x.com, subscription, browser login; iData, team)"."""
    method, who, extra = split_account_label(account)
    details = ", ".join(part for part in (who, SIGN_IN_WORDS.get(method, "")) if part)
    details += ("; %s" % extra) if extra else ""
    role = "the main command" if is_main else "the backup command"
    return "%s `%s`%s" % (role, command_base_name(name), " (%s)" % details if details else " (account unknown)")


def claude_change_subject(name, account, is_main, same_command=False, unusable=""):
    """Subject that says, in words, which account the loop is on now and how it signs in:
    "now on the backup account: cc account claude2 (subscription, sign-in token)",
    "back on the main account: dev@x.com (subscription, browser login)",
    "cc switched to cc account claude3 (subscription, sign-in token)". The organization and plan stay in the body.
    `unusable` ("main account unusable") goes in front when the change came from an account that cannot be used."""
    subject = claude_change_subject_core(name, account, is_main, same_command)
    return "%s, %s" % (unusable, subject) if unusable else subject


def claude_change_subject_core(name, account, is_main, same_command):
    """The part of the subject that names the account the loop is on now."""
    method, who, _ = split_account_label(account)
    who = who or "`%s`" % command_base_name(name)
    how = " (%s)" % SIGN_IN_WORDS[method] if method in SIGN_IN_WORDS else ""
    if same_command:
        return "%s switched to %s%s" % (command_base_name(name), who, how)
    return "%s: %s%s" % ("back on the main account" if is_main else "now on the backup account", who, how)


def return_to_main_command(opts):
    """When the loop is on a fallback and the main command's remembered reset moment has passed, goes back to the main command
    (index 0). Returns True when it went back; the next notify_claude_change announces it."""
    moment = getattr(opts, "main_reset", None)
    if getattr(opts, "claude_index", 0) > 0 and moment and datetime.datetime.now() >= moment and claude_names(opts)[0] not in unusable_names(opts):
        opts.claude_index, opts.main_reset, opts.picker_tried = 0, None, []
        print("[loop] the main command `%s` should be free again (reset %s): back on it" % (claude_names(opts)[0], moment.strftime("%d/%m %H:%M")))
        return True
    return False


def remember_reset(opts, moment, phrase=""):
    """Keeps a known reset time of a limited command (for the wait when every command is limited), with the words that name the
    window that ran out ("5-hour window, back at 17:05"; '' when unknown) for the notice of that wait."""
    if moment:
        opts.__dict__.setdefault("limit_resets", []).append(moment)
        if phrase:
            opts.__dict__.setdefault("limit_reset_phrases", {})[moment] = phrase


def limit_suffix(phrase):
    """" (5-hour window, back at 17:05)" for a notice; '' when the window is unknown (the notice keeps its older text)."""
    return " (%s)" % phrase if phrase else ""


def usage_key(name, cc_account=""):
    """Key of a command in meta.json `claude_usage`: its base name, plus `:<account>` when the `cc` picker named one ("cc:claudep")."""
    base = command_base_name(name)
    return "%s:%s" % (base, cc_account) if cc_account else base


def record_usage(meta, key, windows, status):
    """Keeps the last usage windows read from a command's output in meta.json: {"<key>": {"windows", "status", "read_at"}}.
    A turn whose output had no rate limit event leaves the earlier reading as it was."""
    if windows or status:
        meta.setdefault("claude_usage", {})[key] = {"windows": windows, "status": status,
                                                    "read_at": datetime.datetime.now().isoformat(timespec="seconds")}


def next_fallback_name(opts):
    """The command a limited main command would move to (the next one that is not unusable); '' when there is none."""
    names, blocked = claude_names(opts), unusable_names(opts)
    return next((name for name in names[getattr(opts, "claude_index", 0) + 1:] if name not in blocked), "")


def other_usage_lines(opts, meta, main_name):
    """One line per command or account other than the main one with its last known usage, for the weekly alert."""
    main_base = command_base_name(main_name)
    usage = meta.get("claude_usage") or {}
    lines = []
    for key, entry in usage.items():
        if key.split(":")[0] != main_base:
            read_at = str((entry or {}).get("read_at") or "")[11:16]
            lines.append("- `%s`: %s%s" % (key, usage_windows.describe_windows((entry or {}).get("windows")), ", read at %s" % read_at if read_at else ""))
    known = {key.split(":")[0] for key in usage}
    lines += ["- `%s`: usage unknown (not run yet)" % command_base_name(name) for name in claude_names(opts)
              if command_base_name(name) != main_base and command_base_name(name) not in known]
    return lines
