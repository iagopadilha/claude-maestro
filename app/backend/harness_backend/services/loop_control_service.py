"""User commands on the loop of ONE project: check the preflight, start, answer, resume, note, pause and instruct.

Port of `loop_check`/`loop_start`/`answer`/`resume`/`note`/`pause`/`instruction`/`relaunch` of the legacy panel, same
rules and responses. Each public method returns `(http_status, body)`. Only the user's click reaches here (token on the
server); the orchestrator/Claude never triggers these commands.
"""
import datetime
import math
import os
import re
import sys
import threading
from typing import Protocol

from ..domain.roadmap_settings import parse_roadmap_e2e
from ..infrastructure.claude_client import redact_secrets
from ..infrastructure.process_runner import START_FAILURE_CODE, TIMEOUT_CODE
from ..repositories.answers_repository import USER_INSTRUCTION_QUESTION, USER_NOTE_QUESTION
from .dispatch_default import compute_default_dispatch

LAUNCH_MARKER_PREFIX = "[loop-launch]"  # read by WorkspaceService to find the current launch in monitor-relaunch.log
STATUS_RUNNING = "running"
STATUS_AWAITING_USER = "awaiting_user"
STATUSES_RESUMABLE_BY_INSTRUCTION = ("error", "stopped", "done")
LOOP_BODY_FIELDS = ("wave", "e2e", "push", "setup", "budget", "max_cost", "timeout", "branch", "continue_on_blocked", "pause_on_blocked")
MIN_WAVE, MAX_WAVE = 1, 8
MIN_TURN_TIMEOUT_MINUTES, MAX_TURN_TIMEOUT_MINUTES = 1, 600
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,80}")
SETUP_LIMIT = 200
REPOS_NOT_DEFINED_CODE = "repos_not_defined"
REPOS_NOT_DEFINED_MESSAGE = ("Define the repositories first: describe the work and let the AI propose which repositories will be changed, or tick "
                             "them by hand (Repositories, \"edit\"), then confirm. The loop creates their worktrees by itself when it starts.")
CHECK_TIMEOUT_SECONDS = 60
CHECK_MESSAGE_LIMIT = 500
INSTRUCTION_ATTACHMENT_LIMIT = 2000
MAX_INSTRUCTION_ATTACHMENTS = 5
FEATURE_SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")
MONITOR_URL_VARIABLE = "LOOP_MONITOR_URL"
CHECK_PROBLEM_MARK = "[check] PROBLEM:"
CHECK_WARNING_MARK = "[check] WARNING:"
CHECK_READY_MARK = "[check] READY"
CHECK_COMMAND_MARK = "Command:"
ATTACHED_CONTEXT_HEADER = "Attached context:"
INSTRUCTION_RESULT_NOTE = "note"
INSTRUCTION_RESULT_ANSWER = "answer"
INSTRUCTION_RESULT_NOTE_AND_RESUME = "note+resume"
LOOP_ALREADY_RUNNING_ERROR = "a loop is already running (or being relaunched) in this worktree"
MISSING_LOOP_COMMAND_ERROR = "LOOP-CMD.json missing or without argv: run run_loop.py once to record the command"
NO_LOOP_ERROR = "there is no loop yet: start one from the New loop panel"


def is_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_non_negative_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return not (isinstance(value, float) and (math.isnan(value) or math.isinf(value))) and value >= 0


def convert_body_to_loop_flags(body):
    """Strict whitelist of the loop/check and loop/start body -> (flags, None) or (None, error). Never uses a shell."""
    unknown = sorted(set(body) - set(LOOP_BODY_FIELDS))
    if unknown:
        return None, "unknown field: %s" % ", ".join(str(name) for name in unknown)
    flags = []
    if "wave" in body:
        if not is_integer(body["wave"]) or not MIN_WAVE <= body["wave"] <= MAX_WAVE:
            return None, "wave must be an integer from %d to %d" % (MIN_WAVE, MAX_WAVE)
        flags += ["--wave", str(body["wave"])]
    for name in ("e2e", "push", "continue_on_blocked", "pause_on_blocked"):
        if name in body and not isinstance(body[name], bool):
            return None, "%s must be a boolean" % name
    if body.get("e2e"):
        flags.append("--e2e")
    if body.get("push") is False:
        flags.append("--no-push")
    if body.get("continue_on_blocked"):
        flags.append("--continue-on-blocked")
    if body.get("pause_on_blocked"):
        flags.append("--pause-on-blocked")
    for name, flag in (("budget", "--budget"), ("max_cost", "--max-cost")):
        if name in body:
            if not is_finite_non_negative_number(body[name]):
                return None, "%s must be a number greater than or equal to 0" % name
            flags += [flag, str(body[name])]
    if "timeout" in body:
        if not is_integer(body["timeout"]) or not MIN_TURN_TIMEOUT_MINUTES <= body["timeout"] <= MAX_TURN_TIMEOUT_MINUTES:
            return None, "timeout must be an integer from %d to %d (minutes)" % (MIN_TURN_TIMEOUT_MINUTES, MAX_TURN_TIMEOUT_MINUTES)
        flags += ["--timeout", str(body["timeout"])]
    branch = body.get("branch", "")
    if not isinstance(branch, str):
        return None, "branch must be text"
    if branch:
        if not BRANCH_PATTERN.fullmatch(branch) or ".." in branch or branch.startswith("-"):
            return None, "invalid branch: use only letters, digits, . _ / - (up to 80), without '..' and not starting with '-'"
        flags += ["--branch", branch]
    setup = body.get("setup", "")
    if not isinstance(setup, str):
        return None, "setup must be text"
    if len(setup) > SETUP_LIMIT:
        return None, "setup is longer than %d characters" % SETUP_LIMIT
    if setup:
        flags += ["--setup=" + setup] if setup.startswith("-") else ["--setup", setup]  # a single argument, never mistaken for a flag
    return flags, None


def parse_check_output(text):
    """Lines `[check] PROBLEM:`, `[check] WARNING:` and `[check] READY. Command:` (+ command on the same line or the next)."""
    problems, warnings, command, ready = [], [], "", False
    lines = text.splitlines()
    for index, raw in enumerate(lines):
        line = raw.strip()
        if line.startswith(CHECK_PROBLEM_MARK):
            problems.append(line[len(CHECK_PROBLEM_MARK):].strip())
        elif line.startswith(CHECK_WARNING_MARK):
            warnings.append(line[len(CHECK_WARNING_MARK):].strip())
        elif line.startswith(CHECK_READY_MARK):
            ready = True
            on_same_line = line.partition(CHECK_COMMAND_MARK)[2].strip()
            command = on_same_line or (lines[index + 1].strip() if index + 1 < len(lines) else "")
    return {"ready": ready and not problems, "problems": problems, "warnings": warnings, "command": command}


def compose_instruction_text(text, feature, attachments):
    composed = ("[%s] " % feature if feature else "") + text.strip()
    if attachments:
        composed += "\n" + ATTACHED_CONTEXT_HEADER + "\n" + "\n".join("- " + item.strip() for item in attachments if item.strip())
    return composed


class ILoopControlService(Protocol):
    def check_preflight(self, context, body) -> tuple: ...
    def start_new_loop(self, context, body) -> tuple: ...
    def answer_loop_question(self, context, body) -> tuple: ...
    def resume_loop(self, context) -> tuple: ...
    def write_user_note(self, context, body) -> tuple: ...
    def request_loop_pause(self, context) -> tuple: ...
    def send_instruction_to_loop(self, context, body) -> tuple: ...
    def has_live_relaunch(self, project_id) -> bool: ...
    def is_busy(self) -> bool: ...


class LoopControlService:
    def __init__(self, config, log, loop_status_service, process_runner, loop_command_file_repository, answers_repository,
                 get_app_url):
        self.config = config
        self.log = log
        self.loop_status_service = loop_status_service
        self.process_runner = process_runner
        self.loop_command_file_repository = loop_command_file_repository
        self.answers_repository = answers_repository
        self.get_app_url = get_app_url
        self.scope_defined_provider = None  # (context) -> bool, set by the multi-repo module; None = no gate (tests, bare module)
        self.registry_lock = threading.Lock()
        self.relaunch_locks_by_project = {}
        self.relaunched_pid_by_project = {}

    # ------------------------------------------------ busy state and locks
    def get_relaunch_lock(self, project_id):
        with self.registry_lock:
            return self.relaunch_locks_by_project.setdefault(project_id, threading.Lock())

    def has_live_relaunch(self, project_id):
        return self.process_runner.is_alive(self.relaunched_pid_by_project.get(project_id, 0))

    def is_busy(self):
        """IBusyProvider: a loop launched by this app that is still alive keeps the app on."""
        with self.registry_lock:
            pids = list(self.relaunched_pid_by_project.values())
        return any(self.process_runner.is_alive(pid) for pid in pids)

    def is_loop_alive(self, context):
        return self.loop_status_service.get_loop_summary(context)["running"] or self.has_live_relaunch(context.project.id)

    def get_project_panel_url(self, context):
        return "%sp/%s/" % (self.get_app_url() or "", context.project.id)

    # ------------------------------------------------ process launch
    def spawn_detached_loop(self, context, action, argv, folder, extra_env=None):
        """Call with the relaunch lock held. Output goes to `internal/monitor-relaunch.log`."""
        log_path = self.loop_command_file_repository.get_relaunch_log_path(context.work_dir)
        launch_env = dict(extra_env or {}, PYTHONUNBUFFERED="1")  # step lines reach the log at once (the panel reads them while it runs)
        try:
            self.write_launch_marker(log_path, action)
            pid = self.process_runner.spawn_detached(argv, folder, log_path, launch_env)
        except OSError as failure:
            self.log.error("loop_command_failed", project=context.project.id, action=action, error=str(failure)[:300])
            return 500, {"ok": False, "error": "could not relaunch the loop: %s" % failure}
        self.relaunched_pid_by_project[context.project.id] = pid
        self.log.info("loop_command_launched", project=context.project.id, action=action, pid=pid)
        return 200, {"ok": True, "pid": pid}

    @staticmethod
    def write_launch_marker(log_path, action):
        """One `[loop-launch] <iso time> <action>` line right before the process starts: the log accumulates across launches and the
        workspace reader needs to know where the CURRENT launch begins (everything after the last marker)."""
        os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(log_path, "ab") as handle:
            handle.write(("%s %s %s\n" % (LAUNCH_MARKER_PREFIX, stamp, action)).encode("utf-8"))

    def relaunch_loop_from_saved_command(self, context, action, answer=None):
        """Detached run_loop.py with the argv of LOOP-CMD.json (+ `--answer <answer>`)."""
        with self.get_relaunch_lock(context.project.id):
            if self.is_loop_alive(context):
                return 409, {"ok": False, "error": LOOP_ALREADY_RUNNING_ERROR}
            command = self.loop_command_file_repository.read_loop_command(context.work_dir)
            argv = command.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                return 409, {"ok": False, "error": MISSING_LOOP_COMMAND_ERROR}
            argv = list(argv) + (["--answer", answer] if answer else [])
            folder = command.get("cwd") if isinstance(command.get("cwd"), str) and os.path.isdir(command.get("cwd")) else context.repo_path
            return self.spawn_detached_loop(context, action, argv, folder)

    def run_loop_check(self, context, flags):
        """Synchronous `run_loop.py <repo> --check <flags>` (reads the output, creates nothing, spends nothing)."""
        argv = [sys.executable, self.config.engine_script_path("run_loop"), context.repo_path, "--check"] + flags
        code, output, error = self.process_runner.run_and_wait(argv, context.repo_path, CHECK_TIMEOUT_SECONDS)
        if code == TIMEOUT_CODE:
            return 504, {"ok": False, "error": "the --check took longer than %d s" % CHECK_TIMEOUT_SECONDS}
        if code == START_FAILURE_CODE and not output:
            return 500, {"ok": False, "error": "could not run run_loop.py: %s" % redact_secrets(error, CHECK_MESSAGE_LIMIT)}
        result = parse_check_output(output or "")
        if not result["ready"] and not result["problems"]:
            last_lines = (error or output or "").strip().splitlines()
            result["problems"] = [last_lines[-1] if last_lines else "the --check did not confirm READY (code %s)" % code]
        result["problems"] = [redact_secrets(item, CHECK_MESSAGE_LIMIT) for item in result["problems"]]
        result["warnings"] = [redact_secrets(item, CHECK_MESSAGE_LIMIT) for item in result["warnings"]]
        return 200, dict({"ok": True}, **result)

    # ------------------------------------------------ commands
    def convert_body_with_roadmap_defaults(self, context, body):
        """Flags of the body + the project-wide E2E decided in the approved ROADMAP (`**E2E:** yes`). An `e2e` sent explicitly in the
        body (old clients) wins over the ROADMAP, both ways; without it the ROADMAP decides (absent = no)."""
        flags, _dispatch, error = self.resolve_flags_and_dispatch(context, body)
        return flags, error

    def resolve_flags_and_dispatch(self, context, body):
        """(flags, dispatch, error). Without `wave` in the body the dispatch is computed from the ROADMAP (auto); an explicit one wins."""
        flags, error = convert_body_to_loop_flags(body)
        if error:
            return None, None, error
        roadmap = None
        if "e2e" not in body:
            roadmap = self.read_roadmap_text(context)
            if parse_roadmap_e2e(roadmap):
                flags.append("--e2e")
        if "wave" in body:
            return flags, {"wave": body["wave"], "auto": False, "reason": "chosen by you"}, None
        dispatch = compute_default_dispatch(roadmap if roadmap is not None else self.read_roadmap_text(context))
        if dispatch["wave"] > MIN_WAVE:
            flags += ["--wave", str(dispatch["wave"])]
        return flags, dispatch, None

    @staticmethod
    def read_roadmap_text(context):
        try:
            with open(os.path.join(context.specs_dir, "project", "ROADMAP.md"), encoding="utf-8", errors="replace") as handle:
                return handle.read()
        except OSError:
            return ""

    def check_preflight(self, context, body):
        flags, dispatch, error = self.resolve_flags_and_dispatch(context, body)
        if error:
            return 400, {"ok": False, "error": error}
        self.log.info("loop_command_launched", project=context.project.id, action="loop_check", pid=None)
        status, result = self.run_loop_check(context, flags)
        return status, (dict(result, dispatch=dispatch) if status == 200 else result)

    def start_new_loop(self, context, body):
        """New loop (or a continuation, if the worktree exists): preflight first; `LOOP_MONITOR_URL` = this project's panel."""
        flags, _dispatch, error = self.resolve_flags_and_dispatch(context, body)
        if error:
            return 400, {"ok": False, "error": error}
        with self.get_relaunch_lock(context.project.id):
            if self.is_loop_alive(context):
                return 409, {"ok": False, "error": "a loop is already running (or being started) in this worktree"}
            if (self.scope_defined_provider and not os.path.isdir(context.project.loop_worktree_path())
                    and not self.scope_defined_provider(context)):  # a loop that already exists keeps the scope it has
                return 409, {"ok": False, "code": REPOS_NOT_DEFINED_CODE, "error": REPOS_NOT_DEFINED_MESSAGE}
            status, checked = self.run_loop_check(context, flags)
            if status != 200:
                return status, checked
            if not checked["ready"]:
                return 409, {"ok": False, "error": "preflight failed: the loop was not started", "problems": checked["problems"],
                             "warnings": checked["warnings"]}
            argv = [sys.executable, self.config.engine_script_path("run_loop"), context.repo_path] + flags
            return self.spawn_detached_loop(context, "loop_start", argv, context.repo_path,
                                            {MONITOR_URL_VARIABLE: self.get_project_panel_url(context)})

    def answer_loop_question(self, context, body):
        summary = self.loop_status_service.get_loop_summary(context)
        if summary["status"] != STATUS_AWAITING_USER:
            return 409, {"ok": False, "error": "the loop is not waiting for an answer (status: %s)" % summary["status"]}
        text, option = body.get("text"), body.get("option")
        if option is not None and not is_integer(option):
            return 400, {"ok": False, "error": "option must be an integer"}
        if option is not None:
            loop_json = summary.get("json") if isinstance(summary.get("json"), dict) else {}
            options = loop_json.get("options") if isinstance(loop_json.get("options"), list) else []
            if not 1 <= option <= len(options):
                return 400, {"ok": False, "error": "invalid option %d: the notice listed %d option(s)" % (option, len(options))}
            text = str(options[option - 1])
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "give a text or an option"}
        return self.relaunch_loop_from_saved_command(context, "answer", text.strip())

    def resume_loop(self, context):
        if self.loop_status_service.get_loop_summary(context)["running"]:
            return 409, {"ok": False, "error": "the loop is already running"}
        return self.relaunch_loop_from_saved_command(context, "resume")

    def write_user_note(self, context, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return 400, {"ok": False, "error": "empty text"}
        if self.loop_status_service.get_loop_summary(context)["status"] != STATUS_RUNNING:
            return 409, {"ok": False, "error": "a note only works with the loop running (when stopped, use answer or resume)"}
        self.answers_repository.append_pending_answer(context.work_dir, USER_NOTE_QUESTION, text)
        self.log.info("loop_command_launched", project=context.project.id, action="note", pid=None)
        return 200, {"ok": True}

    def request_loop_pause(self, context):
        if self.loop_status_service.get_loop_summary(context)["status"] != STATUS_RUNNING:
            return 409, {"ok": False, "error": "the loop can only be paused while it is running"}
        self.loop_command_file_repository.create_pause_file(context.work_dir)
        self.log.info("loop_command_launched", project=context.project.id, action="pause", pid=None)
        return 200, {"ok": True}

    def validate_instruction_body(self, body):
        """(text, feature, attachments, None) or (None, None, None, error)."""
        text, feature, attachments = body.get("text"), body.get("feature"), body.get("attachments", [])
        if not isinstance(text, str) or not text.strip():
            return None, None, None, "empty text"
        if feature is not None and not (isinstance(feature, str) and FEATURE_SLUG_PATTERN.fullmatch(feature)):
            return None, None, None, "invalid feature"
        attachments = [] if attachments is None else attachments
        if (not isinstance(attachments, list) or len(attachments) > MAX_INSTRUCTION_ATTACHMENTS
                or not all(isinstance(item, str) and len(item) <= INSTRUCTION_ATTACHMENT_LIMIT for item in attachments)):
            return None, None, None, "attachments: up to %d texts of up to %d characters" % (MAX_INSTRUCTION_ATTACHMENTS,
                                                                                               INSTRUCTION_ATTACHMENT_LIMIT)
        return text, feature, attachments, None

    def send_instruction_to_loop(self, context, body):
        """Instruction to the CURRENT version: note (running), answer (awaiting), note+resume (error/stopped/done)."""
        text, feature, attachments, error = self.validate_instruction_body(body)
        if error:
            return 400, {"ok": False, "error": error}
        composed = compose_instruction_text(text, feature, attachments)
        status = self.loop_status_service.get_loop_summary(context)["status"]
        if status == STATUS_RUNNING:
            self.answers_repository.append_pending_answer(context.work_dir, USER_INSTRUCTION_QUESTION, composed)
            self.log.info("loop_command_launched", project=context.project.id, action="instruction_note", pid=None)
            return 200, {"ok": True, "action": INSTRUCTION_RESULT_NOTE}
        if status == STATUS_AWAITING_USER:
            code, data = self.answer_loop_question(context, {"text": composed})
            return code, (dict(data, action=INSTRUCTION_RESULT_ANSWER) if code == 200 else data)
        if status in STATUSES_RESUMABLE_BY_INSTRUCTION:
            return self.write_instruction_and_resume(context, composed)
        return 409, {"ok": False, "error": NO_LOOP_ERROR}

    def write_instruction_and_resume(self, context, composed):
        command = self.loop_command_file_repository.read_loop_command(context.work_dir)
        if not isinstance(command.get("argv"), list) or not command["argv"]:
            return 409, {"ok": False, "error": "LOOP-CMD.json missing: run run_loop.py once to record the command"}
        if self.is_loop_alive(context):
            return 409, {"ok": False, "error": LOOP_ALREADY_RUNNING_ERROR}
        self.answers_repository.append_pending_answer(context.work_dir, USER_INSTRUCTION_QUESTION, composed)
        code, data = self.relaunch_loop_from_saved_command(context, "instruction_resume")
        return code, (dict(data, action=INSTRUCTION_RESULT_NOTE_AND_RESUME) if code == 200 else data)
