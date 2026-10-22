"""AI summary of a quarantined feature, kept in `<work>/.specs/project/loop/internal/quarantine/<slug>.summary.json`.

One small JSON per feature (atomic write): `{"text", "at", "failures", "turn", "cost_usd"}`. `failures` and `turn` say which state of the
quarantine the text was written for, so a later quarantine of the same feature does not show an old summary.
"""
import os
import re
from typing import Protocol

from .loop_command_file_repository import get_loop_internal_dir, read_json_object, write_json_atomic

QUARANTINE_FOLDER_NAME = "quarantine"
SUMMARY_FILE_SUFFIX = ".summary.json"
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,80}$")


class IQuarantineSummaryRepository(Protocol):
    def read_summary(self, work_dir, slug) -> dict: ...
    def write_summary(self, work_dir, slug, summary) -> None: ...


class QuarantineSummaryRepository:
    def get_summary_path(self, work_dir, slug):
        """None for a slug outside the feature-slug format (never builds a path from free text)."""
        if not isinstance(slug, str) or not SLUG_PATTERN.fullmatch(slug) or ".." in slug:
            return None
        return os.path.join(get_loop_internal_dir(work_dir), QUARANTINE_FOLDER_NAME, slug + SUMMARY_FILE_SUFFIX)

    def read_summary(self, work_dir, slug):
        path = self.get_summary_path(work_dir, slug)
        return read_json_object(path) if path else {}

    def write_summary(self, work_dir, slug, summary):
        path = self.get_summary_path(work_dir, slug)
        if path:
            write_json_atomic(path, summary)
