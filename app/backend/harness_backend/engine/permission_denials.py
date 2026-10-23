#!/usr/bin/env python3
"""Permission denials of a turn: which ones always stop the loop (risky), how a tolerated one is written down, and whether the turn advanced.

Moved out of `run_loop.py` as it was.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import json
import re

import decisions  # noqa: E402  (AI decisions log: the one grammar owner of VALIDATE.md)
import loop_state  # noqa: E402  (typed state: loop folder paths)
import notify  # noqa: E402  (notices: console, toast, file, email, webhook; variables in the app .env)
from loop_io import load_json, read  # noqa: E402


# ---------------------------------------------------------------- permission denial
# A denial of a common command (write evidence, read a file) that the agent redoes another way does NOT stop the loop: it
# becomes a line in VALIDATE.md and the turn carries on. It always stops when the denied command looks risky, or when the
# turn did not advance.
RISKY_DENIAL_RE = re.compile(r"git\s+push|--force|push\s+-f|rm\s+-rf|DROP\s+(TABLE|DATABASE)|TRUNCATE|DELETE\s+FROM", re.I)


SECRET_LIKE_RE = re.compile(r"[A-Za-z0-9+/=_-]{24,}")


def prod_hint_patterns():
    """GUARD_PROD_HINTS (the guard's production words, comma-separated; default prod,production) as whole-word regexes."""
    raw = os.environ.get("GUARD_PROD_HINTS")
    if raw is None:
        raw = notify.envload.get("GUARD_PROD_HINTS", "prod,production")
    return [re.compile(r"(?<![A-Za-z0-9])%s(?![A-Za-z0-9])" % re.escape(item.strip()), re.I) for item in raw.split(",") if item.strip()]


def project_deny_patterns(work):
    """The project's `deny_bash_regex` from PERMISSIONS.json (block rules written with rules.py): a denied command that matches
    one of them is never tolerated."""
    if not work:
        return []
    path = loop_state.loop_path(work, "PERMISSIONS.json")
    if not os.path.exists(path):
        path = os.path.join(work, ".specs", "project", "PERMISSIONS.json")
    patterns = []
    for pattern in load_json(path).get("deny_bash_regex") or []:
        try:
            patterns.append(re.compile(str(pattern), re.I))
        except re.error:
            continue
    return patterns


def is_risky_denial(denial, work=""):
    """A denial that always stops the loop: any MCP tool, or a command that looks like push, force, rm -rf, destructive SQL,
    production (GUARD_PROD_HINTS) or something the project forbids (PERMISSIONS.json deny_bash_regex)."""
    if str(denial.get("tool_name") or "").startswith("mcp__"):
        return True
    text = json.dumps(denial.get("tool_input") or {}, ensure_ascii=False)
    if RISKY_DENIAL_RE.search(text):
        return True
    command = str((denial.get("tool_input") or {}).get("command") or text)
    return any(pattern.search(text) for pattern in prod_hint_patterns()) or any(pattern.search(command) for pattern in project_deny_patterns(work))


def denial_text(denial):
    """A short line without secrets (a long sequence becomes ***) for VALIDATE.md, the notice and the summary."""
    tool_input = denial.get("tool_input") or {}
    text = tool_input.get("command") or tool_input.get("file_path") or json.dumps(tool_input, ensure_ascii=False)
    text = SECRET_LIKE_RE.sub("***", re.sub(r"\s+", " ", str(text)).strip())
    return "%s: %s" % (denial.get("tool_name"), text[:140])


def note_tolerated_denials(work, turn, denials, progress):
    """Notes each tolerated denial in VALIDATE.md; the final summary and the PR show that file as 'For you to validate'."""
    for denial in denials:
        decisions.append(work, "general", turn, "important", "permission denied (%s)" % denial_text(denial),
                         "carried on, the turn advanced (%s)" % progress, "check whether what was denied was needed")


def denial_progress(prev, specs, state, new_commits):
    """Why the turn counts as advanced (empty text = it did not advance): new commit, ROADMAP changed or it stopped asking you."""
    if new_commits:
        return "new commit"
    before = read(os.path.join(prev["dir"], "ROADMAP.md")) if prev else ""
    if before != read(os.path.join(specs, "ROADMAP.md")):
        return "ROADMAP changed"
    if state.get("status") in ("done", "awaiting_user"):
        return "stopped asking you"
    return ""
