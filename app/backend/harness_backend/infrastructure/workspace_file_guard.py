"""Post-job file guard: a snapshot of a folder before a `claude -p` job and, after it, the undo of every change outside the
allowed files (created = deleted, changed or deleted = restored from the snapshot's in-memory copy).

It is the second barrier of the planning chat: the first is the permission rules of the CLI (`Edit(/.specs/...)` in
`--allowedTools` with `--permission-mode dontAsk`), which the user's own settings can widen. Files larger than the
per-file copy limit (or beyond the total limit) are only compared, never copied: a change to one of them is reported as
`unrestorable`. Symbolic links and junctions are skipped (never followed nor copied). Never raises.
"""
import os
from dataclasses import dataclass, field
from typing import Protocol

BACKUP_FILE_LIMIT_BYTES = 1024 * 1024
BACKUP_TOTAL_LIMIT_BYTES = 64 * 1024 * 1024


def to_relative_key(root, path):
    return os.path.relpath(path, root).replace("\\", "/")


def normalize_key(relative):
    return os.path.normcase(relative.replace("\\", "/")).replace("\\", "/")


def is_link_or_junction(path):
    return os.path.islink(path) or getattr(os.path, "isjunction", lambda _: False)(path)


@dataclass
class WorkspaceSnapshot:
    root: str
    ignored: tuple
    files: dict = field(default_factory=dict)  # relative path -> (size, mtime_ns)
    folders: set = field(default_factory=set)
    copies: dict = field(default_factory=dict)  # relative path -> bytes


class IWorkspaceFileGuard(Protocol):
    def take_snapshot(self, root, ignored_paths=()) -> WorkspaceSnapshot: ...
    def revert_unexpected_changes(self, snapshot, allowed_paths) -> dict: ...


class WorkspaceFileGuard:
    def __init__(self, backup_file_limit_bytes=BACKUP_FILE_LIMIT_BYTES, backup_total_limit_bytes=BACKUP_TOTAL_LIMIT_BYTES):
        self.backup_file_limit_bytes = backup_file_limit_bytes
        self.backup_total_limit_bytes = backup_total_limit_bytes

    # ------------------------------------------------ snapshot
    def take_snapshot(self, root, ignored_paths=()):
        """`ignored_paths`: relative paths (`/` separator) of folders or files that are neither compared nor restored."""
        snapshot = WorkspaceSnapshot(os.path.abspath(root), tuple(normalize_key(item).strip("/") for item in ignored_paths))
        copied_bytes = 0
        for relative, path, size, mtime_ns in self.walk_files(snapshot):
            snapshot.files[relative] = (size, mtime_ns)
            if size <= self.backup_file_limit_bytes and copied_bytes + size <= self.backup_total_limit_bytes:
                content = self.read_bytes(path)
                if content is not None:
                    snapshot.copies[relative] = content
                    copied_bytes += len(content)
        snapshot.folders = set(self.walk_folders(snapshot))
        return snapshot

    def is_ignored(self, snapshot, relative):
        key = normalize_key(relative)
        return any(key == item or key.startswith(item + "/") for item in snapshot.ignored)

    def walk_folders(self, snapshot):
        for current, subfolders, _ in os.walk(snapshot.root, followlinks=False):
            kept = []
            for name in subfolders:
                path = os.path.join(current, name)
                relative = to_relative_key(snapshot.root, path)
                if is_link_or_junction(path) or self.is_ignored(snapshot, relative):
                    continue
                kept.append(name)
                yield relative
            subfolders[:] = kept

    def walk_files(self, snapshot):
        """(relative, path, size, mtime_ns) of every regular file outside the ignored paths."""
        for current, subfolders, files in os.walk(snapshot.root, followlinks=False):
            subfolders[:] = [name for name in subfolders
                             if not is_link_or_junction(os.path.join(current, name))
                             and not self.is_ignored(snapshot, to_relative_key(snapshot.root, os.path.join(current, name)))]
            for name in files:
                path = os.path.join(current, name)
                relative = to_relative_key(snapshot.root, path)
                if self.is_ignored(snapshot, relative) or is_link_or_junction(path):
                    continue
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                yield relative, path, info.st_size, info.st_mtime_ns

    # ------------------------------------------------ revert
    def revert_unexpected_changes(self, snapshot, allowed_paths):
        """Undoes every change outside `allowed_paths` (relative; a folder allows everything below it). {"reverted", "removed", "unrestorable"} (relative paths)."""
        allowed = {normalize_key(item).strip("/") for item in allowed_paths}
        is_allowed = lambda relative: any(normalize_key(relative) == item or normalize_key(relative).startswith(item + "/") for item in allowed)
        report = {"reverted": [], "removed": [], "unrestorable": []}
        current = {}
        for relative, path, size, mtime_ns in self.walk_files(snapshot):
            current[relative] = (size, mtime_ns)
            if is_allowed(relative):
                continue
            before = snapshot.files.get(relative)
            if before is None:
                self.remove_file(path, relative, report)
            elif before != (size, mtime_ns) and self.read_bytes(path) != snapshot.copies.get(relative, b"\0missing"):
                self.restore_file(snapshot, relative, report)
        for relative in snapshot.files:
            if relative not in current and not is_allowed(relative):
                self.restore_file(snapshot, relative, report)
        self.remove_new_empty_folders(snapshot)
        return report

    def restore_file(self, snapshot, relative, report):
        content = snapshot.copies.get(relative)
        if content is None:
            report["unrestorable"].append(relative)
            return
        path = os.path.join(snapshot.root, relative)
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "wb") as handle:
                handle.write(content)
            report["reverted"].append(relative)
        except OSError:
            report["unrestorable"].append(relative)

    @staticmethod
    def remove_file(path, relative, report):
        try:
            os.remove(path)
            report["removed"].append(relative)
        except OSError:
            report["unrestorable"].append(relative)

    def remove_new_empty_folders(self, snapshot):
        """Folders born during the job that ended up empty (deepest first). An allowed file keeps its folder."""
        for relative in sorted(set(self.walk_folders(snapshot)) - snapshot.folders, key=len, reverse=True):
            try:
                os.rmdir(os.path.join(snapshot.root, relative))
            except OSError:
                pass

    @staticmethod
    def read_bytes(path):
        try:
            with open(path, "rb") as handle:
                return handle.read()
        except OSError:
            return None
