"""Planning mode: the live chat that WRITES the planning documents (`/orchestrator plan`), the gate checklist and the
document preview of a project that never approved a roadmap.

One conversation per project (`internal/planning/thread.json`), one `claude -p` session resumed on every message. Each
reply is a claude job (ClaudeJobService: one job per project at a time, kept in `internal/jobs/`) with: the account's
default model, US$ 2 per message, 600 s, no MCP (except the read-only Grafana/Loki tools when the project opted in, ACCESS.json
`grafana_read`), `--permission-mode dontAsk`, `--allowedTools Read Grep Glob Skill
Edit(/.specs/project/PROJECT.md) Edit(/.specs/project/ROADMAP.md) Edit(/.specs/project/FLOWS.md)
Edit(/.specs/project/TEST-ENVIRONMENT.md) Edit(/.specs/project/RULES.md)` (an `Edit` rule also
covers Write; `/path` in a CLI rule = the session's working directory) and `--disallowedTools Bash NotebookEdit Agent`.
The user's settings can widen those rules, so after every reply the file guard undoes any change outside those
documents (logged as `planning_guard_reverted`). The conversation never approves: only the user's click does.
"""
import os
import re
import threading
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, PLANNING_BUSY_CODE, PLANNING_PREREQUISITES_CODE, InvalidRequestError,
                                     JobInProgressError, JobNotFoundError, LoopRunningError, LoopWorktreeExistsError,
                                     PlanningGatesError)
from ..config import app_config
from ..infrastructure.claude_client import redact_secrets
from ..repositories.loop_command_file_repository import now_iso
from ..repositories.specs_file_repository import loop_state, run_hints
import access_settings  # noqa: E402  (engine folder is on sys.path through specs_file_repository)
import grafana_access  # noqa: E402
from .claude_job_service import JOB_STATE_ERROR, JOB_STATE_OK, JOB_TYPE_PLANNING, ClaudeJobCommand
from .planning_gates import PREREQUISITE_GATES, are_gates_ok, are_prerequisites_ok, describe_gates_for_ai
from .roadmap_service import extract_open_questions

RECOMMENDED_SEPARATOR = re.compile(r"\s*\|\s*recommended\s*:\s*", re.IGNORECASE)


def describe_open_questions(roadmap_text):
    """[{id, text, recommended}] of the ROADMAP `## Open questions`, complete (the gate shows them cut); same parser as the gate.
    Item format: `<question> | recommended: <answer>`; without the separator `recommended` is empty."""
    described = []
    for index, item in enumerate(extract_open_questions(roadmap_text or ""), start=1):
        parts = RECOMMENDED_SEPARATOR.split(item, maxsplit=1)
        question, recommended = parts[0], parts[1] if len(parts) > 1 else ""
        described.append({"id": "q%d" % index, "text": question.strip(), "recommended": recommended.strip()})
    return described

PLANNING_DOCUMENTS = {"project": ".specs/project/PROJECT.md", "roadmap": ".specs/project/ROADMAP.md",
                      "flows": ".specs/project/FLOWS.md", "environment": ".specs/project/TEST-ENVIRONMENT.md"}
PLANNING_FIRST_PROMPT = "/orchestrator plan --headless"
PLANNING_MODEL = ""  # empty = the account's default model, as in run_loop
PLANNING_BUDGET_USD = 2
PLANNING_TIMEOUT_SECONDS = 600
PLANNING_TEXT_LIMIT = 8000
PLANNING_REPLY_LIMIT = 20000
PLANNING_PERMISSION_MODE = "dontAsk"  # what the rules do not pre-approve is denied (nobody answers a prompt in -p)
PLANNING_RULES_PATH = ".specs/project/RULES.md"  # not previewed, but the chat fills it from what the user says cannot be done
PLANNING_SUGGESTED_REPOS_PATH = ".specs/project/REPOS-SUGGESTED.json"  # repos the chat proposes from the catalog; the user confirms in the panel
PLANNING_EDITABLE_PATHS = tuple(PLANNING_DOCUMENTS.values()) + (PLANNING_RULES_PATH, PLANNING_SUGGESTED_REPOS_PATH)
PLANNING_ALLOWED_TOOLS = ["Read", "Grep", "Glob", "Skill"] + ["Edit(/%s)" % path for path in PLANNING_EDITABLE_PATHS]
PLANNING_DISALLOWED_TOOLS = ["Bash", "NotebookEdit", "Agent"]
PLANNING_CREDENTIALS_NOTE = (
    "[Test credentials: the file .specs/project/loop/internal/credential-names.json lists every $NAME of TEST-ENVIRONMENT.md with its "
    "source and whether it is set, NEVER the values. Read it to know what exists. Never ask the user for a password, token or any value "
    "and never write one in a document or in chat: write only the $NAME in TEST-ENVIRONMENT.md and tell the user to fill the value in the "
    "panel's Test environment tab.]")
PLANNING_GRAFANA_NOTE = (
    "\n[Grafana: the user allowed read-only lookups. You may use ONLY the mcp__grafana__ Loki tools (query_loki_logs, list_loki_label_names, "
    "list_loki_label_values, query_loki_stats, query_loki_patterns, find_error_pattern_logs) and list_datasources, to find sample "
    "data (real ids, routes, payload shapes) for TEST-ENVIRONMENT.md and FLOWS.md. Never copy a secret, token or personal data from a log "
    "into a document.]")
PLANNING_HOOK_PROFILE = "planning"  # guard.py profile of the PreToolUse hook: Write/Edit only inside .specs/ (extra layer, deterministic)
GUARD_IGNORED_PATHS = (".git", "node_modules", ".specs/project/loop/internal", ".specs/project/loop/json/MONITOR.json",
                        ".specs/.trash")
GUARD_NOTE = "\n\n(The app undid changes outside the planning documents: %s)"
GUARD_NOTE_FILES_SHOWN = 8
ROLE_USER = "user"
ROLE_AI = "ai"
PUBLIC_MESSAGE_FIELDS = ("role", "text", "at", "cost_usd")


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


class IPlanningService(Protocol):
    def get_planning(self, context) -> dict: ...
    def send_message(self, context, text) -> dict: ...
    def get_planning_job(self, context, job_id) -> dict: ...
    def reset_conversation(self, context, confirm) -> dict: ...


class PlanningService:
    def __init__(self, planning_thread_repository, specs_file_repository, project_progress_service, planning_requirement_service,
                 claude_job_service, claude_client, workspace_file_guard, log, test_environment_service=None):
        self.test_environment_service = test_environment_service  # writes credential-names.json (names without values) for the AI
        self.planning_thread_repository = planning_thread_repository
        self.specs_file_repository = specs_file_repository
        self.project_progress_service = project_progress_service
        self.planning_requirement_service = planning_requirement_service
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.log = log
        self.thread_lock = threading.Lock()

    # ------------------------------------------------ read
    def get_planning(self, context):
        gates = self.project_progress_service.build_planning_gates(context)
        thread = self.planning_thread_repository.read_thread(context.work_dir)
        job = self.get_running_planning_job(context)
        approval = loop_state.roadmap_approval(context.work_dir)
        docs = {key: self.specs_file_repository.read_text(os.path.join(context.work_dir, *relative.split("/")))
                for key, relative in PLANNING_DOCUMENTS.items()}
        return {
            "required": self.planning_requirement_service.is_planning_required(context),
            "thread": {"messages": [{field: message.get(field) for field in PUBLIC_MESSAGE_FIELDS} for message in thread["messages"]],
                       "busy": job is not None, "job_id": job["id"] if job else None, "session": bool(thread.get("session_id"))},
            "docs": docs,
            "open_questions": describe_open_questions(docs.get("roadmap")),
            "codebase": self.read_codebase_map(context),
            "gates": gates,
            "can_approve": are_gates_ok(gates) and not self.project_progress_service.is_loop_running(context),
            "approved": approval["approved"],
        }

    def read_codebase_map(self, context):
        """The brownfield map for the side panel: {folder, commit, docs: {name: text}} (read-only; empty docs = not mapped yet)."""
        facts = self.project_progress_service.read_map_facts(context.work_dir, context.repo_path)
        relative, root = run_hints.find_map_folder(context.work_dir, context.repo_path)
        folder = os.path.join(root, *relative.split("/"))
        return {"folder": facts["map_folder"], "commit": facts["map_commit"],
                "docs": {name: self.specs_file_repository.read_text(os.path.join(folder, name)) for name in facts["map_files"]}}

    def get_planning_job(self, context, job_id):
        """{state, reply?, error?, cost_usd} of a planning reply job; another job type = 404."""
        job = self.claude_job_service.get_job(context.repo_path, job_id)
        if job.get("type") != JOB_TYPE_PLANNING:
            raise JobNotFoundError("job not found")
        result = {"ok": True, "state": job["state"], "cost_usd": job.get("cost_usd")}
        if job["state"] == JOB_STATE_OK:
            result["reply"] = job.get("reply") or ""
        elif job["state"] == JOB_STATE_ERROR:
            result["error"] = job.get("error") or "claude finished with an error"
        return result

    def get_running_planning_job(self, context):
        job = self.claude_job_service.get_running_job(context.repo_path)
        return job if job and job.get("type") == JOB_TYPE_PLANNING else None

    # ------------------------------------------------ send
    def send_message(self, context, text):
        message = self.validate_text(text)
        with self.thread_lock:
            gates = self.ensure_can_send(context)
            work_dir = context.work_dir
            thread = self.planning_thread_repository.read_thread(work_dir)
            session = thread.get("session_id") or ""
            self.project_progress_service.ensure_test_environment_template(context)  # before the snapshot: it is the app's file, not the AI's
            names_file = self.test_environment_service.write_credential_names_file(context) if self.test_environment_service else ""
            snapshot = self.workspace_file_guard.take_snapshot(work_dir, GUARD_IGNORED_PATHS)
            allowed_tools, disallowed_tools, mcp_config = self.build_grafana_read_access(work_dir)
            argv = self.claude_client.build_command(PLANNING_MODEL, PLANNING_BUDGET_USD, allowed_tools, disallowed_tools,
                                                    no_mcp=True, resume_session=session, permission_mode=PLANNING_PERMISSION_MODE,
                                                    hook_profile=PLANNING_HOOK_PROFILE, mcp_config_path=mcp_config)
            status = describe_gates_for_ai(gates)  # every turn: the AI sees which documents and gates are still red
            if names_file:
                status += "\n\n" + PLANNING_CREDENTIALS_NOTE + (PLANNING_GRAFANA_NOTE if mcp_config else "")
            stdin_text = status + "\n\n" + message if session else PLANNING_FIRST_PROMPT + "\n" + status + "\n\n" + message
            thread["messages"].append({"role": ROLE_USER, "text": message, "at": now_iso(), "cost_usd": None})
            self.planning_thread_repository.write_thread(work_dir, thread)
            command = ClaudeJobCommand(argv, stdin_text, session, timeout_seconds=PLANNING_TIMEOUT_SECONDS,
                                       finalize=lambda result: self.finish_reply(context, snapshot, result))
            try:
                job = self.claude_job_service.start_job(context.repo_path, JOB_TYPE_PLANNING, command)
            except Exception:
                thread["messages"].pop()
                self.planning_thread_repository.write_thread(work_dir, thread)
                raise
        self.log.info("planning_message_sent", project=context.project.id, job=job["id"], resuming_session=bool(session))
        return {"ok": True, "job_id": job["id"]}

    @staticmethod
    def build_grafana_read_access(work_dir):
        """(allowed, disallowed, mcp config path): the planning rules, plus the read-only Grafana/Loki tools and their config only when the
        project opted in (ACCESS.json `grafana_read`) and the user's `grafana` MCP server could be copied. Otherwise nothing changes."""
        allowed, disallowed = list(PLANNING_ALLOWED_TOOLS), list(PLANNING_DISALLOWED_TOOLS)
        if not access_settings.read_access(work_dir)[access_settings.GRAFANA_READ_KEY]:
            return allowed, disallowed, ""
        mcp_config = grafana_access.write_mcp_config(app_config.get_user_data_dir())
        if not mcp_config:
            return allowed, disallowed, ""
        return allowed + list(grafana_access.READ_TOOLS), disallowed + list(grafana_access.DENIED_WRITE_TOOLS), mcp_config

    def ensure_can_send(self, context):
        """Order of the refusals: loop running, loop worktree, prerequisites (git/structure/conformance/map), a job running."""
        if self.project_progress_service.is_loop_running(context):
            raise LoopRunningError("loop running: planning happens with the loop stopped", code=LOOP_RUNNING_CODE)
        if os.path.normcase(os.path.abspath(context.work_dir)) != os.path.normcase(os.path.abspath(context.repo_path)):
            raise LoopWorktreeExistsError("the loop worktree already exists (%s): edit the roadmap in Files" % context.work_dir)
        gates = self.project_progress_service.build_planning_gates(context)
        if not are_prerequisites_ok(gates):
            failing = [gate["label"] for gate in gates if gate["id"] in PREREQUISITE_GATES and not gate["ok"] and not gate["optional"]]
            raise PlanningGatesError("prerequisites missing before planning: %s" % ", ".join(failing), gates, PLANNING_PREREQUISITES_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a reply (or another claude job) is already in progress in this project: wait for it to finish",
                                     code=PLANNING_BUSY_CODE)
        return gates

    def finish_reply(self, context, snapshot, result):
        """Runs in the job thread: undoes changes outside the documents, then records the reply and the session."""
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, PLANNING_EDITABLE_PATHS)
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("planning_guard_reverted", project=context.project.id, reverted=report["reverted"][:50],
                          removed=report["removed"][:50], unrestorable=report["unrestorable"][:50])
        result = dict(result)
        if result.get("ok"):
            reply = redact_secrets(result.get("reply") or "", PLANNING_REPLY_LIMIT)
            if touched:
                reply += GUARD_NOTE % ", ".join(touched[:GUARD_NOTE_FILES_SHOWN])
            result["reply"] = reply
        with self.thread_lock:
            thread = self.planning_thread_repository.read_thread(context.work_dir)
            if result.get("ok"):
                thread["messages"].append({"role": ROLE_AI, "text": result["reply"], "at": now_iso(),
                                           "cost_usd": result.get("cost_usd") if is_number(result.get("cost_usd")) else None})
            thread["session_id"] = result.get("session_id") or thread.get("session_id")
            self.planning_thread_repository.write_thread(context.work_dir, thread)
        return result

    # ------------------------------------------------ reset
    def reset_conversation(self, context, confirm):
        """New conversation (new session on the next message). The documents stay."""
        if confirm is not True:
            raise InvalidRequestError("confirm must be true")
        with self.thread_lock:
            if self.get_running_planning_job(context):
                raise JobInProgressError("a reply is in progress: wait for it to finish", code=PLANNING_BUSY_CODE)
            thread = self.planning_thread_repository.read_thread(context.work_dir)
            thread.update(messages=[], session_id=None)
            self.planning_thread_repository.write_thread(context.work_dir, thread)
        self.log.info("planning_conversation_reset", project=context.project.id)
        return {"ok": True}

    # ------------------------------------------------ support
    @staticmethod
    def validate_text(text):
        if not isinstance(text, str) or not text.strip():
            raise InvalidRequestError("empty text")
        if len(text) > PLANNING_TEXT_LIMIT:
            raise InvalidRequestError("text is longer than %d characters" % PLANNING_TEXT_LIMIT)
        return text.strip()
