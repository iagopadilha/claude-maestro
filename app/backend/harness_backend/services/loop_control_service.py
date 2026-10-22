"""User commands on the loop of ONE project: check the preflight, start, answer, resume, note, pause and instruct.

Port of `loop_check`/`loop_start`/`answer`/`resume`/`note`/`pause`/`instruction`/`relaunch` of the legacy panel, same
rules and responses. Each public method returns `(http_status, body)`. Only the user's click reaches here (token on the
server); the orchestrator/Claude never triggers these commands.
"""
import datetime
import math
import os
import re
import subprocess
import sys
import threading
from typing import Protocol

from ..domain.roadmap_settings import parse_loop_settings, read_roadmap_base_branch
from ..infrastructure.claude_client import redact_secrets
from ..infrastructure.process_runner import START_FAILURE_CODE, TIMEOUT_CODE
from ..repositories.answers_repository import USER_INSTRUCTION_QUESTION, USER_NOTE_QUESTION
from ..repositories.loop_command_file_repository import read_json_object, write_json_atomic
from .dispatch_default import compute_default_dispatch
from .loop_worktree_service import get_integration_branch
from .planning_gates import resolve_base_branch

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import context_files  # noqa: E402
import loop_state  # noqa: E402
import project_repos  # noqa: E402
import subprojects  # noqa: E402

LAUNCH_MARKER_PREFIX = "[loop-launch]"  # read by WorkspaceService to find the current launch in monitor-relaunch.log
STATUS_RUNNING = "running"
STATUS_AWAITING_USER = "awaiting_user"
STATUSES_RESUMABLE_BY_INSTRUCTION = ("error", "stopped", "done")
LEFTOVER_CHOICES = ("commit", "discard")
RUN_CHOICE_FIELDS = ("push", "remove_worktree_on_finish")  # the only keys of the start/check body; everything else is decided in the ROADMAP
MIN_WAVE = 1
ENGINE_DEFAULT_BUDGET_TURN, ENGINE_DEFAULT_MAX_COST, ENGINE_DEFAULT_TURN_MINUTES = 5.0, 50.0, 60  # run_loop.py defaults: not repeated in the argv
AGENTS_MODEL = "sonnet"  # subagents keep `model: sonnet` in their frontmatter (MODEL-03)
DELIVERY_PENDING_FILE_NAME = "delivery-pending.json"  # internal/ of the REPOSITORY: the choice made before the worktree exists
REPOS_NOT_DEFINED_CODE = "repos_not_defined"
REPOS_NOT_DEFINED_MESSAGE = ("Define the repositories first: tell the planning chat which repositories to change and which only to read, or "
                             "choose them by hand (Repositories) and confirm. The loop creates the worktrees by itself when it starts.")
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


def format_amount(value):
    """`5.0` -> "5", `7.5` -> "7.5" (the text the engine option receives)."""
    return str(int(value)) if float(value).is_integer() else str(value)


def read_run_choices(body):
    """({"push": bool|None, "remove_worktree_on_finish": bool|None}, None) or (None, error). Other body keys are ignored."""
    body = body if isinstance(body, dict) else {}
    choices = {}
    for name in RUN_CHOICE_FIELDS:
        if name in body and body[name] is not None and not isinstance(body[name], bool):
            return None, "%s must be a boolean" % name
        choices[name] = body.get(name)
    return choices, None


def convert_settings_to_loop_flags(settings, wave, push, orchestrator_model=None, base_branch=""):
    """run_loop.py options for the ROADMAP loop settings (`parse_loop_settings`). A value equal to the engine default is not repeated;
    `base_branch` = the ROADMAP `Branch base` when the line is there and valid (else the engine falls back to the environment, then developer);
    `orchestrator_model` None = no option (the check), "" = `--orchestrator-model ""` (no `--model` flag in the turns). Never a shell."""
    flags = []
    if wave > MIN_WAVE or settings["dispatch"] != "auto":  # an explicit `Dispatch: 1` must still be sent (the engine default is 3)
        flags += ["--wave", str(wave)]
    if settings["e2e"]:
        flags.append("--e2e")
    if not push:
        flags.append("--no-push")
    if settings["when_stuck"] == "continue":
        flags.append("--continue-on-blocked")
    if settings["budget_turn"] != ENGINE_DEFAULT_BUDGET_TURN:
        flags += ["--budget", format_amount(settings["budget_turn"])]
    if settings["budget_loop"] != ENGINE_DEFAULT_MAX_COST:
        flags += ["--max-cost", format_amount(settings["budget_loop"])]
    if settings["minutes_turn"] != ENGINE_DEFAULT_TURN_MINUTES:
        flags += ["--timeout", str(settings["minutes_turn"])]
    if settings["branch"]:
        flags += ["--branch", settings["branch"]]
    if base_branch:
        flags += ["--base-branch", base_branch]
    if settings["setup"]:
        flags += ["--setup=" + settings["setup"]] if settings["setup"].startswith("-") else ["--setup", settings["setup"]]  # a single argument
    if orchestrator_model is not None:
        flags += ["--orchestrator-model", orchestrator_model]
    return flags


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
    def get_loop_settings(self, context) -> tuple: ...
    def raise_loop_limit(self, context, body) -> tuple: ...
    def answer_loop_question(self, context, body) -> tuple: ...
    def resume_loop(self, context, body=None) -> tuple: ...
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

    def relaunch_loop_from_saved_command(self, context, action, answer=None, extra_flags=None):
        """Detached run_loop.py with the argv of LOOP-CMD.json (+ `--answer <answer>`, + extra flags such as `--leftovers commit`)."""
        with self.get_relaunch_lock(context.project.id):
            if self.is_loop_alive(context):
                return 409, {"ok": False, "error": LOOP_ALREADY_RUNNING_ERROR}
            command = self.loop_command_file_repository.read_loop_command(context.work_dir)
            argv = command.get("argv")
            if not isinstance(argv, list) or not argv or not all(isinstance(item, str) for item in argv):
                return 409, {"ok": False, "error": MISSING_LOOP_COMMAND_ERROR}
            argv = list(argv) + (["--answer", answer] if answer else []) + list(extra_flags or [])
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
        """Flags of the ROADMAP loop settings + the run choices of the body (`push`); (flags, error)."""
        flags, _dispatch, error = self.resolve_flags_and_dispatch(context, body)
        return flags, error

    def read_loop_settings(self, context):
        """`parse_loop_settings` of the ROADMAP in the work folder (the worktree copy is the live one once a loop ran)."""
        return parse_loop_settings(self.read_roadmap_text(context))

    def resolve_flags_and_dispatch(self, context, body):
        """(flags, dispatch, error). Everything but `push` comes from the ROADMAP header; `Dispatch: auto` is computed, a number is explicit."""
        choices, error = read_run_choices(body)
        if error:
            return None, None, error
        settings = self.read_loop_settings(context)["settings"]
        if settings["dispatch"] == "auto":
            dispatch = compute_default_dispatch(self.read_roadmap_text(context))
        else:
            dispatch = {"wave": settings["dispatch"], "auto": False, "reason": "set in the ROADMAP"}
        push = settings["push_pr"] if choices["push"] is None else choices["push"]
        base_branch = read_roadmap_base_branch(self.read_roadmap_text(context))
        flags = convert_settings_to_loop_flags(settings, dispatch["wave"], push, base_branch=base_branch)
        claude_binary = getattr(self.config, "claude_binary", "") or ""
        if claude_binary:  # AUD-36: the engine runs the same `claude` the app runs (a demo/test fake), not always the one on PATH
            flags += ["--claude", claude_binary]
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
        if status != 200:
            return status, result
        result["warnings"] = list(result["warnings"]) + self.describe_settings_warnings(self.read_loop_settings(context))
        return status, dict(result, dispatch=dispatch, repos=self.describe_uncommitted_context_by_repo(context))

    @staticmethod
    def describe_settings_warnings(loop_settings):
        """One warning per invalid ROADMAP loop setting (the default is used); never a problem, the loop still starts."""
        return ["ROADMAP setting \"%s: %s\" is invalid (%s)" % (item["name"], item["value"], item["message"]) for item in loop_settings["problems"]]

    @staticmethod
    def describe_uncommitted_context_by_repo(context):
        """[{"name", "uncommitted_context"}] per repo, the same list as `GET workspace` (empty once the repo's worktree exists)."""
        described = []
        for repo in project_repos.load_repos(context.repo_path):
            pending = []
            if os.path.isdir(repo.path) and not os.path.isdir(repo.loop_worktree_path()):
                pending = context_files.uncommitted_context(repo.path, subprojects.load(context.repo_path, repo.name)["entry"])
            described.append({"name": repo.name, "uncommitted_context": pending})
        return described

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
            self.record_delivery_choices(context, body)
            argv = ([sys.executable, self.config.engine_script_path("run_loop"), context.repo_path] + flags
                    + ["--orchestrator-model", self.config.orchestrator_model])  # "" = the engine sends no --model
            return self.spawn_detached_loop(context, "loop_start", argv, context.repo_path,
                                            {MONITOR_URL_VARIABLE: self.get_project_panel_url(context)})

    # ------------------------------------------------ run choices (delivery), settings and spend cap
    def pending_delivery_path(self, context):
        return os.path.join(loop_state.internal_dir(context.repo_path), DELIVERY_PENDING_FILE_NAME)  # internal/, where run_loop.merge_pending_delivery reads it (loop_path would put a .json in json/)

    def build_delivery_choices(self, context, body):
        """{push_pr, remove_worktree_on_finish}: the two checkboxes of the Start dialog (push defaults to the ROADMAP `Push and PR`)."""
        choices, _ = read_run_choices(body)
        push = self.read_loop_settings(context)["settings"]["push_pr"] if choices["push"] is None else choices["push"]
        remove = True if choices["remove_worktree_on_finish"] is None else choices["remove_worktree_on_finish"]
        return {"push_pr": push, "remove_worktree_on_finish": remove}

    def record_delivery_choices(self, context, body):
        """START-06. Worktree already there (continuation): merged into its meta.json BEFORE the engine starts (it loads meta once and
        saves its own copy afterwards, so a later write would be lost). No worktree yet: the engine creates meta.json itself, so the choice
        waits in `internal/delivery-pending.json` of the repository and `read_delivery` falls back to it until the engine merges it."""
        delivery = self.build_delivery_choices(context, body)
        meta_path = loop_state.loop_path(context.project.loop_worktree_path(), "meta.json")
        if os.path.isfile(meta_path):
            meta = read_json_object(meta_path)
            meta["delivery"] = delivery
            write_json_atomic(meta_path, meta)
            return
        write_json_atomic(self.pending_delivery_path(context), delivery)

    def read_delivery(self, context):
        """The recorded delivery choices: worktree meta, else the pending file, else the defaults (push per ROADMAP, remove on Finish)."""
        recorded = (self.loop_command_file_repository.read_meta(context.work_dir) or {}).get("delivery")
        if not isinstance(recorded, dict):
            recorded = read_json_object(self.pending_delivery_path(context))
        defaults = self.build_delivery_choices(context, {})
        return {name: recorded[name] if isinstance(recorded.get(name), bool) else defaults[name] for name in defaults}

    def get_loop_settings(self, context):
        """`GET loop/settings`: what the Start dialog shows, read-only (the values are decided in planning)."""
        loop_settings = self.read_loop_settings(context)
        worktree = context.project.loop_worktree_path()
        worktree_created = os.path.isdir(worktree)
        meta = self.loop_command_file_repository.read_meta(worktree) if worktree_created else {}
        repos = self.describe_uncommitted_context_by_repo(context)
        reattach_branch = None if worktree_created else self.find_reattach_branch(context)
        return 200, {"settings": loop_settings["settings"], "problems": loop_settings["problems"], "missing": loop_settings["missing"], "defaults": loop_settings["defaults"],
                     "integration_branch": get_integration_branch(self.read_roadmap_text(context)),
                     "base_branch_resolved": resolve_base_branch(loop_settings),
                     "delivery": self.read_delivery(context),
                     "workspace": {"repos": repos, "worktree_created": worktree_created, "branch": meta.get("branch") or "",
                                   "reattach_branch": reattach_branch,
                                   "uncommitted": any(item["uncommitted_context"] for item in repos)},
                     "models": {"orchestrator": self.config.orchestrator_model, "agents": AGENTS_MODEL}}

    @staticmethod
    def find_reattach_branch(context):
        """The branch the next start would re-attach to (same rule as run_loop.reattach_branch_of): the primary repo's branch recorded by the
        last Close loop, still existing, and that run NOT finished by a wrap-up Finish. None when the next start creates a new loop branch."""
        primary = project_repos.primary_of(project_repos.load_repos(context.repo_path))
        recorded = (read_json_object(loop_state.loop_path(context.repo_path, "last-run.json")).get("repos") or {}).get(primary.name)
        if not isinstance(recorded, dict) or recorded.get("finished") or not recorded.get("branch"):
            return None
        try:
            found = subprocess.run(["git", "-C", primary.path, "rev-parse", "--verify", "--quiet", "refs/heads/" + recorded["branch"]],
                                   capture_output=True, timeout=15)
        except (OSError, subprocess.SubprocessError):
            return None
        return recorded["branch"] if found.returncode == 0 else None

    def raise_loop_limit(self, context, body):
        """`POST loop/raise-limit {max_cost_usd}`: the spend cap of THIS run goes up (meta `max_cost_usd`, which wins over `--max-cost`)
        and the same loop resumes. Refused unless the new cap is above what was already spent (`cost_usd`, never reset)."""
        value = (body or {}).get("max_cost_usd")
        if not is_finite_non_negative_number(value) or value <= 0:
            return 400, {"ok": False, "error": "max_cost_usd must be a number greater than 0"}
        meta = self.loop_command_file_repository.read_meta(context.work_dir)
        spent = meta.get("cost_usd") if is_finite_non_negative_number(meta.get("cost_usd")) else 0.0
        if value <= spent:
            return 400, {"ok": False, "error": "the new limit must be above the US$ %s already spent" % format_amount(round(spent, 4)),
                         "cost_usd": spent}
        if self.is_loop_alive(context):  # no status gate: a cap pause may still read as `no_loop` until loop_state knows `paused`
            return 409, {"ok": False, "error": LOOP_ALREADY_RUNNING_ERROR}
        meta["max_cost_usd"] = float(value)
        write_json_atomic(loop_state.loop_path(context.work_dir, "meta.json"), meta)
        code, data = self.resume_loop(context, None)
        if code != 200:
            return code, data
        self.log.info("loop_command_launched", project=context.project.id, action="raise_limit", pid=data.get("pid"))
        return 202, {"ok": True, "cost_usd": spent, "max_cost_usd": float(value)}

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

    def resume_loop(self, context, body=None):
        """Relaunch; `{"leftovers": "commit"|"discard"}` also settles the files an earlier turn left uncommitted (`--leftovers`)."""
        if self.loop_status_service.get_loop_summary(context)["running"]:
            return 409, {"ok": False, "error": "the loop is already running"}
        leftovers = (body or {}).get("leftovers")
        if leftovers not in (None, "") + LEFTOVER_CHOICES:
            return 400, {"ok": False, "error": "leftovers must be one of: %s" % ", ".join(LEFTOVER_CHOICES)}
        return self.relaunch_loop_from_saved_command(context, "resume", extra_flags=["--leftovers", leftovers] if leftovers else None)

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
