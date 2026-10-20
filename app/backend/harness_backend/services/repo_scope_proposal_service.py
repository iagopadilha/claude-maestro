"""Proposal of the repositories a loop will touch, BEFORE any loop exists: the user describes the work in plain words and a
confined headless `claude -p` picks, from the repo catalog, the repositories that would be changed. Nothing is written here: the
answer is a proposal the user ticks and confirms in the repository manager (which then uses the ordinary add/confirm routes).

Confinement: read-only tools, no MCP, `dontAsk`, and the file guard undoes any change the AI could still make in the folder.
"""
import json

from ..domain.project_errors import InvalidRequestError
from ..infrastructure.claude_client import redact_secrets
from .repo_catalog_service import extract_json_object
from .repo_registry_service import normalize_path

DESCRIPTION_LIMIT = 2000
PROPOSED_LIMIT = 10
REASON_LIMIT = 300
PROPOSAL_MODEL = ""  # empty = the account's default model
PROPOSAL_BUDGET_USD = 1
PROPOSAL_TIMEOUT_SECONDS = 180
PROPOSAL_PERMISSION_MODE = "dontAsk"
PROPOSAL_ALLOWED_TOOLS = ["Read", "Grep", "Glob"]
PROPOSAL_DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch"]
GUARD_IGNORED_PATHS = (".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__")
NO_CATALOG_MESSAGE = "The repo catalog is empty: scan repositories first, or tick them by hand."
PROMPT_TEMPLATE = (
    "A developer will describe work. From the CATALOG below choose ONLY the other repositories (not the primary one, `%s`) that this "
    "work would CHANGE. Do not choose a repository that is only read or merely related. Answer with only one JSON object "
    '(no prose, no code fence): {"repos": [{"name": "<catalog name>", "reason": "<one short sentence>"}]}. An empty list is a valid '
    "answer. Never invent a name that is not in the catalog. You cannot and must not change any file.\n\n"
    "WORK:\n%s\n\nCATALOG (JSON):\n%s")


class RepoScopeProposalService:
    def __init__(self, repo_registry_service, repo_catalog_service, claude_client, workspace_file_guard, log):
        self.repo_registry_service = repo_registry_service
        self.repo_catalog_service = repo_catalog_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.log = log

    def propose(self, context, description):
        """{"ok", "proposed": [{name, path, reason}], "message", "cost_usd"}; only catalog repos that are not listed yet."""
        text = description.strip() if isinstance(description, str) else ""
        if not text or len(text) > DESCRIPTION_LIMIT:
            raise InvalidRequestError("describe the work in up to %d characters" % DESCRIPTION_LIMIT)
        self.repo_registry_service.ensure_loop_not_running(context)
        _, stored = self.repo_registry_service.read_stored(context)
        listed = {normalize_path(repo.path) for repo in stored}
        candidates = [item for item in self.repo_catalog_service.get_index()["repos"]
                      if item.get("name") and normalize_path(item["path"]) not in listed]
        if not candidates:
            return {"ok": True, "proposed": [], "message": NO_CATALOG_MESSAGE, "cost_usd": 0}
        catalog_text = json.dumps([{"name": item["name"], "purpose": item.get("purpose", ""), "capabilities": item.get("capability_ids", [])}
                                   for item in candidates], ensure_ascii=False)
        primary_name = next(repo.name for repo in stored if repo.primary)
        reply, cost, error = self.ask(context.repo_path, PROMPT_TEMPLATE % (primary_name, text, catalog_text))
        if error:
            return {"ok": False, "proposed": [], "message": error, "cost_usd": cost}
        by_name = {item["name"]: item for item in candidates}
        proposed = []
        for item in (reply.get("repos") if isinstance(reply.get("repos"), list) else []):
            name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
            if name in by_name and name not in {row["name"] for row in proposed}:
                proposed.append({"name": name, "path": by_name[name]["path"], "reason": str(item.get("reason") or "").strip()[:REASON_LIMIT]})
        self.log.info("repo_scope_proposed", project=context.project.id, proposed=len(proposed))
        return {"ok": True, "proposed": proposed[:PROPOSED_LIMIT], "cost_usd": cost,
                "message": "" if proposed else "The AI found no other repository to change: only the primary repository is in scope."}

    def ask(self, folder, prompt):
        """(parsed reply, cost_usd, error)."""
        snapshot = self.workspace_file_guard.take_snapshot(folder, GUARD_IGNORED_PATHS)
        argv = self.claude_client.build_command(PROPOSAL_MODEL, PROPOSAL_BUDGET_USD, PROPOSAL_ALLOWED_TOOLS, PROPOSAL_DISALLOWED_TOOLS,
                                                no_mcp=True, permission_mode=PROPOSAL_PERMISSION_MODE)
        run = self.claude_client.run_prompt(argv, folder, prompt, PROPOSAL_TIMEOUT_SECONDS)
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, ())
        if report["reverted"] + report["removed"] + report["unrestorable"]:
            self.log.warn("repo_scope_guard_reverted", folder=folder)
        cost = run.get("cost_usd") if isinstance(run.get("cost_usd"), (int, float)) else 0
        if not run.get("ok"):
            return None, cost, redact_secrets(run.get("error") or "claude finished with an error", 300)
        parsed = extract_json_object(run.get("reply"))
        if parsed is None:
            return None, cost, "The AI did not return a JSON answer."
        return parsed, cost, ""
