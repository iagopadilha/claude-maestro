"""Wrap-up of a finished loop (`GET wrap-up`) and Finish (`POST wrap-up/finish`).

Finish never invents a delivery or a close: it orchestrates the two existing pieces. Delivery = `engine/delivery.py` through the engine
script service (the way the app runs it today, push of the `loop/*` branch + PR, never a merge). Close = `LoopWorktreeService.close_loop`
(`engine/close_loop.py`: verified backup, plan back to the repository, `last-run.json`, `git worktree remove`).

Modes of `finish`:
- `safe`: delivery when pending (and the run asked for it), then close WITHOUT force, only when the run asked to remove the worktree
  (`meta.delivery.remove_worktree_on_finish`, on for runs that predate it). Anything that would be lost (uncommitted files, unpushed
  commits, a push nobody confirmed) refuses with 409 and a reason per repo; nothing is removed. A repo with only a compare link (no PR
  could be created) is closed: choosing Finish is the confirmation.
- `keep_worktree`: delivery only, the worktree stays.
- `remove_anyway`: needs `confirm_text` equal to the repo folder name; no delivery; the verified backup still runs, then the worktree goes
  with `git worktree remove --force`. The local branch and its commits stay.
Unreviewed critical decisions block every mode unless `accept_unreviewed` is true. A running loop or a running follow-up job refuses.
"""
import os
from typing import Protocol

from ..domain.project_errors import LOOP_RUNNING_CODE, InvalidRequestError, LoopRunningError, RepoConflictError
from ..repositories.specs_file_repository import loop_state
from .loop_panel_service import effective_cost_cap, read_command_limits
from .loop_worktree_service import extract_last_json, get_context_integration_branch

MODE_SAFE = "safe"
MODE_KEEP_WORKTREE = "keep_worktree"
MODE_REMOVE_ANYWAY = "remove_anyway"
FINISH_MODES = (MODE_SAFE, MODE_KEEP_WORKTREE, MODE_REMOVE_ANYWAY)
FOLLOW_UP_JOB_TYPE = "follow_up"
DELIVERY_SCRIPT = "delivery"
DELIVERY_TIMEOUT_SECONDS = 300
PR_CONFIRMED = ("PR created", "PR already existed")
COMPARE_LINK_PREFIX = "compare link"
PUSH_DONE_PREFIX = "done"
VERIFIED_STATUS = "verified"
WRAP_UP_BLOCKED_CODE = "wrap_up_blocked"
NO_WORKTREE_CODE = "no_worktree"
FOLLOW_UP_RUNNING_CODE = "follow_up_running"
FOLLOW_UP_RUNNING_MESSAGE = "a follow-up round is running: wait for it to finish"
LISTED_FILES_LIMIT = 50
LISTED_COMMITS_LIMIT = 20
RESULT_TEXT_LIMIT = 300
REASON_KIND_UNCOMMITTED = "uncommitted"
REASON_KIND_UNPUSHED = "unpushed"
REASON_KIND_BY_CODE = {"tree_dirty": REASON_KIND_UNCOMMITTED, "push_unconfirmed": REASON_KIND_UNPUSHED}


class IWrapUpService(Protocol):
    def get_wrap_up(self, context) -> dict: ...
    def finish(self, context, body) -> dict: ...


def to_count(value):
    """`unreviewed_critical` of the decisions service may be a list of items or a number."""
    if isinstance(value, (list, tuple)):
        return len(value)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def is_push_done(result):
    """True when the delivery record (aggregate with `repos`, or a single repo's) says every push is done."""
    repos = result.get("repos")
    if isinstance(repos, dict) and repos:
        return all(str(item.get("push") or "").startswith(PUSH_DONE_PREFIX) for item in repos.values() if isinstance(item, dict))
    return str(result.get("push") or "").startswith(PUSH_DONE_PREFIX)


def is_delivery_done(delivery_record, checked_repos):
    """Delivery is done when the record says every push is done, or when the close check confirmed every repo's branch on the remote
    (the remote holds the worktree HEAD): a remote with no PR and no compare link still counts as delivered."""
    return is_push_done(delivery_record) or (bool(checked_repos) and all(item.get("pushed") is True for item in checked_repos))


class WrapUpService:
    def __init__(self, loop_worktree_service, loop_status_service, loop_file_repository, loop_command_file_repository,
                 engine_script_service, decisions_service, claude_job_service, git_client, log, loop_control_service=None):
        self.loop_worktree_service = loop_worktree_service
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.loop_command_file_repository = loop_command_file_repository
        self.engine_script_service = engine_script_service
        self.decisions_service = decisions_service  # open_counts(context) and unreviewed_critical(context)
        self.claude_job_service = claude_job_service
        self.git_client = git_client
        self.log = log
        self.loop_control_service = loop_control_service  # read_delivery(context) -> {push_pr, remove_worktree_on_finish}; meta alone without it
        self.delivery_timeout_seconds = DELIVERY_TIMEOUT_SECONDS

    # ------------------------------------------------ read
    def get_wrap_up(self, context):
        summary = self.loop_status_service.get_loop_summary(context)
        meta = summary.get("meta") or {}
        delivery_settings = self.read_delivery_settings(context, meta)
        delivery_record = self.read_delivery_record(context)
        check = self.read_close_check(context)
        return {"status": summary["status"], "summary": self.build_summary(context, meta, delivery_record),
                "delivery": dict(delivery_settings, done=is_delivery_done(delivery_record, check["repos"]), integration_branch=get_context_integration_branch(context)),
                "check": check,
                "unreviewed_critical": to_count(self.decisions_service.unreviewed_critical(context))}

    def read_delivery_settings(self, context, meta):
        """The run's delivery choices from the loop control (meta.delivery, the pending file, then on/on); a run that predates them keeps
        today's behavior (deliver and remove)."""
        reader = getattr(self.loop_control_service, "read_delivery", None)
        saved = reader(context) if reader else meta.get("delivery")
        saved = saved if isinstance(saved, dict) else {}
        return {"push_pr": saved["push_pr"] if isinstance(saved.get("push_pr"), bool) else True,
                "remove_worktree_on_finish": saved["remove_worktree_on_finish"] if isinstance(saved.get("remove_worktree_on_finish"), bool) else True}

    def read_delivery_record(self, context):
        record = self.loop_file_repository.read_json(loop_state.loop_path(context.work_dir, "delivery.json"))
        return record if record else {}

    def build_summary(self, context, meta, delivery_record):
        features, _ = loop_state.parse_roadmap(self.loop_file_repository.read_roadmap(context.work_dir))
        link = delivery_record.get("pr") or meta.get("pr")
        pr_status = str(delivery_record.get("pr_status") or meta.get("pr_status") or "")
        return {"features_done": sum(1 for fields in features.values() if fields.get("status") == VERIFIED_STATUS),
                "features_total": len(features),
                "pr_url": link if link and pr_status in PR_CONFIRMED else None,
                "compare_url": link if link and pr_status.startswith(COMPARE_LINK_PREFIX) else None,
                "cost_usd": meta.get("cost_usd"), "max_cost_usd": self.read_effective_cap(context, meta), "turns": meta.get("turns")}

    def read_effective_cap(self, context, meta):
        """meta.max_cost_usd (raised in the panel), else the saved command's --max-cost, else 50: the same rule as the panel's cost meter."""
        command = self.loop_command_file_repository.read_loop_command(context.work_dir)
        return effective_cost_cap(meta, read_command_limits(command))

    def read_close_check(self, context):
        """The close check per repo; a project with no worktree has nothing to check (empty list)."""
        if not os.path.isdir(context.project.loop_worktree_path()):
            return {"repos": []}
        try:
            return {"repos": [self.describe_checked_repo(context, item) for item in self.loop_worktree_service.check_close_loop(context)["repos"]]}
        except RepoConflictError as failure:  # the close routine is not available: the wrap-up still shows the rest
            self.log.warn("wrap_up_check_unavailable", project=context.project.id, message=str(failure)[:RESULT_TEXT_LIMIT])
            return {"repos": []}

    @staticmethod
    def blocking_reasons(item):
        """Check reasons that stop a safe finish: everything except a missing PR (the push itself is checked by `push_unconfirmed`)."""
        return [reason for reason in item.get("reasons") or [] if reason.get("code") != "pr_missing"]

    def describe_checked_repo(self, context, item):
        """The engine item plus the keys the panel reads: repo, pushed, uncommitted (files), unpushed (commits), pr_url, compare_url."""
        codes = [reason.get("code") for reason in item.get("reasons") or []]
        worktree = self.find_worktree(context, item["name"]) if os.path.isdir(context.project.loop_worktree_path()) else ""
        link, pr_status = item.get("pr"), str(item.get("pr_status") or "")
        notes = [{"kind": "pr_missing", "blocking": False, "message": "%s: %s" % (item["name"], reason.get("text"))}
                 for reason in item.get("reasons") or [] if reason.get("code") == "pr_missing"]
        return dict(item, notes=notes, repo=item["name"], pushed="push_unconfirmed" not in codes,
                    uncommitted=self.list_uncommitted_files(worktree) if "tree_dirty" in codes else [],
                    unpushed=self.list_unpushed_commits(worktree, item) if "push_unconfirmed" in codes else [],
                    pr_url=link if link and pr_status in PR_CONFIRMED else None,
                    compare_url=link if link and pr_status.startswith(COMPARE_LINK_PREFIX) else None)

    # ------------------------------------------------ finish
    def finish(self, context, body):
        mode, confirm_text, accept_unreviewed = self.validate_body(body)
        self.ensure_can_finish(context, mode, confirm_text, accept_unreviewed)
        summary = self.loop_status_service.get_loop_summary(context)
        meta = summary.get("meta") or {}
        settings = self.read_delivery_settings(context, meta)
        before = self.read_close_check(context)["repos"]  # the primary comes first: the receipt names its branch
        if mode != MODE_REMOVE_ANYWAY and settings["push_pr"]:
            self.deliver_when_pending(context, before)
        delivery_record = self.read_delivery_record(context)
        receipt = self.build_receipt(context, meta, delivery_record, before)
        if mode == MODE_REMOVE_ANYWAY:
            closed = self.close_worktrees(context, {"force_remove": True, "confirm_compare_link": True, "finished": True})
        elif mode == MODE_SAFE and settings["remove_worktree_on_finish"]:
            closed = self.close_after_check(context)
        else:
            closed = None
        if closed is not None:
            receipt.update(backup_dir=closed.get("backup"), closed=closed["closed"], kept=closed["kept"], worktree_removed=bool(closed["closed"]))
        self.log.info("wrap_up_finished", project=context.project.id, mode=mode, removed=receipt["worktree_removed"])
        return {"ok": True, "mode": mode, "receipt": receipt}

    @staticmethod
    def validate_body(body):
        body = body if isinstance(body, dict) else {}
        mode = body.get("mode")
        if mode not in FINISH_MODES:
            raise InvalidRequestError("mode must be one of: %s" % ", ".join(FINISH_MODES))
        confirm_text, accept_unreviewed = body.get("confirm_text"), body.get("accept_unreviewed", False)
        if confirm_text is not None and not isinstance(confirm_text, str):
            raise InvalidRequestError("confirm_text must be text")
        if not isinstance(accept_unreviewed, bool):
            raise InvalidRequestError("accept_unreviewed must be true or false")
        return mode, (confirm_text or "").strip(), accept_unreviewed

    def ensure_can_finish(self, context, mode, confirm_text, accept_unreviewed):
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("the loop is running: wait for it to stop before finishing", code=LOOP_RUNNING_CODE)
        running_job = self.claude_job_service.get_running_job(context.repo_path)
        if running_job and running_job.get("type") == FOLLOW_UP_JOB_TYPE:
            raise RepoConflictError(FOLLOW_UP_RUNNING_MESSAGE, code=FOLLOW_UP_RUNNING_CODE)
        if not os.path.isdir(context.project.loop_worktree_path()):
            raise RepoConflictError("there is no loop worktree to finish", code=NO_WORKTREE_CODE)
        folder_name = os.path.basename(os.path.normpath(context.repo_path))
        if mode == MODE_REMOVE_ANYWAY and confirm_text != folder_name:
            raise InvalidRequestError("type the repository folder name (%s) to confirm removing the worktree anyway" % folder_name)
        unreviewed = self.decisions_service.unreviewed_critical(context)
        if to_count(unreviewed) and not accept_unreviewed:
            reason = {"repo": None, "kind": "unreviewed_critical", "files": [], "commits": [], "decisions": [item.get("id") for item in unreviewed if isinstance(item, dict)]
                      if isinstance(unreviewed, (list, tuple)) else [],
                      "message": "%d critical decision(s) were not reviewed: review them, or accept them as decided" % to_count(unreviewed)}
            raise self.build_blocked(reason["message"], [reason])

    @staticmethod
    def build_blocked(message, reasons):
        return RepoConflictError(message, code=WRAP_UP_BLOCKED_CODE, extras={"reasons": reasons})

    def deliver_when_pending(self, context, checked_repos):
        """Push + PR through the engine's delivery script, only when the delivery did not complete or a repo holds commits the remote
        lacks (a later round); a PR that already exists is reused by the script. It never merges and never raises."""
        unconfirmed = any(reason.get("code") == "push_unconfirmed" for item in checked_repos for reason in item.get("reasons") or [])
        if is_push_done(self.read_delivery_record(context)) and not unconfirmed:
            return
        result = self.engine_script_service.run_engine_script_and_wait(DELIVERY_SCRIPT, context.work_dir, [], self.delivery_timeout_seconds)
        returned = extract_last_json(result.output)
        if isinstance(returned.get("push"), str):
            self.loop_command_file_repository.write_delivery_to_meta(context.work_dir, returned["push"], returned.get("pr"),
                                                                     returned.get("pr_status") or "not created")
        self.log.info("wrap_up_delivery_ran", project=context.project.id, timed_out=result.timed_out, push=str(returned.get("push"))[:RESULT_TEXT_LIMIT])

    def close_after_check(self, context):
        """The safe close: every repo must be able to close (a compare-link-only repo is confirmed by this very click)."""
        checked = self.loop_worktree_service.check_close_loop(context)["repos"]
        blocked = [item for item in checked if self.blocking_reasons(item)]
        if blocked:
            reasons = [reason for item in blocked for reason in self.describe_blocked_repo(context, item)]
            raise self.build_blocked("nothing was removed: %s" % "; ".join(reason["message"] for reason in reasons), reasons)
        # a confirmed push with no PR (non-GitHub remote, no gh) is a note, not a blocker: allow_missing_pr lifts only that reason
        return self.close_worktrees(context, {"confirm_compare_link": any(item.get("needs_confirm") for item in checked), "allow_missing_pr": True, "finished": True})

    def close_worktrees(self, context, close_body):
        result = self.loop_worktree_service.close_loop(context, close_body)
        failed = [(name, item) for name, item in result["repos"].items() if isinstance(item, dict) and item.get("status") != "closed"]
        if failed:
            reasons = [{"repo": name, "kind": "close_failed", "files": [], "commits": [], "message": "; ".join(str(reason.get("text")) for reason in item.get("reasons") or [])
                        or "the worktree was kept"} for name, item in failed]
            error = self.build_blocked("the close did not finish: %s" % "; ".join("%s: %s" % (reason["repo"], reason["message"]) for reason in reasons), reasons)
            error.extras["closed"] = list(result["closed"])
            error.extras["backup"] = result.get("backup")
            raise error
        return result

    # ------------------------------------------------ reasons and receipt
    def describe_blocked_repo(self, context, item):
        """One 409 reason per blocking check reason of the repo: `{repo, kind, files|commits, message}`."""
        name, worktree = item["name"], self.find_worktree(context, item["name"])
        reasons = []
        for reason in self.blocking_reasons(item):
            kind = REASON_KIND_BY_CODE.get(reason.get("code"), reason.get("code") or "blocked")
            entry = {"repo": name, "kind": kind, "files": [], "commits": [], "message": "%s: %s" % (name, reason.get("text") or kind)}
            if kind == REASON_KIND_UNCOMMITTED:
                entry["files"] = self.list_uncommitted_files(worktree)
                entry["message"] = "%s: %d uncommitted file(s) would be lost" % (name, len(entry["files"]))
            elif kind == REASON_KIND_UNPUSHED:
                entry["commits"] = self.list_unpushed_commits(worktree, item)
            reasons.append(entry)
        return reasons

    def find_worktree(self, context, repo_name):
        recorded = (self.loop_status_service.get_loop_summary(context).get("meta") or {}).get("repos")
        recorded = recorded.get(repo_name) if isinstance(recorded, dict) else None
        if isinstance(recorded, dict) and recorded.get("worktree"):
            return recorded["worktree"]
        return context.work_dir

    def list_uncommitted_files(self, worktree):
        code, output, _ = self.git_client.run_git(worktree, "status", "--porcelain", "--untracked-files=all", "--", ".", ":(exclude).specs")
        lines = output.splitlines() if code == 0 else []
        return [line[3:].strip().strip('"') for line in lines if line.strip()][:LISTED_FILES_LIMIT]

    def list_unpushed_commits(self, worktree, item):
        """`<sha> <subject>` of the commits the remote does not hold: against the remote branch, else against the base the loop forked from."""
        for upstream in ("origin/%s" % item.get("branch"), "origin/%s" % item.get("base"), item.get("base")):
            if not upstream or upstream.endswith("/None") or upstream == "None":
                continue
            code, output, _ = self.git_client.run_git(worktree, "log", "--oneline", "-n", str(LISTED_COMMITS_LIMIT), "%s..HEAD" % upstream)
            if code == 0:
                return output.splitlines()
        return []

    def build_receipt(self, context, meta, delivery_record, checked_repos):
        summary = self.build_summary(context, meta, delivery_record)
        primary = checked_repos[0] if checked_repos else {}
        return {"pr_url": summary["pr_url"], "compare_url": summary["compare_url"], "branch": primary.get("branch") or meta.get("branch"),
                "backup_dir": None, "cost_usd": summary["cost_usd"], "max_cost_usd": summary["max_cost_usd"], "turns": summary["turns"],
                "worktree_removed": False}
