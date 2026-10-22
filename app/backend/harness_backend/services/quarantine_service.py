"""Quarantine of ONE feature (the loop paused because a feature failed twice): the detail the pause dialog shows, an AI summary of it,
and the three things the user can do about it (try again with guidance, skip, throw away uncommitted files).

Reads: the ROADMAP (blocked feature, tries, dependents), the pipeline events of the feature (its failed attempts), the verifier
transcript (failing tests) and `git status` of the work folder. Writes: only through the existing mechanics (an instruction/answer to the
loop for retry and skip, a read-only `claude -p` job for the summary, a git restore of listed files for revert). Texts leave redacted.
"""
import os
import threading
from typing import Protocol

from ..infrastructure.claude_client import redact_secrets
from ..repositories.loop_file_repository import loop_state
from ..repositories.loop_read_formatting import clip_redacted_text, now_iso, redact_credentials
from ..repositories.chat_repository import THREAD_BUSY_REASON
from .agent_history_service import HISTORY_ID_PATTERN
from .chat_service import (CHAT_ALLOWED_TOOLS, CHAT_BUDGET_USD, CHAT_DISALLOWED_TOOLS, CHAT_MODEL, CHAT_TIMEOUT_SECONDS, MAX_CONCURRENT_JOBS,
                           READ_ONLY_PREFIX, REPLY_LIMIT, ChatService, busy_jobs_error, is_number)
from .failed_test_parser import parse_failed_tests
from .loop_panel_service import QUARANTINE_OPTIONS
from .pipeline_service import EVENT_BLOCKED, EVENT_FAIL, STEPS, STEP_GATE, STEP_VERIFY
from ..infrastructure.claude_client import call_kind

BLOCKED_STATUS = "blocked"
NOT_QUARANTINED_ERROR = "feature is not in quarantine"
ATTEMPTS_LIMIT = 10
REASON_LIMIT = 1200
OUT_LIMIT = 300
SUMMARY_LIMIT = 1200
GUIDANCE_LIMIT = 4000
REVERT_PATHS_LIMIT = 200
SPECS_FOLDER_PREFIX = ".specs/"
GATE_NOTE_PREFIX = "gate"
DEFAULT_RETRY_OPTION = "try the feature again"
DEFAULT_SKIP_OPTION = "skip the feature and carry on with the others"
RETRY_OPTION_WORDS = ("again", "retry")
SKIP_OPTION_WORDS = ("skip",)
GUIDANCE_LABEL = "Guidance from the user: "
SUMMARY_JOB_THREAD_PREFIX = "quarantine-summary:"
SUMMARY_INSTRUCTION = ("Explain this quarantine in 2 or 3 plain sentences, no headings and no bullet lists: the root cause as far as the facts show it, "
                       "and what the user has to decide (try again, with which guidance, or skip). Say so when the facts do not show the cause. "
                       "You may read the files named above with Read, Grep and Glob.")
JOB_STATE_OK = "ok"
JOB_STATE_ERROR = "error"
UNEXPECTED_FAILURE = "unexpected failure"
SOURCE_AI = "ai"
SOURCE_FACTS = "facts"


def list_transitive_dependents(features, slug):
    """Slugs of the features that depend on `slug`, directly or not, and are not verified yet (ROADMAP order). `dir:` deps are ignored."""
    direct = {name: loop_state.split_deps(fields)[0] for name, fields in features.items()}
    found, pending = set(), [slug]
    while pending:
        current = pending.pop()
        for name, dependencies in direct.items():
            if current in dependencies and name not in found and name != slug:
                found.add(name)
                pending.append(name)
    return [name for name in features if name in found and features[name].get("status") != "verified"]


def build_facts_sentence(detail):
    """A plain sentence made only of the facts of the detail (used while there is no AI summary)."""
    parts = ["Feature %s is in quarantine after %d failed attempt(s)" % (detail["id"], detail["failures"])]
    if detail["step"] is not None:
        parts[0] += " at the %s step (%s)" % (STEPS[detail["step"]]["key"], detail["step_agent"])
        if detail["turn"] is not None:
            parts[0] += " in turn %d" % detail["turn"]
    parts[0] += "."
    last_out = next((attempt["out"] for attempt in reversed(detail["attempts"]) if attempt["out"]), None)
    if last_out:
        parts.append("Last result: %s" % last_out.rstrip(".") + ".")
    if detail["failed_tests"]:
        names = ", ".join(test["name"] for test in detail["failed_tests"][:3])
        parts.append("%d failing test(s): %s%s." % (len(detail["failed_tests"]), names, ", ..." if len(detail["failed_tests"]) > 3 else ""))
    if detail["uncommitted"]:
        parts.append("%d uncommitted file(s) were left in the work folder." % len(detail["uncommitted"]))
    if detail["skip_impact"]:
        parts.append("Skipping it also blocks: %s." % ", ".join(detail["skip_impact"]))
    return " ".join(parts)


def build_context_text(detail, work_dir):
    """Facts of the quarantine as text for a read-only claude run (summary job and the `quarantine` chat scope)."""
    lines = ["Quarantined feature: %s." % detail["id"], "Reason: %s" % detail["reason"],
             "Failures: %d. Step: %s." % (detail["failures"], ("%s (%s)" % (STEPS[detail["step"]]["key"], detail["step_agent"]))
                                          if detail["step"] is not None else "unknown")]
    if detail["attempts"]:
        lines.append("Failed attempts:")
        lines += ["- turn %s %s %s: %s" % (attempt["turn"], attempt["agent"], attempt["status"], attempt["out"] or "(no text)")
                  for attempt in detail["attempts"]]
    if detail["failed_tests"]:
        lines.append("Failed tests:")
        lines += ["- %s | why: %s | expected: %s | got: %s" % (test["name"], test["why"], test["expected"], test["got"])
                  for test in detail["failed_tests"]]
    if detail["uncommitted"]:
        lines.append("Uncommitted files in the work folder: " + ", ".join(detail["uncommitted"]))
    if detail["skip_impact"]:
        lines.append("If skipped, these features stay blocked too: " + ", ".join(detail["skip_impact"]))
    lines.append("Feature files: " + ChatService.describe_feature_dir(work_dir, detail["id"]))
    lines.append(ChatService.build_general_context(work_dir))
    return redact_secrets("\n".join(lines))


class IQuarantineService(Protocol):
    def get_detail(self, context, slug) -> tuple: ...
    def start_summary(self, context, slug) -> tuple: ...
    def retry_feature(self, context, slug, body) -> tuple: ...
    def skip_feature(self, context, slug) -> tuple: ...
    def revert_files(self, context, slug, body) -> tuple: ...
    def build_chat_context(self, context, slug): ...


class QuarantineService:
    def __init__(self, log, loop_status_service, loop_file_repository, pipeline_service, claude_transcript_repository, loop_control_service,
                 git_client, claude_client, chat_job_repository, summary_repository):
        self.log = log
        self.loop_status_service = loop_status_service
        self.loop_file_repository = loop_file_repository
        self.pipeline_service = pipeline_service
        self.claude_transcript_repository = claude_transcript_repository
        self.loop_control_service = loop_control_service
        self.git_client = git_client
        self.claude_client = claude_client
        self.chat_job_repository = chat_job_repository
        self.summary_repository = summary_repository
        self.timeout_seconds = CHAT_TIMEOUT_SECONDS

    # ------------------------------------------------ detail
    def read_roadmap_features(self, work_dir):
        return loop_state.parse_roadmap(self.loop_file_repository.read_roadmap(work_dir))[0]

    def get_detail(self, context, slug):
        """(200, detail) of a blocked feature; 404 when the feature does not exist or is not blocked."""
        detail = self.build_detail(context, slug)
        if detail is None:
            return 404, {"ok": False, "error": NOT_QUARANTINED_ERROR}
        return 200, detail

    def build_detail(self, context, slug):
        work_dir = context.work_dir
        features = self.read_roadmap_features(work_dir)
        fields = features.get(slug)
        if not fields or fields.get("status") != BLOCKED_STATUS:
            return None
        tries = int(fields["tries"]) if str(fields.get("tries") or "").isdigit() else 0
        attempts = self.list_attempts(context, slug)
        last = attempts[-1] if attempts else None
        step = last["step"] if last else (STEP_GATE if str(fields.get("note") or "").startswith(GATE_NOTE_PREFIX) else None)
        detail = {"id": slug, "reason": self.build_reason(context, slug, fields, tries), "step": step,
                  "step_agent": STEPS[step]["agent"] if step is not None else None, "failures": tries,
                  "turn": last["turn"] if last else None,
                  "attempts": [{key: attempt[key] for key in ("turn", "agent", "status", "out", "agent_id")} for attempt in attempts],
                  "failed_tests": self.read_failed_tests(work_dir, attempts), "uncommitted": self.list_uncommitted(work_dir),
                  "skip_impact": list_transitive_dependents(features, slug), "options": list(QUARANTINE_OPTIONS)}
        detail["summary"] = self.read_summary(work_dir, detail)
        return detail

    def build_reason(self, context, slug, fields, tries):
        """The pause reason of LOOP.json when it names this feature, else the ROADMAP note with the failure count."""
        summary = self.loop_status_service.get_loop_summary(context)
        reason = str(summary.get("reason") or "")
        if summary["status"] == "awaiting_user" and slug in reason:
            return redact_credentials(reason, REASON_LIMIT).strip()
        note = str(fields.get("note") or "")
        return clip_redacted_text("%s (%d failures)" % (note, tries) if note else "%d failures" % tries, REASON_LIMIT)

    def list_attempts(self, context, slug):
        """Runs of this feature that failed or blocked it, oldest first (the last ones only)."""
        rows = [event for event in self.pipeline_service.list_feature_events(context, slug) if event["status"] in (EVENT_FAIL, EVENT_BLOCKED)]
        return rows[-ATTEMPTS_LIMIT:]

    def read_failed_tests(self, work_dir, attempts):
        """Failing tests of the latest verifier run that shows any: its final return first, else the output of its tool calls."""
        for attempt in reversed(attempts):
            found = HISTORY_ID_PATTERN.match(attempt.get("agent_id") or "")
            if attempt["step"] != STEP_VERIFY or not found:
                continue
            transcript = self.read_agent_transcript(work_dir, int(found.group(1)), found.group(2))
            if not transcript:
                continue
            tests = parse_failed_tests(transcript.get("final_return")) or parse_failed_tests(
                "\n".join(str(item.get("result") or "") for item in transcript.get("log", [])))
            if tests:
                return tests
        return []

    def read_agent_transcript(self, work_dir, turn_number, agent_name):
        record = self.loop_file_repository.read_turn(work_dir, turn_number)
        _, subagents = self.claude_transcript_repository.locate_session_files(work_dir, record.get("session_id"))
        path = next((item[1] for item in subagents if item[0] == agent_name), None)
        return self.claude_transcript_repository.read_transcript(path) if path else None

    def list_uncommitted(self, work_dir):
        """Files `git status` shows as changed in the work folder, outside `.specs/` (those are the loop's own state, never thrown away)."""
        entries = self.git_client.list_status_entries(work_dir) if os.path.isdir(work_dir) else None
        return [entry["path"] for entry in entries or [] if not entry["path"].startswith(SPECS_FOLDER_PREFIX)]

    def read_summary(self, work_dir, detail):
        cached = self.summary_repository.read_summary(work_dir, detail["id"])
        if cached.get("text") and cached.get("failures") == detail["failures"] and cached.get("turn") == detail["turn"]:
            return {"text": redact_credentials(cached["text"], SUMMARY_LIMIT), "source": SOURCE_AI, "at": cached.get("at")}
        return {"text": clip_redacted_text(build_facts_sentence(detail), SUMMARY_LIMIT), "source": SOURCE_FACTS, "at": None}

    # ------------------------------------------------ summary job (read-only claude, polled through chat/job/<job>)
    def start_summary(self, context, slug):
        detail = self.build_detail(context, slug)
        if detail is None:
            return 404, {"ok": False, "error": NOT_QUARANTINED_ERROR}
        job_id, reason = self.chat_job_repository.reserve_job(context.project.id, SUMMARY_JOB_THREAD_PREFIX + slug, MAX_CONCURRENT_JOBS)
        if job_id is None:
            return 409, {"ok": False, "error": "a summary of this feature is already being written" if reason == THREAD_BUSY_REASON
                         else busy_jobs_error()}
        argv = self.claude_client.build_command(CHAT_MODEL, CHAT_BUDGET_USD, CHAT_ALLOWED_TOOLS, CHAT_DISALLOWED_TOOLS, no_mcp=True)
        stdin_text = READ_ONLY_PREFIX + "\n\n" + build_context_text(detail, context.work_dir) + "\n\n" + SUMMARY_INSTRUCTION
        run_dir = context.work_dir if os.path.isdir(context.work_dir) else context.repo_path
        self.log.info("quarantine_summary_started", project=context.project.id, job=job_id, feature=slug)
        threading.Thread(target=self.run_summary_job, args=(context, job_id, detail, argv, stdin_text, run_dir), daemon=True).start()
        return 202, {"ok": True, "job": job_id}

    def run_summary_job(self, context, job_id, detail, argv, stdin_text, run_dir):
        result = {"state": JOB_STATE_ERROR, "reply": None, "cost_usd": None, "error": UNEXPECTED_FAILURE, "thread": None}
        try:
            with call_kind("quarantine"):
                returned = self.claude_client.run_prompt(argv, run_dir, stdin_text, self.timeout_seconds)
            if returned["ok"]:
                text = redact_secrets(returned.get("reply"), REPLY_LIMIT).strip()
                cost = returned["cost_usd"] if is_number(returned.get("cost_usd")) else None
                result = {"state": JOB_STATE_OK, "reply": text, "cost_usd": cost, "error": None, "thread": None}
                self.summary_repository.write_summary(context.work_dir, detail["id"], {
                    "text": text[:SUMMARY_LIMIT], "at": now_iso(), "failures": detail["failures"], "turn": detail["turn"], "cost_usd": cost})
            else:
                result["error"] = returned.get("error") or UNEXPECTED_FAILURE
        except Exception as failure:  # the job never takes the app down
            result = dict(result, state=JOB_STATE_ERROR, reply=None, error="failed to run claude: %s" % type(failure).__name__)
        finally:
            self.chat_job_repository.finish_job(job_id, result)
            self.log.info("quarantine_summary_finished", project=context.project.id, job=job_id, state=result["state"])

    # ------------------------------------------------ decisions (through the answer/instruction mechanics of the loop)
    def pick_option_text(self, context, words, default):
        """The wording of the option the loop listed (LOOP.json `options`) so the answer reads like the click on it; else the default wording."""
        options = self.loop_status_service.get_loop_summary(context).get("json", {}).get("options")
        options = options if isinstance(options, list) else []
        return next((str(option) for option in options if any(word in str(option).lower() for word in words)), default)

    def retry_feature(self, context, slug, body):
        guidance = body.get("guidance", "")
        guidance = "" if guidance is None else guidance
        if not isinstance(guidance, str):
            return 400, {"ok": False, "error": "guidance must be text"}
        if len(guidance) > GUIDANCE_LIMIT:
            return 400, {"ok": False, "error": "guidance is longer than %d characters" % GUIDANCE_LIMIT}
        if self.build_detail_or_none(context, slug) is None:
            return 404, {"ok": False, "error": NOT_QUARANTINED_ERROR}
        text = self.pick_option_text(context, RETRY_OPTION_WORDS, DEFAULT_RETRY_OPTION)
        if guidance.strip():
            text += ". " + GUIDANCE_LABEL + guidance.strip()
        return self.loop_control_service.send_instruction_to_loop(context, {"text": text, "feature": slug})

    def skip_feature(self, context, slug):
        if self.build_detail_or_none(context, slug) is None:
            return 404, {"ok": False, "error": NOT_QUARANTINED_ERROR}
        text = self.pick_option_text(context, SKIP_OPTION_WORDS, DEFAULT_SKIP_OPTION)
        return self.loop_control_service.send_instruction_to_loop(context, {"text": text, "feature": slug})

    def build_detail_or_none(self, context, slug):
        """The blocked feature's ROADMAP fields (cheap check for the commands, which do not need the whole detail); None = not quarantined."""
        fields = self.read_roadmap_features(context.work_dir).get(slug)
        return fields if fields and fields.get("status") == BLOCKED_STATUS else None

    # ------------------------------------------------ revert
    def revert_files(self, context, slug, body):
        paths = body.get("paths")
        if (not isinstance(paths, list) or not paths or len(paths) > REVERT_PATHS_LIMIT
                or not all(isinstance(path, str) and path.strip() for path in paths)):
            return 400, {"ok": False, "error": "paths: a list of 1 to %d file paths from the uncommitted list" % REVERT_PATHS_LIMIT}
        if self.build_detail_or_none(context, slug) is None:
            return 404, {"ok": False, "error": NOT_QUARANTINED_ERROR}
        if self.loop_control_service.is_loop_alive(context):
            return 409, {"ok": False, "error": "the loop is running: nothing can be reverted until it stops"}
        work_dir = context.work_dir
        entries = {entry["path"]: entry for entry in (self.git_client.list_status_entries(work_dir) or [])
                   if not entry["path"].startswith(SPECS_FOLDER_PREFIX)}
        wanted = [path.strip().replace("\\", "/").removeprefix("./") for path in paths]
        outside = [path for path in wanted if path not in entries]
        if outside:
            return 400, {"ok": False, "error": "not in the uncommitted list: %s" % ", ".join(clip_redacted_text(path, OUT_LIMIT) for path in outside[:5])}
        reverted, failed = [], []
        for path in dict.fromkeys(wanted):
            done, error = self.git_client.discard_path_changes(work_dir, entries[path])
            if done:
                reverted.append(path)
            else:
                failed.append({"path": path, "error": clip_redacted_text(error, OUT_LIMIT)})
        self.log.info("quarantine_revert", project=context.project.id, feature=slug, reverted=len(reverted), failed=len(failed))
        body_out = {"ok": not failed, "reverted": reverted, "failed": failed}
        return (200 if not failed else 500), (body_out if not failed else dict(body_out, error="some files could not be reverted"))

    # ------------------------------------------------ chat scope
    def build_chat_context(self, context, slug):
        """Text for the `quarantine` chat scope; None when the feature is not in quarantine."""
        detail = self.build_detail(context, slug)
        return build_context_text(detail, context.work_dir) if detail else None
