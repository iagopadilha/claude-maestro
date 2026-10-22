"""Which Claude account the loop is using, for the panel: command, auth method, e-mail, organization, plan, `cc` account name.

`claude <command> auth status` takes seconds (the `cc` picker asks every account for its usage), so the rows are cached and the
panel poll never waits for them: a stale cache is refreshed by one background thread while the poll gets the last rows (or
none yet). The diagnostics screen (`GET /api/app/info`) may wait, since the user asked for that screen.
"""
import datetime
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
METHOD_CODES = {"login": "login", "token": "token", "API key": "api_key"}  # label word (what `auth status` gives) -> code kept in files
METHOD_WORDS = {"login": "subscription · browser login", "token": "subscription · sign-in token", "api_key": "API key · billed per token",
                "": "sign-in unknown"}
UNKNOWN_COMMAND = "unknown"
EXHAUSTED_PCT = 100.0
WINDOW_LABELS = {"five_hour": "5-hour", "seven_day": "weekly"}  # window key (what Claude Code names it) -> wording shown
WINDOW_ORDER = ("five_hour", "seven_day")

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


def method_code(method):
    """Code of a sign-in method given as a label word ("login", "token", "API key") or already as a code ("api_key"); '' when unknown."""
    text = str(method or "").strip()
    return METHOD_CODES.get(text) or (text if text in METHOD_WORDS and text else "")


def build_turn_auth(record):
    """Structured sign-in of one turn record (or the meta): {"auth_method", "auth_email", "auth_plan", "cc_account"}, never a secret.
    The loop writes these fields next to `claude_account`; a turn written before that is read from the `claude_account` label."""
    record = record or {}
    if "auth_method" in record or "cc_account" in record:
        return {"auth_method": method_code(record.get("auth_method")), "auth_email": str(record.get("auth_email") or ""),
                "auth_plan": str(record.get("auth_plan") or ""), "cc_account": str(record.get("cc_account") or "")}
    parsed = parse_account_label(record.get("claude_account"))
    return {"auth_method": method_code(parsed["method"]), "auth_email": parsed["email"], "auth_plan": parsed["plan"],
            "cc_account": parsed["cc_account"]}


def build_turn_claude(record):
    """The command, sign-in and usage fields a turn row carries: {"claude", "claude_account", "auth_method", "auth_email", "auth_plan",
    "cc_account", "usage_windows", "limit_status"}. The usage fields are empty for a turn written before the loop read them."""
    record = record or {}
    return dict({"claude": str(record.get("claude") or ""), "claude_account": str(record.get("claude_account") or "")}, **build_turn_auth(record),
                usage_windows=clean_windows(record.get("usage_windows")), limit_status=str(record.get("limit_status") or ""))


def to_float(value):
    """A number as float; None for anything else (booleans included)."""
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def clean_windows(windows):
    """Windows as the loop wrote them ({key: {"pct", "reset_at", "reset_local"}}) -> the same shape with only valid entries; {} when none."""
    cleaned = {}
    for key, window in (windows.items() if isinstance(windows, dict) else []):
        pct = to_float((window or {}).get("pct")) if isinstance(window, dict) else None
        if pct is None:
            continue
        reset_at = to_float(window.get("reset_at"))
        cleaned[str(key)] = {"pct": pct, "reset_at": int(reset_at) if reset_at else None, "reset_local": str(window.get("reset_local") or "")}
    return cleaned


def build_window_rows(windows):
    """[{key, label, pct, reset_at, reset_local, exhausted}]: 5-hour first, weekly second, any other window after them."""
    cleaned = clean_windows(windows)
    keys = [key for key in WINDOW_ORDER if key in cleaned] + sorted(key for key in cleaned if key not in WINDOW_ORDER)
    return [dict(cleaned[key], key=key, label=WINDOW_LABELS.get(key) or key.replace("_", " "), exhausted=cleaned[key]["pct"] >= EXHAUSTED_PCT)
            for key in keys]


def parse_read_at(text):
    """Epoch seconds of an ISO time written by the loop (a time without zone is local time); None when unreadable."""
    try:
        return datetime.datetime.fromisoformat(str(text)).timestamp()
    except (TypeError, ValueError):
        return None


def split_usage_key(key):
    """"cc:claudep" -> ("cc", "claudep"); "claude" -> ("claude", "")."""
    command, _, account = str(key).partition(":")
    return command, account


def format_window_line(window):
    """"5-hour 60 % (reset 14:30)" / "weekly 33 % (reset Mon 27/10 09:00)": the local reset text is shown as the loop wrote it."""
    reset_at = window.get("reset_at")
    reset = ""
    if reset_at:
        moment = datetime.datetime.fromtimestamp(reset_at)
        same_day = moment.date() == datetime.date.today()
        reset = " (reset %s)" % moment.strftime("%H:%M" if same_day or window["key"] == "five_hour" else "%a %d/%m %H:%M")
    return "%s %g %%%s" % (window["label"], round(window["pct"], 1), reset)


def format_usage_line(row):
    """One doctor line for a usage-view row: the windows joined by " · ", with "exhausted" on the ones at 100 %; "usage unknown" without data."""
    if not row["windows"]:
        return "usage unknown"
    return " · ".join(format_window_line(window) + (" EXHAUSTED" if window["exhausted"] else "") for window in row["windows"])


def add_usage_row(rows, key, field, cost_usd):
    """Counts one `field` ("turns" or "calls") and its cost in the row of `key`."""
    row = rows.setdefault(key, {"turns": 0, "calls": 0, "cost_usd": 0.0})
    row[field] += 1
    row["cost_usd"] += cost_usd or 0.0


def build_usage_totals(turn_rows, call_rows=()):
    """Turns (loop) and panel calls summed per sign-in method and per command:
    {"by_method": [{"method", "turns", "calls", "cost_usd"}], "by_command": [{"command", "turns", "calls", "cost_usd"}], "turns", "calls", "cost_usd"}.
    A row carries `claude` (command), `auth_method` and `cost_usd`; rows without a command count as "unknown"."""
    by_method, by_command = {}, {}
    for field, rows in (("turns", turn_rows), ("calls", call_rows)):
        for row in rows:
            cost = row.get("cost_usd") if isinstance(row.get("cost_usd"), (int, float)) else 0.0
            add_usage_row(by_method, method_code(row.get("auth_method")), field, cost)
            add_usage_row(by_command, command_name(row.get("claude")) or UNKNOWN_COMMAND, field, cost)

    def finish(rows, key_name):
        return [dict({key_name: key}, turns=row["turns"], calls=row["calls"], cost_usd=round(row["cost_usd"], 4))
                for key, row in sorted(rows.items(), key=lambda item: (-item[1]["cost_usd"], item[0]))]
    methods = finish(by_method, "method")
    return {"by_method": methods, "by_command": finish(by_command, "command"), "turns": sum(row["turns"] for row in methods),
            "calls": sum(row["calls"] for row in methods), "cost_usd": round(sum(row["cost_usd"] for row in methods), 4)}


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

    def build_usage_view(self, meta, turn_rows=(), call_rows=(), wait=False):
        """Usage per command/account (see the module function): the configured commands come from the account cache, which never blocks
        unless `wait` (the diagnostics screen)."""
        return build_usage_view(meta, self.get_rows(wait=wait), turn_rows, call_rows, self.clock())

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
                "auth_method": method_code(parsed["method"] or auth.get("method")),
                "switches": meta.get("claude_switches") or 0,
                "label": label or (row or {}).get("label") or ""}


def newest_usage_entry(rows, key):
    """The newest turn/call row (the list is oldest first for turns) of `key` that has usage windows, as {windows, status, read_at}; None if none."""
    for row in reversed(list(rows or [])):
        command, account = command_name(row.get("claude")), str(row.get("cc_account") or "")
        if (command + (":" + account if account else "")) == key and clean_windows(row.get("usage_windows")):
            return {"windows": row["usage_windows"], "status": row.get("limit_status"), "read_at": row.get("at") or row.get("read_at") or ""}
    return None


def build_usage_view(meta, configured_rows=(), turn_rows=(), call_rows=(), now=None):
    """The usage of every command/account, from what the loop already wrote (never queries anything, never waits):
    [{key, command, cc_account, windows: [{key, label, pct, reset_at, reset_local, exhausted}], status, read_at, age_seconds}].
    Source order per key: `meta.claude_usage`, else the newest turn, else the newest panel call (newest first). A configured
    command never seen gets empty windows ("usage unknown"). Configured commands come first, in their order."""
    now = time.time() if now is None else now
    stored = (meta or {}).get("claude_usage")
    stored = stored if isinstance(stored, dict) else {}
    keys = [command_name(row.get("name")) for row in configured_rows or [] if command_name(row.get("name"))]
    keys += sorted(key for key in stored if key not in keys)
    for rows in (turn_rows, call_rows):
        for row in reversed(list(rows or [])):
            key = command_name(row.get("claude")) + (":" + str(row.get("cc_account")) if row.get("cc_account") else "")
            if key and key not in keys and clean_windows(row.get("usage_windows")):
                keys.append(key)
    keys = [key for key in keys if key in stored or not any(other.startswith(key + ":") for other in keys)]  # `cc` is shown per picked account
    view = []
    for key in keys:
        entry = stored.get(key) if isinstance(stored.get(key), dict) else None
        if not entry or not clean_windows(entry.get("windows")):
            entry = newest_usage_entry(turn_rows, key) or newest_usage_entry(call_rows[::-1] if call_rows else call_rows, key) or {}
        command, account = split_usage_key(key)
        read_epoch = parse_read_at(entry.get("read_at"))
        view.append({"key": key, "command": command, "cc_account": account, "windows": build_window_rows(entry.get("windows")),
                     "status": str(entry.get("status") or ""), "read_at": str(entry.get("read_at") or ""),
                     "age_seconds": max(0, int(now - read_epoch)) if read_epoch is not None else None})
    return view


def build_shared_claude_account_service(config):
    """The ONE service per configured command list, shared by the loop panel and the diagnostics so `auth status` runs once."""
    primary = getattr(config, "claude_binary", "") or ""
    fallbacks = list(getattr(config, "claude_fallback_binaries", None) or [])
    key = (primary, tuple(fallbacks))
    with _shared_lock:
        if key not in _shared_services:
            _shared_services[key] = ClaudeAccountService(lambda: claude_binaries.describe(primary, fallbacks))
        return _shared_services[key]
