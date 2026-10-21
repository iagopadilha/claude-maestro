"""Repositories decided in the main planning chat: the AI writes `.specs/project/REPOS-SUGGESTED.json`
(`{"repos": [{"name"?, "path", "role": "edit"|"reference", "reason"}]}`) and the server applies it to REPOS.json after each reply.

The AI never edits REPOS.json (its guard profile stays narrow): this service validates every entry (folder exists, is a git repo,
primary stays `edit`, duplicates collapse with `edit` winning), merges the valid ones, and keeps the refusals in
`.specs/project/loop/internal/planning/repos_rejected.json` so the next status block and the workspace bar can show them.
Nothing here touches a folder on disk besides the two JSON files.
"""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone

from ..domain.project_errors import RepoConflictError

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import project_repos  # noqa: E402

SUGGESTED_FILE_PARTS = (".specs", "project", "REPOS-SUGGESTED.json")
PLANNING_FOLDER_PARTS = (".specs", "project", "loop", "internal", "planning")
REJECTED_FILE_NAME = "repos_rejected.json"
APPLIED_MARK_FILE_NAME = "repos_applied.json"  # sha1 of the suggestion file already applied (a stale file never undoes a panel change)
CLOSE_LOOP_FIRST_CODE = "close_loop_first"
CLOSE_LOOP_FIRST_MESSAGE = "close the loop first: repository %s already has a loop worktree, so it cannot become reference"
SUGGESTED_LIMIT = 20
TEXT_LIMIT = 400


def normalize_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def planning_file_path(context, file_name):
    return os.path.join(context.work_dir, *PLANNING_FOLDER_PARTS, file_name)


def ensure_can_switch_to_reference(repo):
    """Refuses `edit` -> `reference` while a loop worktree exists for the repo (the loop is still open). Raises RepoConflictError."""
    if not repo.primary and os.path.isdir(repo.loop_worktree_path()):
        raise RepoConflictError(CLOSE_LOOP_FIRST_MESSAGE % repo.name, code=CLOSE_LOOP_FIRST_CODE)


def load_last_rejections(context):
    """[{"path", "reason"}] refused by the last application of the chat's suggestion (empty list = none, or file absent/broken)."""
    try:
        with open(planning_file_path(context, REJECTED_FILE_NAME), encoding="utf-8") as handle:
            rows = json.load(handle).get("rejected")
    except (OSError, ValueError, AttributeError):
        return []
    return [{"path": str(row.get("path") or ""), "reason": str(row.get("reason") or "")}
            for row in rows or [] if isinstance(row, dict)]


class PlanningReposService:
    def __init__(self, git_client, log):
        self.git_client = git_client
        self.log = log

    def apply_suggested_repos(self, context):
        """Applies REPOS-SUGGESTED.json to REPOS.json. Returns {"applied": [{name, path, role, reason, primary}], "rejected": [{path, reason}]}.
        No file, a broken file or a file already applied = nothing applied and the previous rejections stay."""
        raw = self.read_suggestion_text(context)
        if raw is None:
            return {"applied": [], "rejected": []}
        digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        if self.read_applied_digest(context) == digest:
            return {"applied": [], "rejected": []}
        entries, rejected = self.parse_entries(raw)
        valid = self.validate_entries(entries, rejected)
        stored = project_repos.load_repos(context.repo_path)
        applied = self.merge_into(stored, valid, rejected)
        if applied is not None and (applied or not rejected):
            project_repos.save_repos(context.repo_path, None, stored, scope_defined=True, scope_source=project_repos.SCOPE_SOURCE_PLANNING)
        self.write_json(planning_file_path(context, REJECTED_FILE_NAME), {"at": datetime.now(timezone.utc).isoformat(), "rejected": rejected})
        self.write_json(planning_file_path(context, APPLIED_MARK_FILE_NAME), {"sha1": digest})
        self.log.info("planning_repos_applied", project=context.project.id, applied=len(applied or []), rejected=len(rejected))
        return {"applied": applied or [], "rejected": rejected}

    # ------------------------------------------------ parse and validate
    @staticmethod
    def read_suggestion_text(context):
        try:
            with open(os.path.join(context.repo_path, *SUGGESTED_FILE_PARTS), encoding="utf-8") as handle:
                return handle.read()
        except OSError:
            return None

    @staticmethod
    def parse_entries(raw):
        """(entries, rejected). Accepts `{"repos": [...]}` and the legacy bare list; anything else is one rejection."""
        try:
            data = json.loads(raw)
        except ValueError:
            return [], [{"path": "REPOS-SUGGESTED.json", "reason": "the file is not valid JSON"}]
        rows = data.get("repos") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return [], [{"path": "REPOS-SUGGESTED.json", "reason": 'expected {"repos": [{"path", "role", "reason"}]}'}]
        return rows[:SUGGESTED_LIMIT], []

    def validate_entries(self, entries, rejected):
        """Valid entries collapsed by folder (edit wins): [{path, key, name, role, reason}]. Refusals are appended to `rejected`."""
        by_key = {}
        for item in entries:
            path = str(item.get("path") or "").strip() if isinstance(item, dict) else ""
            label = path or (str(item.get("name") or "") if isinstance(item, dict) else str(item))
            problem = self.find_problem(item, path)
            if problem:
                rejected.append({"path": label, "reason": problem})
                continue
            folder = os.path.abspath(os.path.expanduser(path))
            key = normalize_path(folder)
            role = item.get("role") or project_repos.ROLE_EDIT
            reason = str(item.get("reason") or "").strip()[:TEXT_LIMIT]
            name = str(item.get("name") or "").strip()
            current = by_key.get(key)
            if current is None:
                by_key[key] = {"path": folder, "key": key, "name": name, "role": role, "reason": reason}
            else:  # two suggestions for the same folder: one entry, edit wins
                if role == project_repos.ROLE_EDIT:
                    current["role"] = role
                current["reason"] = current["reason"] or reason
                current["name"] = current["name"] or name
        return list(by_key.values())

    def find_problem(self, item, path):
        if not isinstance(item, dict):
            return "each repository must be an object with path, role and reason"
        if not path:
            return "give the repository folder in `path`"
        if item.get("role") not in (None,) + project_repos.ROLES:
            return "role must be edit or reference"
        folder = os.path.abspath(os.path.expanduser(path))
        if not os.path.isdir(folder):
            return "folder does not exist"
        if not self.git_client.is_git_repo(folder):
            return "not a git repository"
        if not self.git_client.get_current_branch(folder):
            return "the repository has no current branch (detached HEAD)"
        return ""

    # ------------------------------------------------ merge
    @staticmethod
    def merge_into(stored, valid, rejected):
        """Updates `stored` (RepoRef list, primary first) in place; returns the applied rows. A refused role switch goes to `rejected`."""
        applied, taken = [], {repo.name for repo in stored}
        by_key = {normalize_path(repo.path): repo for repo in stored}
        for entry in valid:
            existing = by_key.get(entry["key"])
            if existing is not None and existing.primary:  # the primary stays edit; only its reason may be filled
                existing.reason = existing.reason or entry["reason"]
                continue
            if existing is not None:
                if existing.role == project_repos.ROLE_EDIT and entry["role"] == project_repos.ROLE_REFERENCE:
                    try:
                        ensure_can_switch_to_reference(existing)
                    except RepoConflictError as error:
                        rejected.append({"path": entry["path"], "reason": str(error)})
                        continue
                existing.role = entry["role"]
                existing.reason = entry["reason"] or existing.reason
                target = existing
            else:
                name = project_repos.unique_name(project_repos.short_name_of(entry["name"] or entry["path"]), taken)
                taken.add(name)
                target = project_repos.RepoRef(name=name, path=entry["path"], role=entry["role"], reason=entry["reason"])
                stored.append(target)
                by_key[entry["key"]] = target
            applied.append({"name": target.name, "path": target.path, "role": target.role, "reason": target.reason, "primary": False})
        return applied

    # ------------------------------------------------ files
    @staticmethod
    def read_applied_digest(context):
        try:
            with open(planning_file_path(context, APPLIED_MARK_FILE_NAME), encoding="utf-8") as handle:
                return json.load(handle).get("sha1")
        except (OSError, ValueError, AttributeError):
            return None

    @staticmethod
    def write_json(path, data):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
