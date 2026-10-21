"""Files behind the subprojects table: `.specs/project/SUBPROJECTS.json` (the list, keyed by repository name),
`.specs/project/SUBPROJECTS-SUGGESTED.json` (the AI's suggestion, read and deleted here, written only by the AI) and
`.specs/project/loop/internal/subprojects/map-queue.json` (the sequential map queue).

Writes are atomic (temporary file in the same folder, then replace), UTF-8, LF, two-space indent and a final newline, so a
read followed by a write of the same data leaves the same bytes. Reads are tolerant: a missing file is "absent" and an
unreadable or invalid one comes back as a `SubprojectsFileError`; nothing here raises into the panel for bad content.
"""
import json
import os
from dataclasses import dataclass

from .specs_file_repository import SpecsFileRepository, TEXT_ENCODING

SUBPROJECTS_RELATIVE = (".specs", "project", "SUBPROJECTS.json")
SUGGESTED_RELATIVE = (".specs", "project", "SUBPROJECTS-SUGGESTED.json")
MAP_QUEUE_RELATIVE = (".specs", "project", "loop", "internal", "subprojects", "map-queue.json")
SUBPROJECTS_FILE_VERSION = 1


@dataclass(frozen=True)
class SubprojectsFileError:
    """Returned (never raised) when a file exists but cannot be used."""
    path: str
    error: str


def render_json_text(data):
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


class SubprojectsFileRepository:
    def __init__(self, specs_file_repository=None):
        self.specs_file_repository = specs_file_repository or SpecsFileRepository()

    # ------------------------------------------------ paths
    @staticmethod
    def subprojects_path(work_dir):
        return os.path.join(work_dir, *SUBPROJECTS_RELATIVE)

    @staticmethod
    def suggested_path(work_dir):
        return os.path.join(work_dir, *SUGGESTED_RELATIVE)

    @staticmethod
    def map_queue_path(work_dir):
        return os.path.join(work_dir, *MAP_QUEUE_RELATIVE)

    # ------------------------------------------------ generic tolerant json object
    def read_json_object(self, path):
        """The parsed object, None when the file does not exist, or a SubprojectsFileError (unreadable, not UTF-8, invalid
        JSON, or not a JSON object)."""
        if not os.path.exists(path):
            return None
        content = self.specs_file_repository.read_bytes(path)
        if content is None:
            return SubprojectsFileError(path, "the file cannot be read")
        try:
            data = json.loads(content.decode(TEXT_ENCODING))
        except (UnicodeDecodeError, ValueError) as error:
            return SubprojectsFileError(path, "the file is not valid JSON: %s" % error)
        if not isinstance(data, dict):
            return SubprojectsFileError(path, "the file does not hold a JSON object")
        return data

    def write_json_object(self, path, data):
        self.specs_file_repository.write_bytes_atomic(path, render_json_text(data).encode(TEXT_ENCODING))  # creates the folders

    # ------------------------------------------------ SUBPROJECTS.json
    def read_subprojects(self, work_dir):
        """Whole file: `{"version":1,"repos":{...}}`, None when absent, SubprojectsFileError when unusable."""
        data = self.read_json_object(self.subprojects_path(work_dir))
        if isinstance(data, dict) and not isinstance(data.get("repos"), dict):
            return SubprojectsFileError(self.subprojects_path(work_dir), "the file has no `repos` object")
        return data

    def write_subprojects(self, work_dir, data):
        self.write_json_object(self.subprojects_path(work_dir), data)

    def read_repo_entry(self, work_dir, repo_name):
        """Entry of one repository, None when the file or the entry is absent, SubprojectsFileError when the file is unusable."""
        data = self.read_subprojects(work_dir)
        if data is None or isinstance(data, SubprojectsFileError):
            return data
        entry = data["repos"].get(repo_name)
        return entry if isinstance(entry, dict) else None

    def write_repo_entry(self, work_dir, repo_name, entry):
        """Replaces (or adds) one repository's entry, keeping the others. An unusable existing file is replaced by a new one
        holding only this entry (the caller read it before and showed the error)."""
        data = self.read_subprojects(work_dir)
        if not isinstance(data, dict):
            data = {"version": SUBPROJECTS_FILE_VERSION, "repos": {}}
        data.setdefault("version", SUBPROJECTS_FILE_VERSION)
        data["repos"][repo_name] = entry
        self.write_subprojects(work_dir, data)

    # ------------------------------------------------ SUBPROJECTS-SUGGESTED.json
    def read_suggested(self, work_dir):
        """The AI's suggestion, None when absent, SubprojectsFileError when unusable."""
        return self.read_json_object(self.suggested_path(work_dir))

    def delete_suggested(self, work_dir):
        """True when a file was removed; False when there was none (or it could not be removed)."""
        try:
            os.remove(self.suggested_path(work_dir))
            return True
        except OSError:
            return False

    # ------------------------------------------------ map-queue.json
    def read_map_queue(self, work_dir):
        """`{"queue":[paths],"current":...,"done":[],"failed":{path: reason}}`, None when absent, SubprojectsFileError when unusable."""
        return self.read_json_object(self.map_queue_path(work_dir))

    def write_map_queue(self, work_dir, queue_state):
        self.write_json_object(self.map_queue_path(work_dir), queue_state)
