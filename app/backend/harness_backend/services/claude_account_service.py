"""Which Claude account the loop is using, for the panel: command, auth method, e-mail, organization, plan, `cc` account name.

`claude <command> auth status` takes seconds (the `cc` picker asks every account for its usage), so the rows are cached and the
panel poll never waits for them: a stale cache is refreshed by one background thread while the poll gets the last rows (or
none yet). The diagnostics screen (`GET /api/app/info`) may wait, since the user asked for that screen.
"""
import os
import re
import threading
import time

from ..engine import claude_binaries

CACHE_TTL_SECONDS = 300
DEFAULT_COMMAND = claude_binaries.DEFAULT_BINARY_NAME
COMMAND_EXTENSION_PATTERN = re.compile(r"\.(cmd|bat|exe|py)$", re.I)
CC_ACCOUNT_LABEL_PATTERN = re.compile(r"cc account +(\S+)")
EMAIL_PATTERN = re.compile(r"\S+@\S+")
ORG_PLAN_PATTERN = re.compile(r"\(([^)]*)\)\s*$")
KNOWN_METHODS = ("login", "token", "API key")
MAIN_ROLE = "main"

_shared_services = {}
_shared_lock = threading.Lock()


def command_name(path_or_name):
    """`claude`, `cc`: a command path/name without its folder and extension ('' when empty)."""
    return COMMAND_EXTENSION_PATTERN.sub("", os.path.basename(str(path_or_name or "").replace("\\", "/")))


def parse_account_label(label):
    """Fields of a label written by the loop ("login desenv10@x.com (iData, team)", "token, cc account claude2") ->
    {"method", "email", "org", "plan", "cc_account"}; unknown parts are ''."""
    text = str(label or "").strip()
    fields = {"method": "", "email": "", "org": "", "plan": "", "cc_account": ""}
    if not text:
        return fields
    extra = ORG_PLAN_PATTERN.search(text)
    if extra:
        parts = [part.strip() for part in extra.group(1).split(",")] + [""]
        fields["org"], fields["plan"] = parts[0], parts[1]
        text = text[:extra.start()].strip()
    for method in KNOWN_METHODS:
        if text.startswith(method):
            fields["method"] = method
            break
    email = EMAIL_PATTERN.search(text)
    fields["email"] = email.group(0).strip(",") if email else ""
    account = CC_ACCOUNT_LABEL_PATTERN.search(text)
    fields["cc_account"] = account.group(1) if account else ""
    return fields


class ClaudeAccountService:
    def __init__(self, describe_commands, ttl_seconds=CACHE_TTL_SECONDS, clock=time.time):
        """`describe_commands()` -> the rows of `claude_binaries.describe`."""
        self.describe_commands = describe_commands
        self.ttl_seconds = ttl_seconds
        self.clock = clock
        self.rows = None
        self.read_at = 0.0
        self.lock = threading.Lock()
        self.refreshing = False

    def is_fresh(self):
        return self.rows is not None and self.clock() - self.read_at < self.ttl_seconds

    def refresh(self):
        """Runs the (slow) describe now and stores it; a failure keeps the previous rows."""
        try:
            rows = self.describe_commands()
        except Exception:  # noqa: BLE001 - the panel must never fail because `auth status` did
            rows = None
        with self.lock:
            if rows is not None:
                self.rows, self.read_at = rows, self.clock()
            self.refreshing = False
        return rows

    def get_rows(self, wait=False):
        """Cached rows ([] when none yet). `wait` = compute now when stale; otherwise a stale cache starts ONE background refresh."""
        if not self.is_fresh():
            if wait:
                self.refresh()
            else:
                with self.lock:
                    start = not self.refreshing
                    self.refreshing = True
                if start:
                    threading.Thread(target=self.refresh, name="claude-account-refresh", daemon=True).start()
        return list(self.rows or [])

    def find_row(self, rows, command):
        """Row of `command` (compared by name without extension); the main row (first) when `command` is empty."""
        if not command:
            return rows[0] if rows else None
        return next((row for row in rows if command_name(row.get("name")) == command), None)

    def build_current_account(self, meta):
        """`{command, role, method, email, org, plan, cc_account, switches, label}` of the account in use. The per-turn label
        in meta (what the loop really ran) wins; the diagnostics row fills what it lacks and gives the command before turn 1."""
        meta = meta or {}
        rows = self.get_rows()
        command = command_name(meta.get("claude"))
        row = self.find_row(rows, command)
        auth = (row or {}).get("auth") or {}
        label = str(meta.get("claude_account") or "")
        parsed = parse_account_label(label)
        same_account = not parsed["email"] or parsed["email"] == auth.get("email")  # the row only completes the same account
        return {"command": command or command_name((row or {}).get("name")),
                "role": (row or {}).get("role") or (MAIN_ROLE if command == DEFAULT_COMMAND else ""),
                "method": parsed["method"] or auth.get("method") or "",
                "email": parsed["email"] or auth.get("email") or "",
                "org": parsed["org"] or (auth.get("org") if same_account else "") or "",
                "plan": parsed["plan"] or (auth.get("plan") if same_account else "") or "",
                "cc_account": parsed["cc_account"],
                "switches": meta.get("claude_switches") or 0,
                "label": label or (row or {}).get("label") or ""}


def build_shared_claude_account_service(config):
    """The ONE service per configured command list, shared by the loop panel and the diagnostics so `auth status` runs once."""
    primary = getattr(config, "claude_binary", "") or ""
    fallbacks = list(getattr(config, "claude_fallback_binaries", None) or [])
    key = (primary, tuple(fallbacks))
    with _shared_lock:
        if key not in _shared_services:
            _shared_services[key] = ClaudeAccountService(lambda: claude_binaries.describe(primary, fallbacks))
        return _shared_services[key]
