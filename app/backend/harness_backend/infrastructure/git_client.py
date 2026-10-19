"""Git client (reads the repo state and creates a new repo). No shell; everything through ProcessRunner; never raises.

Never pushes on its own nor touches a protected branch: the loop's push and PR belong to `engine/delivery.py`. The only push here is
`commit_all_and_push`, run on the user's click for the user's own folder and current branch.
"""
import os
from typing import Protocol

GIT_BINARY_NAME = "git"
GIT_TIMEOUT_SECONDS = 30
INTEGRATION_BRANCH_VARIABLE = "LOOP_INTEGRATION_BRANCH"
DEFAULT_INTEGRATION_BRANCH = "developer"
OTHER_COMPARISON_BASES = ("main", "master")
WORKTREE_LINE_PREFIX = "worktree "
BRANCH_LINE_PREFIX = "branch "
BRANCH_REF_PREFIX = "refs/heads/"
DEFAULT_GIT_USER_NAME = "loop-harness"
DEFAULT_GIT_USER_EMAIL = "loop-harness@localhost"
NO_REPLY_EMAIL_DOMAIN = "users.noreply.github.com"


def default_comparison_bases():
    """Integration branch (LOOP_INTEGRATION_BRANCH, default `developer`) and then main/master."""
    integration = (os.environ.get(INTEGRATION_BRANCH_VARIABLE) or DEFAULT_INTEGRATION_BRANCH).strip()
    return (integration,) + OTHER_COMPARISON_BASES


class IGitClient(Protocol):
    def is_git_repo(self, folder) -> bool: ...
    def get_current_branch(self, folder) -> str: ...
    def get_short_head(self, folder) -> str: ...
    def list_worktrees(self, folder) -> list: ...
    def count_unpushed_commits(self, folder, bases=None): ...
    def has_uncommitted_changes(self, folder, include_untracked=True) -> bool: ...
    def commit_all_and_push(self, folder, message, push=True) -> dict: ...
    def init_repo_with_initial_commit(self, folder, message, user="") -> tuple: ...


class GitClient:
    def __init__(self, process_runner, git_binary=GIT_BINARY_NAME):
        self.process_runner = process_runner
        self.git_binary = git_binary

    def run_git(self, folder, *args):
        """(code, output stripped, error) of `git -C folder ...`."""
        code, output, error = self.process_runner.run_and_wait([self.git_binary, "-C", folder] + list(args), folder, GIT_TIMEOUT_SECONDS)
        return code, (output or "").strip(), (error or "").strip()

    def is_git_repo(self, folder):
        """True if `folder` is inside a git work tree (repo or worktree)."""
        if not folder or not os.path.isdir(folder):
            return False
        code, output, _ = self.run_git(folder, "rev-parse", "--is-inside-work-tree")
        return code == 0 and output == "true"

    def get_short_head(self, folder):
        """Short sha of HEAD; '' when it cannot be read (no commit, not a repo)."""
        code, output, _ = self.run_git(folder, "rev-parse", "--short", "HEAD")
        return output if code == 0 else ""

    def get_current_branch(self, folder):
        """Current branch name; '' with a detached HEAD or outside a repo."""
        code, output, _ = self.run_git(folder, "branch", "--show-current")
        return output if code == 0 else ""

    def list_worktrees(self, folder):
        """`git worktree list --porcelain` -> [{"path", "branch", "head", "is_main": bool}]. The 1st item is the main repo.
        `branch` comes without `refs/heads/` ('' with a detached HEAD). Outside a repo = []."""
        code, output, _ = self.run_git(folder, "worktree", "list", "--porcelain")
        if code != 0:
            return []
        worktrees, current = [], {}
        for line in output.splitlines() + [""]:
            if line.startswith(WORKTREE_LINE_PREFIX):
                current = {"path": os.path.normpath(line[len(WORKTREE_LINE_PREFIX):].strip()), "branch": "", "head": ""}
            elif line.startswith(BRANCH_LINE_PREFIX) and current:
                reference = line[len(BRANCH_LINE_PREFIX):].strip()
                current["branch"] = reference[len(BRANCH_REF_PREFIX):] if reference.startswith(BRANCH_REF_PREFIX) else reference
            elif line.startswith("HEAD ") and current:
                current["head"] = line[len("HEAD "):].strip()
            elif not line.strip() and current:
                current["is_main"] = not worktrees
                worktrees.append(current)
                current = {}
        return worktrees

    def count_unpushed_commits(self, folder, bases=None):
        """HEAD commits not pushed yet: against the upstream; without an upstream, against `origin/<base>` or `<base>` (in the
        order of `bases`, default: the integration branch, main, master). Returns an int, or None when there is no reference."""
        code, upstream, _ = self.run_git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
        references = [upstream] if code == 0 and upstream else []
        if not references:
            for base in dict.fromkeys(base for base in (bases or default_comparison_bases()) if isinstance(base, str) and base):
                references += ["origin/" + base, base]
        for reference in references:
            code, count, _ = self.run_git(folder, "rev-list", "--count", "%s..HEAD" % reference)
            if code == 0 and count.isdigit():
                return int(count)
        return None

    def has_uncommitted_changes(self, folder, include_untracked=True):
        """True if `git status --porcelain` has any line (with `include_untracked=False`, only tracked files)."""
        args = ["status", "--porcelain"] + ([] if include_untracked else ["--untracked-files=no"])
        code, output, _ = self.run_git(folder, *args)
        return code == 0 and bool(output)

    def get_config(self, folder, key):
        code, output, _ = self.run_git(folder, "config", key)
        return output if code == 0 else ""

    def commit_all_and_push(self, folder, message, push=True):
        """`git add -A` + commit (when there is something staged) + push of the CURRENT branch. Only called from a user click.

        Pushes to the upstream when one is set, else `git push -u origin <branch>`. Never forces, never switches branch, never touches
        another branch. Returns {"ok", "branch", "committed", "pushed", "commit", "error"}; `ok` is False at the first failing step
        (a commit that succeeded stays: the error then names the push)."""
        result = {"ok": False, "branch": "", "committed": False, "pushed": False, "commit": "", "error": ""}
        branch = self.get_current_branch(folder)
        if not branch:
            result["error"] = "the folder has no current branch (detached HEAD): check out a branch first"
            return result
        result["branch"] = branch
        code, _, error = self.run_git(folder, "add", "-A")
        if code != 0:
            result["error"] = "git add failed: %s" % (error or code)
            return result
        code, staged, _ = self.run_git(folder, "diff", "--cached", "--name-only")
        if code == 0 and staged:
            name = self.get_config(folder, "user.name") or DEFAULT_GIT_USER_NAME
            email = self.get_config(folder, "user.email") or DEFAULT_GIT_USER_EMAIL
            code, _, error = self.run_git(folder, "-c", "user.name=" + name, "-c", "user.email=" + email, "commit", "-m", message)
            if code != 0:
                result["error"] = "git commit failed: %s" % (error or code)
                return result
            result["committed"], result["commit"] = True, self.get_short_head(folder)
        if not push:
            result["ok"] = True
            return result
        has_upstream = self.run_git(folder, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")[0] == 0
        code, _, error = self.run_git(folder, *(["push"] if has_upstream else ["push", "-u", "origin", branch]))
        if code != 0:
            result["error"] = "git push failed: %s" % (error.splitlines()[-1] if error else code)
            return result
        result["pushed"], result["ok"] = True, True
        return result

    def init_repo_with_initial_commit(self, folder, message, user=""):
        """`git init` + `git add -A` + initial commit (`--allow-empty`: an empty folder also gets a commit). Returns (ok, error).

        Commit identity: `user` (when given) becomes user.name only for this commit (email = the configured one or
        `<user>@users.noreply.github.com`); without `user` and without a configured identity, it uses `loop-harness`.
        Nothing is written to the git config. A folder that already is a repo: no re-init, just (True, '') without a commit.
        """
        if not folder or not os.path.isdir(folder):
            return False, "folder does not exist: %s" % folder
        if self.is_git_repo(folder) and os.path.exists(os.path.join(folder, ".git")):
            return True, ""
        code, _, error = self.run_git(folder, "init")
        if code != 0:
            return False, "git init failed: %s" % (error or code)
        code, _, error = self.run_git(folder, "add", "-A")
        if code != 0:
            return False, "git add failed: %s" % (error or code)
        name = user or self.get_config(folder, "user.name") or DEFAULT_GIT_USER_NAME
        email = self.get_config(folder, "user.email") or ("%s@%s" % (user, NO_REPLY_EMAIL_DOMAIN) if user else DEFAULT_GIT_USER_EMAIL)
        code, _, error = self.run_git(folder, "-c", "user.name=" + name, "-c", "user.email=" + email,
                                      "commit", "--allow-empty", "-m", message)
        if code != 0:
            return False, "git commit failed: %s" % (error or code)
        return True, ""
