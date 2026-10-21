"""Folder choice to open/create a project: native dialog (tkinter in a python SUBPROCESS with fixed code, no shell, with a
timeout) and, without tkinter, the front end's own folder browser (only subfolder names, flagging `.specs`/`.git`).
"""
import os
import string
import sys
from typing import Protocol

from ..domain.project_errors import InvalidPathError

FOLDER_DIALOG_TIMEOUT_SECONDS = 300
NO_TKINTER_EXIT_CODE = 3
PATH_OUTPUT_PREFIX = "PATH:"
NO_TKINTER_OUTPUT_PREFIX = "NO_TKINTER:"
LISTED_FOLDERS_LIMIT = 500
CANCELLED_REASON = "cancelled"
INITIAL_FOLDER_VARIABLE = "HARNESS_DIALOG_INITIAL_DIR"
FOLDER_DIALOG_CODE = (
    "import sys\n"
    "try:\n"
    "    import tkinter\n"
    "    from tkinter import filedialog\n"
    "    root = tkinter.Tk()\n"
    "except Exception as failure:\n"
    "    print('" + NO_TKINTER_OUTPUT_PREFIX + "' + type(failure).__name__ + ': ' + str(failure)[:200])\n"
    "    sys.exit(" + str(NO_TKINTER_EXIT_CODE) + ")\n"
    "root.withdraw()\n"
    "root.attributes('-topmost', True)\n"
    "import os\n"
    "dialog_options = {'parent': root, 'title': 'Choose the project folder', 'mustexist': True}\n"
    "initial_folder = os.environ.get('" + INITIAL_FOLDER_VARIABLE + "', '')\n"
    "if initial_folder and os.path.isdir(initial_folder):\n"
    "    dialog_options['initialdir'] = initial_folder\n"
    "chosen = filedialog.askdirectory(**dialog_options)\n"
    "root.destroy()\n"
    "print('" + PATH_OUTPUT_PREFIX + "' + (chosen or ''))\n"
)


def list_system_roots():
    """Drives (Windows) or `/` (everything else), plus the user's home folder."""
    if os.name == "nt":
        roots = ["%s:\\" % letter for letter in string.ascii_uppercase if os.path.isdir("%s:\\" % letter)]
    else:
        roots = ["/"]
    return roots + [os.path.expanduser("~")]


class IFolderPickerService(Protocol):
    def pick_folder_with_dialog(self, initial=None) -> dict: ...
    def list_subfolders(self, path) -> dict: ...


class FolderPickerService:
    def __init__(self, process_runner, log, dialog_argv=None, timeout_seconds=FOLDER_DIALOG_TIMEOUT_SECONDS):
        self.process_runner = process_runner
        self.log = log
        self.dialog_argv = list(dialog_argv) if dialog_argv else [sys.executable, "-c", FOLDER_DIALOG_CODE]
        self.timeout_seconds = timeout_seconds

    def pick_folder_with_dialog(self, initial=None):
        """{"path": "...|null", "reason": "..."}: null with a reason when there is no tkinter, it failed or the user cancelled.
        `initial`: an existing folder the dialog opens in (anything else is ignored)."""
        extra_env = {"PYTHONIOENCODING": "utf-8"}
        if isinstance(initial, str) and initial.strip() and os.path.isdir(os.path.expanduser(initial.strip())):
            extra_env[INITIAL_FOLDER_VARIABLE] = os.path.abspath(os.path.expanduser(initial.strip()))
        code, output, error = self.process_runner.run_and_wait(self.dialog_argv, None, self.timeout_seconds, extra_env=extra_env)
        lines = [line.strip() for line in (output or "").splitlines() if line.strip()]
        for line in reversed(lines):
            if line.startswith(PATH_OUTPUT_PREFIX):
                path = line[len(PATH_OUTPUT_PREFIX):].strip()
                return {"path": os.path.normpath(path), "reason": None} if path else {"path": None, "reason": CANCELLED_REASON}
            if line.startswith(NO_TKINTER_OUTPUT_PREFIX):
                self.log.warn("folder_dialog_without_tkinter", detail=line[len(NO_TKINTER_OUTPUT_PREFIX):][:200])
                return {"path": None, "reason": "native dialog unavailable (no tkinter): use the folder browser"}
        self.log.warn("folder_dialog_failed", code=code, error=(error or "")[-200:])
        return {"path": None, "reason": "native dialog failed (code %s): use the folder browser" % code}

    def list_subfolders(self, path):
        """No path: the roots. With a path: only the subfolder names (no files, no hidden ones), flagging projects."""
        if not path:
            return {"path": None, "parent": None, "folders": [self.describe_folder(root, root) for root in list_system_roots()],
                    "truncated": False}
        folder = os.path.abspath(os.path.expanduser(str(path)))
        if not os.path.isdir(folder):
            raise InvalidPathError("not an existing folder: %s" % folder)
        try:
            names = sorted(os.listdir(folder), key=str.lower)
        except OSError as failure:
            raise InvalidPathError("no access to %s: %s" % (folder, type(failure).__name__))
        visible = [name for name in names if not name.startswith((".", "$")) and os.path.isdir(os.path.join(folder, name))]
        parent = os.path.dirname(folder)
        return {"path": folder, "parent": parent if parent != folder else None,
                "folders": [self.describe_folder(name, os.path.join(folder, name)) for name in visible[:LISTED_FOLDERS_LIMIT]],
                "truncated": len(visible) > LISTED_FOLDERS_LIMIT}

    @staticmethod
    def describe_folder(name, path):
        return {"name": name, "path": path, "has_specs": os.path.isdir(os.path.join(path, ".specs")),
                "has_git": os.path.exists(os.path.join(path, ".git"))}
