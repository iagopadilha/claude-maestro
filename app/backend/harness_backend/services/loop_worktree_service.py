"""Loop worktrees of ONE project: list (preview), push of the feature branch only, and cleanup WITH a verified backup.

A valid target: branch `loop/*`, a sibling folder of the MAIN repo with `-worktree-loop` in its name; never the main repo,
never another worktree. Clean: (1) mandatory backup in the output folder (`loop_summary.py --out` + copy of `.specs/`
without node_modules/bin/obj/.git, checked by file count); a failure aborts without deleting; (2) `git worktree remove`
without `--force` (only with `force` on a dirty worktree); (3) `git worktree prune`. The branch and its commits are never
deleted.
"""
import datetime
import json
import os
import re
import shutil
from typing import Protocol

from ..config import envload
from ..domain.project_errors import LOOP_RUNNING_CODE, InvalidRequestError, LoopRunningError, RepoConflictError
from ..infrastructure.claude_client import redact_secrets
from ..infrastructure.git_client import DEFAULT_INTEGRATION_BRANCH, INTEGRATION_BRANCH_VARIABLE, OTHER_COMPARISON_BASES

LOOP_BRANCH_PREFIX = "loop/"
LOOP_WORKTREE_FOLDER_MARK = "-worktree-loop"
FOLDERS_OUTSIDE_BACKUP = ("node_modules", "bin", "obj", ".git")
INTEGRATION_BRANCH_VARIABLES = ("LOOP_PR_BASE", INTEGRATION_BRANCH_VARIABLE)  # same order as engine/delivery.py
SCRIPT_TIMEOUT_SECONDS = 180
OUTPUT_LIMIT = 4000
PUSH_LIMIT = 500
BACKUP_ERROR_LIMIT = 200
MAX_TARGETS = 20
PR_MODE_VARIABLE = "LOOP_PR_MODE"
PR_MODE_OFF = "off"
BACKUP_STAMP_FORMAT = "%Y-%m-%d-%H%M"
BACKUP_FALLBACK_PROJECT_NAME = "project"
TIMED_OUT_TEXT = "timed out"
CLOSE_LOOP_SCRIPT = "close_loop"
CLOSE_LOOP_TIMEOUT_SECONDS = 600  # a close backs up and removes worktrees: longer than a push
CLOSE_UNAVAILABLE_CODE = "close_loop_unavailable"
CLOSE_FAILED_CODE = "close_failed"
LOOP_FINISHED_STATUS = "done"
LOOP_RUNNING_REASON = "The loop is still running; stop it before closing."


def is_same_path(first, second):
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def read_env_file_value(name):
    """Value of `name` in the app .env files (same lookup as the engine), without touching os.environ."""
    for path in envload.candidates():
        try:
            with open(path, encoding="utf-8-sig") as handle:
                value = envload.parse(handle.read()).get(name, "").strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def get_integration_branch():
    """PR target of a delivery: LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH (environment, then the app .env), else the default."""
    for name in INTEGRATION_BRANCH_VARIABLES:
        value = (os.environ.get(name) or "").strip() or read_env_file_value(name)
        if value:
            return value
    return DEFAULT_INTEGRATION_BRANCH


def extract_last_json(text):
    """Last JSON object printed in a script output (delivery.py prints it indented); {} when there is none."""
    start = text.find("{")
    while start != -1:
        try:
            value = json.loads(text[start:].strip())
            return value if isinstance(value, dict) else {}
        except ValueError:
            start = text.find("{", start + 1)
    return {}


def count_backup_files(folder):
    total = 0
    for _, subfolders, files in os.walk(folder):
        subfolders[:] = [name for name in subfolders if name not in FOLDERS_OUTSIDE_BACKUP]
        total += len(files)
    return total


class ILoopWorktreeService(Protocol):
    def list_loop_worktrees(self, context) -> list: ...
    def select_targets_from_preview(self, requested_targets, worktrees) -> tuple: ...
    def match_cited_targets(self, cited, worktrees) -> list: ...
    def push_worktree_branch(self, worktree) -> dict: ...
    def clean_worktree_with_backup(self, context, worktree, force) -> dict: ...
    def check_close_loop(self, context) -> dict: ...
    def close_loop(self, context, body) -> dict: ...


class LoopWorktreeService:
    def __init__(self, config, log, git_client, engine_script_service, loop_command_file_repository, loop_status_service=None):
        self.loop_status_service = loop_status_service  # only the Close loop routes need it (is the loop alive?)
        self.config = config
        self.log = log
        self.git_client = git_client
        self.engine_script_service = engine_script_service
        self.loop_command_file_repository = loop_command_file_repository
        self.script_timeout_seconds = SCRIPT_TIMEOUT_SECONDS

    def get_main_repo(self, context):
        """1st item of `git worktree list` from the project repo (which may be the loop worktree itself)."""
        worktrees = self.git_client.list_worktrees(context.repo_path)
        main_repo = worktrees[0]["path"] if worktrees else ""
        return os.path.normpath(main_repo) if main_repo and os.path.isdir(main_repo) else context.repo_path

    def is_loop_worktree(self, worktree, main_repo):
        path = worktree.get("path", "")
        return (worktree.get("branch", "").startswith(LOOP_BRANCH_PREFIX) and os.path.isdir(path)
                and not is_same_path(path, main_repo)
                and is_same_path(os.path.dirname(path), os.path.dirname(main_repo))
                and LOOP_WORKTREE_FOLDER_MARK in os.path.basename(path))

    def get_comparison_bases(self, worktree_path):
        """meta.json `base` (the branch the loop forked from), the integration branch, then main/master."""
        meta_base = self.loop_command_file_repository.read_meta(worktree_path).get("base")
        bases = ([meta_base] if isinstance(meta_base, str) and meta_base else []) + [get_integration_branch()] + list(OTHER_COMPARISON_BASES)
        return list(dict.fromkeys(bases))

    def list_loop_worktrees(self, context):
        """[{"path","branch","dirty","unpushed_commits","has_specs"}] of the worktrees the Act mode may touch."""
        main_repo = self.get_main_repo(context)
        found = []
        for worktree in self.git_client.list_worktrees(main_repo):
            if not self.is_loop_worktree(worktree, main_repo):
                continue
            path = worktree["path"]
            found.append({"path": os.path.normpath(path), "branch": worktree["branch"],
                          "dirty": self.git_client.has_uncommitted_changes(path),
                          "unpushed_commits": self.git_client.count_unpushed_commits(path, self.get_comparison_bases(path)),
                          "has_specs": os.path.isdir(os.path.join(path, ".specs"))})
        return found

    def select_targets_from_preview(self, requested_targets, worktrees):
        """Requested targets -> preview worktrees (by path). (list, None) or (None, error): one bad target aborts everything."""
        if (not isinstance(requested_targets, list) or len(requested_targets) > MAX_TARGETS
                or not all(isinstance(item, str) and item.strip() for item in requested_targets)):
            return None, "targets must be a list of paths (up to %d)" % MAX_TARGETS
        chosen = []
        for target in requested_targets:
            found = next((worktree for worktree in worktrees if is_same_path(worktree["path"], target)), None)
            if found is None:
                return None, "target outside the list of loop worktrees: %s" % redact_secrets(target, BACKUP_ERROR_LIMIT)
            if found not in chosen:
                chosen.append(found)
        return chosen, None

    def match_cited_targets(self, cited, worktrees):
        """Preview paths cited by the user (path, branch name or folder name); the rest is dropped."""
        return [worktree["path"] for worktree in worktrees if any(
            is_same_path(worktree["path"], item)
            or item.strip().lower() in (worktree["branch"].lower(), os.path.basename(worktree["path"]).lower())
            for item in cited if item.strip())]

    def push_worktree_branch(self, worktree):
        """`delivery.py <target>` with LOOP_PR_MODE=off: push only, no PR; delivery.py refusals still apply."""
        item = {"path": worktree["path"], "branch": worktree["branch"], "ok": False, "push": None, "output": ""}
        result = self.engine_script_service.run_engine_script_and_wait("delivery", worktree["path"], [], self.script_timeout_seconds,
                                                                       {PR_MODE_VARIABLE: PR_MODE_OFF})
        returned_json = extract_last_json(result.output)
        item["push"] = redact_secrets(returned_json["push"], PUSH_LIMIT) if isinstance(returned_json.get("push"), str) else None
        output = TIMED_OUT_TEXT if result.timed_out else (result.output + ("\n" + result.error if result.error.strip() else "")).strip()
        item["output"] = redact_secrets(output, OUTPUT_LIMIT)
        item["ok"] = (not result.timed_out) and result.code == 0
        return item

    def get_output_dir(self):
        return os.path.abspath(self.config.output_dir)

    def back_up_worktree(self, worktree):
        """Backup in <output_dir>/loop-<project>-<YYYY-MM-DD-HHMM>/ (summary + specs/), checked. (folder, None) or (folder, error)."""
        project = re.sub(r"%s.*$" % re.escape(LOOP_WORKTREE_FOLDER_MARK), "", os.path.basename(worktree["path"])) or BACKUP_FALLBACK_PROJECT_NAME
        base = os.path.join(self.get_output_dir(), "loop-%s-%s" % (project, datetime.datetime.now().strftime(BACKUP_STAMP_FORMAT)))
        folder, suffix = base, 1
        try:
            while os.path.exists(folder):
                suffix += 1
                folder = "%s-%d" % (base, suffix)
            os.makedirs(folder)
            result = self.engine_script_service.run_engine_script_and_wait("loop_summary", worktree["path"], ["--out", folder],
                                                                           self.script_timeout_seconds)
            if result.timed_out or result.code != 0:
                detail = (result.error or result.output).strip()[-BACKUP_ERROR_LIMIT:] or "no output"
                return folder, "backup failed: loop_summary.py did not finish cleanly (%s)" % redact_secrets(detail, BACKUP_ERROR_LIMIT)
            if not any(name.lower().endswith(".md") for name in os.listdir(folder)):
                return folder, "backup failed: the summary was not generated in %s" % folder
            source = os.path.join(worktree["path"], ".specs")
            if os.path.isdir(source):
                destination = os.path.join(folder, "specs")
                shutil.copytree(source, destination, ignore=shutil.ignore_patterns(*FOLDERS_OUTSIDE_BACKUP))
                source_files, destination_files = count_backup_files(source), count_backup_files(destination)
                if source_files != destination_files:
                    return folder, "backup failed: the .specs copy does not match (%d x %d files)" % (source_files, destination_files)
            return folder, None
        except (OSError, shutil.Error) as failure:
            return folder, "backup failed: %s" % redact_secrets(str(failure), BACKUP_ERROR_LIMIT)

    def clean_worktree_with_backup(self, context, worktree, force):
        item = {"path": worktree["path"], "branch": worktree["branch"], "backup": None, "deleted": False, "error": None, "ok": False}
        main_repo = self.get_main_repo(context)
        if is_same_path(worktree["path"], main_repo):  # belt and braces: never the main repo
            item["error"] = "refused: it is the main repo"
            return item
        if worktree["dirty"] and not force:
            item["error"] = "worktree with uncommitted changes: confirm again with force:true to delete it anyway"
            return item
        backup_dir, error = self.back_up_worktree(worktree)
        item["backup"] = backup_dir
        if error:
            item["error"] = error + " (nothing was deleted)"
            return item
        args = ["worktree", "remove"] + (["--force"] if (worktree["dirty"] and force) else []) + [worktree["path"]]
        code, _, _ = self.git_client.run_git(main_repo, *args)
        if code != 0 and os.path.isdir(worktree["path"]):
            item["error"] = "git worktree remove failed (the folder is still there; the backup is in %s)" % backup_dir
            return item
        self.git_client.run_git(main_repo, "worktree", "prune")
        deleted = not os.path.isdir(worktree["path"])
        item["deleted"], item["ok"] = deleted, deleted
        if not deleted:
            item["error"] = "the folder is still there after git worktree remove"
        self.log.info("loop_worktree_deleted" if deleted else "loop_worktree_not_deleted", project=context.project.id,
                      branch=worktree["branch"], backup=backup_dir)
        return item

    # ------------------------------------------------ Close loop (the routine itself is `engine/close_loop.py`)
    def check_close_loop(self, context):
        """`GET .../close-loop/check`: read only. Per-repo readiness comes from the engine routine; this adds the loop state."""
        returned = self.run_close_loop_routine(context, ["--check"])
        running = self.is_loop_running(context)
        repos = [self.complete_repo_readiness(item, running) for item in returned.get("repos") or [] if isinstance(item, dict)]
        return {"ok": True, "can_close": bool(repos) and all(item["can_close"] for item in repos), "loop_finished": self.is_loop_finished(context),
                "repos": repos}

    def close_loop(self, context, body):
        """`POST .../close-loop`: one failing repo never stops the others; the per-repo outcome is in the body."""
        confirm = body.get("confirm_compare_link", False)
        names = body.get("repos")
        if not isinstance(confirm, bool):
            raise InvalidRequestError("confirm_compare_link must be true or false")
        if names is not None and (not isinstance(names, list) or not all(isinstance(name, str) and name.strip() for name in names)):
            raise InvalidRequestError("repos must be a list of repo names")
        if self.is_loop_running(context):
            raise LoopRunningError("the loop is running: stop it before closing", code=LOOP_RUNNING_CODE)
        arguments = ["--close", "--out", self.get_output_dir()] + (["--confirm-compare-link"] if confirm else [])
        arguments += ["--repos", ",".join(name.strip() for name in names)] if names else []
        returned = self.run_close_loop_routine(context, arguments)
        repos = returned.get("repos") if isinstance(returned.get("repos"), dict) else {}
        result = dict(returned, ok=True, repos=repos)
        result.setdefault("closed", [name for name, item in repos.items() if isinstance(item, dict) and item.get("status") == "closed"])
        result.setdefault("kept", [name for name, item in repos.items() if isinstance(item, dict) and item.get("status") != "closed"])
        self.log.info("close_loop_done", project=context.project.id, closed=len(result["closed"]), kept=len(result["kept"]))
        return result

    def run_close_loop_routine(self, context, arguments):
        """Thin adapter over the engine routine (Track A2, `engine/close_loop.py`): `python close_loop.py <primary repo> <arguments>`
        prints ONE JSON object last in stdout. Contract expected from it: `--check` -> {"repos": [...]} with the per-repo items of
        the close-loop/check payload; `--close` -> the body of `POST close-loop` without `ok`."""
        if not os.path.isfile(self.config.engine_script_path(CLOSE_LOOP_SCRIPT)):
            raise RepoConflictError("the close routine (engine/close_loop.py) is not available in this version", code=CLOSE_UNAVAILABLE_CODE)
        result = self.engine_script_service.run_engine_script_and_wait(CLOSE_LOOP_SCRIPT, context.repo_path, arguments,
                                                                       CLOSE_LOOP_TIMEOUT_SECONDS)
        returned = extract_last_json(result.output)
        if result.timed_out or not returned:
            detail = TIMED_OUT_TEXT if result.timed_out else (result.error or result.output).strip()[-BACKUP_ERROR_LIMIT:] or "no output"
            raise RepoConflictError("close routine failed: %s" % redact_secrets(detail, BACKUP_ERROR_LIMIT), code=CLOSE_FAILED_CODE)
        return returned

    def is_loop_running(self, context):
        return bool(self.loop_status_service and self.loop_status_service.is_loop_running(context))

    def is_loop_finished(self, context):
        if self.loop_status_service is None:
            return False
        return self.loop_status_service.get_loop_summary(context)["status"] == LOOP_FINISHED_STATUS

    @staticmethod
    def complete_repo_readiness(item, running):
        """Normalizes one engine item to the contract and adds the `loop_running` reason while the loop is alive."""
        reasons = [reason for reason in item.get("reasons") or [] if isinstance(reason, dict)]
        if running and not any(reason.get("code") == LOOP_RUNNING_CODE for reason in reasons):
            reasons.insert(0, {"code": LOOP_RUNNING_CODE, "text": LOOP_RUNNING_REASON})
        only_pr_missing = bool(reasons) and all(reason.get("code") == "pr_missing" for reason in reasons)
        return dict(item, can_close=bool(item.get("can_close")) and not reasons, reasons=reasons,
                    needs_confirm=bool(item.get("needs_confirm")) and only_pr_missing)
