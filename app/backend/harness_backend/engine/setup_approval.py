"""Approval of the one-time `setup` commands stored in `<primary repo>/.specs/project/REPOS.json`.

Why: the engine runs that command with a shell when it creates a worktree, and REPOS.json is a plain file an agent can write. So a stored
command only runs if the USER saved that exact text through the app (Repositories: add repo / set setup). The app then records the
SHA-256 of the text in `<app data folder>/setup-approvals.json` (`~/.loop-harness`, or LOOP_DATA_DIR: outside every repository, so agents
cannot reach it) keyed by the primary repo and the repo folder; the engine compares before it runs anything. A text that differs (edited in
the file) or was never approved is refused. The approval is for the text: nothing else about REPOS.json is trusted by this module.
Stdlib only, no app service.
"""
import hashlib
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402,F401  (puts the harness_backend package on sys.path)
from harness_backend.config import app_config  # noqa: E402

APPROVALS_FILE_NAME = "setup-approvals.json"
APPROVALS_VERSION = 1
NOT_APPROVED_MESSAGE = ("setup command of repo %s was not saved through the app (or it was changed in REPOS.json afterwards), so it was NOT run: %s. "
                        "Open Repositories, re-enter the setup for this repo and save it, then run again.")


def approvals_file_path():
    return os.path.join(app_config.get_user_data_dir(), APPROVALS_FILE_NAME)


def normalize_key(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def hash_setup(setup):
    return hashlib.sha256(str(setup).strip().encode("utf-8")).hexdigest()


def load_approvals():
    """{primary key: {repo key: sha256}}; an absent or unreadable file approves nothing."""
    try:
        with open(approvals_file_path(), encoding="utf-8") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        return {}
    projects = content.get("projects") if isinstance(content, dict) else None
    return projects if isinstance(projects, dict) else {}


def save_approvals(projects):
    """Atomic write (temp file + replace). The ONLY place the approvals file is written."""
    target = approvals_file_path()
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=".setup-approvals-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump({"version": APPROVALS_VERSION, "projects": projects}, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def approve_setup(primary_repo, repo_path, setup):
    """Records that the user saved `setup` for the repo at `repo_path` of this project. An empty text approves nothing."""
    if not str(setup or "").strip():
        return
    projects = load_approvals()
    entry = projects.get(normalize_key(primary_repo))
    entry = entry if isinstance(entry, dict) else {}
    entry[normalize_key(repo_path)] = hash_setup(setup)
    projects[normalize_key(primary_repo)] = entry
    save_approvals(projects)


def is_setup_approved(primary_repo, repo_path, setup):
    entry = load_approvals().get(normalize_key(primary_repo))
    return isinstance(entry, dict) and entry.get(normalize_key(repo_path)) == hash_setup(setup)


def describe_unapproved_setup(primary_repo, repo):
    """"" when `repo` has no stored setup or the stored text is the approved one; else the refusal text (for the step line and the exit)."""
    if not repo.setup or is_setup_approved(primary_repo, repo.path, repo.setup):
        return ""
    return NOT_APPROVED_MESSAGE % (repo.name, repo.setup[:120])
