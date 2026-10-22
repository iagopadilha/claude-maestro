"""Act mode: deterministic actions from a WHITELIST (code, not the AI), always with the user's `confirm:true`.

`deliver` (delivery.py: push of the feature branch only + PR to the integration branch), `summary` (loop_summary.py),
`push` (delivery.py with LOOP_PR_MODE=off per loop worktree) and `clean_worktrees` (with a verified backup in the output
folder). The free-text router (`claude -p` haiku, no tools and no MCP) only PROPOSES an action from the list; an invalid
reply = `none`. Every run goes to `internal/actions.log`. Actions apply only to the current version (the server ignores `turn`).
"""
import json
import os
import re
import threading
from typing import Protocol

from ..infrastructure.claude_client import redact_secrets
from .loop_worktree_service import MAX_TARGETS, extract_last_json, get_context_integration_branch

ACTION_DELIVER = "deliver"
ACTION_SUMMARY = "summary"
ACTION_PUSH = "push"
ACTION_CLEAN_WORKTREES = "clean_worktrees"
ACTION_NONE = "none"
ALLOWED_ACTIONS = (ACTION_DELIVER, ACTION_SUMMARY, ACTION_PUSH, ACTION_CLEAN_WORKTREES)
ROUTER_ACTIONS = ALLOWED_ACTIONS + (ACTION_NONE,)
WORKTREE_ACTIONS = (ACTION_PUSH, ACTION_CLEAN_WORKTREES)
DELIVER_TITLE = "Deliver: push the branch + PR to %s"
ACTION_TITLES = {ACTION_SUMMARY: "Generate the loop summary", ACTION_PUSH: "Push the feature branches (no PR)",
                 ACTION_CLEAN_WORKTREES: "Clean loop worktrees (backup first)"}
ACTION_TIMEOUT_SECONDS = 180
PREVIEW_TIMEOUT_SECONDS = 60
ROUTER_TIMEOUT_SECONDS = 60
OUTPUT_LIMIT = 4000
PREVIEW_PUSH_LIMIT = 500
REASON_LIMIT = 300
LOG_SUMMARY_LIMIT = 200
ROUTER_TEXT_LIMIT = 2000
EXPLANATION_LIMIT = 300
MAX_CONCURRENT_JOBS = 3
ROUTER_MODEL = "haiku"
ROUTER_BUDGET_USD = 0.2
ROUTER_ALLOWED_TOOLS = [""]  # `--allowedTools ""`: no tool at all
ROUTER_DISALLOWED_TOOLS = ["Read", "Grep", "Glob", "Bash", "Edit", "Write", "NotebookEdit"]
CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"
CONFIDENCES = (CONFIDENCE_HIGH, CONFIDENCE_LOW)
LOOP_RUNNING_REASON = "the loop is running"
NO_LOOP_REASON = "there is no loop in this worktree"
NO_LOOP_WORKTREE_REASON = "there is no loop worktree"
NO_WORKTREE_REASON = "no worktree"
STATUS_RUNNING = "running"
STATUS_NO_LOOP = "no_loop"
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
UNEXPECTED_FAILURE = "unexpected failure"
DRY_RUN_PUSH_PREFIX = "dry-run"
DONE_PUSH_PREFIX = "done"
EXISTING_PR_PREFIX = "PR"
DEFAULT_PROPOSAL = {"action": ACTION_NONE, "explanation": "I did not understand the request as one of the available actions.",
                    "confidence": CONFIDENCE_LOW}
ROUTER_PROMPT = ("You are a router. Pick ONE action for the user's request among: "
                 "\"deliver\" (push the feature branch and open a PR to the integration branch), \"summary\" (generate the final loop summary), "
                 "\"push\" (only push the feature branches, no PR), \"clean_worktrees\" (delete the loop worktrees) or \"none\" "
                 "(anything else, a question or an instruction). Do not run anything and do not use tools. "
                 "Reply ONLY with a JSON object: {\"action\":\"deliver|summary|push|clean_worktrees|none\",\"explanation\":\"1 to 2 sentences\","
                 "\"confidence\":\"high|low\",\"targets\":[\"branch or folder name cited by the user\"]} (targets only for push and "
                 "clean_worktrees and only if the user cites them).\n\n"
                 "User request:\n")


def parse_router_proposal(text):
    """Valid proposal {action, explanation, confidence[, cited_targets]}; an invalid reply or one outside the whitelist = `none`."""
    raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", str(text or "").strip())
    data, start = None, raw.find("{")
    while data is None and start != -1:
        try:
            data = json.loads(raw[start:raw.rindex("}") + 1])
        except ValueError:
            start = raw.find("{", start + 1)
    if not isinstance(data, dict) or data.get("action") not in ROUTER_ACTIONS:
        return dict(DEFAULT_PROPOSAL)
    explanation = data.get("explanation") if isinstance(data.get("explanation"), str) else ""
    proposal = {"action": data["action"], "explanation": redact_secrets(explanation, EXPLANATION_LIMIT) or DEFAULT_PROPOSAL["explanation"],
                "confidence": data.get("confidence") if data.get("confidence") in CONFIDENCES else CONFIDENCE_LOW}
    if data["action"] in WORKTREE_ACTIONS and isinstance(data.get("targets"), list):
        proposal["cited_targets"] = [item for item in data["targets"] if isinstance(item, str)][:MAX_TARGETS]
    return proposal


class IActionService(Protocol):
    def list_available_actions(self, context) -> tuple: ...
    def run_confirmed_action(self, context, body) -> tuple: ...
    def propose_action_from_text(self, context, body) -> tuple: ...


class ActionService:
    def __init__(self, config, log, loop_status_service, loop_control_service, loop_command_file_repository,
                 loop_worktree_service, engine_script_service, chat_job_repository, claude_client):
        self.config = config
        self.log = log
        self.loop_status_service = loop_status_service
        self.loop_control_service = loop_control_service
        self.loop_command_file_repository = loop_command_file_repository
        self.loop_worktree_service = loop_worktree_service
        self.engine_script_service = engine_script_service
        self.chat_job_repository = chat_job_repository
        self.claude_client = claude_client
        self.action_timeout_seconds = ACTION_TIMEOUT_SECONDS
        self.router_timeout_seconds = ROUTER_TIMEOUT_SECONDS
        self.registry_lock = threading.Lock()
        self.action_locks_by_project = {}

    def get_action_lock(self, project_id):
        with self.registry_lock:
            return self.action_locks_by_project.setdefault(project_id, threading.Lock())

    def decide_block_reason(self, context, summary):
        """Reason not to act now (loop running, relaunched loop alive, no loop) or None."""
        if summary["running"] or summary["status"] == STATUS_RUNNING or self.loop_control_service.has_live_relaunch(context.project.id):
            return LOOP_RUNNING_REASON
        if summary["status"] == STATUS_NO_LOOP:
            return NO_LOOP_REASON
        return None

    def get_output_dir(self):
        return os.path.abspath(self.config.output_dir)

    def record_action(self, context, action, ok, summary):
        self.loop_command_file_repository.record_executed_action(context.work_dir, action, ok, redact_secrets(summary, LOG_SUMMARY_LIMIT))
        self.log.info("action_executed", project=context.project.id, action=action, ok=ok)

    # ------------------------------------------------ GET actions
    def build_deliver_preview(self, context, meta, block_reason):
        base = get_context_integration_branch(context)
        deliver = {"id": ACTION_DELIVER, "title": DELIVER_TITLE % base, "available": False, "unavailable_reason": block_reason,
                   "preview": {"branch": meta.get("branch"), "base": base,
                               "existing_pr": meta.get("pr") if str(meta.get("pr_status") or "").startswith(EXISTING_PR_PREFIX) else None,
                               "open_features": self.loop_command_file_repository.list_open_features(context.work_dir),
                               "push": None}}
        if block_reason:
            return deliver
        if not os.path.isdir(context.work_dir):
            deliver["unavailable_reason"] = NO_WORKTREE_REASON
            return deliver
        result = self.engine_script_service.run_engine_script_and_wait("delivery", context.work_dir, ["--dry-run"], PREVIEW_TIMEOUT_SECONDS)
        push = extract_last_json(result.output).get("push")
        push = push if isinstance(push, str) else None
        if result.timed_out:
            deliver["unavailable_reason"] = "the --dry-run took longer than %d s" % PREVIEW_TIMEOUT_SECONDS
        elif push is None:
            text = (result.error or result.output).strip()
            deliver["unavailable_reason"] = redact_secrets(text.splitlines()[-1] if text else "the --dry-run did not answer", REASON_LIMIT)
        else:
            deliver["preview"]["push"] = redact_secrets(push, PREVIEW_PUSH_LIMIT)
            deliver["available"] = push.startswith(DRY_RUN_PUSH_PREFIX)
            deliver["unavailable_reason"] = None if deliver["available"] else redact_secrets(push, PREVIEW_PUSH_LIMIT)
        return deliver

    def list_available_actions(self, context):
        """Preview of each action; the `deliver` one comes from `delivery.py --dry-run` (never pushes)."""
        summary = self.loop_status_service.get_loop_summary(context)
        block_reason = self.decide_block_reason(context, summary)
        meta = summary.get("meta") if isinstance(summary.get("meta"), dict) else self.loop_command_file_repository.read_meta(context.work_dir)
        deliver = self.build_deliver_preview(context, meta, block_reason)
        generate_summary = {"id": ACTION_SUMMARY, "title": ACTION_TITLES[ACTION_SUMMARY], "available": block_reason is None,
                            "unavailable_reason": block_reason, "preview": {"destination": self.get_output_dir()}}
        loop_running = summary["running"] or block_reason == LOOP_RUNNING_REASON
        worktrees = [] if loop_running else self.loop_worktree_service.list_loop_worktrees(context)
        worktree_reason = LOOP_RUNNING_REASON if loop_running else (None if worktrees else NO_LOOP_WORKTREE_REASON)
        push = {"id": ACTION_PUSH, "title": ACTION_TITLES[ACTION_PUSH], "available": worktree_reason is None,
                "unavailable_reason": worktree_reason, "preview": {"worktrees": worktrees}}
        clean = {"id": ACTION_CLEAN_WORKTREES, "title": ACTION_TITLES[ACTION_CLEAN_WORKTREES], "available": worktree_reason is None,
                 "unavailable_reason": worktree_reason, "preview": {"worktrees": worktrees, "backup_dir": self.get_output_dir()}}
        return 200, {"actions": [deliver, generate_summary, push, clean]}

    # ------------------------------------------------ POST action/run
    def run_confirmed_action(self, context, body):
        action, confirm = body.get("action"), body.get("confirm")
        if not isinstance(action, str) or action not in ALLOWED_ACTIONS:  # nothing outside the whitelist, by any path
            return 400, {"ok": False, "error": "action outside the allowed list: %s" % ", ".join(ALLOWED_ACTIONS)}
        if confirm is not True:
            return 400, {"ok": False, "error": "confirm must be exactly true"}
        lock = self.get_action_lock(context.project.id)
        if not lock.acquire(blocking=False):
            return 409, {"ok": False, "error": "an action is already running"}
        try:
            if action in WORKTREE_ACTIONS:
                return self.run_worktree_action(context, action, body)
            return self.run_delivery_or_summary_script(context, action)
        finally:
            lock.release()

    def run_delivery_or_summary_script(self, context, action):
        block_reason = self.decide_block_reason(context, self.loop_status_service.get_loop_summary(context))
        if block_reason:
            return 409, {"ok": False, "error": block_reason}
        script_name = "delivery" if action == ACTION_DELIVER else "loop_summary"
        args = ["--out", self.get_output_dir()] if action == ACTION_SUMMARY else []  # always: loop_summary.py alone writes to the cwd
        self.log.info("action_launched", project=context.project.id, action=action)
        result = self.engine_script_service.run_engine_script_and_wait(script_name, context.work_dir, args, self.action_timeout_seconds)
        output = redact_secrets((result.output + ("\n" + result.error if result.error.strip() else "")).strip(), OUTPUT_LIMIT)
        returned_json = extract_last_json(result.output) if action == ACTION_DELIVER else {}
        push = redact_secrets(returned_json["push"], PREVIEW_PUSH_LIMIT) if isinstance(returned_json.get("push"), str) else None
        pr = returned_json.get("pr") if isinstance(returned_json.get("pr"), str) else None
        pr_status = redact_secrets(returned_json["pr_status"], REASON_LIMIT) if isinstance(returned_json.get("pr_status"), str) else None
        ok = (not result.timed_out) and result.code == 0
        if result.timed_out:
            output = (output + "\n" if output else "") + "timed out (%s s): the process was killed" % self.action_timeout_seconds
        if action == ACTION_DELIVER and ok and push and push.startswith(DONE_PUSH_PREFIX):
            self.loop_command_file_repository.write_delivery_to_meta(context.work_dir, push, pr, pr_status)
        first_line = push if action == ACTION_DELIVER and push else (output.splitlines()[0] if output else ("ok" if ok else "failed"))
        self.record_action(context, action, ok, first_line)
        return 200, {"ok": ok, "output": output, "pr": pr, "pr_status": pr_status, "push": push}

    def run_worktree_action(self, context, action, body):
        """push (feature branches only, no PR) and clean_worktrees (verified backup). Call with the action lock held."""
        force = body.get("force", False)
        if not isinstance(force, bool):
            return 400, {"ok": False, "error": "force must be a boolean"}
        if self.decide_block_reason(context, self.loop_status_service.get_loop_summary(context)) == LOOP_RUNNING_REASON:
            return 409, {"ok": False, "error": LOOP_RUNNING_REASON}
        worktrees = self.loop_worktree_service.list_loop_worktrees(context)
        if "targets" not in body and action == ACTION_PUSH:
            targets = worktrees
        else:
            targets, error = self.loop_worktree_service.select_targets_from_preview(body.get("targets"), worktrees)
            if error:
                return 400, {"ok": False, "error": error}
        if not targets:
            return 400, {"ok": False, "error": "no target: give targets from the /api/actions preview"}
        self.log.info("action_launched", project=context.project.id, action=action, targets=len(targets), force=force)
        if action == ACTION_PUSH:
            results = [self.loop_worktree_service.push_worktree_branch(target) for target in targets]
        else:
            results = [self.loop_worktree_service.clean_worktree_with_backup(context, target, force) for target in targets]
        ok = all(item["ok"] for item in results)
        summary = "; ".join("%s: %s" % (item["branch"], item.get("push") if action == ACTION_PUSH else (item.get("error") or "deleted"))
                            for item in results)
        self.record_action(context, action, ok, summary)
        return 200, {"ok": ok, "results": results, "output": redact_secrets(summary, OUTPUT_LIMIT)}

    # ------------------------------------------------ POST action/propose (router)
    def propose_action_from_text(self, context, body):
        """Asynchronous like the chat: 202 {job}; the result is in GET chat/job/<job> (or the alias action/job/<job>) with `proposal`."""
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "empty text"}
        if len(text) > ROUTER_TEXT_LIMIT:
            return 413, {"ok": False, "error": "text is longer than %d characters" % ROUTER_TEXT_LIMIT}
        job_id, _ = self.chat_job_repository.reserve_job(context.project.id, None, MAX_CONCURRENT_JOBS, {"proposal": None})
        if job_id is None:
            return 409, {"ok": False, "error": "there are %d replies in progress; wait for one to finish" % MAX_CONCURRENT_JOBS}
        self.log.info("router_job_started", project=context.project.id, job=job_id)
        threading.Thread(target=self.run_router_job, args=(context, job_id, ROUTER_PROMPT + text.strip()), daemon=True).start()
        return 202, {"ok": True, "job": job_id}

    def run_router_job(self, context, job_id, stdin_text):
        result = {"state": JOB_STATE_ERROR, "reply": None, "cost_usd": None, "error": UNEXPECTED_FAILURE, "thread": None, "proposal": None}
        try:
            argv = self.claude_client.build_command(ROUTER_MODEL, ROUTER_BUDGET_USD, ROUTER_ALLOWED_TOOLS, ROUTER_DISALLOWED_TOOLS, no_mcp=True)
            folder = context.work_dir if os.path.isdir(context.work_dir) else context.repo_path
            returned = self.claude_client.run_prompt(argv, folder, stdin_text, self.router_timeout_seconds)
            if not returned["ok"]:
                result["error"] = returned.get("error") or UNEXPECTED_FAILURE
            else:
                proposal = parse_router_proposal(returned.get("reply"))
                cited = proposal.pop("cited_targets", None)
                if cited is not None:  # only targets that exist in the preview; the rest is dropped
                    proposal["targets"] = self.loop_worktree_service.match_cited_targets(cited, self.loop_worktree_service.list_loop_worktrees(context))
                result = {"state": JOB_STATE_OK, "reply": proposal["explanation"], "cost_usd": returned.get("cost_usd"), "error": None,
                          "thread": None, "proposal": proposal}
        except Exception as failure:  # the job never takes the app down
            result["error"] = "failed to run claude: %s" % type(failure).__name__
        finally:
            self.chat_job_repository.finish_job(job_id, result)
            self.log.info("router_job_finished", project=context.project.id, job=job_id, state=result["state"])
