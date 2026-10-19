"""RepoRegistry: the repos of ONE project (`<primary repo>/.specs/project/REPOS.json`) and the `multi` flag.

The file is shared with the engine (`engine/project_repos.py` reads it and owns the writer). Nothing here changes a folder
on disk: adding or removing a repo only edits the list. No file = one primary repo (single-repo project, no migration).
Turning `multi` off keeps the listed repos in the file (checking it again restores them) while the engine sees only the primary.
"""
import json
import os
import sys
from typing import Protocol

from ..domain.project import RepoRef
from ..domain.project_errors import (LOOP_RUNNING_CODE, InvalidRepoError, InvalidRequestError, LoopRunningError,
                                     RepoConflictError, RepoNotFoundError)

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import detect_setup  # noqa: E402
import project_repos  # noqa: E402
import repo_catalog  # noqa: E402
from detect_setup import HEAVY_FOLDER_NAMES, IGNORED_FILES_ARGUMENTS, suggest_copy_files  # noqa: E402

DETACHED_HEAD_CODE = "detached_head"
ALREADY_LISTED_CODE = "already_listed"
PRIMARY_CODE = "primary"
SUGGESTED_FILE_PARTS = (".specs", "project", "REPOS-SUGGESTED.json")
SUGGESTED_LIMIT = 10
SUGGESTED_TEXT_LIMIT = 400
SETUP_LIMIT = 200


def normalize_path(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def to_relative_posix(path):
    return str(path).replace("\\", "/").strip()


class IRepoRegistryService(Protocol):
    def list_repos(self, context) -> dict: ...
    def add_repo(self, context, path, copy, setup=None) -> dict: ...
    def set_setup(self, context, name, setup) -> dict: ...
    def read_suggested_repos(self, context) -> list: ...
    def set_multi(self, context, multi) -> dict: ...
    def remove_repo(self, context, name) -> dict: ...
    def set_copy_list(self, context, name, copy) -> dict: ...
    def list_copy_suggestions(self, context, name) -> dict: ...
    def resolve(self, context, name) -> RepoRef: ...


class RepoRegistryService:
    def __init__(self, git_client, loop_status_service, log):
        self.git_client = git_client
        self.loop_status_service = loop_status_service
        self.log = log

    # ------------------------------------------------ read
    def list_repos(self, context):
        multi, stored = self.read_stored(context)
        effective = stored if multi else stored[:1]
        return {"ok": True, "multi": multi, "repos": [self.describe_repo(repo) for repo in effective],
                "suggested": self.read_suggested_repos(context)}

    def resolve(self, context, name):
        """The listed repo called `name` (the stored list, so a repo of an unchecked project can still be reached by name)."""
        _, stored = self.read_stored(context)
        found = project_repos.find_repo(stored, name)
        if found is None:
            raise RepoNotFoundError("repo not found: %s" % name)
        return found

    def list_effective_repos(self, context):
        """What the engine sees: `project_repos.load_repos` (primary first; one entry unless `multi`)."""
        return project_repos.load_repos(context.repo_path)

    # ------------------------------------------------ write
    def add_repo(self, context, path, copy=None, setup=None):
        multi, stored = self.read_stored(context)
        folder = self.validate_candidate_folder(path)
        if any(normalize_path(repo.path) == normalize_path(folder) for repo in stored):
            raise InvalidRepoError("this folder is already listed", code=ALREADY_LISTED_CODE)
        copy_list = self.validate_copy_list(copy or [])
        name = project_repos.unique_name(project_repos.short_name_of(folder), {repo.name for repo in stored})
        added = RepoRef(name=name, path=folder, primary=False, copy=copy_list, setup=self.validate_setup(setup), copy_configured=copy is not None)
        project_repos.save_repos(context.repo_path, True, stored + [added])
        self.log.info("repo_added", project=context.project.id, repo=name)
        return {"ok": True, "repo": self.describe_repo(added), "multi": True}

    def set_multi(self, context, multi):
        if not isinstance(multi, bool):
            raise InvalidRequestError("multi must be true or false")
        self.ensure_loop_not_running(context)
        _, stored = self.read_stored(context)
        project_repos.save_repos(context.repo_path, multi, stored)
        self.log.info("repo_multi_changed", project=context.project.id, multi=multi)
        return self.list_repos(context)

    def remove_repo(self, context, name):
        multi, stored = self.read_stored(context)
        target = project_repos.find_repo(stored, name)
        if target is None:
            raise RepoNotFoundError("repo not found: %s" % name)
        if target.primary:
            raise RepoConflictError("the primary repo of the project cannot be removed", code=PRIMARY_CODE)
        self.ensure_loop_not_running(context)
        project_repos.save_repos(context.repo_path, multi, [repo for repo in stored if repo.name != name])
        self.log.info("repo_removed", project=context.project.id, repo=name)
        return {"ok": True, "removed": name, "note": "no folder or worktree was deleted"}

    def set_copy_list(self, context, name, copy):
        multi, stored = self.read_stored(context)
        target = project_repos.find_repo(stored, name)
        if target is None:
            raise RepoNotFoundError("repo not found: %s" % name)
        if copy is None:  # back to automatic: the `copy` key disappears from REPOS.json
            target.copy, target.copy_configured = [], False
        else:
            target.copy, target.copy_configured = self.validate_copy_list(copy), True
        project_repos.save_repos(context.repo_path, multi, stored)
        return {"ok": True, "name": name, "copy": list(target.copy), "copy_configured": target.copy_configured}

    def set_setup(self, context, name, setup):
        """Stores the one-time setup command of a repo (empty = none). It runs in that repo's worktree when the worktree is created."""
        multi, stored = self.read_stored(context)
        target = project_repos.find_repo(stored, name)
        if target is None:
            raise RepoNotFoundError("repo not found: %s" % name)
        target.setup = self.validate_setup(setup)
        project_repos.save_repos(context.repo_path, multi, stored)
        return {"ok": True, "name": name, "setup": target.setup or None}

    def read_suggested_repos(self, context):
        """`.specs/project/REPOS-SUGGESTED.json` written by the planning AI: [{name, reason, evidence}]. Keeps only entries whose name is
        in the repo catalog (and not already listed in the project), at most SUGGESTED_LIMIT. Absent or broken file = []."""
        path = os.path.join(context.repo_path, *SUGGESTED_FILE_PARTS)
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return []
        if not isinstance(data, list):
            return []
        catalog_paths = self.read_catalog_paths_by_name()
        listed = {repo.name for repo in self.read_stored(context)[1]}
        suggested, seen = [], set()
        for item in data:
            name = str(item.get("name") or "").strip() if isinstance(item, dict) else ""
            if not name or name in seen or name in listed or name not in catalog_paths:
                continue
            seen.add(name)
            suggested.append({"name": name, "path": catalog_paths[name], "reason": str(item.get("reason") or "").strip()[:SUGGESTED_TEXT_LIMIT],
                              "evidence": str(item.get("evidence") or "").strip()[:SUGGESTED_TEXT_LIMIT]})
            if len(suggested) >= SUGGESTED_LIMIT:
                break
        return suggested

    @staticmethod
    def read_catalog_paths_by_name():
        """{catalog repo name: folder path} of the repo catalog (empty when it cannot be read)."""
        try:
            entries = repo_catalog.load_catalog()["repos"]
        except (OSError, ValueError, KeyError, TypeError):
            return {}
        return {str(entry.get("name")): str(entry.get("path") or key) for key, entry in entries.items()
                if isinstance(entry, dict) and entry.get("name")}

    def list_copy_suggestions(self, context, name):
        """Git-ignored files that look like local configuration; heavy folders are never listed."""
        target = self.resolve(context, name)
        code, output, _ = self.git_client.run_git(target.path, *IGNORED_FILES_ARGUMENTS) if os.path.isdir(target.path) else (1, "", "")
        suggestions = suggest_copy_files(target.path, output if code == 0 else "")
        for item in suggestions:  # never configured: everything suggested is copied automatically, so it shows ticked
            item["ticked"] = True if not target.copy_configured else item["path"] in target.copy
        return {"ok": True, "name": name, "copy_configured": target.copy_configured, "suggestions": suggestions}

    # ------------------------------------------------ support
    def read_stored(self, context):
        """(multi, every repo in the file, primary FIRST). No file = (False, [the primary])."""
        data = project_repos.read_repos_file(context.repo_path)
        primary_key = normalize_path(context.repo_path)
        stored, taken = [], set()
        for item in data["repos"]:
            path = str(item["path"])
            name = project_repos.unique_name(str(item.get("name") or project_repos.short_name_of(path)), taken)
            taken.add(name)
            copy = [str(entry) for entry in item.get("copy") or [] if isinstance(entry, str)]
            stored.append(RepoRef(name=name, path=path, primary=normalize_path(path) == primary_key,
                                  worktree_path=str(item.get("worktree_path") or ""), copy=copy, setup=str(item.get("setup") or "").strip(),
                                  copy_configured=isinstance(item.get("copy"), list)))
        if not any(repo.primary for repo in stored):
            primary = project_repos.single_repo(context.repo_path)[0]
            primary.name = project_repos.unique_name(primary.name, taken)
            stored.insert(0, primary)
        stored.sort(key=lambda repo: not repo.primary)
        return bool(data["multi"]), stored

    def describe_repo(self, repo):
        exists = os.path.isdir(repo.path)
        is_git = exists and self.git_client.is_git_repo(repo.path)
        branch = (self.git_client.get_current_branch(repo.path) or None) if is_git else None
        return {"name": repo.name, "path": repo.path, "primary": repo.primary, "branch": branch if exists else None,
                "worktree_path": repo.loop_worktree_path(), "copy": list(repo.copy), "setup": repo.setup or None, "exists": exists, "is_git": is_git,
                "detected_setup": detect_setup.detect_setup(repo.path), "setup_configured": bool(repo.setup), "copy_configured": repo.copy_configured}

    def validate_candidate_folder(self, path):
        if not isinstance(path, str) or not path.strip():
            raise InvalidRepoError("give the repo folder")
        folder = os.path.abspath(os.path.expanduser(path.strip()))
        if not os.path.isdir(folder):
            raise InvalidRepoError("not a folder: %s" % folder)
        if not self.git_client.is_git_repo(folder):
            raise InvalidRepoError("not a git repository: %s" % folder)
        if not self.git_client.get_current_branch(folder):
            raise InvalidRepoError("the repo has no current branch (detached HEAD): check out a branch first", code=DETACHED_HEAD_CODE)
        return folder

    @staticmethod
    def validate_setup(setup):
        if setup is None:
            return ""
        if not isinstance(setup, str) or "\n" in setup or "\r" in setup:
            raise InvalidRepoError("setup must be a single-line command")
        cleaned = setup.strip()
        if len(cleaned) > SETUP_LIMIT:
            raise InvalidRepoError("setup is limited to %d characters" % SETUP_LIMIT)
        return cleaned

    @staticmethod
    def validate_copy_list(copy):
        if not isinstance(copy, list) or not all(isinstance(entry, str) and entry.strip() for entry in copy):
            raise InvalidRepoError("copy must be a list of file paths")
        cleaned = []
        for entry in copy:
            relative = to_relative_posix(entry)
            while relative.startswith("./"):
                relative = relative[2:]
            parts = relative.split("/")
            if os.path.isabs(entry) or relative.startswith("/") or ".." in parts or relative.endswith("/") or ":" in parts[0]:
                raise InvalidRepoError("copy entries are files relative to the repo: %s" % entry)
            if set(parts) & set(HEAVY_FOLDER_NAMES):
                raise InvalidRepoError("never copy files from %s: %s" % ("/".join(HEAVY_FOLDER_NAMES), entry))
            if relative not in cleaned:
                cleaned.append(relative)
        return cleaned

    def ensure_loop_not_running(self, context):
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("a loop is running in this project: stop it first", code=LOOP_RUNNING_CODE)
