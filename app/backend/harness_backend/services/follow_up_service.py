"""Follow-up of a finished loop, as a CHAT: a planning-like `claude -p` job (type `follow_up`) turns the user's messages (and the decisions
they asked to change) into NEW features of ONE stage `Follow-up <n>` appended to the ROADMAP, with FLOWS entries; the user reviews the stage
and **Continue loop** starts a new loop round on the same worktree and branch, with every guard of a normal round.

One thread per round (FollowUpThreadRepository) whose messages continue one claude session (`--resume`). The job: `--model sonnet`
(JOB_MODEL_CEILING), hook profile `planning` (Write/Edit only inside `.specs/`), `Edit` allowed only on ROADMAP.md and FLOWS.md, no MCP,
US$ 1 per message and US$ 5 per round, run in the loop worktree. Before each turn the app snapshots the work folder and keeps the text of
ROADMAP.md and FLOWS.md; afterwards (finalize) it undoes anything outside those two files and accepts ONLY edits of the round's
`Follow-up <n>` stage plus appended FLOWS text (validate_round_stage against the round's baseline): otherwise both files go back to their
text BEFORE THIS TURN (earlier good turns survive) and the job ends as `error` with the reason.

The stage id is the first word of the heading (loop_state.STAGE_LINE), so the heading is written `## Stage Follow-up-<n> - Follow-up <n>`:
one id per round. `Follow-up <n>` with a space is accepted too.
Refused (409) while the loop runs, while any claude job runs, without a loop worktree, or (code follow_up_budget) once the round spent US$ 5.
Run ("Continue loop") closes the round (`thread.closed_at`): the next send is round n+1 with spend 0.
"""
import os
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, InvalidRequestError, InvalidRoadmapError, JobInProgressError, LoopRunningError,
                                     RepoConflictError)
from ..domain.follow_up_stage import (ANY_STAGE_HEADING, FEATURE_LINE, OPEN_FEATURE_STATUSES, STAGE_HEADING,  # noqa: F401  (re-exported)
                                      describe_stage_change, list_follow_up_stages, normalize_newlines, validate_appended_stage,  # noqa: F401
                                      validate_round_stage)
from ..engine import decisions as decisions_engine
from ..infrastructure.claude_client import redact_secrets
from ..repositories.follow_up_thread_repository import FollowUpThreadRepository
from ..repositories.loop_command_file_repository import now_iso
from ..repositories.specs_file_repository import loop_state
from .claude_job_service import JOB_MODEL_CEILING, JOB_STATE_ERROR, ClaudeJobCommand  # noqa: F401
from .planning_service import GUARD_IGNORED_PATHS, is_number
from ..infrastructure.claude_client import call_kind

JOB_TYPE_FOLLOW_UP = "follow_up"
FOLLOW_UP_BUDGET_USD = 1  # per message; the body can no longer change it
FOLLOW_UP_ROUND_BUDGET_USD = 5
FOLLOW_UP_BUDGET_CODE = "follow_up_budget"
FOLLOW_UP_TIMEOUT_SECONDS = 900
FOLLOW_UP_TEXT_LIMIT = 8000
FOLLOW_UP_REPLY_LIMIT = 20000
FOLLOW_UP_PERMISSION_MODE = "dontAsk"
FOLLOW_UP_HOOK_PROFILE = "planning"
FOLLOW_UP_DISALLOWED_TOOLS = ["Bash", "NotebookEdit", "Agent"]
ROADMAP_PATH = ".specs/project/ROADMAP.md"
FLOWS_PATH = ".specs/project/FLOWS.md"
EDITABLE_PATHS = (ROADMAP_PATH, FLOWS_PATH)
DRAFT_RELATIVE_PATH = ".specs/project/loop/internal/follow-up-draft.json"
NO_WORKTREE_CODE = "no_worktree"
NO_STAGE_CODE = "no_follow_up_stage"
FOLLOW_UP_BUSY_CODE = "follow_up_busy"
FINALIZE_REVERTED_NOTE = "\n\n(The app restored ROADMAP.md and FLOWS.md: %s)"
FOLLOW_UP_BUDGET_REACHED_REASON = "Follow-up budget of this round reached (US$ %d)" % FOLLOW_UP_ROUND_BUDGET_USD
FOLLOW_UP_NO_STAGE_REASON = "The follow-up stage has no feature yet"
FOLLOW_UP_FINISHED_REASON = "The loop worktree is gone (the work was finished): use Back to planning for new work"
FOLLOW_UP_LOOP_RUNNING_REASON = "The loop is running: wait for it to stop"
FOLLOW_UP_JOB_RUNNING_REASON = "A claude job is running in this project: wait for it to finish"
SUMMARIZED_NOTE = "Earlier messages were summarized"
RETRY_HISTORY_MESSAGES = 6
# A resumed turn that fails with one of these (lower case) fragments in `error` lost its session (or the claude build does not know the
# flag): the turn is retried ONCE as a fresh session. Any other failure (timeout, budget, a real error) is not retried.
RESUME_FAILURE_MARKERS = ("no conversation found", "session not found", "session id", "--resume", "unknown option")

FOLLOW_UP_RULES = """You are the planner of a project whose loop already ran. The user wants a FOLLOW-UP: new work on top of what was built.
Turn the request below into NEW features appended to .specs/project/ROADMAP.md as ONE new stage, and add matching entries to .specs/project/FLOWS.md.
Rules:
- Read .specs/project/ROADMAP.md, FLOWS.md, PROJECT.md and RULES.md first (and the decisions listed below, if any). Edit ONLY ROADMAP.md and FLOWS.md.
- APPEND at the end of ROADMAP.md a stage with exactly this heading (the id is the first word, so it carries the number): `## Stage Follow-up-%(n)s - Follow-up %(n)s`
- Never edit, move, reword, reorder or delete any existing line of ROADMAP.md or FLOWS.md above your stage; never edit another stage; never change a status. The app restores both files if you do.
- This is a conversation: in a later message of the same round you may add, edit or remove features of YOUR stage `Follow-up-%(n)s` (and append to FLOWS.md). Slugs of earlier stages are taken.
- Under the new heading write one line per new feature in the ROADMAP grammar already used by the file: `- [slug] status=pending scope=<Small|Medium|Large> kind=<back|front|generic> paths=<paths> deps=<slugs> title="<what the user gets>"`.
  A slug is unique in the whole file (kebab-case, 3-6 words). `refactor-*` slugs follow the refactor rules of the file (`regression=` only when the user asked for it).
  `deps=` may name features of earlier stages (already verified). In a multi-repo project a path carries its repo: `paths=<repo>:<path>` (a path without prefix belongs to the primary repo).
- Append to FLOWS.md one entry per new feature, in the format the file already uses.
- Do not write code, specs or tests; the loop does that. Do not approve anything.
- Reply short: the slugs you added and one line on what each does."""


class IFollowUpService(Protocol):
    def get_follow_up(self, context) -> dict: ...
    def send_follow_up(self, context, body) -> dict: ...
    def run_follow_up(self, context) -> tuple: ...


class FollowUpTurn:
    """What one chat turn needs in its finalize (job thread): the round, the text before the turn, the round baseline and how it started."""

    def __init__(self, round_number, snapshot, before, baseline, executed_slugs, resumed, fresh_prompt_parts):
        self.round_number = round_number
        self.snapshot = snapshot
        self.before = before
        self.baseline = baseline
        self.executed_slugs = executed_slugs
        self.resumed = resumed
        self.fresh_prompt_parts = fresh_prompt_parts  # (context block, request text) to rebuild a fresh prompt on a resume failure
        self.job_id = ""  # set by send right after the job starts (best effort: a very fast job may finish first)


class FollowUpService:
    def __init__(self, specs_file_repository, claude_job_service, claude_client, workspace_file_guard, loop_status_service,
                 loop_control_service, log, follow_up_thread_repository=None):
        self.specs_file_repository = specs_file_repository
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.loop_status_service = loop_status_service
        self.loop_control_service = loop_control_service  # start_new_loop(context, body) -> (status, payload)
        self.log = log
        self.follow_up_thread_repository = follow_up_thread_repository or FollowUpThreadRepository()

    # ------------------------------------------------ read
    def get_follow_up(self, context):
        work_dir = context.work_dir
        draft = self.read_draft(work_dir)
        jobs = [job for job in self.claude_job_service.list_jobs(context.repo_path) if job.get("type") == JOB_TYPE_FOLLOW_UP]
        roadmap_text = self.read_text(work_dir, ROADMAP_PATH)
        round_number = self.follow_up_thread_repository.current_round(work_dir, roadmap_text)
        thread = self.follow_up_thread_repository.read_thread(work_dir, round_number)
        stage = self.find_round_stage(roadmap_text, round_number)
        if stage:
            stage = dict(stage, name="Follow-up %s" % stage["n"] if stage["n"] else "Follow-up")
        finished = not self.has_loop_worktree(context)
        return {"round": round_number,
                "thread": {"messages": thread["messages"], "spent_usd": float(thread.get("spent_usd") or 0),
                           "summary_note": str(thread.get("summary_note") or "")},
                "stage": stage,
                "last_job": jobs[0] if jobs else None,
                "caps": {"message_usd": FOLLOW_UP_BUDGET_USD, "round_usd": FOLLOW_UP_ROUND_BUDGET_USD},
                "can_send": self.decide_can_send(context, thread, finished),
                "can_continue": self.decide_can_continue(context, stage, finished),
                "finished": finished,
                "draft": {"text": str(draft.get("text") or ""), "decisions": list(draft.get("decisions") or [])}}

    def decide_busy_reason(self, context, finished):
        """Why neither send nor continue is possible right now ('' when nothing blocks)."""
        if finished:
            return FOLLOW_UP_FINISHED_REASON
        if self.loop_status_service.is_loop_running(context):
            return FOLLOW_UP_LOOP_RUNNING_REASON
        if self.claude_job_service.get_running_job(context.repo_path):
            return FOLLOW_UP_JOB_RUNNING_REASON
        return ""

    def decide_can_send(self, context, thread, finished):
        reason = self.decide_busy_reason(context, finished)
        if not reason and float(thread.get("spent_usd") or 0) >= FOLLOW_UP_ROUND_BUDGET_USD:
            reason = FOLLOW_UP_BUDGET_REACHED_REASON
        return {"ok": not reason, "reason": reason}

    def decide_can_continue(self, context, stage, finished):
        reason = self.decide_busy_reason(context, finished)
        if not reason and not (stage and any(feature["status"] in OPEN_FEATURE_STATUSES for feature in stage["features"])):
            reason = FOLLOW_UP_NO_STAGE_REASON
        return {"ok": not reason, "reason": reason}

    @staticmethod
    def find_round_stage(roadmap_text, round_number):
        return next((stage for stage in list_follow_up_stages(roadmap_text) if stage["n"] == round_number), None)

    @staticmethod
    def has_loop_worktree(context):
        worktree = context.project.loop_worktree_path()
        return os.path.isdir(worktree) and os.path.normcase(os.path.abspath(context.work_dir)) == os.path.normcase(os.path.abspath(worktree))

    def read_draft(self, work_dir):
        return self.specs_file_repository.read_json(os.path.join(work_dir, *DRAFT_RELATIVE_PATH.split("/")))

    def write_draft_text(self, work_dir, text):
        """Merges into the draft file: the `decisions` key belongs to the decisions service and is never touched here."""
        draft = dict(self.read_draft(work_dir), text=text)
        draft.setdefault("decisions", [])
        self.specs_file_repository.write_json_atomic(os.path.join(work_dir, *DRAFT_RELATIVE_PATH.split("/")), draft)

    def read_text(self, work_dir, relative):
        return self.specs_file_repository.read_text(os.path.join(work_dir, *relative.split("/"))) or ""

    # ------------------------------------------------ send
    def send_follow_up(self, context, body):
        body = body if isinstance(body, dict) else {}  # `budget_usd` in the body is ignored: the cap is fixed (US$ 1 per message)
        text = self.validate_text(body.get("text"))
        decision_ids = self.validate_decision_ids(body.get("decisions"))
        work_dir = context.work_dir
        self.describe_decisions(work_dir, decision_ids)  # an unknown id of the body is a 400
        if not text and not decision_ids:
            raise InvalidRequestError("write what the follow-up should do, or pick decisions to change")
        self.ensure_can_start(context)
        before = {path: self.read_text(work_dir, path) for path in EDITABLE_PATHS}
        round_number = self.follow_up_thread_repository.current_round(work_dir, before[ROADMAP_PATH])
        thread = self.follow_up_thread_repository.read_thread(work_dir, round_number)
        if float(thread.get("spent_usd") or 0) >= FOLLOW_UP_ROUND_BUDGET_USD:
            raise RepoConflictError(FOLLOW_UP_BUDGET_REACHED_REASON, code=FOLLOW_UP_BUDGET_CODE)
        if not (thread.get("baseline") or {}).get("roadmap"):
            thread["baseline"] = {"roadmap": before[ROADMAP_PATH], "flows": before[FLOWS_PATH]}
        baseline = thread["baseline"]
        marked_ids = [str(item) for item in self.read_draft(work_dir).get("decisions") or []]
        decisions_text = self.describe_decisions(work_dir, list(dict.fromkeys(decision_ids + marked_ids)), strict=False)
        context_block = self.compose_context_block(context, round_number, before, baseline, decisions_text)
        session_id = str(thread.get("session_id") or "")
        resumed = bool(session_id)
        if resumed:
            prompt = self.compose_resumed_prompt(round_number, text, context_block)
        else:
            prompt = self.compose_fresh_prompt(round_number, text, context_block)
        thread["messages"].append({"role": "user", "text": text, "decisions": decision_ids, "at": now_iso()})
        self.follow_up_thread_repository.write_thread(work_dir, round_number, thread)
        snapshot = self.workspace_file_guard.take_snapshot(work_dir, GUARD_IGNORED_PATHS)
        turn = FollowUpTurn(round_number, snapshot, before, baseline, self.list_executed_slugs(baseline["roadmap"]), resumed,
                            (context_block, text))
        command = ClaudeJobCommand(self.build_argv(session_id), prompt, timeout_seconds=FOLLOW_UP_TIMEOUT_SECONDS, work_dir=work_dir,
                                   finalize=lambda result: self.finish_job(context, turn, result))
        self.write_draft_text(work_dir, text)  # kept if the job fails, so the user does not retype it
        job = self.claude_job_service.start_job(context.repo_path, JOB_TYPE_FOLLOW_UP, command)
        turn.job_id = job["id"]
        self.log.info("follow_up_sent", project=context.project.id, job=job["id"], number=round_number, resumed=resumed,
                      decisions=len(decision_ids))
        return {"ok": True, "job": job}

    def build_argv(self, session_id):
        allowed_tools = ["Read", "Grep", "Glob"] + ["Edit(/%s)" % path for path in EDITABLE_PATHS]
        return self.claude_client.build_command(JOB_MODEL_CEILING, FOLLOW_UP_BUDGET_USD, allowed_tools, FOLLOW_UP_DISALLOWED_TOOLS, no_mcp=True,
                                                permission_mode=FOLLOW_UP_PERMISSION_MODE, hook_profile=FOLLOW_UP_HOOK_PROFILE,
                                                resume_session=session_id or "")

    @staticmethod
    def validate_text(text):
        if text is None:
            return ""
        if not isinstance(text, str):
            raise InvalidRequestError("text must be text")
        text = text.strip()
        if len(text) > FOLLOW_UP_TEXT_LIMIT:
            raise InvalidRequestError("text is too long (at most %d characters)" % FOLLOW_UP_TEXT_LIMIT)
        return text

    @staticmethod
    def validate_decision_ids(value):
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise InvalidRequestError("decisions must be a list of decision ids")
        return list(dict.fromkeys(item.strip() for item in value))

    @staticmethod
    def describe_decisions(work_dir, decision_ids, strict=True):
        """The VALIDATE.md lines the user picked, as prompt text; an unknown id is a 400 (the list changed under the user).
        `strict=False` (ids marked earlier in the draft) skips the ids that no longer exist."""
        if not decision_ids:
            return ""
        items, unparsed = decisions_engine.parse(decisions_engine.read_file(work_dir))
        by_id = {item["id"]: item for item in items + unparsed}
        unknown = [item_id for item_id in decision_ids if item_id not in by_id]
        if unknown and strict:
            raise InvalidRequestError("unknown decision id: %s" % unknown[0])
        return "\n".join("- %s" % by_id[item_id]["raw"].lstrip("- ").strip() for item_id in decision_ids if item_id in by_id)

    @staticmethod
    def list_executed_slugs(baseline_roadmap):
        """Slugs of the baseline ROADMAP: every earlier stage is already executed (the round's own stage is not in the baseline)."""
        features, _ = loop_state.parse_roadmap(normalize_newlines(baseline_roadmap))
        return list(features)

    def compose_context_block(self, context, round_number, texts, baseline, decisions_text):
        """Executed stages (read-only), the round's current stage, the loop status and the decisions the user asked to change."""
        features, _ = loop_state.parse_roadmap(normalize_newlines(baseline["roadmap"]))
        executed = ["%s (%s)" % (slug, fields.get("status")) for slug, fields in features.items()]
        stage = self.find_round_stage(texts[ROADMAP_PATH], round_number)
        current = "\n".join("- " + line for line in stage["lines"].values()) if stage and stage["lines"] else "(none yet)"
        summary = self.loop_status_service.get_loop_summary(context)
        parts = ["Features of earlier stages (already executed, read-only): " + (", ".join(executed) or "(none)"),
                 "Current stage Follow-up-%d (you may edit it):\n%s" % (round_number, current),
                 "Loop: status=%s turns=%s" % (summary.get("status"), (summary.get("meta") or {}).get("turns", "?"))]
        if decisions_text:
            parts.append("Decisions the user asked to CHANGE (from .specs/project/loop/VALIDATE.md; each line: doubt | what was chosen | how to revert):\n"
                         + decisions_text)
        return "\n\n".join(parts)

    @staticmethod
    def compose_fresh_prompt(number, text, context_block, history=None, summary_note=""):
        parts = [FOLLOW_UP_RULES % {"n": number}, context_block]
        if history:
            lines = "\n".join("%s: %s" % (message.get("role"), str(message.get("text") or "")[:1000]) for message in history)
            parts.append("%s. Last messages of this conversation:\n%s" % (summary_note, lines))
        parts.append("FOLLOW-UP-NUMBER: %d\n\nUser request:\n%s" % (number, text or "(no extra text: change the decisions listed above)"))
        return "\n\n".join(parts)

    @staticmethod
    def compose_resumed_prompt(number, text, context_block):
        """The session already holds the rules: a short status preface (the stage may have changed by hand) and the new message."""
        return "Status before this message:\n%s\n\nFOLLOW-UP-NUMBER: %d\n\nUser request:\n%s" % (
            context_block, number, text or "(no extra text: change the decisions listed above)")

    def ensure_can_start(self, context):
        """Shared by send and run: a loop or a claude job running, or no loop worktree, refuses with 409."""
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("the loop is running: wait for it to stop before a follow-up", code=LOOP_RUNNING_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running in this project: wait for it to finish", code=FOLLOW_UP_BUSY_CODE)
        if not self.has_loop_worktree(context):
            raise RepoConflictError("there is no loop worktree: a follow-up needs the finished loop's worktree (use Back to planning for new work)",
                                    code=NO_WORKTREE_CODE)

    # ------------------------------------------------ finalize (job thread)
    def finish_job(self, context, turn, result):
        """Keeps the turn's stage edit, or restores ROADMAP.md and FLOWS.md to their text BEFORE THIS TURN and ends the job as an error."""
        work_dir = context.work_dir
        thread = self.follow_up_thread_repository.read_thread(work_dir, turn.round_number)
        result = dict(result)
        if turn.resumed and not result.get("ok") and self.is_resume_failure(result):
            result = self.retry_as_fresh_session(context, turn, thread, result)
        report = self.workspace_file_guard.revert_unexpected_changes(turn.snapshot, list(EDITABLE_PATHS))
        if report["reverted"] or report["removed"] or report["unrestorable"]:
            self.log.warn("follow_up_guard_reverted", project=context.project.id, reverted=report["reverted"][:50], removed=report["removed"][:50])
        after = {path: self.read_text(work_dir, path) for path in EDITABLE_PATHS}
        if not result.get("ok"):
            problem = "the follow-up job did not finish cleanly"
        else:
            problem = validate_round_stage(turn.baseline["roadmap"], after[ROADMAP_PATH], turn.baseline["flows"], after[FLOWS_PATH],
                                           turn.round_number, turn.executed_slugs)
        reply = redact_secrets(result.get("reply") or "", FOLLOW_UP_REPLY_LIMIT)
        cost = result.get("cost_usd") if is_number(result.get("cost_usd")) else 0
        message = {"role": "assistant", "text": reply, "cost_usd": cost, "job_id": turn.job_id, "at": now_iso()}
        if problem is None:
            self.write_draft_text(work_dir, "")
            changed = describe_stage_change(self.find_round_stage(turn.before[ROADMAP_PATH], turn.round_number),
                                            self.find_round_stage(after[ROADMAP_PATH], turn.round_number))
            message.update(state="ok", error="", changed=changed)
            result["reply"] = reply
        else:
            for path in EDITABLE_PATHS:
                self.specs_file_repository.write_text_atomic(os.path.join(work_dir, *path.split("/")), turn.before[path])
            self.log.warn("follow_up_reverted", project=context.project.id, reason=problem[:200])
            message.update(state="error", error=problem, changed={"added": [], "removed": [], "edited": []})
            if result.get("ok"):
                result.update(ok=False, error=problem, reply=reply + FINALIZE_REVERTED_NOTE % problem)
        thread["messages"].append(message)
        thread["spent_usd"] = round(float(thread.get("spent_usd") or 0) + float(cost), 6)  # a failed turn still cost money
        if result.get("session_id"):
            thread["session_id"] = result["session_id"]
        self.follow_up_thread_repository.write_thread(work_dir, turn.round_number, thread)
        return result

    @staticmethod
    def is_resume_failure(result):
        error = str(result.get("error") or "").lower()
        return any(marker in error for marker in RESUME_FAILURE_MARKERS)

    def retry_as_fresh_session(self, context, turn, thread, failed_result):
        """The session is gone: ONE more run as a fresh session with the context block plus the last messages."""
        history = thread["messages"][:-1][-RETRY_HISTORY_MESSAGES:]  # the last message is this turn's own user message
        context_block, text = turn.fresh_prompt_parts
        prompt = self.compose_fresh_prompt(turn.round_number, text, context_block, history, SUMMARIZED_NOTE)
        thread["summary_note"] = SUMMARIZED_NOTE
        thread["session_id"] = None
        self.log.warn("follow_up_resume_failed", project=context.project.id, error=str(failed_result.get("error") or "")[:200])
        with call_kind("follow_up"):
            return self.claude_client.run_prompt(self.build_argv(""), context.work_dir, prompt, FOLLOW_UP_TIMEOUT_SECONDS)

    # ------------------------------------------------ run
    def run_follow_up(self, context):
        """A new loop round on the same worktree and branch, started the way the Start dialog starts one: the delivery choices of the
        first start (meta.delivery) go in the body, the loop settings come from the ROADMAP header. Clicking Run is the approval of the
        reviewed stage, so the approval file is renewed for the new feature list. Once the loop starts the round is closed."""
        self.ensure_can_start(context)
        work_dir = context.work_dir
        roadmap_text = self.read_text(work_dir, ROADMAP_PATH)
        stages = list_follow_up_stages(roadmap_text)
        if not stages or not any(feature["status"] in OPEN_FEATURE_STATUSES for feature in stages[-1]["features"]):
            raise RepoConflictError("there is no Follow-up stage with open features: send a follow-up first", code=NO_STAGE_CODE)
        delivery = self.loop_control_service.read_delivery(context)  # meta.delivery, then delivery-pending.json, then on/on
        body = {"push": bool(delivery.get("push_pr", True)), "remove_worktree_on_finish": bool(delivery.get("remove_worktree_on_finish", True))}
        try:
            loop_state.approve_roadmap(work_dir, now_iso())
        except ValueError as failure:
            raise InvalidRoadmapError(str(failure))
        round_number = self.follow_up_thread_repository.current_round(work_dir, roadmap_text)
        self.log.info("follow_up_run", project=context.project.id, stage=stages[-1]["id"])
        status, payload = self.loop_control_service.start_new_loop(context, body)
        if status < 300:
            thread = self.follow_up_thread_repository.read_thread(work_dir, round_number)
            thread["closed_at"] = now_iso()
            self.follow_up_thread_repository.write_thread(work_dir, round_number, thread)
        return status, payload
