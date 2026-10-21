"""Follow-up of a finished loop: a planning-like `claude -p` job (type `follow_up`) turns the user's instructions (and the decisions they
asked to change) into NEW features appended to the ROADMAP as a stage `Follow-up <n>`, with FLOWS entries; the user reviews the stage and
**Run follow-up** starts a new loop round on the same worktree and branch, with every guard of a normal round.

The job: `--model sonnet` (JOB_MODEL_CEILING), hook profile `planning` (Write/Edit only inside `.specs/`), `Edit` allowed only on
ROADMAP.md and FLOWS.md, no MCP, US$ 1 by default (5 at most), run in the loop worktree. Before it runs the app snapshots the work folder and
keeps the text of ROADMAP.md and FLOWS.md; afterwards (finalize) it undoes anything outside those two files and accepts ONLY an appended
`Follow-up <n>` stage plus appended FLOWS text: if an earlier line of either file changed, or the stage is not the next follow-up, both files
are restored and the job ends as `error` with the reason.

The stage id is the first word of the heading (loop_state.STAGE_LINE), so the heading is written `## Stage Follow-up-<n> - Follow-up <n>`:
one id per round. `Follow-up <n>` with a space is accepted too.
Refused (409) while the loop runs, while any claude job runs, or without a loop worktree.
"""
import os
from typing import Protocol

from ..domain.project_errors import (LOOP_RUNNING_CODE, InvalidRequestError, InvalidRoadmapError, JobInProgressError, LoopRunningError,
                                     RepoConflictError)
from ..domain.follow_up_stage import (ANY_STAGE_HEADING, FEATURE_LINE, OPEN_FEATURE_STATUSES, STAGE_HEADING,  # noqa: F401  (re-exported)
                                      list_follow_up_stages, normalize_newlines, validate_appended_stage)
from ..engine import decisions as decisions_engine
from ..infrastructure.claude_client import redact_secrets
from ..repositories.loop_command_file_repository import now_iso
from ..repositories.specs_file_repository import loop_state
from .claude_job_service import JOB_MODEL_CEILING, JOB_STATE_ERROR, ClaudeJobCommand
from .planning_service import GUARD_IGNORED_PATHS, is_number

JOB_TYPE_FOLLOW_UP = "follow_up"
FOLLOW_UP_BUDGET_USD = 1
FOLLOW_UP_MAX_BUDGET_USD = 5
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

FOLLOW_UP_RULES = """You are the planner of a project whose loop already ran. The user wants a FOLLOW-UP: new work on top of what was built.
Turn the request below into NEW features appended to .specs/project/ROADMAP.md as ONE new stage, and add matching entries to .specs/project/FLOWS.md.
Rules:
- Read .specs/project/ROADMAP.md, FLOWS.md, PROJECT.md and RULES.md first (and the decisions listed below, if any). Edit ONLY ROADMAP.md and FLOWS.md.
- APPEND at the end of ROADMAP.md a stage with exactly this heading (the id is the first word, so it carries the number): `## Stage Follow-up-%(n)s - Follow-up %(n)s`
- Never edit, move, reword, reorder or delete any existing line of ROADMAP.md or FLOWS.md; never edit another stage; never change a status. The app restores both files if you do.
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


class FollowUpService:
    def __init__(self, specs_file_repository, claude_job_service, claude_client, workspace_file_guard, loop_status_service,
                 loop_control_service, log):
        self.specs_file_repository = specs_file_repository
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.loop_status_service = loop_status_service
        self.loop_control_service = loop_control_service  # start_new_loop(context, body) -> (status, payload)
        self.log = log

    # ------------------------------------------------ read
    def get_follow_up(self, context):
        draft = self.read_draft(context.work_dir)
        jobs = [job for job in self.claude_job_service.list_jobs(context.repo_path) if job.get("type") == JOB_TYPE_FOLLOW_UP]
        stages = list_follow_up_stages(self.read_text(context.work_dir, ROADMAP_PATH))
        return {"draft": {"text": str(draft.get("text") or ""), "decisions": list(draft.get("decisions") or [])},
                "last_job": jobs[0] if jobs else None,
                "stage": dict(stages[-1], name="Follow-up %s" % stages[-1]["n"] if stages[-1]["n"] else "Follow-up") if stages else None}

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
        body = body if isinstance(body, dict) else {}
        text, budget = self.validate_text(body.get("text")), self.validate_budget(body.get("budget_usd"))
        decision_ids = self.validate_decision_ids(body.get("decisions"))
        work_dir = context.work_dir
        decisions_text = self.describe_decisions(work_dir, decision_ids)
        if not text and not decision_ids:
            raise InvalidRequestError("write what the follow-up should do, or pick decisions to change")
        self.ensure_can_start(context)
        number = len(list_follow_up_stages(self.read_text(work_dir, ROADMAP_PATH))) + 1
        snapshot = self.workspace_file_guard.take_snapshot(work_dir, GUARD_IGNORED_PATHS)
        before = {path: self.read_text(work_dir, path) for path in EDITABLE_PATHS}
        allowed_tools = ["Read", "Grep", "Glob"] + ["Edit(/%s)" % path for path in EDITABLE_PATHS]
        argv = self.claude_client.build_command(JOB_MODEL_CEILING, budget, allowed_tools, FOLLOW_UP_DISALLOWED_TOOLS, no_mcp=True,
                                                permission_mode=FOLLOW_UP_PERMISSION_MODE, hook_profile=FOLLOW_UP_HOOK_PROFILE)
        command = ClaudeJobCommand(argv, self.compose_prompt(number, text, decisions_text), timeout_seconds=FOLLOW_UP_TIMEOUT_SECONDS,
                                   work_dir=work_dir, finalize=lambda result: self.finish_job(context, snapshot, before, number, result))
        self.write_draft_text(work_dir, text)  # kept if the job fails, so the user does not retype it
        job = self.claude_job_service.start_job(context.repo_path, JOB_TYPE_FOLLOW_UP, command)
        self.log.info("follow_up_sent", project=context.project.id, job=job["id"], number=number, decisions=len(decision_ids))
        return {"ok": True, "job": job}

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
    def validate_budget(value):
        if value is None:
            return FOLLOW_UP_BUDGET_USD
        if not is_number(value) or not 0 < value <= FOLLOW_UP_MAX_BUDGET_USD:
            raise InvalidRequestError("budget_usd must be a number above 0 and at most %d" % FOLLOW_UP_MAX_BUDGET_USD)
        return value

    @staticmethod
    def validate_decision_ids(value):
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise InvalidRequestError("decisions must be a list of decision ids")
        return list(dict.fromkeys(item.strip() for item in value))

    @staticmethod
    def describe_decisions(work_dir, decision_ids):
        """The VALIDATE.md lines the user picked, as prompt text; an unknown id is a 400 (the list changed under the user)."""
        if not decision_ids:
            return ""
        items, unparsed = decisions_engine.parse(decisions_engine.read_file(work_dir))
        by_id = {item["id"]: item for item in items + unparsed}
        unknown = [item_id for item_id in decision_ids if item_id not in by_id]
        if unknown:
            raise InvalidRequestError("unknown decision id: %s" % unknown[0])
        return "\n".join("- %s" % by_id[item_id]["raw"].lstrip("- ").strip() for item_id in decision_ids)

    @staticmethod
    def compose_prompt(number, text, decisions_text):
        parts = [FOLLOW_UP_RULES % {"n": number}]
        if decisions_text:
            parts.append("Decisions the user asked to CHANGE (from .specs/project/loop/VALIDATE.md; each line: doubt | what was chosen | how to revert):\n" + decisions_text)
        parts.append("FOLLOW-UP-NUMBER: %d\n\nUser request:\n%s" % (number, text or "(no extra text: change the decisions listed above)"))
        return "\n\n".join(parts)

    def ensure_can_start(self, context):
        """Shared by send and run: a loop or a claude job running, or no loop worktree, refuses with 409."""
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("the loop is running: wait for it to stop before a follow-up", code=LOOP_RUNNING_CODE)
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running in this project: wait for it to finish", code=FOLLOW_UP_BUSY_CODE)
        worktree = context.project.loop_worktree_path()
        if not os.path.isdir(worktree) or os.path.normcase(os.path.abspath(context.work_dir)) != os.path.normcase(os.path.abspath(worktree)):
            raise RepoConflictError("there is no loop worktree: a follow-up needs the finished loop's worktree (use Back to planning for new work)",
                                    code=NO_WORKTREE_CODE)

    # ------------------------------------------------ finalize (job thread)
    def finish_job(self, context, snapshot, before, number, result):
        """Keeps the new stage, or restores ROADMAP.md and FLOWS.md and turns the job into an error with the reason."""
        work_dir = context.work_dir
        report = self.workspace_file_guard.revert_unexpected_changes(snapshot, list(EDITABLE_PATHS))
        if report["reverted"] or report["removed"] or report["unrestorable"]:
            self.log.warn("follow_up_guard_reverted", project=context.project.id, reverted=report["reverted"][:50], removed=report["removed"][:50])
        problem = None
        if not result.get("ok"):
            problem = "the follow-up job did not finish cleanly"
        else:
            problem = validate_appended_stage(before[ROADMAP_PATH], self.read_text(work_dir, ROADMAP_PATH),
                                              before[FLOWS_PATH], self.read_text(work_dir, FLOWS_PATH), number)
        result = dict(result)
        if problem is None:
            self.write_draft_text(work_dir, "")
            result["reply"] = redact_secrets(result.get("reply") or "", FOLLOW_UP_REPLY_LIMIT)
            return result
        for path in EDITABLE_PATHS:
            self.specs_file_repository.write_text_atomic(os.path.join(work_dir, *path.split("/")), before[path])
        self.log.warn("follow_up_reverted", project=context.project.id, reason=problem[:200])
        if result.get("ok"):
            result.update(ok=False, error=problem, reply=redact_secrets(result.get("reply") or "", FOLLOW_UP_REPLY_LIMIT) + FINALIZE_REVERTED_NOTE % problem)
        return result

    # ------------------------------------------------ run
    def run_follow_up(self, context):
        """A new loop round on the same worktree and branch, started the way the Start dialog starts one: the delivery choices of the
        first start (meta.delivery) go in the body, the loop settings come from the ROADMAP header. Clicking Run is the approval of the
        reviewed stage, so the approval file is renewed for the new feature list."""
        self.ensure_can_start(context)
        work_dir = context.work_dir
        stages = list_follow_up_stages(self.read_text(work_dir, ROADMAP_PATH))
        if not stages or not any(feature["status"] in OPEN_FEATURE_STATUSES for feature in stages[-1]["features"]):
            raise RepoConflictError("there is no Follow-up stage with open features: send a follow-up first", code=NO_STAGE_CODE)
        delivery = self.loop_control_service.read_delivery(context)  # meta.delivery, then delivery-pending.json, then on/on
        body = {"push": bool(delivery.get("push_pr", True)), "remove_worktree_on_finish": bool(delivery.get("remove_worktree_on_finish", True))}
        try:
            loop_state.approve_roadmap(work_dir, now_iso())
        except ValueError as failure:
            raise InvalidRoadmapError(str(failure))
        self.log.info("follow_up_run", project=context.project.id, stage=stages[-1]["id"])
        return self.loop_control_service.start_new_loop(context, body)
