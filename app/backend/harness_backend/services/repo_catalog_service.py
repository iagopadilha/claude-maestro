"""Repo catalog job and reads (`/api/app/catalog`).

The job summarizes each requested repo with a headless `claude -p` and stores the validated result in
`~/.loop-harness/repo-catalog.json` (engine/repo_catalog.py). Confinement, in three layers:
  1. the AI has NO write tool: `--allowedTools Read Grep Glob Skill`, `--disallowedTools Bash Edit Write NotebookEdit Agent Web...`,
     `--permission-mode dontAsk`, no MCP; it only answers with a JSON sheet;
  2. the file guard snapshots the repo before and undoes ANY change after (nothing is allowed to change in a repo);
  3. only this service writes, and only the catalog file, through `repo_catalog.store_entry`, after `repo_catalog.validate`.
A repo without `docs/codebase` is not sent to the AI: it is stored as `weak` and the job asks to map it first. A repo whose HEAD did
not change keeps its sheet (`unchanged`). Jobs are kept in memory (one scan at a time, app-wide).
"""
import json
import os
import re
import secrets
import threading

from ..domain.project_errors import InvalidRequestError, JobInProgressError, JobNotFoundError
from ..engine import repo_catalog
from ..infrastructure.claude_client import redact_secrets
from ..infrastructure.claude_client import call_kind

CATALOG_JOB_TYPE = "repo-catalog"
CATALOG_BUSY_CODE = "busy"
JOB_STATE_RUNNING = "running"
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
STATUS_SCANNED = "scanned"
STATUS_UNCHANGED = "unchanged"
STATUS_WEAK = "weak"
STATUS_ERROR = "error"
CATALOG_MODEL = ""  # empty = the account's default model
CATALOG_BUDGET_USD = 1
CATALOG_TIMEOUT_SECONDS = 600
CATALOG_PERMISSION_MODE = "dontAsk"
CATALOG_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Skill"]
CATALOG_DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit", "Agent", "WebFetch", "WebSearch"]
GUARD_IGNORED_PATHS = (".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__")
PATHS_LIMIT = 50
WEAK_MESSAGE = "No docs/codebase; map it first."
PROMPT = (
    "Use the repo-catalog skill format. Summarize THIS repository (your working directory) as ONE JSON object and answer with only "
    "that object (no prose, no code fence):\n"
    '{"purpose": "<one sentence, at most 200 characters>", "capabilities": [{"id": "<kebab-case>", "description": "<one sentence>", '
    '"evidence": "<path of an existing file, relative to the repo root, forward slashes>"}]}\n'
    "Start from docs/codebase/*, then confirm each capability in code. Rules: never invent a capability; every evidence file must exist "
    "(open or search it before citing); never include secrets, hosts or customer names; 3 to 12 capabilities; you cannot and must not "
    "change any file.")
FENCE_PATTERN = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


def extract_json_object(reply):
    """First JSON object found in the reply (tolerates a code fence or text around it); None when there is none."""
    text = FENCE_PATTERN.sub("", (reply or "").strip())
    decoder = json.JSONDecoder()
    for index in (position for position, character in enumerate(text) if character == "{"):
        try:
            value, _ = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict):
            return value
    return None


class RepoCatalogService:
    def __init__(self, claude_client, workspace_file_guard, log, known_paths_provider=None):
        """`known_paths_provider() -> [path]` (optional): folders the app already knows (listed repos), shown as never scanned."""
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.log = log
        self.known_paths_provider = known_paths_provider
        self.jobs_lock = threading.Lock()
        self.jobs = {}  # id -> job dict (in memory; a scan is short and its result lives in the catalog file)
        self.running_job_id = ""
        self.job_counter = 0

    # ------------------------------------------------ reads
    def get_index(self):
        """{"ok", "repos": [{name, path, weak, stale, purpose, capability_ids}]}: what the planning chat reads."""
        entries = repo_catalog.load_catalog()["repos"]
        repos = []
        for key, entry in entries.items():
            if not isinstance(entry, dict):
                continue
            path = entry.get("path") or key
            repos.append({"name": entry.get("name", ""), "path": path, "weak": bool(entry.get("weak")), "stale": self.is_stale(path, entry),
                          "purpose": entry.get("purpose", ""),
                          "capability_ids": [item.get("id") for item in entry.get("capabilities", []) if isinstance(item, dict)]})
        known = {repo_catalog.normalize_path(item["path"]) for item in repos}
        for path in self.list_known_paths():
            normalized = repo_catalog.normalize_path(path)
            if normalized not in known:
                repos.append({"name": os.path.basename(normalized), "path": normalized, "weak": True, "stale": True, "purpose": "",
                              "capability_ids": []})
        return {"ok": True, "repos": repos}

    def get_sheet(self, path):
        """The stored entry of the repo or None (the controller answers 404)."""
        if not isinstance(path, str) or not path.strip():
            raise InvalidRequestError("path is required")
        entry = repo_catalog.get_entry(path)
        return {"ok": True, "entry": entry} if entry else None

    def get_job(self, job_id):
        with self.jobs_lock:
            job = self.jobs.get(job_id)
            if not job:
                raise JobNotFoundError("job not found")
            return {"ok": True, "job": json.loads(json.dumps(job))}

    # ------------------------------------------------ scan job
    def start_scan(self, paths):
        """Starts the background job over `paths`; JobInProgressError (409 `busy`) while another scan runs."""
        normalized = self.validate_paths(paths)
        with self.jobs_lock:
            if self.running_job_id:
                raise JobInProgressError("a catalog scan is already running: wait for it to finish", code=CATALOG_BUSY_CODE)
            self.job_counter += 1
            job = {"id": "cat-%d-%s" % (self.job_counter, secrets.token_hex(3)), "type": CATALOG_JOB_TYPE, "state": JOB_STATE_RUNNING,
                   "started_at": repo_catalog.now_iso(), "finished_at": None, "cost_usd": 0, "error": "", "results": []}
            self.jobs[job["id"]] = job
            self.running_job_id = job["id"]
        threading.Thread(target=self.run_job, args=(job, normalized), daemon=True, name="repo-catalog-" + job["id"]).start()
        self.log.info("repo_catalog_started", job=job["id"], repos=len(normalized))
        return {"ok": True, "job_id": job["id"]}

    @staticmethod
    def validate_paths(paths):
        if not isinstance(paths, list) or not paths or len(paths) > PATHS_LIMIT:
            raise InvalidRequestError("paths must be a list of 1 to %d folders" % PATHS_LIMIT)
        normalized = []
        for path in paths:
            if not isinstance(path, str) or not os.path.isdir(path):
                raise InvalidRequestError("not a folder: %s" % (path if isinstance(path, str) else "(invalid)"))
            if path not in normalized:
                normalized.append(path)
        return normalized

    def run_job(self, job, paths):
        try:
            for path in paths:
                result, cost = self.scan_one(path)
                with self.jobs_lock:
                    job["results"].append(result)
                    job["cost_usd"] = round(job["cost_usd"] + (cost or 0), 4)
            outcome = {"state": JOB_STATE_OK, "error": ""}
        except Exception as failure:  # a job never takes the app down
            self.log.error("repo_catalog_failed", job=job["id"], type=type(failure).__name__, message=str(failure)[:200])
            outcome = {"state": JOB_STATE_ERROR, "error": "internal failure: %s" % type(failure).__name__}
        with self.jobs_lock:
            job.update(outcome, finished_at=repo_catalog.now_iso())
            self.running_job_id = ""
        self.log.info("repo_catalog_finished", job=job["id"], state=outcome["state"], cost_usd=job["cost_usd"])

    def scan_one(self, path):
        """(result, cost_usd) of one repo. Never raises for a repo-level problem: it becomes status `error`."""
        facts = repo_catalog.scan(path)
        result = {"path": facts["path"], "status": STATUS_ERROR, "weak": not facts["has_docs_codebase"], "rejected": [], "message": ""}
        if not facts["head"]:
            result["message"] = "Not a git repository, or it has no commit yet."
            return result, 0
        if repo_catalog.is_cached(path, facts["head"]):
            entry = repo_catalog.get_entry(path)
            return dict(result, status=STATUS_UNCHANGED, weak=bool(entry.get("weak"))), 0
        if not facts["has_docs_codebase"]:
            repo_catalog.store_entry(repo_catalog.build_weak_entry(facts))
            return dict(result, status=STATUS_WEAK, weak=True, message=WEAK_MESSAGE), 0
        sheet, cost, error = self.ask_for_sheet(path)
        if error:
            return dict(result, message=error), cost
        entry, rejected = repo_catalog.build_entry(facts, sheet, path)
        if entry is None:
            reasons = "; ".join(item["reason"] for item in rejected)
            return dict(result, message="The AI sheet was refused: " + redact_secrets(reasons, 300), rejected=[]), cost
        repo_catalog.store_entry(entry)
        return dict(result, status=STATUS_SCANNED, weak=entry["weak"], rejected=rejected), cost

    def ask_for_sheet(self, path):
        """(sheet, cost_usd, error): runs the confined `claude -p` in the repo and undoes any change it made there."""
        snapshot = self.workspace_file_guard.take_snapshot(path, GUARD_IGNORED_PATHS)
        argv = self.claude_client.build_command(CATALOG_MODEL, CATALOG_BUDGET_USD, CATALOG_ALLOWED_TOOLS, CATALOG_DISALLOWED_TOOLS,
                                                no_mcp=True, permission_mode=CATALOG_PERMISSION_MODE)
        with call_kind("catalog"):
            run = self.claude_client.run_prompt(argv, path, PROMPT, CATALOG_TIMEOUT_SECONDS)
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, ())  # nothing may change in the repo
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("repo_catalog_guard_reverted", repo=path, files=touched[:50])
        cost = run.get("cost_usd") if isinstance(run.get("cost_usd"), (int, float)) else 0
        if not run.get("ok"):
            return None, cost, run.get("error") or "claude finished with an error"
        sheet = extract_json_object(run.get("reply"))
        if sheet is None:
            return None, cost, "The AI did not return a JSON sheet."
        return sheet, cost, ""

    # ------------------------------------------------ support
    def is_stale(self, path, entry):
        head = repo_catalog.read_head(path) if os.path.isdir(path) else ""
        return bool(head and head != entry.get("head"))

    def list_known_paths(self):
        if not self.known_paths_provider:
            return []
        try:
            return [path for path in self.known_paths_provider() if isinstance(path, str)]
        except Exception as failure:  # the list of known folders is optional
            self.log.warn("repo_catalog_known_paths_failed", type=type(failure).__name__)
            return []
