"""Workspace of a project: what exists on disk for the loop (a git worktree per repo) and the job that creates it.

`get_workspace` tells, per repo, the folder, the worktree, the loop branch, the base branch and a state
(`not_created | ready | loop_running | dirty | missing`) plus the plain-language explanation. `create_workspace` starts a job
(registry of `claude_job_service`, type `workspace-create`, no claude involved) that runs the ENGINE routine and turns each
engine step line into a step the panel polls. The worktree is always created by the engine (the same code the loop start uses);
nothing here runs `git worktree add`. An existing worktree is never recreated.
"""
import datetime
import os
import re
import sys
import time
from typing import Protocol

from ..domain.project_errors import InvalidRepoError, JobInProgressError, JobNotFoundError, RepoConflictError
from ..repositories.loop_command_file_repository import RELAUNCH_LOG_FILE_NAME
from ..repositories.specs_file_repository import loop_state
from .claude_job_service import JOB_TYPE_WORKSPACE_CREATE, now_iso
from .loop_control_service import LAUNCH_MARKER_PREFIX

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import project_repos  # noqa: E402

BUSY_CODE = "busy"
UNKNOWN_REPO_CODE = "unknown_repo"
WORKTREE_EXISTS_CODE = "worktree_exists"
COMMIT_FAILED_CODE = "commit_push_failed"
COMMIT_MESSAGE_LIMIT = 200
DEFAULT_COMMIT_MESSAGE = "chore: save work before the loop worktree is created"
STATE_NOT_CREATED = "not_created"
STATE_READY = "ready"
STATE_LOOP_RUNNING = "loop_running"
STATE_DIRTY = "dirty"
STATE_MISSING = "missing"
EXPLANATION = ("The loop works in a separate folder (a git worktree) on a new loop/<date> branch, so your folder and branch are "
               "never touched. Nothing is pushed until you deliver.")
UNCOMMITTED_NOTE = "Your folder has uncommitted changes. The worktree is born from the last commit and does not carry them."
MISSING_NOTE = "The repo folder does not exist anymore; restore it or remove the repo from the list."
STANDARD_COPIED_NAMES = (".specs", "CLAUDE.md", ".claude")  # copied at creation by the engine; not the user's copy list
SPECS_PREFIX = ".specs/"
ENSURE_WORKTREES_FLAG = "--ensure-worktrees"  # the engine entry the create job calls (adapter below); see EngineWorktreeAdapter
ONLY_REPOS_FLAG = "--repos"
CREATE_TIMEOUT_SECONDS = 600
POLL_SECONDS = 0.3
CREATE_LOG_FILE_NAME = "workspace-create.log"
STEP_LINE = re.compile(r"^\[loop\] (?:\[([^\]]+)\] )?(check|warn|worktree-add|reattach|copy|setup|ready|reuse|error): (.*)$")
LEGACY_REUSE_LINE = re.compile(r"^\[loop\] reusing worktree (.+) on branch (\S+)$")
LEGACY_CREATED_LINE = re.compile(r"^\[loop\] worktree created: (.+?) \(branch (\S+), base ([^;]+);")
LEGACY_SETUP_LINE = re.compile(r"^\[loop\] one-time setup: (.*)$")
READY_TEXT = re.compile(r"^path=(.*) branch=(\S+) base=(\S+)$")
LOG_OUTPUT_LIMIT = 300
LOOP_START_JOB_ID = "loop-start"  # read-only job built from the loop's own output while it creates worktrees at start
TURN_LINE = re.compile(r"^\[loop\] turn \d+")
START_LOG_TAIL_BYTES = 256 * 1024


def to_posix(path):
    return str(path).replace("\\", "/")


def parse_step_line(line, primary_name):
    """One engine stdout line -> {"repo","kind","text"} or None (a line that is not a step still goes to the log).
    The three legacy single-repo lines (no `[repo]` tag) map to reuse / ready / setup for the primary repo."""
    line = line.strip()
    found = STEP_LINE.match(line)
    if found:
        return {"repo": found.group(1) or primary_name, "kind": found.group(2), "text": found.group(3)}
    found = LEGACY_REUSE_LINE.match(line)
    if found:
        return {"repo": primary_name, "kind": "reuse", "text": "reusing worktree %s on branch %s" % (found.group(1), found.group(2))}
    found = LEGACY_CREATED_LINE.match(line)
    if found:
        return {"repo": primary_name, "kind": "ready", "text": "path=%s branch=%s base=%s" % (found.group(1), found.group(2), found.group(3).strip())}
    found = LEGACY_SETUP_LINE.match(line)
    if found:
        return {"repo": primary_name, "kind": "setup", "text": found.group(1)}
    return None


class IWorkspaceService(Protocol):
    def get_workspace(self, context) -> dict: ...
    def create_workspace(self, context, repo_names) -> dict: ...
    def commit_and_push(self, context, repo_name, message=None, push=True) -> dict: ...
    def get_create_job(self, context, job_id) -> dict: ...


class EngineWorktreeAdapter:
    """Thin adapter over the engine routine (Track A1, `ensure_worktrees` in `engine/run_loop.py`).

    Runs `python run_loop.py <primary repo> --ensure-worktrees [--repos a,b]` detached, its output going to a log file that this
    adapter tails line by line (so the panel sees each step while it happens). The process prints the step lines of the contract
    (`[loop] [<repo>] <kind>: <text>`) and exits. If the engine does not know the flag yet, the output has no `ready`/`reuse` line
    and the job ends with an error that quotes the last output line: nothing is created by this adapter in that case.
    """

    def __init__(self, config, process_runner):
        self.config = config
        self.process_runner = process_runner

    def run(self, primary_repo, repo_names, log_path, on_output_line, timeout_seconds=CREATE_TIMEOUT_SECONDS):
        """Starts the routine and calls `on_output_line(line)` for every output line while it runs. Returns None, or an error text."""
        arguments = [ENSURE_WORKTREES_FLAG] + ([ONLY_REPOS_FLAG, ",".join(repo_names)] if repo_names else [])
        argv = [sys.executable, self.config.engine_script_path("run_loop"), primary_repo] + arguments
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        try:
            if os.path.exists(log_path):
                os.remove(log_path)
            pid = self.process_runner.spawn_detached(argv, primary_repo, log_path, {"PYTHONUNBUFFERED": "1"})
        except OSError as failure:
            return "could not start the engine: %s" % failure
        position, pending, deadline = 0, "", time.monotonic() + timeout_seconds
        while True:
            alive = self.process_runner.is_alive(pid)
            position, pending = self.read_new_lines(log_path, position, pending, on_output_line, flush=not alive)
            if not alive:
                return None
            if time.monotonic() > deadline:
                self.process_runner.kill_tree(pid)
                return "timed out after %d minutes" % (timeout_seconds // 60)
            time.sleep(POLL_SECONDS)

    @staticmethod
    def read_new_lines(log_path, position, pending, on_output_line, flush):
        try:
            with open(log_path, "rb") as handle:
                handle.seek(position)
                data = handle.read()
        except OSError:
            return position, pending
        text = pending + data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        pending = "" if flush else lines.pop()
        for line in lines:
            if line.strip():
                on_output_line(line.rstrip("\r"))
        return position + len(data), pending


class WorkspaceService:
    def __init__(self, repo_registry_service, loop_status_service, loop_file_repository, git_client, claude_job_service, worktree_adapter,
                 log, start_in_progress_provider=None):
        """`start_in_progress_provider(context) -> bool` (optional): True while a loop launched by this app is alive (its lock may not exist yet)."""
        self.start_in_progress_provider = start_in_progress_provider
        self.repo_registry_service = repo_registry_service
        self.loop_file_repository = loop_file_repository
        self.loop_status_service = loop_status_service
        self.git_client = git_client
        self.claude_job_service = claude_job_service
        self.worktree_adapter = worktree_adapter
        self.log = log

    # ------------------------------------------------ read
    def get_workspace(self, context):
        multi, stored = self.repo_registry_service.read_stored(context)
        stored_setup = {repo.name: repo.setup for repo in stored}
        loop_running = self.loop_status_service.is_loop_running(context)
        meta = self.loop_file_repository.read_meta(context.work_dir)
        if not meta:  # the work dir is still the primary repo while no loop ran: the bare worktree holds the recorded branch and base
            meta = self.loop_file_repository.read_meta(context.project.loop_worktree_path())
        cli_setup = self.read_saved_setup(context)  # the CLI --setup applies to the primary when REPOS.json has none for it
        repos = [self.describe_repo(context, repo, meta, loop_running, stored_setup.get(repo.name) or (cli_setup if repo.primary else None))
                 for repo in project_repos.load_repos(context.repo_path)]
        job = self.find_running_create_job(context) or self.read_loop_start_job(context)
        return {"ok": True, "multi": multi, "explanation": EXPLANATION, "repos": repos, "job": job}

    def describe_repo(self, context, repo, meta, loop_running, setup):
        folder, worktree = repo.path, repo.loop_worktree_path()
        folder_exists, worktree_exists = os.path.isdir(folder), os.path.isdir(worktree)
        recorded = (meta.get("repos") or {}).get(repo.name) if isinstance(meta.get("repos"), dict) else None
        if recorded is None and repo.primary:
            recorded = meta
        recorded = recorded if isinstance(recorded, dict) else {}
        branch = (self.git_client.get_current_branch(worktree) or recorded.get("branch") or None) if worktree_exists else None
        base = recorded.get("base") if worktree_exists and recorded.get("base") else (self.git_client.get_current_branch(folder) or None) if folder_exists else None
        notes, dirty = [], False
        if not folder_exists:
            state = STATE_MISSING
            notes.append(MISSING_NOTE)
        elif not worktree_exists:
            state = STATE_NOT_CREATED
            dirty = self.has_changes_outside_specs(folder)
            if dirty:
                notes.append(UNCOMMITTED_NOTE)
        else:
            dirty = self.has_changes_outside_specs(worktree)
            state = STATE_LOOP_RUNNING if loop_running else (STATE_DIRTY if dirty else STATE_READY)
        return {"name": repo.name, "primary": repo.primary, "folder": folder, "worktree": worktree, "worktree_exists": worktree_exists,
                "branch": branch, "base": base, "state": state, "dirty": dirty, "copy": list(repo.copy), "setup": setup or None,
                "notes": notes}

    def has_changes_outside_specs(self, worktree):
        """Uncommitted work (of a worktree or of the user folder) that is not plan state (`.specs/` changes every turn and does not count)."""
        code, output, _ = self.git_client.run_git(worktree, "status", "--porcelain")
        if code != 0:
            return False
        for line in output.splitlines():
            path = to_posix(line[3:].strip().strip('"'))
            if not path.startswith(SPECS_PREFIX):
                return True
        return False

    def read_saved_setup(self, context):
        """The `--setup` text of the last recorded loop command (LOOP-CMD.json); None when there was none."""
        command = self.loop_file_repository.read_loop_command(context.work_dir)
        argv = command.get("argv") if isinstance(command.get("argv"), list) else []
        for index, item in enumerate(argv):
            if item == "--setup" and index + 1 < len(argv):
                return str(argv[index + 1]) or None
            if isinstance(item, str) and item.startswith("--setup="):
                return item[len("--setup="):] or None
        return None

    def find_running_create_job(self, context):
        job = self.claude_job_service.get_running_job(context.repo_path)
        return job if job and job.get("type") == JOB_TYPE_WORKSPACE_CREATE else None

    def read_loop_start_job(self, context):
        """Read-only job of a creation done by the LOOP at start: the step lines in the detached loop's output
        (`internal/monitor-relaunch.log`, appended across launches; each launch starts with a `[loop-launch]` marker line, and only the lines
        after the LAST marker are read; a log with no marker, from an older app, keeps the old rule: the whole tail). Only while the loop is alive and has not reached its first turn
        (a `[loop] turn N` line closes the creation). None when nothing is being created."""
        alive = self.loop_status_service.is_loop_running(context) or bool(
            self.start_in_progress_provider and self.start_in_progress_provider(context))
        if not alive:
            return None
        candidates = [loop_state.loop_path(folder, RELAUNCH_LOG_FILE_NAME) for folder in dict.fromkeys((context.work_dir, context.repo_path))]
        existing = [path for path in candidates if os.path.isfile(path)]  # the launch wrote it in the primary before the worktree held loop state
        log_path = max(existing, key=os.path.getmtime) if existing else candidates[0]
        try:
            with open(log_path, "rb") as handle:
                size = handle.seek(0, os.SEEK_END)
                handle.seek(max(0, size - START_LOG_TAIL_BYTES))
                text = handle.read().decode("utf-8", errors="replace")
            started_at = datetime.datetime.fromtimestamp(os.path.getctime(log_path)).isoformat(timespec="seconds")
        except OSError:
            return None
        primary_name = project_repos.primary_of(project_repos.load_repos(context.repo_path)).name
        lines = text.splitlines()
        marker_positions = [index for index, line in enumerate(lines) if line.startswith(LAUNCH_MARKER_PREFIX)]
        if marker_positions:  # only the CURRENT launch counts: what follows the last marker line
            lines = lines[marker_positions[-1] + 1:]
        steps = []
        for line in lines:
            if TURN_LINE.match(line.strip()):
                steps = []  # the creation of that launch is over
                continue
            step = parse_step_line(line, primary_name)
            if step is not None:
                steps.append({"at": "", "repo": step["repo"], "kind": step["kind"], "text": step["text"]})
        if not steps:
            return None
        error = next((step["text"] for step in steps if step["kind"] == "error"), "")
        return {"id": LOOP_START_JOB_ID, "type": JOB_TYPE_WORKSPACE_CREATE, "state": "error" if error else "running",
                "started_at": started_at, "finished_at": None, "error": error, "steps": steps, "result": None}

    # ------------------------------------------------ save the user's folder (commit + push on its current branch)
    def commit_and_push(self, context, repo_name, message=None, push=True):
        """`git add -A`, commit and push in the USER's folder of a repo that has no worktree yet, so the worktree (born from the last
        commit) carries the work. Current branch only; a worktree that already exists is never touched (its state is the loop's)."""
        if not isinstance(push, bool):
            raise InvalidRepoError("push must be true or false")
        repo = self.repo_registry_service.resolve(context, repo_name)
        if self.loop_status_service.is_loop_running(context):
            raise RepoConflictError("a loop is running in this project: stop it first", code=BUSY_CODE)
        if not os.path.isdir(repo.path):
            raise InvalidRepoError("the repo folder does not exist: %s" % repo.path)
        if os.path.isdir(repo.loop_worktree_path()):
            raise RepoConflictError("the worktree of %s already exists: the loop works there, your folder is not copied again" % repo.name,
                                    code=WORKTREE_EXISTS_CODE)
        text = (message or "").strip() if isinstance(message, str) or message is None else None
        if text is None or len(text) > COMMIT_MESSAGE_LIMIT or "\n" in text:
            raise InvalidRepoError("the commit message must be one line of up to %d characters" % COMMIT_MESSAGE_LIMIT)
        outcome = self.git_client.commit_all_and_push(repo.path, text or DEFAULT_COMMIT_MESSAGE, push)
        self.log.info("workspace_commit_push", project=context.project.id, repo=repo.name, ok=outcome["ok"], committed=outcome["committed"],
                      pushed=outcome["pushed"])
        if not outcome["ok"]:
            raise RepoConflictError(outcome["error"], code=COMMIT_FAILED_CODE, extras={"committed": outcome["committed"], "branch": outcome["branch"]})
        return {"ok": True, "repo": repo.name, "branch": outcome["branch"], "committed": outcome["committed"], "pushed": outcome["pushed"],
                "commit": outcome["commit"]}

    # ------------------------------------------------ create
    def create_workspace(self, context, repo_names):
        """Starts the create job. Existing worktrees are skipped (a `reuse` step), never recreated."""
        repos = project_repos.load_repos(context.repo_path)
        targets = self.select_targets(repos, repo_names)
        if self.loop_status_service.is_loop_running(context):
            raise RepoConflictError("a loop is running in this project: its worktrees already exist", code=BUSY_CODE)
        primary = project_repos.primary_of(repos)

        def task(add_step):
            return self.run_create_task(context, primary, repos, targets, add_step)

        try:
            job = self.claude_job_service.start_task_job(context.repo_path, JOB_TYPE_WORKSPACE_CREATE, task)
        except JobInProgressError as failure:
            raise RepoConflictError(str(failure), code=BUSY_CODE) from failure
        self.log.info("workspace_create_started", project=context.project.id, repos=",".join(repo.name for repo in targets))
        return {"ok": True, "job_id": job["id"]}

    def get_create_job(self, context, job_id):
        if job_id == LOOP_START_JOB_ID:
            job = self.read_loop_start_job(context)
            if job is None:
                raise JobNotFoundError("job not found")
            return {"ok": True, "job": job}
        job = self.claude_job_service.get_job(context.repo_path, job_id)
        if job.get("type") != JOB_TYPE_WORKSPACE_CREATE:
            raise JobNotFoundError("job not found")
        return {"ok": True, "job": job}

    @staticmethod
    def select_targets(repos, repo_names):
        """The repos to create: the named ones, or (empty/absent) every repo whose worktree folder does not exist yet."""
        if repo_names is not None and (not isinstance(repo_names, list) or not all(isinstance(name, str) for name in repo_names)):
            raise InvalidRepoError("repos must be a list of repo names")
        if not repo_names:
            return [repo for repo in repos if not os.path.isdir(repo.loop_worktree_path())]
        by_name = {repo.name: repo for repo in repos}
        unknown = [name for name in repo_names if name not in by_name]
        if unknown:
            raise InvalidRepoError("unknown repo: %s" % ", ".join(unknown), code=UNKNOWN_REPO_CODE)
        return [by_name[name] for name in dict.fromkeys(repo_names)]

    def run_create_task(self, context, primary, repos, targets, add_step):
        """Job body (runs in the job thread): engine routine, steps streamed, result collected from `ready`/`reuse`/`copy` lines."""
        created, output_tail = {}, []
        to_create = [repo for repo in targets if not os.path.isdir(repo.loop_worktree_path())]
        for repo in targets:
            if repo not in to_create:  # never recreated: say so, record where it is
                add_step({"at": now_iso(), "repo": repo.name, "kind": "reuse", "text": "worktree already exists: %s" % repo.loop_worktree_path()})
                created[repo.name] = {"worktree": repo.loop_worktree_path(), "branch": self.git_client.get_current_branch(repo.loop_worktree_path()) or None,
                                      "base": self.git_client.get_current_branch(repo.path) or None, "reattached": False, "copied": []}
        if not to_create:
            return {"ok": True, "result": {"repos": created}, "error": ""}

        def on_output_line(line):
            output_tail.append(line)
            del output_tail[:-5]
            step = parse_step_line(line, primary.name)
            if step is None:
                return
            add_step({"at": now_iso(), "repo": step["repo"], "kind": step["kind"], "text": step["text"]})
            entry = created.setdefault(step["repo"], {"worktree": None, "branch": None, "base": None, "reattached": False, "copied": []})
            if step["kind"] == "ready":
                ready = READY_TEXT.match(step["text"])
                if ready:
                    entry.update(worktree=ready.group(1), branch=ready.group(2), base=ready.group(3))
            elif step["kind"] == "reattach":
                entry["reattached"] = True
            elif step["kind"] == "copy":
                entry["copied"] = [name.strip() for name in step["text"].split(",") if name.strip() and name.strip() not in STANDARD_COPIED_NAMES]

        failure = self.worktree_adapter.run(primary.path, [repo.name for repo in to_create],
                                            loop_state.loop_path(context.repo_path, CREATE_LOG_FILE_NAME), on_output_line)
        missing = [repo.name for repo in to_create if not created.get(repo.name, {}).get("worktree")]
        if failure or missing:
            reason = failure or next((line for line in reversed(output_tail) if line.strip()), "") or "the engine did not report a worktree"
            return {"ok": False, "result": {"repos": created},
                    "error": ("%s (not created: %s)" % (reason[:LOG_OUTPUT_LIMIT], ", ".join(missing))) if missing else reason[:LOG_OUTPUT_LIMIT]}
        return {"ok": True, "result": {"repos": created}, "error": ""}
