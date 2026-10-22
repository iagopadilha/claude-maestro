"""Which command and sign-in each `claude -p` call of the panel used (chat, map, roadmap, validation, follow-up...).

`ClaudeClient.run_prompt` hands every finished call to `record_call`; one line per call goes to the project's call log (see
repositories/claude_call_log_repository.py): time, kind, command, sign-in method, account/e-mail, cost, ok, and the 5-hour/weekly usage windows the call's own output reported (`usage_windows`,
`limit_status`; empty when the output had no rate limit event). Recording never raises and
never waits for `auth status`: the sign-in method comes from the account cache the loop panel already keeps warm (empty before it is read).
"""
import datetime
import threading

from ..engine import claude_binaries
from ..engine import usage_windows
from ..repositories.claude_call_log_repository import ClaudeCallLogRepository
from .claude_account_service import build_shared_claude_account_service, command_name, method_code

UNKNOWN_KIND = "panel"
PUBLIC_CALL_LIMIT = 50

_shared_services = {}
_shared_lock = threading.Lock()


class ClaudeCallLogService:
    def __init__(self, repository, account_service=None, clock=datetime.datetime.now):
        """`account_service` (ClaudeAccountService) tells the sign-in of a command; None = the method stays unknown."""
        self.repository = repository
        self.account_service = account_service
        self.clock = clock

    def build_entry(self, kind, result):
        """One log line from the result of `run_prompt` (binary, account, cost_usd, ok); never a secret or the reply text."""
        command = command_name(result.get("binary") or claude_binaries.DEFAULT_BINARY_NAME)
        auth = self.find_command_auth(command)
        cc_account = str(result.get("account") or "")
        email = str(auth.get("email") or "") if not cc_account else ""
        cost = result.get("cost_usd")
        rate_limit = (result.get("raw") or {}).get("rate_limit") or {}
        return {"at": self.clock().isoformat(timespec="seconds"), "kind": kind or UNKNOWN_KIND, "claude": command,
                "auth_method": method_code(auth.get("method")), "auth_email": email, "auth_plan": str(auth.get("plan") or ""),
                "cc_account": cc_account, "cost_usd": cost if isinstance(cost, (int, float)) else None, "ok": bool(result.get("ok")),
                "usage_windows": usage_windows.with_local_reset(usage_windows.windows_of(rate_limit)),
                "limit_status": str(rate_limit.get("status") or "")}

    def find_command_auth(self, command):
        if self.account_service is None:
            return {}
        try:
            rows = self.account_service.get_rows()
            return (self.account_service.find_row(rows, command) or {}).get("auth") or {}
        except Exception:  # noqa: BLE001 - the call log must never break the call it describes
            return {}

    def record_call(self, folder, kind, result):
        """Stores one call under `folder` (the folder the command ran in). Never raises."""
        try:
            self.repository.append_call(folder, self.build_entry(kind, result or {}))
        except Exception:  # noqa: BLE001
            pass

    def list_calls(self, folders, limit=PUBLIC_CALL_LIMIT):
        """The newest `limit` calls of one or more folders (the repository and its worktree), newest first."""
        seen, calls = set(), []
        for folder in folders:
            for entry in self.repository.read_calls(folder):
                key = (entry.get("at"), entry.get("kind"), entry.get("claude"), entry.get("cost_usd"))
                if key not in seen:
                    seen.add(key)
                    calls.append(entry)
        calls.sort(key=lambda entry: str(entry.get("at") or ""), reverse=True)
        return calls[:limit]

    def list_all_calls(self, folders):
        """Every kept call (not cut to the public limit), for the totals."""
        return self.list_calls(folders, limit=10 ** 6)


def build_shared_claude_call_log_service(config):
    """The ONE service per configured command list: every ClaudeClient records into it and the panel reads from it."""
    key = (getattr(config, "claude_binary", "") or "", tuple(getattr(config, "claude_fallback_binaries", None) or []))
    with _shared_lock:
        if key not in _shared_services:
            _shared_services[key] = ClaudeCallLogService(ClaudeCallLogRepository(), build_shared_claude_account_service(config))
        return _shared_services[key]
