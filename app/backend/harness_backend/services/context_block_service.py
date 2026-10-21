"""Decides WHICH `CLAUDE.md` files get the generated `maestro:context` block and writes it (CTXF-01..CTXF-05, design D24).

  - every non-skipped, existing subproject of a CONFIRMED monorepo;
  - the root of a single-project repository (also `monorepo:false`, or a repository with no stored list that is not a monorepo)
    and of a non-monorepo edit repository the app sets up;
  - NEVER the root of a confirmed monorepo, a skipped subproject, or anything while the list is not confirmed.

The folder is the work folder (the loop worktree when it exists, else the repository). `SUBPROJECTS.json` lives in the primary's work
folder: `subprojects_root` says where to read it (default = the given folder). Nothing here raises: a problem is logged.

Wiring (composition): `subprojects_service.attach_context_block_service(service)`, `specs_structure_service.attach_context_block_service(service)`,
`map_job_runner.attach_context_block_service(service)`, `subproject_map_service.attach_context_block_service(service)`,
`system_map_service.attach_context_block_service(service)`.
"""
import os
import sys

from ..repositories.specs_file_repository import run_hints  # also puts the engine folder on sys.path

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import context_block  # noqa: E402
import readiness_checks  # noqa: E402
import subprojects as subprojects_engine  # noqa: E402

SYSTEM_RELATIVE_PATH = "docs/codebase/SYSTEM.md"
MAP_RELATIVE_FOLDER = "docs/codebase"


class ContextBlockService:
    def __init__(self, code_inventory_service=None, log=None):
        """`code_inventory_service` (optional): `thresholds() -> (lines, kb, note)` gives the giant-file threshold in effect."""
        self.code_inventory_service = code_inventory_service
        self.log = log

    # ------------------------------------------------ public entry points (never raise)
    def refresh_repo(self, work_folder, repo_key=None, subprojects_root=None):
        """Rewrites the block in every folder of that repository that gets one. Returns the relative paths written ("" = the root)."""
        written = []
        try:
            for relative_path in self.target_paths(work_folder, repo_key, subprojects_root):
                if self.write_folder(work_folder, relative_path):
                    written.append(relative_path)
        except Exception as failure:  # a nicety of the map/Conformance: never break the operation that triggered it
            self.warn("context_block_refresh_failed", folder=work_folder, type=type(failure).__name__, message=str(failure)[:200])
        return written

    def refresh_folder(self, work_folder, repo_key, relative_path="", subprojects_root=None):
        """Rewrites the block of one folder ("" = the root) when the rules give it one. True when the file was written."""
        try:
            wanted = subprojects_engine.normalize(relative_path) if relative_path else ""
            if wanted not in self.target_paths(work_folder, repo_key, subprojects_root):
                return False
            return self.write_folder(work_folder, wanted)
        except Exception as failure:
            self.warn("context_block_refresh_failed", folder=work_folder, path=relative_path, type=type(failure).__name__,
                      message=str(failure)[:200])
            return False

    # ------------------------------------------------ which folders
    def target_paths(self, work_folder, repo_key=None, subprojects_root=None):
        """Relative paths ("" = the root) that get a block: the non-skipped existing subprojects of a confirmed monorepo, [""] for a
        single project, [] for an unconfirmed monorepo, a monorepo root or a missing folder."""
        if not work_folder or not os.path.isdir(work_folder):
            return []
        key = repo_key or subprojects_engine.default_repo_name(work_folder)
        entry = subprojects_engine.load(subprojects_root or work_folder, key)["entry"]
        if entry is None:
            # never listed: a repository that is not a monorepo is a single project; a monorepo not confirmed yet gets nothing
            return [] if subprojects_engine.detect(work_folder).get("monorepo") else [""]
        if entry.get("monorepo") is not True:
            return [""]
        if entry.get("confirmed") is not True:
            return []
        paths = []
        for project in subprojects_engine.entry_projects(entry):
            path = subprojects_engine.normalize(project["path"])
            if project.get("skip") is True or not path:
                continue
            if os.path.isdir(os.path.join(work_folder, *path.split("/"))):
                paths.append(path)
        return paths

    # ------------------------------------------------ facts and write
    def write_folder(self, work_folder, relative_path):
        folder = os.path.join(work_folder, *relative_path.split("/")) if relative_path else work_folder
        facts = self.gather_facts(work_folder, relative_path)
        result = context_block.write(folder, facts)
        if result["error"]:
            self.warn("context_block_not_written", folder=folder, error=result["error"])
        elif result["written"]:
            self.info("context_block_written", folder=folder)
        return result["written"]

    def gather_facts(self, work_folder, relative_path):
        folder = os.path.join(work_folder, *relative_path.split("/")) if relative_path else work_folder
        return {"conformance": self.read_conformance(work_folder, relative_path),
                "map_documents": run_hints.list_map_documents(os.path.join(folder, *MAP_RELATIVE_FOLDER.split("/"))),
                "system_pointer": self.system_pointer(work_folder, relative_path),
                "commands": self.read_commands(folder),
                "giant_lines": self.giant_lines()}

    @staticmethod
    def read_conformance(work_folder, relative_path):
        found = subprojects_engine.read_conformance(work_folder, relative_path)
        return found if found in ("STANDARD", "LEGACY") else None

    @staticmethod
    def system_pointer(work_folder, relative_path):
        """Relative path from the subproject to the root `SYSTEM.md` ("" for a root or when the file does not exist)."""
        if not relative_path or not os.path.isfile(os.path.join(work_folder, *SYSTEM_RELATIVE_PATH.split("/"))):
            return ""
        depth = len(relative_path.split("/"))
        return "/".join([".."] * depth + SYSTEM_RELATIVE_PATH.split("/"))

    @staticmethod
    def read_commands(folder):
        try:
            return readiness_checks.known_commands(folder)
        except Exception:
            return {"build": "", "run": "", "test": ""}

    def giant_lines(self):
        if self.code_inventory_service is None:
            return context_block.DEFAULT_GIANT_LINES
        try:
            return int(self.code_inventory_service.thresholds()[0])
        except Exception:
            return context_block.DEFAULT_GIANT_LINES

    def info(self, event, **fields):
        if self.log is not None:
            self.log.info(event, **fields)

    def warn(self, event, **fields):
        if self.log is not None:
            self.log.warn(event, **fields)
