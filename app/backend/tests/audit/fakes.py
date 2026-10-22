"""Fake `gh` shim and small helpers shared by the audit scenario runner (stdlib only, no network).

The shim (`gh.cmd`, `gh.bat` and a POSIX `gh` script, all calling `gh_shim.py`) appends one JSON line with its argv and cwd to the file named by
`AUDIT_GH_LOG` and exits 1: a real `gh` can never be reached from a scenario run, and every attempt is on record.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import time

GH_SHIM_PY = r'''#!/usr/bin/env python3
import json, os, sys
log = os.environ.get("AUDIT_GH_LOG", "")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
sys.stderr.write("fake gh (audit shim): refusing every call\n")
sys.exit(1)
'''
GH_CMD = '@echo off\r\n"{python}" "%~dp0gh_shim.py" %*\r\nexit /b %ERRORLEVEL%\r\n'
GH_POSIX = '#!/bin/sh\nexec "{python}" "$(dirname "$0")/gh_shim.py" "$@"\n'
IDENTITY = ("-c", "user.name=audit", "-c", "user.email=audit@localhost", "-c", "commit.gpgsign=false")


def write_text(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def build_gh_exe(folder):
    """Windows only: a real `gh.exe` (pip's script launcher + a zipped __main__ that runs gh_shim.py). The engine starts `gh` without a shell, and CreateProcess
    only looks for `gh.exe`, so `gh.cmd` alone would let the REAL gh.exe later on PATH run. Returns True when built."""
    if sys.platform != "win32":
        return False
    try:
        import io
        import zipfile
        import pip._vendor.distlib as distlib
        launcher = os.path.join(os.path.dirname(distlib.__file__), "t64.exe" if sys.maxsize > 2 ** 32 else "t32.exe")
        with open(launcher, "rb") as handle:
            data = handle.read()
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as bundle:
            shim = os.path.join(folder, "gh_shim.py")
            bundle.writestr("__main__.py", "import runpy, sys\nsys.argv[0] = %r\nrunpy.run_path(%r, run_name='__main__')\n" % (shim, shim))
        with open(os.path.join(folder, "gh.exe"), "wb") as handle:
            handle.write(data + ("#!%s\n" % sys.executable).encode("utf-8") + archive.getvalue())
        return True
    except Exception:
        return False


def path_without_real_gh(path, shim_dir):
    """PATH with the shim first and every folder that holds a real gh removed: no scenario can reach the real GitHub CLI."""
    kept = [item for item in path.split(os.pathsep) if item and not any(os.path.isfile(os.path.join(item, name)) for name in ("gh.exe", "gh.cmd", "gh.bat", "gh"))
            or os.path.normcase(os.path.abspath(item)) == os.path.normcase(os.path.abspath(shim_dir))]
    return os.pathsep.join([shim_dir] + [item for item in kept if os.path.normcase(os.path.abspath(item)) != os.path.normcase(os.path.abspath(shim_dir))])


def install_gh_shim(folder):
    """Creates the shim in `folder` and returns it; put it FIRST on PATH."""
    os.makedirs(folder, exist_ok=True)
    write_text(os.path.join(folder, "gh_shim.py"), GH_SHIM_PY)
    with open(os.path.join(folder, "gh.cmd"), "w", encoding="utf-8", newline="") as handle:
        handle.write(GH_CMD.format(python=sys.executable))
    with open(os.path.join(folder, "gh.bat"), "w", encoding="utf-8", newline="") as handle:
        handle.write(GH_CMD.format(python=sys.executable))
    posix = os.path.join(folder, "gh")
    write_text(posix, GH_POSIX.format(python=sys.executable.replace("\\", "/")))
    try:
        os.chmod(posix, 0o755)
    except OSError:
        pass
    build_gh_exe(folder)
    return folder


def read_gh_calls(log_path):
    if not os.path.isfile(log_path):
        return []
    with open(log_path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def git(folder, *args, check=True):
    """git on a TEMP repo only (callers pass scratch folders)."""
    result = subprocess.run(["git", "-C", folder, *IDENTITY, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise RuntimeError("git %s failed in %s: %s" % (" ".join(args), folder, result.stderr.strip()))
    return result.stdout.strip()


def delete_tree(path, attempts=4):
    """Removes a scratch folder (read-only files and Windows locks handled); returns '' or the reason it could not be deleted."""
    def clear_read_only(function, target, _info):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    for attempt in range(attempts):
        if not os.path.exists(path):
            return ""
        try:
            shutil.rmtree(path, onerror=clear_read_only)
        except OSError as error:
            last = "%s: %s" % (type(error).__name__, error)
            time.sleep(0.5 * (attempt + 1))
            continue
        if not os.path.exists(path):
            return ""
    return last if os.path.exists(path) else ""
