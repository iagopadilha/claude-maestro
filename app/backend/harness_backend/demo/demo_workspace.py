"""Folders of one demo run, atomic file writes and the demo's own git repo.

Layout (under a new temporary folder, or `<data-dir>/demo/`):
  acme-checkout/          the fake project (a real git repo with `.specs/`)
  claude-projects/        Claude Code style transcripts the demo app reads (`claude_transcripts_dir`)
  app-data/               the app data folder (`LOOP_DATA_DIR`) when no --data-dir was given
"""
import json
import os
import shutil
import stat
import subprocess
import tempfile
import time

from .demo_story_catalog import PROJECT_NAME

TEMP_PREFIX = "loop-harness-demo-"
DEMO_FOLDER_NAME = "demo"
TRANSCRIPTS_FOLDER_NAME = "claude-projects"
APP_DATA_FOLDER_NAME = "app-data"
REPLACE_ATTEMPTS = 20
REPLACE_RETRY_SECONDS = 0.05
GIT_TIMEOUT_SECONDS = 30
DEMO_AUTHOR_NAME = "Loop Harness demo"
DEMO_AUTHOR_EMAIL = "demo@example.invalid"


class DemoWorkspace:
    def __init__(self, base_dir, app_data_dir, is_temporary):
        self.base_dir = base_dir
        self.app_data_dir = app_data_dir
        self.is_temporary = is_temporary
        self.repo_dir = os.path.join(base_dir, PROJECT_NAME)
        self.transcripts_dir = os.path.join(base_dir, TRANSCRIPTS_FOLDER_NAME)

    @classmethod
    def create(cls, data_dir=""):
        """`data_dir` given: the demo lives in `<data_dir>/demo/` (recreated) and the app uses `data_dir`; otherwise everything
        goes into a new temporary folder."""
        if data_dir:
            data_dir = os.path.realpath(os.path.abspath(data_dir))
            base_dir = os.path.join(data_dir, DEMO_FOLDER_NAME)
            remove_tree(base_dir)
            os.makedirs(base_dir)
            return cls(base_dir, data_dir, is_temporary=False)
        base_dir = os.path.realpath(tempfile.mkdtemp(prefix=TEMP_PREFIX))
        return cls(base_dir, os.path.join(base_dir, APP_DATA_FOLDER_NAME), is_temporary=True)

    def remove(self):
        """Deletes the demo folder (the whole temporary folder, or `<data-dir>/demo/`)."""
        remove_tree(self.base_dir)


def remove_tree(path):
    """rmtree that also removes read-only files (git objects on Windows); a missing folder is fine."""
    def make_writable_and_retry(function, target, _info):
        try:
            os.chmod(target, stat.S_IWRITE)
            function(target)
        except OSError:
            pass
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=make_writable_and_retry)


def write_text_atomically(path, text):
    """Writes through a temporary file + os.replace, retrying while a reader holds the file (Windows)."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = "%s.%d.tmp" % (path, os.getpid())
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    for attempt in range(REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_RETRY_SECONDS)


def write_json_atomically(path, data):
    write_text_atomically(path, json.dumps(data, ensure_ascii=False, indent=2))


def append_json_line(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(data, ensure_ascii=False) + "\n")


def remove_file(path):
    try:
        os.remove(path)
    except OSError:
        pass


class DemoGitRepository:
    """git on the demo repo only (never the user's repos). Identity and signing are set in the demo repo's local config."""

    def __init__(self, repo_dir):
        self.repo_dir = repo_dir

    def run(self, *args):
        env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
        result = subprocess.run(["git", "-C", self.repo_dir] + list(args), capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
                                env=env)
        if result.returncode != 0:
            raise RuntimeError("git %s failed: %s" % (" ".join(args), (result.stderr or result.stdout).strip()[:300]))
        return result.stdout.strip()

    def initialize(self, branch):
        os.makedirs(self.repo_dir, exist_ok=True)
        self.run("init", "-q")
        self.run("symbolic-ref", "HEAD", "refs/heads/" + branch)
        for key, value in (("user.name", DEMO_AUTHOR_NAME), ("user.email", DEMO_AUTHOR_EMAIL), ("commit.gpgsign", "false"),
                           ("core.autocrlf", "false")):
            self.run("config", key, value)

    def commit_paths(self, paths, message):
        """Adds the paths by name and commits; returns the short hash."""
        self.run("add", "--", *paths)
        self.run("commit", "-q", "-m", message)
        return self.get_head(short=True)

    def create_branch(self, branch):
        self.run("checkout", "-q", "-b", branch)

    def get_head(self, short=False):
        return self.run("rev-parse", "--short=7", "HEAD") if short else self.run("rev-parse", "HEAD")

    def reset_to(self, head):
        """Back to `head`, dropping the story's commits and files (ignored `.specs/` stays)."""
        self.run("reset", "-q", "--hard", head)
        self.run("clean", "-q", "-f", "-d")
