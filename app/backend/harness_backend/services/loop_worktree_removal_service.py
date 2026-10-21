"""The loop worktrees a Reset specs deletes with the plan: find them (preview) and remove them (worktree folder + local loop branch).

A repo's worktree is removable only when ALL hold: it sits at the repo's loop worktree path, `git worktree list` of that repo registers
it, it is not the main checkout, and its branch is a loop branch (`loop/*`). Anything else is reported as skipped and never touched
(the primary checkout, other worktrees and other branches are never removed). Removal is `git worktree remove --force` + prune +
`git branch -D` through the git client: no merge, no commit, no push, no backup: what was not delivered or pushed is lost.
"""
import os
from typing import Protocol

from ..engine import project_repos
from ..infrastructure.git_client import OTHER_COMPARISON_BASES, remove_emptied_folder
from ..repositories.specs_file_repository import loop_state
from .loop_worktree_service import LOOP_BRANCH_PREFIX, get_integration_branch, is_same_path

META_FILE_NAME = "meta.json"


def is_empty_folder(path):
    """A real folder (not a link) with nothing inside: the only leftover a reset deletes without git registering it as a worktree."""
    is_junction = getattr(os.path, "isjunction", lambda _: False)
    return os.path.isdir(path) and not os.path.islink(path) and not is_junction(path) and not os.listdir(path)


class ILoopWorktreeRemovalService(Protocol):
    def preview_removable_worktrees(self, context) -> dict: ...
    def remove_loop_worktrees(self, context) -> dict: ...


class LoopWorktreeRemovalService:
    def __init__(self, git_client, specs_file_repository, log):
        self.git_client = git_client
        self.specs_file_repository = specs_file_repository
        self.log = log

    # ------------------------------------------------ read
    def preview_removable_worktrees(self, context):
        """`{"worktrees": [{"repo","primary","path","branch","unpushed_commits"}], "skipped": [{"repo","path","reason"}]}`; read only.
        `unpushed_commits` is None when git has no reference to compare with."""
        removable, skipped = [], []
        for repo in project_repos.load_repos(context.repo_path):
            found, reason = self.inspect_repo_worktree(repo)
            if found:
                removable.append(dict(found, unpushed_commits=self.count_unpushed_commits(repo, found)))
            elif reason:
                skipped.append({"repo": repo.name, "path": os.path.normpath(repo.loop_worktree_path()), "reason": reason})
        return {"worktrees": [{key: item[key] for key in ("repo", "primary", "path", "branch", "unpushed_commits")} for item in removable],
                "skipped": skipped}

    def inspect_repo_worktree(self, repo):
        """(worktree dict, None) when removable; (None, reason) when a folder is there but must not be touched; (None, None) when
        there is no worktree. The dict also carries `main_repo`, the folder the git commands run from."""
        path = os.path.normpath(repo.loop_worktree_path())
        if not os.path.lexists(path):
            return None, None
        if not os.path.isdir(path):
            return None, "the worktree path is not a folder"
        if is_same_path(path, repo.path):
            return None, "it is the repo folder itself"
        registered = self.git_client.list_worktrees(repo.path)
        if not registered:
            return None, "the repo is not a git repository"
        main_repo = registered[0]["path"]
        entry = next((item for item in registered if is_same_path(item["path"], path)), None)
        if entry is None:
            return None, "the folder is not a git worktree of this repo"
        if is_same_path(entry["path"], main_repo):
            return None, "it is the main checkout"
        branch = entry.get("branch", "")
        if not branch.startswith(LOOP_BRANCH_PREFIX):
            return None, "its branch (%s) is not a loop branch" % (branch or "detached HEAD")
        return {"repo": repo.name, "primary": repo.primary, "path": path, "branch": branch, "main_repo": main_repo}, None

    def count_unpushed_commits(self, repo, worktree):
        return self.git_client.count_unpushed_commits(worktree["path"], self.get_comparison_bases(repo, worktree["path"]))

    def get_comparison_bases(self, repo, worktree_path):
        """The branch the loop forked from (meta.json of the worktree), the integration branch, then main/master."""
        meta = self.specs_file_repository.read_json(loop_state.loop_path(worktree_path, META_FILE_NAME))
        recorded = (meta.get("repos") or {}).get(repo.name) if isinstance(meta.get("repos"), dict) else None
        recorded = recorded if isinstance(recorded, dict) else (meta if repo.primary else {})
        base = recorded.get("base")
        bases = ([base] if isinstance(base, str) and base else []) + [get_integration_branch()] + list(OTHER_COMPARISON_BASES)
        return list(dict.fromkeys(bases))

    # ------------------------------------------------ act
    def remove_loop_worktrees(self, context):
        """Removes every removable loop worktree of the project (each repo on its own: one failure never stops the others).
        Returns `{"worktrees_removed": [{"repo","path","branch","branch_deleted"}], "worktree_failures": [{"repo","path","branch","error"}]}`."""
        removed, failures = [], []
        for repo in project_repos.load_repos(context.repo_path):
            found, reason = self.inspect_repo_worktree(repo)
            leftover = os.path.normpath(repo.loop_worktree_path())
            if found is None and reason and is_empty_folder(leftover) and remove_emptied_folder(leftover):
                # an EMPTY folder left at the loop worktree path (an earlier removal on Windows emptied it but could not delete it)
                removed.append({"repo": repo.name, "path": leftover, "branch": None, "branch_deleted": False, "folder_left": False})
                continue
            if found is None:
                if reason:
                    failures.append({"repo": repo.name, "path": os.path.normpath(repo.loop_worktree_path()), "branch": None,
                                     "error": "not removed: %s" % reason})
                continue
            outcome = self.git_client.remove_worktree_and_branch(found["main_repo"], found["path"], found["branch"])
            item = {"repo": found["repo"], "path": found["path"], "branch": found["branch"]}
            if outcome["worktree_removed"]:
                removed.append(dict(item, branch_deleted=outcome["branch_deleted"], folder_left=outcome.get("folder_left", False)))
                if outcome["error"]:  # the worktree is gone but the branch could not be deleted, or its emptied folder is still held open
                    failures.append(dict(item, error=outcome["error"]))
            else:
                failures.append(dict(item, error=outcome["error"]))
            self.log.info("reset_worktree_removed" if outcome["worktree_removed"] else "reset_worktree_not_removed", project=context.project.id,
                          repo=found["repo"], branch=found["branch"], branch_deleted=outcome["branch_deleted"])
        return {"worktrees_removed": removed, "worktree_failures": failures}
