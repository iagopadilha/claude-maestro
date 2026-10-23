"""Path spelling helpers of the orchestrator guard (imported by guard.py; standard library only).

`guard.normalize_path` is the ONE canonical form every path rule compares (protected files, credentials, scope, copied secrets). The pieces that
undo a Windows spelling live here: `\\\\?\\` and `\\\\.\\` prefixes, `\\\\localhost\\C$\\` admin shares, NTFS alternate data streams (`file::$DATA`), trailing dots and
spaces (Windows drops them), and 8.3 short names (`CLAUDE~1.JSO`). All of them are text only except the short name expansion, which asks the
filesystem (the name only means something when the file exists).
"""
import os
import re

_PREFIX_RE = re.compile(r"^//[?.]/")
_LOCAL_SHARE_RE = re.compile(r"^//(?:localhost|127\.0\.0\.1|\[?::1\]?|%s)/([a-zA-Z])\$(?=/|$)" % re.escape((os.environ.get("COMPUTERNAME") or "\0").lower()), re.I)
_DRIVE_COMPONENT_RE = re.compile(r"^[a-zA-Z]:$")
SHORT_NAME_RE = re.compile(r"^[^/.~ ]{1,6}~\d{1,6}(?:\.[^/.]{0,3})?$")
_GIT_BASH_DRIVE_RE = re.compile(r"^/(?:mnt/|cygdrive/)?([a-zA-Z])(?=/|$)")


def strip_windows_prefixes(text):
    """`text` has forward slashes only. `//?/C:/x` and `//./C:/x` lose the device prefix, `//?/UNC/host/share` becomes `//host/share`, and the admin share of
    this machine (`//localhost/C$/x`, `//127.0.0.1/C$/x`, `//[::1]/C$/x`, `//<this computer>/C$/x`) becomes `C:/x`."""
    if _PREFIX_RE.match(text):
        text = text[4:]
        if text[:4].upper() == "UNC/":
            text = "//" + text[4:]
    return _LOCAL_SHARE_RE.sub(lambda found: found.group(1) + ":", text)


def git_bash_drive(text):
    """`/c/Users/x`, `/mnt/c/x`, `/cygdrive/c/x` -> `c:/Users/x` (Windows only: elsewhere `/c/...` is a real folder)."""
    return _GIT_BASH_DRIVE_RE.sub(r"\1:", text) if os.name == "nt" else text


def clean_components(text):
    """Windows only. Per path component: drop an alternate data stream (`name::$DATA`, `name:stream`), trailing dots and spaces (`name.`, `name `), and a
    component made only of dots and spaces (`...`) is the folder itself. The drive (`C:`) and `.` / `..` are left alone."""
    if os.name != "nt":
        return text
    parts = text.split("/")
    cleaned = []
    for position, part in enumerate(parts):
        if part in (".", "..") or (position == 0 and _DRIVE_COMPONENT_RE.match(part)):
            cleaned.append(part)
            continue
        if ":" in part:
            part = part.split(":", 1)[0]
        if part.rstrip(" ") in (".", ".."):
            part = part.rstrip(" ")
        else:
            part = part.rstrip(" .") if part.strip(" .") else ("" if part == "" else ".")
        cleaned.append(part)
    return "/".join(cleaned)


def has_short_name(text):
    """True when some component of `text` looks like an 8.3 short name (`CLAUDE~1.JSO`, `PROGRA~1`)."""
    return os.name == "nt" and any(SHORT_NAME_RE.match(part) for part in text.split("/"))


def expand_short_names(text):
    """Windows only: the long form of the 8.3 short names in an absolute `text` (forward slashes), taken from the filesystem for the longest prefix that
    exists. Components after that prefix, and a name the system cannot expand, stay as written (the caller treats those as unresolved)."""
    if not has_short_name(text) or not re.match(r"^[a-zA-Z]:/", text):
        return text
    parts = text.split("/")
    for length in range(len(parts), 1, -1):
        prefix = "/".join(parts[:length])
        if not os.path.exists(prefix.replace("/", "\\")):
            continue
        long_prefix = _long_path_name(prefix.replace("/", "\\"))
        if long_prefix:
            return "/".join([long_prefix.replace("\\", "/")] + parts[length:])
        break
    return text


def _long_path_name(path):
    """GetLongPathNameW through ctypes, else os.path.realpath (which returns the long name on Windows). '' when neither works."""
    try:
        import ctypes
        buffer = ctypes.create_unicode_buffer(4096)
        size = ctypes.windll.kernel32.GetLongPathNameW(path, buffer, len(buffer))
        if 0 < size < len(buffer):
            return buffer.value
    except Exception:
        pass
    try:
        return os.path.realpath(path)
    except Exception:
        return ""
