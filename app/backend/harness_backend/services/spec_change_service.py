"""Change a definition that is already written, in the middle of (or after) a loop: a chat per scope with a spec-writer style
`claude -p` job that may edit ONLY the files of that scope.

Scopes: a feature slug of the ROADMAP (`.specs/features/<slug>/`) or `_project` (PROJECT.md, ROADMAP.md, FLOWS.md, RULES.md).
Each reply is a claude job (ClaudeJobService, type `spec_change`, run in the loop's work dir) with the account's default
model, US$ 1 per message, 600 s, no MCP, `--permission-mode dontAsk`, `--allowedTools Read Grep Glob Edit(<scope files>)` and
`--disallowedTools Bash NotebookEdit Agent`; afterwards the file guard undoes any change outside the scope and, in the `_project`
scope, a ROADMAP whose features/stages/statuses/deps changed is restored (the approval and the loop's state machine depend on them).
It refuses while the loop is running (pause it first: the orchestrator writes the same specs). It never touches loop state: the
panel offers a button that sends the existing `instruction` to the loop (with the feature) so the loop redoes the affected work.
"""
import os
import re
import threading
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, InvalidRequestError, JobInProgressError, JobNotFoundError, LoopRunningError)
from ..infrastructure.claude_client import redact_secrets
from ..repositories.loop_command_file_repository import now_iso
from ..repositories.specs_file_repository import loop_state
from .claude_job_service import JOB_STATE_ERROR, JOB_STATE_OK, JOB_TYPE_SPEC_CHANGE, ClaudeJobCommand
from .planning_service import GUARD_IGNORED_PATHS, is_number

PROJECT_SCOPE = "_project"
SPEC_CHANGE_BUSY_CODE = "spec_change_busy"
PROJECT_SCOPE_PATHS = (".specs/project/PROJECT.md", ".specs/project/ROADMAP.md", ".specs/project/FLOWS.md", ".specs/project/RULES.md")
ROADMAP_PATH = ".specs/project/ROADMAP.md"
FEATURES_FOLDER = ".specs/features"
FEATURE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
SPEC_CHANGE_MODEL = ""  # empty = the account's default model
SPEC_CHANGE_BUDGET_USD = 1
SPEC_CHANGE_TIMEOUT_SECONDS = 600
SPEC_CHANGE_TEXT_LIMIT = 8000
SPEC_CHANGE_REPLY_LIMIT = 20000
SPEC_CHANGE_PERMISSION_MODE = "dontAsk"
SPEC_CHANGE_HOOK_PROFILE = "planning"  # guard.py profile: Write/Edit only inside .specs/ (extra deterministic layer)
SPEC_CHANGE_DISALLOWED_TOOLS = ["Bash", "NotebookEdit", "Agent"]
SPEC_FILE_LIMIT = 200
GUARD_NOTE = "\n\n(The app undid changes outside this scope: %s)"
ROADMAP_RESTORED_NOTE = ("\n\n(The app restored ROADMAP.md: features, stages, statuses and dependencies cannot be changed here. "
                         "Use the planning flow or Files for that.)")
GUARD_NOTE_FILES_SHOWN = 8
ROLE_USER = "user"
ROLE_AI = "ai"
PUBLIC_MESSAGE_FIELDS = ("role", "text", "at", "cost_usd", "changed")

FEATURE_RULES = """You are the spec-writer of ONE feature of a project that already has its definition written.
The user found something new and wants to CHANGE the definition. Scope: feature `%(slug)s`, files under `.specs/features/%(slug)s/` (spec.md, design.md, tasks.md, ...).
Rules:
- Read the files of the feature (and .specs/project/ROADMAP.md and FLOWS.md for context, read-only) before editing.
- Edit only files under `.specs/features/%(slug)s/`. Never edit code, ROADMAP.md, FLOWS.md or any other file; if the change needs them, say so in the reply instead.
- Keep the requirement IDs stable (add new ones, mark removed ones as removed); change only what the user asked.
- If tasks.md exists, update the tasks the change affects and mark them not done.
- Reply short: what you changed (file and requirement IDs) and which already-built parts are likely affected."""
PROJECT_RULES = """You are the planner of a project that already has its definition written (and may already be running a loop).
The user found something new and wants to CHANGE the definition. Scope: .specs/project/PROJECT.md, ROADMAP.md, FLOWS.md, RULES.md.
Rules:
- Read the files before editing. Edit only these four files. Never edit code or any other file.
- In ROADMAP.md you may change the text, `paths=` and notes of a feature. NEVER add, remove or rename a feature, change a stage, a `status=` or a `deps=` (the app restores the file if you do): if the user asks for that, explain it must be done in the planning flow.
- Keep FLOWS.md covering the same features; change only what the user asked.
- Reply short: what you changed (file and feature slugs) and which features are likely affected."""


class ISpecChangeService(Protocol):
    def get_scope(self, context, scope) -> dict: ...
    def send_message(self, context, scope, text) -> dict: ...
    def get_job(self, context, job_id) -> dict: ...
    def reset_conversation(self, context, scope, confirm) -> dict: ...


def read_roadmap_rows(work_dir):
    """({slug: (stage, status, deps)}, raw text) of the work dir's ROADMAP."""
    text = loop_state.read(os.path.join(work_dir, *ROADMAP_PATH.split("/")))
    features, _ = loop_state.parse_roadmap(text)
    return {slug: (fields.get("stage"), fields.get("status"), fields.get("deps")) for slug, fields in features.items()}, text


class SpecChangeService:
    def __init__(self, spec_change_thread_repository, specs_file_repository, project_progress_service, claude_job_service,
                 claude_client, workspace_file_guard, log):
        self.spec_change_thread_repository = spec_change_thread_repository
        self.specs_file_repository = specs_file_repository
        self.project_progress_service = project_progress_service
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.log = log
        self.thread_lock = threading.Lock()

    # ------------------------------------------------ scope
    def validate_scope(self, context, scope):
        """The scope itself; a feature must have a row in the ROADMAP of the work dir."""
        if scope == PROJECT_SCOPE:
            return scope
        if not isinstance(scope, str) or not FEATURE_SLUG_PATTERN.fullmatch(scope):
            raise InvalidRequestError("invalid scope")
        rows, _ = read_roadmap_rows(context.work_dir)
        if scope not in rows:
            raise InvalidRequestError("unknown feature: %s is not in the ROADMAP" % scope)
        return scope

    @staticmethod
    def build_editable_paths(scope):
        return list(PROJECT_SCOPE_PATHS) if scope == PROJECT_SCOPE else ["%s/%s" % (FEATURES_FOLDER, scope)]

    @staticmethod
    def build_edit_rules(scope):
        if scope == PROJECT_SCOPE:
            return ["Edit(/%s)" % path for path in PROJECT_SCOPE_PATHS]
        return ["Edit(/%s/%s/**)" % (FEATURES_FOLDER, scope)]

    def list_scope_files(self, work_dir, scope):
        """Relative paths (`/`) of the files the AI may edit that exist now."""
        if scope == PROJECT_SCOPE:
            return [path for path in PROJECT_SCOPE_PATHS if os.path.isfile(os.path.join(work_dir, *path.split("/")))]
        root = os.path.join(work_dir, *FEATURES_FOLDER.split("/"), scope)
        found = []
        for current, folders, files in os.walk(root, followlinks=False):
            folders.sort()
            for name in sorted(files):
                found.append(os.path.relpath(os.path.join(current, name), work_dir).replace("\\", "/"))
        return found[:SPEC_FILE_LIMIT]

    def read_scope_texts(self, work_dir, scope):
        return {path: self.specs_file_repository.read_text(os.path.join(work_dir, *path.split("/")))
                for path in self.list_scope_files(work_dir, scope)}

    # ------------------------------------------------ read
    def get_scope(self, context, scope):
        scope = self.validate_scope(context, scope)
        thread = self.spec_change_thread_repository.read_thread(context.work_dir, scope)
        job = self.get_running_job(context)
        messages = [{field: message.get(field) for field in PUBLIC_MESSAGE_FIELDS} for message in thread["messages"]]
        return {"scope": scope, "files": self.list_scope_files(context.work_dir, scope),
                "thread": {"messages": messages, "busy": job is not None, "job_id": job["id"] if job else None,
                           "session": bool(thread.get("session_id"))},
                "loop_running": self.project_progress_service.is_loop_running(context)}

    def get_running_job(self, context):
        job = self.claude_job_service.get_running_job(context.repo_path)
        return job if job and job.get("type") == JOB_TYPE_SPEC_CHANGE else None

    def get_job(self, context, job_id):
        """{state, reply?, error?, cost_usd} of a spec-change job; another job type = 404."""
        job = self.claude_job_service.get_job(context.repo_path, job_id)
        if job.get("type") != JOB_TYPE_SPEC_CHANGE:
            raise JobNotFoundError("job not found")
        result = {"ok": True, "state": job["state"], "cost_usd": job.get("cost_usd")}
        if job["state"] == JOB_STATE_OK:
            result["reply"] = job.get("reply") or ""
        elif job["state"] == JOB_STATE_ERROR:
            result["error"] = job.get("error") or "claude finished with an error"
        return result

    # ------------------------------------------------ send
    def send_message(self, context, scope, text):
        message = self.validate_text(text)
        scope = self.validate_scope(context, scope)
        with self.thread_lock:
            self.ensure_can_send(context)
            work_dir = context.work_dir
            thread = self.spec_change_thread_repository.read_thread(work_dir, scope)
            session = thread.get("session_id") or ""
            snapshot = self.workspace_file_guard.take_snapshot(work_dir, GUARD_IGNORED_PATHS)
            before_texts = self.read_scope_texts(work_dir, scope)
            rows_before, roadmap_before = read_roadmap_rows(work_dir)
            allowed_tools = ["Read", "Grep", "Glob"] + self.build_edit_rules(scope)
            argv = self.claude_client.build_command(SPEC_CHANGE_MODEL, SPEC_CHANGE_BUDGET_USD, allowed_tools, SPEC_CHANGE_DISALLOWED_TOOLS,
                                                    no_mcp=True, resume_session=session, permission_mode=SPEC_CHANGE_PERMISSION_MODE,
                                                    hook_profile=SPEC_CHANGE_HOOK_PROFILE)
            stdin_text = self.compose_prompt(scope, message, include_rules=not session)
            thread["messages"].append({"role": ROLE_USER, "text": message, "at": now_iso(), "cost_usd": None, "changed": []})
            self.spec_change_thread_repository.write_thread(work_dir, scope, thread)
            command = ClaudeJobCommand(
                argv, stdin_text, session, timeout_seconds=SPEC_CHANGE_TIMEOUT_SECONDS, work_dir=work_dir,
                finalize=lambda result: self.finish_reply(context, scope, snapshot, before_texts, rows_before, roadmap_before, result))
            try:
                job = self.claude_job_service.start_job(context.repo_path, JOB_TYPE_SPEC_CHANGE, command)
            except Exception:
                thread["messages"].pop()
                self.spec_change_thread_repository.write_thread(work_dir, scope, thread)
                raise
        self.log.info("spec_change_message_sent", project=context.project.id, scope=scope, job=job["id"], resuming_session=bool(session))
        return {"ok": True, "job_id": job["id"]}

    @staticmethod
    def compose_prompt(scope, message, include_rules):
        rules = ""
        if include_rules:
            rules = (PROJECT_RULES if scope == PROJECT_SCOPE else FEATURE_RULES % {"slug": scope}) + "\n\n"
        return "%sSPEC-CHANGE-SCOPE: %s\n\nUser request:\n%s" % (rules, scope, message)

    def ensure_can_send(self, context):
        if self.project_progress_service.is_loop_running(context):
            raise LoopRunningError("the loop is running: pause it first (the orchestrator writes these specs), change the definition, "
                                   "then resume", code=LOOP_RUNNING_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a reply (or another claude job) is already in progress in this project: wait for it to finish",
                                     code=SPEC_CHANGE_BUSY_CODE)

    def finish_reply(self, context, scope, snapshot, before_texts, rows_before, roadmap_before, result):
        """Runs in the job thread: undoes changes outside the scope, protects the ROADMAP rows, records the reply and the changed files."""
        work_dir = context.work_dir
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, self.build_editable_paths(scope))
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("spec_change_guard_reverted", project=context.project.id, scope=scope, reverted=report["reverted"][:50],
                          removed=report["removed"][:50], unrestorable=report["unrestorable"][:50])
        roadmap_restored = False
        if scope == PROJECT_SCOPE:
            rows_after, roadmap_after = read_roadmap_rows(work_dir)
            if rows_after != rows_before:
                self.specs_file_repository.write_text_atomic(os.path.join(work_dir, *ROADMAP_PATH.split("/")), roadmap_before)
                roadmap_restored = True
                self.log.warn("spec_change_roadmap_restored", project=context.project.id)
        after_texts = self.read_scope_texts(work_dir, scope)
        changed = sorted(path for path in set(before_texts) | set(after_texts) if before_texts.get(path) != after_texts.get(path))
        result = dict(result)
        if result.get("ok"):
            reply = redact_secrets(result.get("reply") or "", SPEC_CHANGE_REPLY_LIMIT)
            if touched:
                reply += GUARD_NOTE % ", ".join(touched[:GUARD_NOTE_FILES_SHOWN])
            if roadmap_restored:
                reply += ROADMAP_RESTORED_NOTE
            result["reply"] = reply
        with self.thread_lock:
            thread = self.spec_change_thread_repository.read_thread(work_dir, scope)
            if result.get("ok"):
                thread["messages"].append({"role": ROLE_AI, "text": result["reply"], "at": now_iso(), "changed": changed,
                                           "cost_usd": result.get("cost_usd") if is_number(result.get("cost_usd")) else None})
            thread["session_id"] = result.get("session_id") or thread.get("session_id")
            self.spec_change_thread_repository.write_thread(work_dir, scope, thread)
        return result

    # ------------------------------------------------ reset
    def reset_conversation(self, context, scope, confirm):
        """New conversation of the scope (new session on the next message). The files stay."""
        if confirm is not True:
            raise InvalidRequestError("confirm must be true")
        scope = self.validate_scope(context, scope)
        with self.thread_lock:
            if self.get_running_job(context):
                raise JobInProgressError("a reply is in progress: wait for it to finish", code=SPEC_CHANGE_BUSY_CODE)
            thread = self.spec_change_thread_repository.read_thread(context.work_dir, scope)
            thread.update(messages=[], session_id=None)
            self.spec_change_thread_repository.write_thread(context.work_dir, scope, thread)
        return {"ok": True}

    # ------------------------------------------------ support
    @staticmethod
    def validate_text(text):
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("empty text")
        if len(text) > SPEC_CHANGE_TEXT_LIMIT:
            raise InvalidRequestError("text is longer than %d characters" % SPEC_CHANGE_TEXT_LIMIT)
        return text.strip()
