"""Reopen planning: after the roadmap was approved (or a loop ran) the user goes back to planning to change the roadmap or the
repositories. The loop screen only runs; repositories and roadmap are defined in planning.

`check_reopen` (read only) lists the loop worktrees the app created and what would be lost. `reopen` (1) refuses while a loop or a
claude job runs, (2) refuses with `needs_confirm` when a worktree is dirty or has unpushed commits and the user did not confirm,
(3) saves the plan that lives in the loop worktree back into the primary repo (`.specs/` features, phases, project docs, the planning
chat, a few kept loop files; never loop state such as LOOP.json/turns, so the next loop starts clean on the ROADMAP statuses),
(4) removes ONLY the worktrees the app created (registered in `git worktree list`, `loop/*` branch, the path REPOS.json/the default
sibling folder says, never the user's folder), (5) removes the roadmap approval and writes the reopened marker (see
planning_requirement_service). Branches and commits are never deleted; nothing is pushed. Reopening is idempotent.
"""
import os
import shutil
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, PLANNING_BUSY_CODE, InvalidRequestError, JobInProgressError, LoopRunningError,
                                     RepoConflictError)
from ..repositories.specs_file_repository import is_link_or_junction, loop_state
from .claude_job_service import now_iso
from .loop_worktree_service import LOOP_BRANCH_PREFIX, is_same_path
from .specs_reset_service import KEPT_LOOP_FILE_NAMES
from .workspace_service import project_repos

NEEDS_CONFIRM_CODE = "needs_confirm"
NOT_REMOVABLE_CODE = "worktree_not_removable"
REMOVE_FAILED_CODE = "worktree_remove_failed"
PLAN_TREES = (".specs/features", ".specs/project/phases")  # copied whole from the worktree
PLAN_FILES = (".specs/project/ROADMAP.md", ".specs/project/FLOWS.md", ".specs/project/STATE.md", ".specs/project/PROJECT.md",
              ".specs/project/RULES.md", ".specs/project/TEST-ENVIRONMENT.md", ".specs/project/ACCESS.json")
PLANNING_THREAD_TREE = ("internal", "planning")  # .specs/project/loop/internal/planning (the chat history and its claude session)
REASON_TEXT = {
    "loop_running": "The loop is running; stop it first.",
    "tree_dirty": "The worktree has uncommitted changes (outside .specs/) that would be lost.",
    "unpushed_commits": "The loop branch has commits that were not pushed. The branch is kept; only the worktree folder goes.",
    "not_app_worktree": "This folder is not a loop worktree created by the app (not registered in git, not on a loop/* branch); it is never removed.",
}


def to_posix(path):
    return str(path).replace("\\", "/")


class IPlanningReopenService(Protocol):
    def check_reopen(self, context) -> dict: ...
    def reopen(self, context, body) -> dict: ...


class PlanningReopenService:
    def __init__(self, git_client, loop_status_service, loop_worktree_service, planning_requirement_service, claude_job_service, log):
        self.git_client = git_client
        self.loop_status_service = loop_status_service
        self.loop_worktree_service = loop_worktree_service
        self.planning_requirement_service = planning_requirement_service
        self.claude_job_service = claude_job_service
        self.log = log

    # ------------------------------------------------ read
    def check_reopen(self, context):
        running = self.loop_status_service.is_loop_running(context)
        worktrees = self.describe_worktrees(context, running)
        can_reopen = not running and all(item["can_remove"] for item in worktrees)
        return {"ok": True, "can_reopen": can_reopen, "blocked_by": LOOP_RUNNING_CODE if running else None,
                "needs_confirm": any(item["dirty"] or item["unpushed"] for item in worktrees), "worktrees": worktrees}

    def describe_worktrees(self, context, running):
        """One item per repo whose loop worktree folder exists: {repo, path, branch, dirty, unpushed, can_remove, reasons}."""
        items = []
        for repo in project_repos.load_repos(context.repo_path):
            path = repo.loop_worktree_path()
            if not os.path.isdir(path):
                continue
            branch = self.find_registered_loop_branch(repo, path)
            item = {"repo": repo.name, "path": os.path.normpath(path), "branch": branch or "", "dirty": False, "unpushed": 0,
                    "can_remove": False, "reasons": []}
            if branch is None:
                item["branch"] = self.git_client.get_current_branch(path) or ""
                item["reasons"].append({"code": "not_app_worktree", "text": REASON_TEXT["not_app_worktree"]})
                items.append(item)
                continue
            item["dirty"] = self.has_changes_outside_specs(path)
            item["unpushed"] = self.git_client.count_unpushed_commits(path, self.loop_worktree_service.get_comparison_bases(path)) or 0
            if running:
                item["reasons"].append({"code": "loop_running", "text": REASON_TEXT["loop_running"]})
            else:
                item["can_remove"] = True
            if item["dirty"]:
                item["reasons"].append({"code": "tree_dirty", "text": REASON_TEXT["tree_dirty"]})
            if item["unpushed"]:
                item["reasons"].append({"code": "unpushed_commits", "text": REASON_TEXT["unpushed_commits"]})
            items.append(item)
        return items

    def find_registered_loop_branch(self, repo, path):
        """Branch of `path` when git lists it as a secondary worktree of the repo on a loop/* branch; None otherwise."""
        if is_same_path(path, repo.path):
            return None
        for worktree in self.git_client.list_worktrees(repo.path)[1:]:
            if is_same_path(worktree["path"], path):
                return worktree["branch"] if worktree["branch"].startswith(LOOP_BRANCH_PREFIX) else None
        return None

    def has_changes_outside_specs(self, worktree):
        code, output, _ = self.git_client.run_git(worktree, "status", "--porcelain")
        if code != 0:
            return True  # unknown state: ask for confirmation rather than assume clean
        return any(not to_posix(line[3:].strip().strip('"')).startswith(".specs/") for line in output.splitlines())

    # ------------------------------------------------ act
    def reopen(self, context, body):
        confirm = body.get("confirm_remove_worktrees", False) if isinstance(body, dict) else False
        if not isinstance(confirm, bool):
            raise InvalidRequestError("confirm_remove_worktrees must be true or false")
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("the loop is running: stop it before reopening the planning", code=LOOP_RUNNING_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job (planning reply, roadmap or map) is running: wait for it to finish", code=PLANNING_BUSY_CODE)
        worktrees = self.describe_worktrees(context, False)
        blocked = [item for item in worktrees if not item["can_remove"]]
        if blocked:
            raise RepoConflictError("a worktree is not a loop worktree created by the app: remove it by hand (%s)" % blocked[0]["path"],
                                    code=NOT_REMOVABLE_CODE, extras={"worktrees": worktrees})
        if any(item["dirty"] or item["unpushed"] for item in worktrees) and not confirm:
            raise RepoConflictError("worktrees with uncommitted or unpushed work: confirm to remove them (the branches are kept)",
                                    code=NEEDS_CONFIRM_CODE, extras={"worktrees": worktrees})
        previous_work_dir = context.work_dir
        if not is_same_path(previous_work_dir, context.repo_path):
            self.save_plan_into_repo(previous_work_dir, context.repo_path)  # a failure aborts here, nothing was removed
        removed, failures = self.remove_worktrees(context, worktrees)
        removed_names = {entry["repo"] for entry in removed}
        remaining = [item["path"] for item in worktrees if item["repo"] not in removed_names]
        self.invalidate_approval(context, [previous_work_dir, *remaining])
        self.planning_requirement_service.mark_reopened(context, now_iso(), [previous_work_dir, *remaining])
        self.log.info("planning_reopened", project=context.project.id, removed=len(removed), failed=len(failures))
        if failures:
            raise RepoConflictError("some worktrees could not be removed: %s" % "; ".join(failures), code=REMOVE_FAILED_CODE,
                                    extras={"removed": removed})
        return {"ok": True, "reopened": True, "removed": removed}

    def save_plan_into_repo(self, worktree, repo_path):
        """The worktree is where planning/loop wrote while it ran: bring the plan back before it is deleted."""
        source_specs, target_specs = os.path.join(worktree, ".specs"), os.path.join(repo_path, ".specs")
        if is_link_or_junction(source_specs) or is_link_or_junction(target_specs):
            raise RepoConflictError(".specs/ is a symbolic link (or junction): refusing to reopen", code=NOT_REMOVABLE_CODE)
        try:
            for relative in PLAN_TREES:
                source = os.path.join(worktree, *relative.split("/"))
                if os.path.isdir(source):
                    shutil.copytree(source, os.path.join(repo_path, *relative.split("/")), dirs_exist_ok=True, symlinks=True)
            for relative in PLAN_FILES:
                source = os.path.join(worktree, *relative.split("/"))
                if os.path.isfile(source):
                    target = os.path.join(repo_path, *relative.split("/"))
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(source, target)
            thread_source = os.path.join(loop_state.loop_dir(worktree), *PLANNING_THREAD_TREE)
            if os.path.isdir(thread_source):
                shutil.copytree(thread_source, os.path.join(loop_state.loop_dir(repo_path), *PLANNING_THREAD_TREE), dirs_exist_ok=True)
            for name in KEPT_LOOP_FILE_NAMES:
                source = loop_state.loop_path(worktree, name)
                if os.path.isfile(source):
                    target = loop_state.loop_path(repo_path, name)
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    shutil.copy2(source, target)
        except (OSError, shutil.Error) as failure:
            raise RepoConflictError("the plan could not be saved into the repo before removing the worktree (%s); nothing was removed"
                                    % str(failure)[:200], code=REMOVE_FAILED_CODE) from failure

    def remove_worktrees(self, context, worktrees):
        """`git worktree remove --force` (the plan was saved; dirt was confirmed) + prune, primary last. Branches stay."""
        removed, failures = [], []
        repos = {repo.name: repo for repo in project_repos.load_repos(context.repo_path)}
        for item in sorted(worktrees, key=lambda entry: repos[entry["repo"]].primary):
            repo = repos[item["repo"]]
            if is_same_path(item["path"], repo.path):  # belt and braces: never the user's own folder
                failures.append("%s: refused, it is the repo folder" % item["repo"])
                continue
            self.git_client.run_git(repo.path, "worktree", "remove", "--force", item["path"])
            self.git_client.run_git(repo.path, "worktree", "prune")
            if os.path.isdir(item["path"]):
                failures.append("%s: git worktree remove failed, the folder is still there" % item["repo"])
                continue
            removed.append({"repo": item["repo"], "path": item["path"], "branch_kept": item["branch"]})
        return removed, failures

    @staticmethod
    def invalidate_approval(context, folders):
        for folder in dict.fromkeys([context.repo_path, *folders]):
            if not os.path.isdir(folder):
                continue
            try:
                os.remove(loop_state.loop_path(folder, loop_state.APPROVAL_NAME))
            except OSError:
                pass
