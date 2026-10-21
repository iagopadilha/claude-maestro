"""Commit setup files and push (BR-03): commits ONLY the setup files of one repo (maps, app CLAUDE.md blocks, `.specs` plan files:
engine/setup_files.py) on the folder's current branch and pushes that branch to its upstream.

Refused (409, `reason` + `code`): detached HEAD, `main`/`master`, no upstream, a loop or a job running, nothing to commit. 400 when
`confirm_branch` is not the current branch (the user must confirm the branch the dialog named). Nothing else in the folder is staged
or committed; the push never forces and never goes to another branch.
"""
import os
import sys

from ..domain.project_errors import InvalidRepoError, RepoConflictError
from ..infrastructure.git_client import DEFAULT_GIT_USER_EMAIL, DEFAULT_GIT_USER_NAME

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import setup_files  # noqa: E402
import subprojects  # noqa: E402

PROTECTED_BRANCHES = ("main", "master")
DETACHED_CODE, PROTECTED_CODE, NO_UPSTREAM_CODE = "detached_head", "protected_branch", "no_upstream"
BUSY_CODE, NOTHING_CODE, PUSH_FAILED_CODE, COMMIT_FAILED_CODE = "busy", "nothing_to_commit", "push_failed", "commit_failed"


class SetupCommitService:
    def __init__(self, repo_registry_service, loop_status_service, claude_job_service, git_client, log):
        self.repo_registry_service = repo_registry_service
        self.loop_status_service = loop_status_service
        self.claude_job_service = claude_job_service
        self.git_client = git_client
        self.log = log

    @staticmethod
    def refuse(reason, code, **extras):
        return RepoConflictError(reason, code=code, extras=dict(extras, reason=reason))

    def commit_setup(self, context, repo_name, confirm_branch):
        """-> {"ok", "commit", "pushed_to", "files", "branch"}; see the module text for the refusals."""
        repo = self.repo_registry_service.resolve(context, repo_name)
        if not os.path.isdir(repo.path):
            raise InvalidRepoError("the repo folder does not exist: %s" % repo.path)
        branch = self.git_client.get_current_branch(repo.path)
        if not branch:
            raise self.refuse("the folder is on a detached HEAD: check out a branch first", DETACHED_CODE)
        if branch.lower() in PROTECTED_BRANCHES:
            raise self.refuse("`%s` is a protected branch: the app never commits or pushes there. Switch to your working branch (for example developer)" % branch,
                              PROTECTED_CODE, branch=branch)
        if not isinstance(confirm_branch, str) or confirm_branch.strip() != branch:
            raise InvalidRepoError("confirm_branch must be the current branch `%s`" % branch, extras={"branch": branch})
        remote, upstream_branch, upstream = self.read_upstream(repo.path, branch)
        if not upstream:
            raise self.refuse("`%s` has no upstream branch: set one first (git push -u origin %s)" % (branch, branch), NO_UPSTREAM_CODE, branch=branch)
        if self.loop_status_service.is_loop_running(context) or self.claude_job_service.get_running_job(context.repo_path):
            raise self.refuse("a loop or a job is running in this project: wait for it to finish", BUSY_CODE)
        entry = subprojects.load(context.repo_path, repo.name)["entry"]
        paths = [item["path"] for item in setup_files.list_setup_files(repo.path, entry)]
        if not paths:
            raise self.refuse("there are no uncommitted setup files in %s" % repo.name, NOTHING_CODE)
        self.commit_paths(repo.path, paths)
        commit = self.git_client.get_short_head(repo.path)
        code, _, error = self.git_client.run_git(repo.path, "push", remote, "HEAD:refs/heads/%s" % upstream_branch)
        if code != 0:
            raise self.refuse("the commit %s was made but the push failed: %s" % (commit, (error.splitlines() or [str(code)])[-1]), PUSH_FAILED_CODE,
                              committed=commit, branch=branch)
        self.log.info("setup_commit_pushed", project=context.project.id, repo=repo.name, branch=branch, files=len(paths))
        return {"ok": True, "repo": repo.name, "branch": branch, "commit": commit, "pushed_to": upstream, "files": paths}

    def read_upstream(self, folder, branch):
        """(remote, branch name at the remote, "remote/branch" text); ("", "", "") when the branch tracks nothing."""
        remote = self.git_client.get_config(folder, "branch.%s.remote" % branch)
        merge = self.git_client.get_config(folder, "branch.%s.merge" % branch)
        if not remote or not merge.startswith("refs/heads/"):
            return "", "", ""
        name = merge[len("refs/heads/"):]
        return remote, name, "%s/%s" % (remote, name)

    def commit_paths(self, folder, paths):
        """Stages and commits exactly `paths` (`commit -- paths` ignores anything else already staged)."""
        code, _, error = self.git_client.run_git(folder, "add", "--", *paths)
        if code != 0:
            raise self.refuse("git add failed: %s" % (error or code), COMMIT_FAILED_CODE)
        name = self.git_client.get_config(folder, "user.name") or DEFAULT_GIT_USER_NAME
        email = self.git_client.get_config(folder, "user.email") or DEFAULT_GIT_USER_EMAIL
        code, _, error = self.git_client.run_git(folder, "-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-m",
                                                 setup_files.COMMIT_MESSAGE, "--", *paths)
        if code != 0:
            raise self.refuse("git commit failed: %s" % (error or code), COMMIT_FAILED_CODE)
