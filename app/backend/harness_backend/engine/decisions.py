#!/usr/bin/env python3
"""The AI decisions log: one grammar owner for .specs/project/loop/VALIDATE.md (stdlib only).

Line grammar (new):
  - [<slug>|general] turn=<N> level=<important|critical> <doubt> | chose: <choice> | revert: <how>
Old lines without turn=/level= still parse (turn None, level important). A line that starts with "- [" but does not
match is kept as `unparsed` (never dropped). The file stays append-only: reviewed state lives elsewhere (app-owned).
"""
import hashlib
import os
import re

LEVELS = ("important", "critical")
DEFAULT_LEVEL = "important"
VALIDATE_FILE_NAME = "VALIDATE.md"
LOOP_FOLDER_NAME = "loop"

LINE_RE = re.compile(r"^\s*-\s*\[(?P<slug>[^\]]+)\]\s*(?P<body>.*)$")
TURN_RE = re.compile(r"^turn=(?P<turn>\d+|unknown)\s+")
LEVEL_RE = re.compile(r"^level=(?P<level>important|critical)\s+")
CHOSE_RE = re.compile(r"\s*\|\s*chose:\s*")
REVERT_RE = re.compile(r"\s*\|\s*revert:\s*")


def validate_path(root):
    """VALIDATE.md of a project or worktree root."""
    return os.path.join(root, ".specs", "project", LOOP_FOLDER_NAME, VALIDATE_FILE_NAME)


def line_id(raw):
    """Stable id of a decision: sha1 of the raw line (whitespace at the ends ignored)."""
    return hashlib.sha1(raw.strip().encode("utf-8")).hexdigest()


def parse_line(raw, line_no=0):
    """One decision dict, or None when the line is not a decision line at all (headings, blank lines, prose)."""
    match = LINE_RE.match(raw)
    if not match:
        return None
    body = match.group("body").strip()
    turn = None
    level = DEFAULT_LEVEL
    turn_match = TURN_RE.match(body)
    if turn_match:
        value = turn_match.group("turn")
        turn = int(value) if value.isdigit() else None
        body = body[turn_match.end():]
    level_match = LEVEL_RE.match(body)
    if level_match:
        level = level_match.group("level")
        body = body[level_match.end():]
    chose_parts = CHOSE_RE.split(body, maxsplit=1)
    if len(chose_parts) != 2:
        return {"unparsed": True, "raw": raw.rstrip("\n"), "line_no": line_no, "id": line_id(raw)}
    doubt, rest = chose_parts
    revert_parts = REVERT_RE.split(rest, maxsplit=1)
    chose = revert_parts[0]
    revert = revert_parts[1] if len(revert_parts) == 2 else ""
    return {
        "id": line_id(raw),
        "slug": match.group("slug").strip(),
        "turn": turn,
        "level": level,
        "doubt": doubt.strip(),
        "chose": chose.strip(),
        "revert": revert.strip(),
        "raw": raw.rstrip("\n"),
        "line_no": line_no,
    }


def parse(text):
    """Returns (items, unparsed). items keep file order; line_no is 1-based."""
    items, unparsed = [], []
    for number, raw in enumerate((text or "").splitlines(), 1):
        parsed = parse_line(raw, number)
        if parsed is None:
            continue
        if parsed.get("unparsed"):
            unparsed.append(parsed)
        else:
            items.append(parsed)
    return items, unparsed


def read_file(root):
    path = validate_path(root)
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def format_line(slug, turn, level, doubt, chose, revert):
    clean = lambda value: re.sub(r"\s+", " ", str(value or "")).replace("|", "/").strip()  # noqa: E731
    level = level if level in LEVELS else DEFAULT_LEVEL
    turn_text = str(int(turn)) if turn is not None else "unknown"
    return "- [%s] turn=%s level=%s %s | chose: %s | revert: %s" % (
        clean(slug) or "general", turn_text, level, clean(doubt), clean(chose), clean(revert))


def append(root, slug, turn, level, doubt, chose, revert):
    """Appends one line (creates the folder and file when missing). Returns the line written."""
    line = format_line(slug, turn, level, doubt, chose, revert)
    path = validate_path(root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    current = read_file(root)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(("" if not current or current.endswith("\n") else "\n") + line + "\n")
    return line


def line_count(root):
    """Number of lines in VALIDATE.md now: the mark saved in meta before a turn."""
    return len(read_file(root).splitlines())


def new_critical(root, since_line):
    """Critical decisions written after line `since_line` (1-based line numbers greater than the mark)."""
    items, _ = parse(read_file(root))
    return [item for item in items if item["level"] == "critical" and item["line_no"] > int(since_line or 0)]
