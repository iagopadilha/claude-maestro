"""AI validation of the subprojects list (job type `subprojects`).

The script detection (`engine/subprojects.py`) stores a list in `.specs/project/SUBPROJECTS.json`. This service asks claude (pinned to
`JOB_MODEL_CEILING`, no MCP, guard profile `subprojects`) to read the root and every candidate and to write ONLY
`.specs/project/SUBPROJECTS-SUGGESTED.json`. When the job ends, the job thread (`finalize`) validates each suggested entry and merges
the valid ones into the stored entry. The AI never edits the real file, so a bad suggestion cannot corrupt the state, and the list stays
`confirmed: false`: only the user's click confirms it.

Merge result stored in the repository entry: `validated_by: "ai"`, `validated_at`, `confirmed: false`, `notes` (the AI's notes),
`rejected: [{path, reason}]` (entries that were not applied) and `differences: [{path, change}]` where change is "added",
"skipped: <reason>", "stack changed: a -> b" (and similar). A failed job or an unreadable suggestion keeps the script list and stores
`validation_error`; the job then ends as an error so the panel shows the failure (VAL-05).
"""
import datetime
import json
import os
import re
import sys

from ..domain.project_errors import (InvalidPathError, InvalidRequestError, JobInProgressError, LoopRunningError,
                                     LoopWorktreeExistsError)
from ..repositories.subprojects_file_repository import SUGGESTED_RELATIVE, SubprojectsFileError
from .claude_job_service import JOB_MODEL_CEILING, ClaudeJobCommand
from .map_job_runner import JOB_PERMISSION_MODE

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprojects as subprojects_engine  # noqa: E402

JOB_TYPE_SUBPROJECTS = "subprojects"
HOOK_PROFILE = "subprojects"
ALLOWED_STACKS = ("dotnet", "nestjs", "react", "node", "python", "go", "java", "rust", "php", "ruby", "unknown")
VALIDATED_BY_AI = "ai"
SUGGESTED_RELATIVE_TEXT = "/".join(SUGGESTED_RELATIVE)
REASON_LIMIT = 300
NOTES_LIMIT = 20
NOTE_LIMIT = 500
DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")

VALIDATION_PROMPT = """You validate the list of subprojects of a repository that may be a monorepo. Work read-only, except for ONE file.

Read the repository root and every candidate folder of the script list below: project files (*.sln, *.csproj, package.json,
pyproject.toml, go.mod, pom.xml, ...), README, CLAUDE.md, docs/ and the first level of the source folders. Decide, for each candidate, its
name, its stack and whether it is a real subproject. You may add a folder the script missed (a folder with its own project file that is
not inside another listed project) and you may propose `skip: true` (with a short `reason`) for a folder that has no code of its own
(scripts, tooling, data, docs, infra).

Write ONLY the file `%(suggested)s` (create the folder if needed) with exactly this JSON shape, and finish:
{"projects": [{"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev", "skip": false, "reason": ""}], "notes": ["short note"]}

Rules:
- `path` is relative to the repository root, uses `/`, exists and is a folder inside the repository. Never list a folder nested inside another listed folder, and never list a folder twice.
- `stack` is one of: %(stacks)s.
- `skill` is `backend-dev` for dotnet and nestjs, `frontend-dev` for react, and an empty string for every other stack.
- `reason` explains a skip (or any change you made); `notes` holds at most a few short sentences for the user.
- Do not edit, create or delete any other file, do not run git, and do not ask questions.

Script list (JSON):
%(projects)s
"""


def now_text():
    return datetime.datetime.now().isoformat(timespec="seconds")


def short_text(value, limit):
    return str(value or "").strip()[:limit]


class SubprojectValidationService:
    def __init__(self, claude_job_service, claude_client, subprojects_file_repository, loop_status_service, repo_registry_service, log):
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.subprojects_file_repository = subprojects_file_repository
        self.loop_status_service = loop_status_service
        self.repo_registry_service = repo_registry_service
        self.log = log

    # ------------------------------------------------ start
    def start(self, context, budget_usd=None, repo_name=None):
        """Registers the job and returns it (public dict, state `running`). The AI runs in the job thread. No spend cap:
        `budget_usd` is accepted and ignored (an old client may still send it)."""
        key, folder = self.resolve_target(context, repo_name)
        self.log.info("subprojects_validation_requested", repo=key)
        try:
            if not os.path.isdir(folder):
                raise InvalidPathError("the repo folder does not exist: %s" % folder)
            self.ensure_repo_free_for_job(context)
            entry = self.load_or_detect_entry(context, key, folder)
        except Exception as refusal:
            self.log.info("subprojects_validation_refused", repo=key, reason=type(refusal).__name__, message=str(refusal)[:200])
            raise
        self.subprojects_file_repository.delete_suggested(context.work_dir)  # a stale suggestion must never be applied
        prompt = self.build_prompt(entry)
        argv = self.claude_client.build_command(JOB_MODEL_CEILING, None, no_mcp=True, prompt_argument=prompt,
                                                permission_mode=JOB_PERMISSION_MODE, hook_profile=HOOK_PROFILE)
        command = ClaudeJobCommand(argv, work_dir=context.work_dir,
                                   finalize=lambda result: self.finalize(context, key, folder, result))
        return self.claude_job_service.start_job(context.repo_path, JOB_TYPE_SUBPROJECTS, command)

    def resolve_target(self, context, repo_name=None):
        """(key in SUBPROJECTS.json, work folder of the repository): the same rule as `SubprojectsService.resolve_target`."""
        primary_name = os.path.basename(os.path.normpath(context.repo_path))
        if not repo_name or repo_name == primary_name:
            return primary_name, context.work_dir
        repo = self.repo_registry_service.resolve(context, repo_name)
        if repo.primary:
            return primary_name, context.work_dir
        worktree = repo.loop_worktree_path()
        return repo.name, worktree if os.path.isdir(worktree) else repo.path

    def ensure_repo_free_for_job(self, context):
        """Same collaborators as `RoadmapService.ensure_repo_free_for_job`, without its roadmap-only wording: the job writes in the
        work folder, so it needs the loop stopped and no other claude job running."""
        if self.loop_status_service.is_loop_running(context):
            raise LoopRunningError("a loop is running in this project: stop it first")
        if self.claude_job_service.get_running_job(context.repo_path):
            raise JobInProgressError("a claude job is already running in this project: wait for it to finish")

    def load_or_detect_entry(self, context, key, folder):
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(stored, SubprojectsFileError):
            raise InvalidRequestError("SUBPROJECTS.json cannot be read: %s" % stored.error)
        if isinstance(stored, dict):
            return stored
        result = subprojects_engine.detect(folder)
        entry = {"monorepo": bool(result["monorepo"]), "confirmed": False, "validated_by": "script", "validated_at": now_text(),
                 "reason": result.get("reason", ""), "projects": list(result["projects"]), "rejected": [],
                 "notes": list(result.get("warnings") or [])}
        self.subprojects_file_repository.write_repo_entry(context.work_dir, key, entry)
        return entry

    @staticmethod
    def build_prompt(entry):
        projects = [{field: project.get(field) for field in ("path", "name", "stack", "skill", "skip", "reason", "has_code")}
                    for project in subprojects_engine.entry_projects(entry)]
        return VALIDATION_PROMPT % {"suggested": SUGGESTED_RELATIVE_TEXT, "stacks": ", ".join(ALLOWED_STACKS),
                                    "projects": json.dumps(projects, ensure_ascii=False, indent=2)}

    # ------------------------------------------------ finalize (job thread)
    def finalize(self, context, key, folder, result):
        """Reads and deletes the suggestion, validates, merges and stores. Returns the job result (ok=False when nothing was applied)."""
        suggestion = self.subprojects_file_repository.read_suggested(context.work_dir)
        self.subprojects_file_repository.delete_suggested(context.work_dir)
        if not result.get("ok"):
            return self.fail(context, key, result, "the validation job failed: %s" % (result.get("error") or "no reason given"))
        if suggestion is None:
            return self.fail(context, key, result, "the AI did not write %s" % SUGGESTED_RELATIVE_TEXT)
        if isinstance(suggestion, SubprojectsFileError):
            return self.fail(context, key, result, "the AI suggestion is unreadable: %s" % suggestion.error)
        if not isinstance(suggestion.get("projects"), list):
            return self.fail(context, key, result, "the AI suggestion has no `projects` list")
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if not isinstance(stored, dict):
            return self.fail(context, key, result, "the stored list is not available to merge the suggestion")
        merged = self.merge_suggestion(folder, stored, suggestion)
        self.subprojects_file_repository.write_repo_entry(context.work_dir, key, merged)
        self.log.info("subprojects_validated", repo=key, projects=len(merged["projects"]), rejected=len(merged["rejected"]),
                      differences=len(merged["differences"]))
        return result

    def fail(self, context, key, result, message):
        """Keeps the script list and stores why the validation did not apply (VAL-05); the job ends as an error."""
        self.log.warn("subprojects_validation_failed", repo=key, message=message[:200])
        stored = self.subprojects_file_repository.read_repo_entry(context.work_dir, key)
        if isinstance(stored, dict):
            try:
                self.subprojects_file_repository.write_repo_entry(context.work_dir, key, dict(stored, validation_error=message[:NOTE_LIMIT]))
            except OSError as failure:
                self.log.warn("subprojects_store_failed", repo=key, type=type(failure).__name__)
        return dict(result, ok=False, error=message)

    # ------------------------------------------------ validation and merge
    def merge_suggestion(self, folder, stored, suggestion):
        """The stored entry with the valid suggested entries applied (the input dicts are not modified)."""
        projects = [dict(project) for project in subprojects_engine.entry_projects(stored)]
        rejected, differences = [], []
        seen = set()
        for raw in suggestion["projects"]:
            raw_path = raw.get("path") if isinstance(raw, dict) else None
            problem = self.find_entry_problem(folder, raw, projects, seen)
            if problem:
                rejected.append({"path": short_text(raw_path, REASON_LIMIT), "reason": problem})
                continue
            path = subprojects_engine.normalize(raw["path"])
            seen.add(path)
            existing = next((project for project in projects if subprojects_engine.normalize(project["path"]) == path), None)
            if existing is None:
                existing = {"path": path, "name": "", "stack": "unknown", "skill": "", "skip": False, "reason": "",
                            "has_code": subprojects_engine.has_code_files(os.path.join(folder, *path.split("/")))}
                projects.append(existing)
                differences.append({"path": path, "change": "added"})
            self.apply_entry(existing, raw, path, differences)
        notes = [short_text(note, NOTE_LIMIT) for note in (suggestion.get("notes") if isinstance(suggestion.get("notes"), list) else
                 [suggestion.get("notes")] if suggestion.get("notes") else []) if short_text(note, NOTE_LIMIT)][:NOTES_LIMIT]
        merged = {key: value for key, value in stored.items() if key != "validation_error"}
        merged.update(projects=projects, confirmed=False, validated_by=VALIDATED_BY_AI, validated_at=now_text(), notes=notes,
                      rejected=rejected, differences=differences)
        return merged

    @staticmethod
    def apply_entry(project, raw, path, differences):
        previous = dict(project)
        name = short_text(raw.get("name"), REASON_LIMIT)
        project["name"] = name or project.get("name") or path.replace("/", "-")
        project["stack"] = raw["stack"]
        project["skill"] = raw.get("skill") or ""
        project["skip"] = bool(raw.get("skip"))
        project["reason"] = short_text(raw.get("reason"), REASON_LIMIT)
        if previous.get("path") and not previous.get("skip") and project["skip"]:
            differences.append({"path": path, "change": "skipped: %s" % (project["reason"] or "no reason given")})
        elif previous.get("skip") and not project["skip"]:
            differences.append({"path": path, "change": "no longer skipped"})
        if previous.get("stack") and previous["stack"] != project["stack"] and previous.get("name"):
            differences.append({"path": path, "change": "stack changed: %s -> %s" % (previous["stack"], project["stack"])})
        if previous.get("name") and previous["name"] != project["name"]:
            differences.append({"path": path, "change": "renamed: %s -> %s" % (previous["name"], project["name"])})

    def find_entry_problem(self, folder, raw, projects, seen):
        """The reason a suggested entry is not applied, or '' when it is valid."""
        if not isinstance(raw, dict):
            return "the entry is not an object"
        raw_path = raw.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return "the path is missing"
        text = raw_path.strip().replace("\\", "/")
        segments = [segment for segment in text.split("/") if segment]
        if text.startswith("/") or DRIVE_PATTERN.match(text) or ".." in segments or not segments or segments == ["."]:
            return "the path is outside the repository"
        path = subprojects_engine.normalize(text)
        target = os.path.join(folder, *path.split("/"))
        if not self.is_inside(folder, target):
            return "the path is outside the repository"
        if not os.path.isdir(target):
            return "the folder does not exist"
        if path in seen:
            return "the path is listed twice"
        if raw.get("stack") not in ALLOWED_STACKS:
            return "the stack %r is not one of: %s" % (raw.get("stack"), ", ".join(ALLOWED_STACKS))
        skill = raw.get("skill") or ""
        expected = subprojects_engine.SKILL_BY_STACK.get(raw["stack"], "")
        if skill != expected:
            return "the skill %r does not match the stack %s" % (skill, raw["stack"])
        if "skip" in raw and not isinstance(raw["skip"], bool):
            return "`skip` is not true or false"
        for other in projects:
            base = subprojects_engine.normalize(other["path"])
            if base == path:
                continue
            if path.startswith(base + "/"):
                return "nested inside the listed project %s" % base
            if base.startswith(path + "/"):
                return "contains the listed project %s" % base
        return ""

    @staticmethod
    def is_inside(folder, target):
        root = os.path.normcase(os.path.realpath(folder))
        resolved = os.path.normcase(os.path.realpath(target))
        return resolved == root or resolved.startswith(root + os.sep)
