"""Runs the code inventory (`engine/code_inventory.py`) for a folder of a repository and reads the last result back (design D17).

The inventory lists the giant files, the entry points and the totals once, deterministically, so the map job can tell the AI where
not to read whole files. It is written to `.specs/project/loop/internal/inventory/<name>.json` (`_root` for the repository root,
the path with "/" replaced by "-" for a subproject). A failure is a note, never an exception and never a failed map job.

Thresholds come from the app config (`inventory_giant_lines`, `inventory_giant_kb` of `config.json`; invalid values fell back to
1500 / 200 and left a note there).
"""
import json
import os

from ..repositories.specs_file_repository import run_hints  # noqa: F401  (also puts the engine folder on sys.path)
import code_inventory as inventory_engine  # noqa: E402

ROOT_NAME = inventory_engine.ROOT_NAME
GIANT_LIST_SIZE = 5


def inventory_name(folder_relative):
    """`_root` for the repository root, else the path with "/" replaced by "-" (the engine's default name)."""
    normalized = (folder_relative or ".").replace("\\", "/").strip("/")
    return ROOT_NAME if normalized in ("", ".") else normalized.replace("/", "-")


def format_thousands(number):
    return "{:,}".format(int(number)).replace(",", " ")


def build_summary(files, giant):
    return "inventory: %s files, %s giant" % (format_thousands(files), format_thousands(giant))


def is_positive_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0


class CodeInventoryService:
    def __init__(self, app_config, log):
        """`app_config`: anything with `inventory_giant_lines`, `inventory_giant_kb` and optional `inventory_threshold_note`."""
        self.app_config = app_config
        self.log = log

    def thresholds(self):
        """(giant_lines, giant_kb, note): the configured values, or the defaults with the note the config left."""
        lines = getattr(self.app_config, "inventory_giant_lines", inventory_engine.DEFAULT_GIANT_LINES)
        kilobytes = getattr(self.app_config, "inventory_giant_kb", inventory_engine.DEFAULT_GIANT_KB)
        note = str(getattr(self.app_config, "inventory_threshold_note", "") or "")
        if not is_positive_number(lines):
            lines, note = inventory_engine.DEFAULT_GIANT_LINES, note or "invalid inventory_giant_lines: using the default"
        if not is_positive_number(kilobytes):
            kilobytes, note = inventory_engine.DEFAULT_GIANT_KB, note or "invalid inventory_giant_kb: using the default"
        return int(lines), int(kilobytes), note

    @staticmethod
    def relative_path(name):
        """Where the inventory of `name` lives, relative to the repository root (forward slashes: it goes into prompts)."""
        return "/".join(inventory_engine.INVENTORY_RELATIVE_DIR.replace("\\", "/").split("/") + [name + ".json"])

    def run(self, repo_root, folder_relative, name=None):
        """Measures `<repo_root>/<folder_relative>` and writes its inventory. Never raises.

        Returns {ok, path (relative to repo_root), files, giant, giant_list [{path, lines}], note, summary}."""
        name = name or inventory_name(folder_relative)
        giant_lines, giant_kb, threshold_note = self.thresholds()
        result = {"ok": False, "path": self.relative_path(name), "files": 0, "giant": 0, "giant_list": [], "note": threshold_note,
                  "summary": ""}
        try:
            root = os.path.abspath(repo_root)
            folder = root if folder_relative in ("", ".") else os.path.abspath(os.path.join(root, folder_relative))
            document = inventory_engine.build_inventory(folder, root, giant_lines, giant_kb)
            inventory_engine.write_atomically(inventory_engine.inventory_path(root, name), document)
        except Exception as failure:  # a broken inventory only costs the map its shortcut
            self.log.warn("code_inventory_failed", folder=folder_relative, type=type(failure).__name__, message=str(failure)[:200])
            message = "the code inventory failed (%s): the map runs without it" % (str(failure)[:200] or type(failure).__name__)
            result["note"] = "; ".join(part for part in (threshold_note, message) if part)
            return result
        files, giant = document["totals"]["files"], len(document["giant"])
        result.update(ok=True, files=files, giant=giant, giant_list=self.top_giants(document), summary=build_summary(files, giant))
        return result

    def read(self, repo_root, name):
        """The last inventory document of `name`, or None (missing or unreadable)."""
        try:
            with open(inventory_engine.inventory_path(os.path.abspath(repo_root), name), encoding="utf-8") as handle:
                document = json.load(handle)
        except (OSError, ValueError):
            return None
        return document if isinstance(document, dict) else None

    def giant_count(self, repo_root, name):
        document = self.read(repo_root, name)
        return len(document.get("giant") or []) if document else 0

    def giant_top(self, repo_root, name, limit=GIANT_LIST_SIZE):
        document = self.read(repo_root, name)
        return self.top_giants(document, limit) if document else []

    @staticmethod
    def top_giants(document, limit=GIANT_LIST_SIZE):
        return [{"path": item.get("path", ""), "lines": item.get("lines", 0)} for item in (document.get("giant") or [])[:limit]]

    def run_for_job(self, repo_root, folder_relative, name, update_progress):
        """The `before_start` step of a map job: runs the inventory and reports the summary (or the note) in the job progress."""
        result = self.run(repo_root, folder_relative, name)
        update_progress(result["summary"] if result["ok"] else result["note"])
        return result

    def prompt_argument(self, name):
        """The text a map prompt gets appended so the mapper finds the inventory (path relative to the repository root)."""
        return " --inventory " + self.relative_path(name)
