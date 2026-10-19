"""Repo catalog: objective facts about a repository, validation of the sheet the AI returns, and the cache per repo.

The catalog file is `<app data folder>/repo-catalog.json` (`~/.loop-harness`, or LOOP_DATA_DIR) and is keyed by the
normalized repo path; an entry is reused while the repo's HEAD sha is the one it was built from. Stdlib only, no app service.

  scan(repo)                    -> facts: name, remote, stack files, has_docs_codebase, head
  validate(entry, repo)         -> [{"id", "reason"}]: schema, purpose <= 200 characters, every evidence path exists in the repo
  build_entry(facts, sheet)     -> (entry, rejected): the sheet minus the rejected capabilities, plus the facts
  index()                       -> [{"name", "purpose", "capability_ids"}]: all the planning chat reads
"""
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import app_paths  # noqa: E402,F401  (puts the harness_backend package on sys.path)
from harness_backend.config import app_config  # noqa: E402

CATALOG_FILE_NAME = "repo-catalog.json"
CATALOG_VERSION = 1
PURPOSE_LIMIT = 200
DESCRIPTION_LIMIT = 300
CAPABILITY_LIMIT = 30
CAPABILITY_ID_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
DOCS_CODEBASE_PARTS = ("docs", "codebase")
GIT_TIMEOUT_SECONDS = 20
STACK_MARKER_FILES = ("package.json", "tsconfig.json", "pyproject.toml", "requirements.txt", "setup.py", "go.mod", "Cargo.toml",
                      "pom.xml", "build.gradle", "build.gradle.kts", "composer.json", "Gemfile", "Dockerfile", "docker-compose.yml",
                      "nest-cli.json", "vite.config.ts", "vite.config.js", "next.config.js", "angular.json")
STACK_MARKER_SUFFIXES = (".sln", ".csproj", ".fsproj")
STACK_SCAN_DEPTH = 2
STACK_SKIPPED_FOLDERS = (".git", "node_modules", "bin", "obj", "dist", "build", ".venv", "venv", "__pycache__")
STACK_MARKER_LIMIT = 30


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def normalize_path(path):
    return os.path.abspath(path).replace("\\", "/").rstrip("/") or "/"


def catalog_file_path():
    return os.path.join(app_config.get_user_data_dir(), CATALOG_FILE_NAME)


# ------------------------------------------------ file
def load_catalog():
    """{"version", "repos"}; an absent or unreadable file is an empty catalog."""
    try:
        with open(catalog_file_path(), encoding="utf-8") as handle:
            content = json.load(handle)
    except (OSError, ValueError):
        content = None
    if not isinstance(content, dict) or not isinstance(content.get("repos"), dict):
        return {"version": CATALOG_VERSION, "repos": {}}
    return {"version": CATALOG_VERSION, "repos": content["repos"]}


def save_catalog(catalog):
    """Atomic write (temp file + replace). The ONLY place the catalog file is written."""
    target = catalog_file_path()
    folder = os.path.dirname(target)
    os.makedirs(folder, exist_ok=True)
    descriptor, temp_path = tempfile.mkstemp(prefix=".repo-catalog-", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(catalog, handle, indent=2, ensure_ascii=False)
        os.replace(temp_path, target)
    except BaseException:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


def get_entry(repo):
    return load_catalog()["repos"].get(normalize_path(repo))


def store_entry(entry):
    catalog = load_catalog()
    catalog["repos"][normalize_path(entry["path"])] = entry
    save_catalog(catalog)


# ------------------------------------------------ facts
def run_git(repo, *arguments):
    """Output of `git -C repo ...` stripped, or '' on any failure."""
    try:
        completed = subprocess.run(["git", "-C", repo] + list(arguments), capture_output=True, text=True, timeout=GIT_TIMEOUT_SECONDS,
                                   encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def read_head(repo):
    return run_git(os.path.abspath(repo), "rev-parse", "HEAD")


def find_stack_markers(repo):
    found = []
    base_depth = repo.rstrip("\\/").count(os.sep)
    for current, subfolders, files in os.walk(repo):
        depth = current.rstrip("\\/").count(os.sep) - base_depth
        subfolders[:] = [name for name in subfolders if name not in STACK_SKIPPED_FOLDERS] if depth < STACK_SCAN_DEPTH else []
        for name in files:
            if name in STACK_MARKER_FILES or name.endswith(STACK_MARKER_SUFFIXES):
                found.append(os.path.relpath(os.path.join(current, name), repo).replace("\\", "/"))
        if len(found) >= STACK_MARKER_LIMIT:
            break
    return sorted(found)[:STACK_MARKER_LIMIT]


def scan(repo):
    """Objective facts: {"name", "path", "remote", "stack_files", "has_docs_codebase", "head"} (head and remote are '' when unknown)."""
    repo = os.path.abspath(repo)
    docs_folder = os.path.join(repo, *DOCS_CODEBASE_PARTS)
    has_docs = os.path.isdir(docs_folder) and any(not name.startswith(".") for name in os.listdir(docs_folder))
    return {"name": os.path.basename(repo.rstrip("\\/")), "path": normalize_path(repo), "remote": run_git(repo, "remote", "get-url", "origin"),
            "stack_files": find_stack_markers(repo), "has_docs_codebase": has_docs, "head": read_head(repo)}


def is_cached(repo, head=None):
    """True while the stored entry was built from the repo's current HEAD (a rescan is not needed)."""
    entry = get_entry(repo)
    head = read_head(repo) if head is None else head
    return bool(entry and head and entry.get("head") == head)


# ------------------------------------------------ validation
def is_evidence_inside_repo(repo, evidence):
    if not isinstance(evidence, str) or not evidence.strip() or os.path.isabs(evidence) or re.match(r"^[A-Za-z]:", evidence):
        return False
    root = os.path.realpath(repo)
    target = os.path.realpath(os.path.join(root, evidence))
    try:
        inside = os.path.commonpath([root, target]) == root
    except ValueError:
        return False
    return inside and os.path.isfile(target)


def validate_sheet_level(entry):
    """Errors that make the whole sheet unusable (id ''): not an object, bad purpose, capabilities not a list or too many."""
    if not isinstance(entry, dict):
        return [{"id": "", "reason": "the sheet is not a JSON object"}]
    errors = []
    purpose = entry.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        errors.append({"id": "", "reason": "purpose is missing"})
    elif len(purpose) > PURPOSE_LIMIT:
        errors.append({"id": "", "reason": "purpose has %d characters (limit %d)" % (len(purpose), PURPOSE_LIMIT)})
    capabilities = entry.get("capabilities")
    if not isinstance(capabilities, list):
        errors.append({"id": "", "reason": "capabilities must be a list"})
    elif len(capabilities) > CAPABILITY_LIMIT:
        errors.append({"id": "", "reason": "more than %d capabilities" % CAPABILITY_LIMIT})
    return errors


def validate_capability(capability, repo, seen_ids):
    """[] when the capability is fine; otherwise one {"id", "reason"} (id '' when it has no usable id)."""
    if not isinstance(capability, dict):
        return [{"id": "", "reason": "a capability is not an object"}]
    capability_id = capability.get("id")
    if not isinstance(capability_id, str) or not CAPABILITY_ID_PATTERN.match(capability_id):
        return [{"id": "", "reason": "capability id must be kebab-case: %s" % (capability_id,)}]
    if capability_id in seen_ids:
        return [{"id": capability_id, "reason": "duplicate capability id"}]
    seen_ids.add(capability_id)
    description = capability.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > DESCRIPTION_LIMIT:
        return [{"id": capability_id, "reason": "description is missing or longer than %d characters" % DESCRIPTION_LIMIT}]
    evidence = capability.get("evidence")
    if not is_evidence_inside_repo(repo, evidence):
        return [{"id": capability_id, "reason": "evidence file not found: %s" % (evidence if isinstance(evidence, str) else "(none)")}]
    return []


def validate(entry, repo):
    """[{"id", "reason"}]; empty = valid. A whole-sheet problem has id ''; a capability problem carries its id."""
    errors = validate_sheet_level(entry)
    if errors and not isinstance(entry.get("capabilities") if isinstance(entry, dict) else None, list):
        return errors
    seen_ids = set()
    for capability in entry["capabilities"] if isinstance(entry, dict) else []:
        errors += validate_capability(capability, repo, seen_ids)
    return errors


def build_entry(facts, sheet, repo):
    """(entry, rejected). Capabilities that fail validation are dropped and listed in `rejected` ({"id", "reason"}); a sheet that is
    unusable as a whole (bad purpose, not an object) gives (None, errors) and nothing should be stored."""
    sheet_errors = validate_sheet_level(sheet)
    if sheet_errors:
        return None, sheet_errors
    rejected, kept, seen_ids = [], [], set()
    for capability in sheet["capabilities"]:
        problems = validate_capability(capability, repo, seen_ids)
        if problems:
            rejected += problems
        else:
            kept.append({"id": capability["id"], "description": capability["description"].strip(),
                         "evidence": capability["evidence"].replace("\\", "/")})
    entry = {"name": facts["name"], "path": facts["path"], "head": facts["head"], "scanned_at": now_iso(),
             "weak": not facts["has_docs_codebase"], "purpose": sheet["purpose"].strip(), "capabilities": kept}
    return entry, rejected


def build_weak_entry(facts):
    """Entry of a repo with no docs/codebase: marked weak, no purpose, no capabilities (the AI is not asked)."""
    return {"name": facts["name"], "path": facts["path"], "head": facts["head"], "scanned_at": now_iso(), "weak": True,
            "purpose": "", "capabilities": []}


# ------------------------------------------------ index
def index():
    """[{"name", "purpose", "capability_ids"}] of every cataloged repo: no descriptions, no evidence."""
    return [{"name": entry.get("name", ""), "purpose": entry.get("purpose", ""),
             "capability_ids": [item.get("id") for item in entry.get("capabilities", []) if isinstance(item, dict)]}
            for entry in load_catalog()["repos"].values() if isinstance(entry, dict)]
