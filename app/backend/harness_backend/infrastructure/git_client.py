"""Git client (reads the repo state and creates a new repo). No shell; everything through ProcessRunner; never raises.

Never pushes on its own nor touches a protected branch: the loop's push and PR belong to `engine/delivery.py`. The only push here is
`commit_all_and_push`, run on the user's click for the user's own folder and current branch.
"""
import os
import shutil
import time
from typing import Protocol

GIT_BINARY_NAME = "git"
GIT_TIMEOUT_SECONDS = 30
WORKTREE_REMOVE_TIMEOUT_SECONDS = 300  # a worktree with node_modules/bin/obj takes far longer than a status call to delete
INTEGRATION_BRANCH_VARIABLE = "LOOP_INTEGRATION_BRANCH"
DEFAULT_INTEGRATION_BRANCH = "developer"
OTHER_COMPARISON_BASES = ("main", "master")
WORKTREE_LINE_PREFIX = "worktree "
BRANCH_LINE_PREFIX = "branch "
BRANCH_REF_PREFIX = "refs/heads/"
DEFAULT_GIT_USER_NAME = "loop-harness"
DEFAULT_GIT_USER_EMAIL = "loop-harness@localhost"
NO_REPLY_EMAIL_DOMAIN = "users.noreply.github.com"
RENAME_STATUSES = ("R", "C")
UNTRACKED_STATUS = "?"
ADDED_STATUS = "A"
NUL_SEPARATOR = chr(0)
LEFTOVER_FOLDER_RETRIES = 5
LEFTOVER_FOLDER_RETRY_SECONDS = 0.4


def same_path(first, second):
    return os.path.normcase(os.path.abspath(first)) == os.path.normcase(os.path.abspath(second))


def remove_emptied_folder(folder):
    """Deletes what `git worktree remove` left of an unregistered worktree (the folder, plus any untracked rest), retrying a few
    times because Windows releases file handles late. Never follows a link. True when the folder is gone."""
    for attempt in range(LEFTOVER_FOLDER_RETRIES):
        if not os.path.lexists(folder):
            return True
        is_junction = getattr(os.path, "isjunction", lambda _: False)
        try:
            if os.path.islink(folder) or is_junction(folder):
                os.unlink(folder)  # a link pointing elsewhere: drop the link, never what it points to
            else:
                shutil.rmtree(folder)
        except OSError:
            time.sleep(LEFTOVER_FOLDER_RETRY_SECONDS * (attempt + 1))
    return not os.path.lexists(folder)


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
    def remove_worktree_and_branch(self, main_repo, worktree_path, branch) -> dict: ...
    def list_status_entries(self, folder): ...
    def discard_path_changes(self, folder, entry) -> tuple: ...
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

    def remove_worktree_and_branch(self, main_repo, worktree_path, branch):
        """`git worktree remove --force <path>`, `git worktree prune`, then `git branch -D <branch>` (when `branch` is not empty), all run from
        the MAIN repo. No merge, no commit, no push: what was in the worktree and not pushed is lost. The caller decides WHAT may be removed.
        Returns {"worktree_removed", "branch_deleted", "folder_left", "error"}. "Removed" = git no longer registers the worktree (on
        Windows `worktree remove` can empty the folder and still fail to delete the folder itself while a program holds it open): the
        branch is deleted then too, and an emptied folder that is left is deleted with retries (`folder_left` when it is still there)."""
        result = {"worktree_removed": False, "branch_deleted": False, "folder_left": False, "error": ""}
        code, _, error = self.process_runner.run_and_wait(
            [self.git_binary, "-C", main_repo, "worktree", "remove", "--force", worktree_path], main_repo, WORKTREE_REMOVE_TIMEOUT_SECONDS)
        self.run_git(main_repo, "worktree", "prune")
        still_registered = any(same_path(item["path"], worktree_path) for item in self.list_worktrees(main_repo))
        if still_registered:
            result["error"] = "git worktree remove failed: %s" % ((error or "").strip().splitlines()[-1] if (error or "").strip() else code)
            return result
        result["worktree_removed"] = True
        result["folder_left"] = not remove_emptied_folder(worktree_path)
        if branch:
            code, _, error = self.run_git(main_repo, "branch", "-D", branch)
            result["branch_deleted"] = code == 0
            if code != 0:
                result["error"] = "git branch -D failed: %s" % (error.splitlines()[-1] if error else code)
        if result["folder_left"] and not result["error"]:
            result["error"] = "the worktree is gone but its folder is still there (a program keeps it open): close it and delete %s by hand" % worktree_path
        return result

    def list_status_entries(self, folder):
        """[{"index", "worktree", "path"}] of `git status --porcelain -z -uall` (every untracked file listed on its own, paths with `/`).
        The output is NOT stripped (the first column of a line is a blank for an unstaged change). None when git fails."""
        code, output, _ = self.process_runner.run_and_wait([self.git_binary, "-c", "core.quotepath=false", "-C", folder, "status",
                                                            "--porcelain", "-z", "-uall"], folder, GIT_TIMEOUT_SECONDS)
        if code != 0:
            return None
        entries, tokens, position = [], (output or "").split(NUL_SEPARATOR), 0
        while position < len(tokens):
            token = tokens[position]
            position += 1
            if len(token) < 4:
                continue
            index_status, worktree_status = token[0], token[1]
            if index_status in RENAME_STATUSES or worktree_status in RENAME_STATUSES:
                position += 1  # a rename/copy entry carries the original path as the next token
            entries.append({"index": index_status, "worktree": worktree_status, "path": token[3:]})
        return entries

    def discard_path_changes(self, folder, entry):
        """Throws away the changes of ONE entry of `list_status_entries`: an untracked file is deleted, a file added to the index is removed
        from index and disk, anything else goes back to HEAD (index and working tree). Returns (ok, error). The path must stay inside `folder`."""
        root = os.path.realpath(folder)
        target = os.path.realpath(os.path.join(root, entry["path"]))
        if os.path.commonpath([root, target]) != root or target == root:
            return False, "path outside the work folder"
        if entry["index"] == UNTRACKED_STATUS:
            try:
                os.remove(target)
                return True, ""
            except OSError as failure:
                return False, str(failure)
        arguments = ["rm", "-f", "--", entry["path"]] if entry["index"] == ADDED_STATUS else [
            "restore", "--source=HEAD", "--staged", "--worktree", "--", entry["path"]]
        code, _, error = self.run_git(folder, *arguments)
        return code == 0, error

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
        if branch.lower() in ("main", "master"):
            result["error"] = "`%s` is a protected branch: nothing is committed or pushed there" % branch
            return result
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
