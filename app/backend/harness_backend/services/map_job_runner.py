"""The `claude -p` map job: a safety snapshot of the repository, the mapping run, the undo of every change outside the allowed
folders and, after it, the app (not the AI) writing the map index and the `CODEBASE` block of `CLAUDE.md`.

Used by the root map (`RoadmapService.map_codebase`) and reusable by a per-subproject map: the folder that holds `docs/codebase`
and `CLAUDE.md` (`map_root`), the prompt, the allowed folders, the model and the hook profile are parameters.
The HTTP request only writes the job record: the snapshot is taken in `before_start`, inside the job thread.
"""
import os

from ..repositories.specs_file_repository import run_hints
from .code_inventory_service import inventory_name as code_inventory_name
from .claude_job_service import JOB_MODEL_CEILING, JOB_TYPE_MAP, ClaudeJobCommand

JOB_PERMISSION_MODE = "auto"  # the same default as run_loop (--permission-mode auto)
MAP_CODEBASE_PROMPT = "/orchestrator map"
MAP_ALLOWED_FOLDERS = (".specs", run_hints.MAP_FOLDER)  # the map job may only change files here; anything else is undone after the job
MAP_GUARD_IGNORED_PATHS = (".git", "node_modules", ".specs/project/loop/internal", ".specs/project/loop/json/MONITOR.json",
                           ".specs/.trash")
MAP_NOTHING_WRITTEN = ("The AI finished but wrote no document in docs/codebase, so nothing was mapped. The usual cause: the /orchestrator "
                       "skill is not loaded in that claude session (update the plugin: claude plugin update "
                       "loop-engineering-harness@loop-engineering-harness) and the AI answered as a plain chat.")
ROOT_INVENTORY_NAME = "_root"
MAP_GUARD_NOTE = "\n\n(The app undid changes outside .specs/ and docs/codebase/: %s)"
MAP_GUARD_FILES_SHOWN = 8


class MapJobRunner:
    def __init__(self, claude_job_service, claude_client, workspace_file_guard, log, short_head_reader=None, code_inventory_service=None):
        """`short_head_reader(repo_path) -> str|None`: short sha of HEAD for the "mapped at" marker (None = unknown).
        `code_inventory_service` (optional, or `attach_code_inventory_service` later): the plain root map (default prompt, no
        `map_root`) then measures the repository in `before_start` and its prompt carries `--inventory <path>`. A subproject map
        gets the same through the hooks of `SubprojectMapService` (see `attach_code_inventory`)."""
        self.code_inventory_service = code_inventory_service
        self.claude_job_service = claude_job_service
        self.claude_client = claude_client
        self.workspace_file_guard = workspace_file_guard
        self.log = log
        self.short_head_reader = short_head_reader
        self.context_block_service = None  # optional (`attach_context_block_service`): the `maestro:context` block of the root

    def attach_context_block_service(self, context_block_service):
        """After a ROOT map the project's `CLAUDE.md` gets the `maestro:context` block (a single project; a monorepo root gets none, and
        a subproject map is refreshed by `SubprojectMapService`, which knows the repository key and where `SUBPROJECTS.json` is)."""
        self.context_block_service = context_block_service

    def attach_code_inventory_service(self, code_inventory_service):
        self.code_inventory_service = code_inventory_service

    def start(self, repo_path, project_id, budget_usd, prompt=MAP_CODEBASE_PROMPT, work_dir=None, allowed_folders=MAP_ALLOWED_FOLDERS,
              ignored_paths=MAP_GUARD_IGNORED_PATHS, map_root="", model=JOB_MODEL_CEILING, hook_profile="spec", job_type=JOB_TYPE_MAP,
              extra_before_start=None, parallel_key=None):
        """Registers the job (202 path) and returns its public record at once.

        `repo_path`: the repository, where the job is registered and the snapshot is taken (so a write into a sibling is caught).
        `work_dir`: where claude runs (default = `repo_path`). `allowed_folders`: relative paths claude may change.
        `map_root`: folder, relative to `repo_path` ("" = the repository root), whose `docs/codebase` receives the index and whose
        `CLAUDE.md` receives the `CODEBASE` block. `extra_before_start(update_progress)`: optional, runs after the snapshot
        in the job thread (e.g. a later inventory); an exception ends the job as error. `budget_usd` None = no `--max-budget-usd`.
        `parallel_key`: see `ClaudeJobCommand` (the subproject maps of a monorepo run next to each other)."""
        holder = {}
        map_root_path = os.path.join(repo_path, map_root) if map_root else repo_path
        map_folder = os.path.join(map_root_path, *run_hints.MAP_FOLDER.split("/"))

        inventory_service = self.code_inventory_service if (not map_root and prompt == MAP_CODEBASE_PROMPT) else None
        if inventory_service is not None:
            # The path is known from the name, so the prompt is built before the thread. If the inventory then fails the record says
            # so (progress) and the mapper simply does not find the file.
            prompt += inventory_service.prompt_argument(ROOT_INVENTORY_NAME)

        def before_start(update_progress):
            holder["snapshot"] = self.workspace_file_guard.take_snapshot(repo_path, ignored_paths)
            if inventory_service is not None:
                inventory_service.run_for_job(repo_path, ".", ROOT_INVENTORY_NAME, update_progress)
            if extra_before_start is not None:
                extra_before_start(update_progress)

        argv = self.claude_client.build_command(model, budget_usd, no_mcp=True, prompt_argument=prompt,
                                                permission_mode=JOB_PERMISSION_MODE, hook_profile=hook_profile)
        command = ClaudeJobCommand(argv, work_dir=work_dir, before_start=before_start, parallel_key=parallel_key,
                                   finalize=lambda result: self.finish(project_id, repo_path, map_root_path, map_folder, holder,
                                                                       allowed_folders, result))
        return self.claude_job_service.start_job(repo_path, job_type, command)

    def finish(self, project_id, repo_path, map_root_path, map_folder, holder, allowed_folders, result):
        """Runs in the job thread: undoes changes outside the allowed folders, fails the job when no map document was written and
        writes the index and the `CODEBASE` block."""
        report = self.workspace_file_guard.revert_unexpected_changes(holder["snapshot"], allowed_folders)
        touched = report["reverted"] + report["removed"] + report["unrestorable"]
        if touched:
            self.log.warn("map_guard_reverted", project=project_id, reverted=report["reverted"][:50],
                          removed=report["removed"][:50], unrestorable=report["unrestorable"][:50])
        result = dict(result)
        if result.get("ok") and touched:
            result["reply"] = str(result.get("reply") or "") + MAP_GUARD_NOTE % ", ".join(touched[:MAP_GUARD_FILES_SHOWN])
        if result.get("ok") and not run_hints.list_map_documents(map_folder):
            result.update(ok=False, error=MAP_NOTHING_WRITTEN)
        if result.get("ok"):  # after the undo above: this is the app (not the AI) writing the index and pointing CLAUDE.md at the map
            try:
                run_hints.write_map_index(map_root_path, self.read_short_head(repo_path))
                run_hints.write_codebase_block(map_root_path)
            except OSError as failure:
                self.log.warn("map_claude_md_block_failed", project=project_id, message=str(failure)[:200])
            if self.context_block_service is not None and os.path.abspath(map_root_path) == os.path.abspath(repo_path):
                self.context_block_service.refresh_folder(repo_path, None, "", subprojects_root=repo_path)
        return result

    def read_short_head(self, repo_path):
        """Short sha of HEAD for the "mapped at" marker; None when git cannot tell (the marker then says it is unknown)."""
        if self.short_head_reader is None:
            return None
        try:
            return self.short_head_reader(repo_path) or None
        except Exception as failure:  # the marker is a nicety: never fail the map for it
            self.log.warn("map_head_unreadable", message=str(failure)[:200])
            return None


def attach_code_inventory(map_job_runner, subproject_map_service, code_inventory_service):
    """Wiring for the composition: the root map (runner) and every subproject map (hooks) run the inventory in `before_start`
    and carry `--inventory <path relative to the repository root>` in their prompt."""
    map_job_runner.attach_code_inventory_service(code_inventory_service)
    if subproject_map_service is None:
        return

    def run_inventory(context, repo_name, folder, project_path, update_progress):
        code_inventory_service.run_for_job(folder, project_path, code_inventory_name(project_path), update_progress)

    def prompt_extension(context, repo_name, folder, project_path):
        return code_inventory_service.prompt_argument(code_inventory_name(project_path))

    subproject_map_service.add_extra_before_start(run_inventory)
    subproject_map_service.add_prompt_extension(prompt_extension)
