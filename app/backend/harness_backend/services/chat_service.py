"""Chat with the AI, ALWAYS read-only and independent of the loop (never touches LOCK/LOOP.json/ANSWERS.md).

`claude -p` (sonnet, US$ 1, only Read/Grep/Glob, no MCP, text through stdin, environment without secrets), cwd = the
project's work. Context: `vNNN:orch` with a known session = `--resume <session> --fork-session`; a subagent or no session =
a prompt with the role, initial prompt, last actions, final return and the feature folder (by path); `general` = `.specs`
files by path. An existing thread continues its own session (`--resume`, no fork). Limits: 1 job per thread, 3 in the app.
"""
import os
import re
import threading
from typing import Protocol

from ..infrastructure.claude_client import redact_secrets
from ..repositories.chat_repository import THREAD_BUSY_REASON, generate_thread_id
from ..repositories.loop_command_file_repository import loop_state, now_iso

CHAT_TIMEOUT_SECONDS = 180
MAX_CONCURRENT_JOBS = 3
QUESTION_TEXT_LIMIT = 8000
REPLY_LIMIT = 20000
TITLE_LIMIT = 60
CONTEXT_ACTION_TEXT_LIMIT = 200
LAST_ACTIONS_COUNT = 8
MAX_TURN_NUMBER = 999999
READ_ONLY_PREFIX = "You are in READ-ONLY mode. Answer questions; do not claim to have changed anything."
USER_QUESTION_HEADER = "User question:\n"
CHAT_MODEL = "sonnet"
CHAT_BUDGET_USD = 1
CHAT_ALLOWED_TOOLS = ["Read", "Grep", "Glob"]
CHAT_DISALLOWED_TOOLS = ["Bash", "Edit", "Write", "NotebookEdit"]
QUESTION_MODE = "question"
ROLE_USER = "user"
ROLE_AI = "ai"
SCOPE_TYPE_AGENT = "agent"
SCOPE_TYPE_GENERAL = "general"
SCOPE_TYPE_ERROR = "error"
SCOPE_TYPE_QUARANTINE = "quarantine"
SCOPE_TYPE_PLAN = "plan"
SCOPE_TYPES = (SCOPE_TYPE_AGENT, SCOPE_TYPE_GENERAL, SCOPE_TYPE_ERROR, SCOPE_TYPE_QUARANTINE, SCOPE_TYPE_PLAN)
PLAN_SCOPE_PREFIX = ("This chat only answers questions about the project plan: the roadmap, the specs, designs and tasks, the codebase maps "
                     "(docs/codebase), the AI decisions and the loop progress. If the question is about something else, say you can only "
                     "answer about those and stop. Read the files with Read, Grep and Glob; never claim to have changed anything.")
ERROR_ID_LIMIT = 200
ORCHESTRATOR_AGENT_ID = "orch"
ORCHESTRATOR_ROLE = "orchestrator"
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
UNEXPECTED_FAILURE = "unexpected failure"
AGENT_ID_PATTERN = re.compile(r"^[A-Za-z0-9:_.-]{1,120}$")
HISTORY_AGENT_ID_PATTERN = re.compile(r"^v(\d{1,6}):([A-Za-z0-9_-]{1,80})$")
FEATURE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def busy_jobs_error():
    return "there are %d replies in progress; wait for one to finish" % MAX_CONCURRENT_JOBS


def validate_chat_scope(scope):
    """Normalized scope {"type","agent_id","turn"} or None when invalid. `error` adds `error_id` and `quarantine` adds `feature`."""
    if not isinstance(scope, dict) or scope.get("type") not in SCOPE_TYPES:
        return None
    if scope["type"] == SCOPE_TYPE_ERROR:
        error_id = scope.get("error_id")
        valid = isinstance(error_id, str) and 0 < len(error_id) <= ERROR_ID_LIMIT and error_id.isprintable()
        return {"type": SCOPE_TYPE_ERROR, "agent_id": None, "turn": None, "error_id": error_id} if valid else None
    if scope["type"] == SCOPE_TYPE_QUARANTINE:
        feature = scope.get("feature")
        valid = isinstance(feature, str) and FEATURE_SLUG_PATTERN.fullmatch(feature)
        return {"type": SCOPE_TYPE_QUARANTINE, "agent_id": None, "turn": None, "feature": feature} if valid else None
    if scope["type"] == SCOPE_TYPE_PLAN:
        return {"type": SCOPE_TYPE_PLAN, "agent_id": None, "turn": None}
    agent_id, turn = scope.get("agent_id"), scope.get("turn")
    if agent_id is not None and not (isinstance(agent_id, str) and AGENT_ID_PATTERN.fullmatch(agent_id)):
        return None
    if turn is not None and not (is_integer(turn) and 0 <= turn <= MAX_TURN_NUMBER):
        return None
    return {"type": scope["type"], "agent_id": agent_id, "turn": turn}


class IChatService(Protocol):
    def send_question(self, context, body) -> tuple: ...
    def get_project_job(self, context, job_id) -> tuple: ...
    def list_project_threads(self, context, agent_id, turn) -> tuple: ...
    def get_project_thread(self, context, thread_id) -> tuple: ...
    def is_busy(self) -> bool: ...


class ChatService:
    def __init__(self, log, chat_repository, chat_job_repository, agent_transcript_repository, claude_client):
        self.log = log
        self.chat_repository = chat_repository
        self.chat_job_repository = chat_job_repository
        self.agent_transcript_repository = agent_transcript_repository
        self.claude_client = claude_client
        self.timeout_seconds = CHAT_TIMEOUT_SECONDS
        self.send_lock = threading.Lock()
        self.scope_context_provider = None  # object with build_error_context(context, error_id) and build_quarantine_context(context, slug); set by the composition

    def is_busy(self):
        """IBusyProvider: a chat (or router) reply in progress keeps the app on."""
        return self.chat_job_repository.count_running_jobs() > 0

    # ------------------------------------------------ context
    @staticmethod
    def describe_feature_dir(work_dir, slug):
        return "%s (read with Read: spec.md and the other files)" % loop_state.feature_dir(work_dir, slug)

    @staticmethod
    def build_general_context(work_dir):
        paths = [os.path.join(work_dir, ".specs", "project", "ROADMAP.md"), loop_state.state_file(work_dir, "STATE.md"),
                 loop_state.state_file(work_dir, "VALIDATE.md"), loop_state.state_file(work_dir, "LOOP.json")]
        return "General loop context. Read what you need with Read:\n" + "\n".join("- " + path for path in paths)

    @staticmethod
    def build_plan_context(work_dir):
        """Context of the `plan` scope: the limiting prefix plus where the plan files live (the work folder = the worktree when it exists)."""
        folders = [os.path.join(work_dir, ".specs", "project"), os.path.join(work_dir, ".specs", "features"),
                   os.path.join(work_dir, "docs", "codebase")]
        return PLAN_SCOPE_PREFIX + "\nPlan files, read what you need with Read/Glob:\n" + "\n".join("- " + path for path in folders)

    def build_agent_context(self, work_dir, agent_id):
        """(session_id for resume+fork or None, context text) of the `agent` scope."""
        found = HISTORY_AGENT_ID_PATTERN.match(agent_id or "")
        if found:
            return self.build_history_agent_context(work_dir, int(found.group(1)), found.group(2))
        slug = (agent_id or "").split(":")[0]
        if agent_id and agent_id != ORCHESTRATOR_AGENT_ID and FEATURE_SLUG_PATTERN.fullmatch(slug):
            return None, "Agent %s (feature %s). Feature files: %s\n\n%s" % (
                agent_id, slug, self.describe_feature_dir(work_dir, slug), self.build_general_context(work_dir))
        return None, self.build_general_context(work_dir)

    def build_history_agent_context(self, work_dir, turn_number, agent_name):
        session_id = self.agent_transcript_repository.read_turn_session_id(work_dir, turn_number)
        main_path, subagents = self.agent_transcript_repository.locate_session_files(work_dir, session_id)
        if agent_name == ORCHESTRATOR_AGENT_ID:
            if session_id:
                return session_id, ""
            summary = self.agent_transcript_repository.read_transcript_summary(main_path) if main_path else None
            role = ORCHESTRATOR_ROLE
        else:
            files = next((item for item in subagents if item[0] == agent_name), None)
            summary = self.agent_transcript_repository.read_transcript_summary(files[1]) if files else None
            role = self.agent_transcript_repository.read_agent_type(files[2]) if files else None
        if summary is None:
            return None, self.build_general_context(work_dir)
        actions = ["- %s %s" % (item["type"], item["text"][:CONTEXT_ACTION_TEXT_LIMIT]) for item in summary["actions"]][-LAST_ACTIONS_COUNT:]
        parts = ["Agent %s of turn %d (feature: %s)." % (role or "?", turn_number, summary.get("feature") or "?"),
                 "Initial agent prompt:\n" + (summary.get("initial_prompt") or "(unavailable)"),
                 "Last actions:\n" + ("\n".join(actions) or "(none)"),
                 "Final return:\n" + (summary.get("final_return") or "(no return yet)")]
        if summary.get("feature"):
            parts.append("Feature files: " + self.describe_feature_dir(work_dir, summary["feature"]))
        return None, redact_secrets("\n\n".join(parts))

    def build_scope_context(self, context, scope):
        """Server-built context of an `error` or `quarantine` scope; None when the item does not exist (or no provider is wired)."""
        if self.scope_context_provider is None:
            return None
        if scope["type"] == SCOPE_TYPE_ERROR:
            return self.scope_context_provider.build_error_context(context, scope["error_id"])
        return self.scope_context_provider.build_quarantine_context(context, scope["feature"])

    # ------------------------------------------------ send
    def send_question(self, context, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "empty text"}
        if len(text) > QUESTION_TEXT_LIMIT:
            return 413, {"ok": False, "error": "text is longer than %d characters" % QUESTION_TEXT_LIMIT}
        work_dir = context.work_dir
        thread_id = body.get("thread")
        with self.send_lock:
            if thread_id is not None and self.chat_job_repository.has_running_job_in_thread(thread_id):
                return 409, {"ok": False, "error": "this thread already has a reply in progress"}
            if self.chat_job_repository.count_running_jobs() >= MAX_CONCURRENT_JOBS:
                return 409, {"ok": False, "error": busy_jobs_error()}
            if thread_id is not None:
                thread = self.chat_repository.read_thread(work_dir, thread_id)
                if thread is None:
                    return 404, {"ok": False, "error": "thread not found"}
                session, context_text, fork = thread.get("session_id"), "", False
                if not session:
                    context_text = thread.get("context") or ""
            else:
                scope = validate_chat_scope(body.get("scope") or {"type": SCOPE_TYPE_GENERAL})
                if scope is None:
                    return 400, {"ok": False, "error": "invalid scope"}
                if scope["type"] in (SCOPE_TYPE_ERROR, SCOPE_TYPE_QUARANTINE):
                    session, context_text = None, self.build_scope_context(context, scope)
                    if context_text is None:
                        return 404, {"ok": False, "error": "%s not found" % ("error" if scope["type"] == SCOPE_TYPE_ERROR else "quarantined feature")}
                elif scope["type"] == SCOPE_TYPE_PLAN:
                    session, context_text = None, self.build_plan_context(work_dir)
                elif scope["type"] == SCOPE_TYPE_AGENT and scope["agent_id"]:
                    session, context_text = self.build_agent_context(work_dir, scope["agent_id"])
                else:
                    session, context_text = None, self.build_general_context(work_dir)
                fork = bool(session)
                thread_id = generate_thread_id()
                thread = {"thread": thread_id, "scope": scope, "title": " ".join(text.split())[:TITLE_LIMIT], "session_id": None,
                          "context": context_text, "created_at": now_iso(), "messages": [], "cost_usd": 0}
            job_id, reason = self.chat_job_repository.reserve_job(context.project.id, thread_id, MAX_CONCURRENT_JOBS)
            if job_id is None:
                message = "this thread already has a reply in progress" if reason == THREAD_BUSY_REASON else busy_jobs_error()
                return 409, {"ok": False, "error": message}
            thread["messages"].append({"role": ROLE_USER, "text": text.strip(), "t": now_iso(), "cost_usd": None, "mode": QUESTION_MODE})
            self.chat_repository.write_thread(work_dir, thread)
        argv = self.claude_client.build_command(CHAT_MODEL, CHAT_BUDGET_USD, CHAT_ALLOWED_TOOLS, CHAT_DISALLOWED_TOOLS, no_mcp=True,
                                                resume_session=session or "", fork_session=fork)
        stdin_text = READ_ONLY_PREFIX + "\n\n" + (context_text + "\n\n" if context_text else "") + USER_QUESTION_HEADER + text.strip()
        run_dir = work_dir if os.path.isdir(work_dir) else context.repo_path
        self.log.info("chat_job_started", project=context.project.id, job=job_id, thread=thread_id, resuming_session=bool(session))
        threading.Thread(target=self.run_chat_job, args=(context, job_id, thread_id, argv, stdin_text, run_dir), daemon=True).start()
        return 202, {"ok": True, "job": job_id, "thread": thread_id}

    def run_chat_job(self, context, job_id, thread_id, argv, stdin_text, run_dir):
        result = {"state": JOB_STATE_ERROR, "reply": None, "cost_usd": None, "error": UNEXPECTED_FAILURE, "thread": thread_id}
        session_id = None
        try:
            returned = self.claude_client.run_prompt(argv, run_dir, stdin_text, self.timeout_seconds)
            if returned["ok"]:
                session_id = returned.get("session_id") or None
                cost = returned["cost_usd"] if is_number(returned.get("cost_usd")) else None
                result = {"state": JOB_STATE_OK, "reply": redact_secrets(returned.get("reply"), REPLY_LIMIT), "cost_usd": cost,
                          "error": None, "thread": thread_id}
            else:
                result["error"] = returned.get("error") or UNEXPECTED_FAILURE
            if result["state"] == JOB_STATE_OK:
                self.write_reply_to_thread(context.work_dir, thread_id, result, session_id)
        except Exception as failure:  # the job never takes the app down
            result = dict(result, state=JOB_STATE_ERROR, reply=None, error="failed to run claude: %s" % type(failure).__name__)
        finally:
            self.chat_job_repository.finish_job(job_id, result)
            self.log.info("chat_job_finished", project=context.project.id, job=job_id, state=result["state"])

    def write_reply_to_thread(self, work_dir, thread_id, result, session_id):
        with self.send_lock:
            thread = self.chat_repository.read_thread(work_dir, thread_id)
            if thread is None:
                return
            thread["messages"].append({"role": ROLE_AI, "text": result["reply"], "t": now_iso(), "cost_usd": result["cost_usd"],
                                       "mode": QUESTION_MODE})
            previous_cost = thread.get("cost_usd") if is_number(thread.get("cost_usd")) else 0
            thread["cost_usd"] = round(previous_cost + (result["cost_usd"] or 0), 6)
            thread["session_id"] = session_id or thread.get("session_id")
            self.chat_repository.write_thread(work_dir, thread)

    # ------------------------------------------------ read
    def get_project_job(self, context, job_id):
        """Chat or router job. A job of another project = 404 (each panel only sees its own)."""
        job = self.chat_job_repository.get_job(job_id)
        if job is None or job.pop("project_id", None) != context.project.id:
            return 404, {"ok": False, "error": "job not found"}
        return 200, job

    def list_project_threads(self, context, agent_id, turn):
        rows = []
        for thread in self.chat_repository.list_threads(context.work_dir):
            scope = thread.get("scope") if isinstance(thread.get("scope"), dict) else {}
            if agent_id is not None and scope.get("agent_id") != agent_id:
                continue
            if turn is not None and scope.get("turn") != turn:
                continue
            rows.append({"thread": thread["thread"], "title": thread.get("title") or "", "scope": scope,
                         "updated_at": thread.get("updated_at"), "message_count": len(thread["messages"]),
                         "cost_usd": thread.get("cost_usd") if is_number(thread.get("cost_usd")) else 0})
        rows.sort(key=lambda row: str(row["updated_at"] or ""), reverse=True)
        return 200, {"threads": rows}

    def get_project_thread(self, context, thread_id):
        thread = self.chat_repository.read_thread(context.work_dir, thread_id)
        if thread is None:
            return 404, {"ok": False, "error": "thread not found"}
        return 200, {"thread": thread_id, "scope": thread.get("scope"), "messages": thread["messages"]}
