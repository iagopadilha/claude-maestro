"""Automatic worktree preparation of a repo, pure: what to install and which local files to copy. Nothing runs while detecting.

Used by the engine (`run_loop.py`) when REPOS.json has no setup / no copy list for a repo, and by the registry service to show it.
"""
import fnmatch
import glob
import os

HEAVY_FOLDER_NAMES = ("node_modules", "bin", "obj", "dist")
SUGGESTION_PATTERNS = (".env", ".env.*", "appsettings.*.json", "*.local.*", "*.local")
SECRET_PATTERNS = (".env", ".env.*", "appsettings.json", "appsettings.*.json")
EXAMPLE_FILE_SUFFIXES = (".example", ".sample", ".template")
SUGGESTION_LIMIT = 100
IGNORED_FILES_ARGUMENTS = ("ls-files", "--others", "--ignored", "--exclude-standard", "--directory")

SETUP_BY_LOCKFILE = (
    ("pnpm-lock.yaml", "pnpm install --frozen-lockfile"),
    ("yarn.lock", "yarn install --frozen-lockfile"),
    ("package-lock.json", "npm ci"),
    ("package.json", "npm install"),
)
SETUP_BY_REQUIREMENTS = (
    ("requirements.txt", "python -m pip install -r requirements.txt"),
    ("poetry.lock", "poetry install"),
    ("go.mod", "go mod download"),
)


def has_dotnet_project(folder):
    for pattern in ("*.sln", "*.csproj", os.path.join("*", "*.sln"), os.path.join("*", "*.csproj")):
        if glob.glob(os.path.join(glob.escape(folder), pattern)):
            return True
    return False


def detect_setup(folder):
    """The one-line setup command for the repo at `folder` (first match only), or None. Looks only at root files."""
    if not folder or not os.path.isdir(folder):
        return None
    for file_name, command in SETUP_BY_LOCKFILE:
        if os.path.isfile(os.path.join(folder, file_name)):
            return command
    if has_dotnet_project(folder):
        return "dotnet restore"
    for file_name, command in SETUP_BY_REQUIREMENTS:
        if os.path.isfile(os.path.join(folder, file_name)):
            return command
    return None


def matches_any(file_name, patterns):
    return any(fnmatch.fnmatch(file_name, pattern) for pattern in patterns)


def suggest_copy_files(folder, ignored_output):
    """Local-config files among the git-ignored paths (`git ls-files --others --ignored ...` output), as sorted
    [{"path", "size", "secret"}]. Heavy folders and example/sample/template files are never suggested."""
    suggestions = []
    for line in ignored_output.splitlines():
        relative = line.replace("\\", "/").strip().strip('"')
        parts = relative.split("/")
        if not relative or relative.endswith("/") or set(parts) & set(HEAVY_FOLDER_NAMES):
            continue
        file_name = parts[-1]
        if file_name.lower().endswith(EXAMPLE_FILE_SUFFIXES) or not matches_any(file_name, SUGGESTION_PATTERNS):
            continue
        absolute = os.path.join(folder, *parts)
        if not os.path.isfile(absolute):
            continue
        suggestions.append({"path": relative, "size": os.path.getsize(absolute), "secret": matches_any(file_name, SECRET_PATTERNS)})
        if len(suggestions) >= SUGGESTION_LIMIT:
            break
    return sorted(suggestions, key=lambda item: item["path"])
