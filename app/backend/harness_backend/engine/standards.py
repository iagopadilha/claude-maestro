#!/usr/bin/env python3
"""Standards slots of a project: the `## Standards` section of a `CLAUDE.md` (standard library only; standalone-runnable like the other
engine scripts and importable by the services).

A generic dev skill knows only the slot name; the company skill that fills it is bound here, in the project:

    ## Standards
    git: idata-git-padroes
    backend: idata:idata-backend-padroes
    frontend: idata-guia-estilo

One `slot: value` line per bound slot, nothing else until the next heading. The value is a skill name, bare or `plugin:skill`. An unknown
slot, an empty value or a value that is not a skill name is ignored and reported as a note. Writing keeps every other line of the file
(and its newline style); an empty mapping removes the section. In a monorepo (rule R-03) the section lives in each subproject's
`CLAUDE.md`, never in the root one.

Usage:
  python standards.py <repo> [--subproject <path>] [--set slot=skill ...] [--clear slot ...] [--json]
(`harness.py standards ...` is the same command.) Without --set/--clear it prints the bindings. It never invents a value.

Python API: SLOTS, SECTION_HEADING, parse(text), read(folder), render(bindings), write(folder, bindings), skill_name(value),
project_bindings(project_root), main(argv).
"""
import argparse
import json
import os
import re
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprojects  # noqa: E402

SLOTS = ("git", "backend", "frontend", "app-map", "observability")
SECTION_HEADING = "## Standards"
CLAUDE_FILE_NAME = "CLAUDE.md"
VALUE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+(:[A-Za-z0-9_.-]+)?$")
HEADING_PATTERN = re.compile(r"^#{1,6}\s")
SECTION_HEADING_PATTERN = re.compile(r"^##[ \t]+Standards[ \t]*$", re.IGNORECASE)
LINE_PATTERN = re.compile(r"^([^:]+):(.*)$")
FENCE_PATTERN = re.compile(r"^\s*(```|~~~)")
BOM = "﻿"
EXIT_OK, EXIT_INVALID_USAGE = 0, 2


def skill_name(value):
    """`plugin:skill` -> `skill`, stripped and lowercased (the same rule as the guard's `normalize_skill_name`)."""
    name = str(value or "").strip().lstrip("/")
    return name.rsplit(":", 1)[-1].strip().lstrip("/").lower()


def line_ending_of(text):
    """The newline style of a text: CRLF when its first line break is one, else LF."""
    index = text.find("\n")
    return "\r\n" if index > 0 and text[index - 1] == "\r" else "\n"


def is_blank(line):
    return not line.strip()


def locate_section(lines):
    """(start, end) of the Standards section in a list of lines (with their endings): `start` is the heading line, `end` the first line of
    the next heading (or len(lines)). None when there is no section. Headings inside a code fence do not count."""
    start, fenced = None, False
    for index, line in enumerate(lines):
        if FENCE_PATTERN.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        text = line.rstrip("\r\n")
        if start is None:
            if SECTION_HEADING_PATTERN.match(text.lstrip(BOM)):
                start = index
        elif HEADING_PATTERN.match(text):
            return start, index
    return (start, len(lines)) if start is not None else None


def parse(text):
    """{"bindings": {slot: value}, "notes": [str]} of the Standards section of `text` (no section = nothing, no notes)."""
    bindings, notes = {}, []
    lines = str(text or "").splitlines(keepends=True)
    span = locate_section(lines)
    if span is None:
        return {"bindings": bindings, "notes": notes}
    for line in lines[span[0] + 1:span[1]]:
        content = line.strip()
        if not content:
            continue
        found = LINE_PATTERN.match(content)
        if not found:
            notes.append("ignored line in the Standards section (expected `slot: skill`): %s" % content[:60])
            continue
        slot, value = found.group(1).strip().lower(), found.group(2).strip()
        if slot not in SLOTS:
            notes.append("ignored unknown slot `%s` (slots: %s)" % (slot, ", ".join(SLOTS)))
        elif not value:
            notes.append("ignored slot `%s`: empty value" % slot)
        elif not VALUE_PATTERN.match(value):
            notes.append("ignored slot `%s`: `%s` is not a skill name" % (slot, value[:60]))
        else:
            bindings[slot] = value
    return {"bindings": bindings, "notes": notes}


def read(folder):
    """parse(<folder>/CLAUDE.md); a missing or unreadable file gives no bindings."""
    try:
        with open(os.path.join(folder, CLAUDE_FILE_NAME), encoding="utf-8-sig") as handle:
            return parse(handle.read())
    except OSError:
        return {"bindings": {}, "notes": []}


def bound_items(bindings):
    """[(slot, value)] in SLOTS order for the known slots that have a non-empty value."""
    mapping = bindings if isinstance(bindings, dict) else {}
    return [(slot, str(mapping[slot]).strip()) for slot in SLOTS if str(mapping.get(slot) or "").strip()]


def render(bindings, ending="\n"):
    """The section text (heading and one line per bound slot, slots in SLOTS order); '' for an empty mapping."""
    items = bound_items(bindings)
    if not items:
        return ""
    return ending.join([SECTION_HEADING] + ["%s: %s" % item for item in items]) + ending


def validate(bindings):
    """Raises ValueError (naming the slot) for an unknown slot or a value that is not a skill name; empty values pass (they clear a slot)."""
    for slot, value in (bindings or {}).items():
        if slot not in SLOTS:
            raise ValueError("unknown slot: %s" % slot)
        text = str(value or "").strip()
        if text and not VALUE_PATTERN.match(text):
            raise ValueError("%s: `%s` is not a skill name (letters, digits, `_`, `.`, `-`, optionally `plugin:skill`)" % (slot, text[:60]))


def replace_section(text, bindings):
    """`text` with its Standards section replaced, appended or removed; everything else untouched."""
    ending = line_ending_of(text)
    section = render(bindings, ending)
    lines = text.splitlines(keepends=True)
    span = locate_section(lines)
    if span is None:
        if not section:
            return text
        if not text:
            return section
        head = text if text.endswith("\n") else text + ending
        return head + ending + section
    start, end = span
    block = lines[start:end]
    trailing = []
    while block and is_blank(block[-1]):
        trailing.insert(0, block.pop())
    if section:
        if not trailing and end < len(lines):
            trailing = [ending]  # keep the next heading apart from the section
        return "".join(lines[:start]) + section + "".join(trailing) + "".join(lines[end:])
    before = lines[:start]
    if end >= len(lines) and before and is_blank(before[-1]):
        before.pop()  # the blank line that separated a section appended at the end of the file
    return "".join(before) + "".join(lines[end:])


def write_text_atomically(path, text):
    temporary = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(temporary, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def write(folder, bindings):
    """Replaces, appends or removes the Standards section of `<folder>/CLAUDE.md` (atomic write, rest of the file and newline style kept).
    An empty mapping (or only empty values) removes the section. ValueError for an unknown slot or an invalid value. Returns read(folder)."""
    validate(bindings)
    path = os.path.join(folder, CLAUDE_FILE_NAME)
    try:
        with open(path, "rb") as handle:
            text = handle.read().decode("utf-8")
    except FileNotFoundError:
        text = None
    updated = replace_section(text or "", bindings)
    if text is None and not updated:
        return read(folder)  # nothing to remove and no file to create
    if updated != text:
        write_text_atomically(path, updated)
    return read(folder)


def project_bindings(project_root):
    """[{"folder": relative path ('.' for the root), "bindings": {...}}] for the root and each non-skipped SUBPROJECTS.json subproject.
    The root is left out when SUBPROJECTS.json lists subprojects (rule R-03: the section lives in each subproject)."""
    if not project_root:
        return []
    root = os.path.abspath(project_root)
    listed = subprojects.entry_projects(subprojects.load(root)["entry"])
    folders = [] if listed else ["."]
    for project in listed:
        relative = subprojects.normalize(project.get("path"))
        if relative and project.get("skip") is not True and relative not in folders:
            folders.append(relative)
    return [{"folder": relative, "bindings": read(os.path.join(root, *relative.split("/")) if relative != "." else root)["bindings"]}
            for relative in folders]


def root_lists_subprojects(project_root):
    return bool(subprojects.entry_projects(subprojects.load(os.path.abspath(project_root))["entry"]))


# ---- CLI ---------------------------------------------------------------------------------------------------------------------------

def parse_assignments(items):
    """['slot=skill', ...] -> {slot: value}; ValueError for a malformed item or an unknown slot."""
    assignments = {}
    for item in items:
        slot, separator, value = str(item).partition("=")
        slot = slot.strip().lower()
        if not separator or slot not in SLOTS:
            raise ValueError("--set expects slot=skill with slot in %s (got `%s`)" % (", ".join(SLOTS), item))
        assignments[slot] = value.strip()
    return assignments


def build_parser():
    parser = argparse.ArgumentParser(prog="harness.py standards", description="Shows or edits the Standards slots of a project's CLAUDE.md")
    parser.add_argument("repo", help="repository folder")
    parser.add_argument("--subproject", default="", help="subproject path inside the repo (monorepo: the section lives there)")
    parser.add_argument("--set", dest="assignments", action="append", nargs="+", default=[], metavar="slot=skill", help="bind a slot")
    parser.add_argument("--clear", dest="cleared", action="append", nargs="+", default=[], metavar="slot", help="unbind a slot")
    parser.add_argument("--json", action="store_true", help="JSON output")
    return parser


def emit(options, folder, result, projects=None):
    if options.json:
        payload = {"folder": folder, "bindings": result["bindings"], "notes": result["notes"], "slots": list(SLOTS)}
        if projects is not None:
            payload["projects"] = projects
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    print("%s" % folder)
    for slot in SLOTS:
        print("  %-13s %s" % (slot, result["bindings"].get(slot, "-")))
    for note in result["notes"]:
        print("  note: " + note)
    for item in projects or []:
        print("  %s: %s" % (item["folder"], ", ".join("%s=%s" % pair for pair in bound_items(item["bindings"])) or "(no slots bound)"))


def main(argv=None):
    options = build_parser().parse_args(sys.argv[1:] if argv is None else argv)
    root = os.path.abspath(options.repo)
    if not os.path.isdir(root):
        print("repository folder does not exist: %s" % options.repo, file=sys.stderr)
        return EXIT_INVALID_USAGE
    relative = subprojects.normalize(options.subproject)
    folder = os.path.join(root, *relative.split("/")) if relative else root
    if not os.path.isdir(folder):
        print("subproject folder does not exist: %s" % options.subproject, file=sys.stderr)
        return EXIT_INVALID_USAGE
    try:
        assignments = parse_assignments([item for group in options.assignments for item in group])
        cleared = [slot.strip().lower() for group in options.cleared for slot in group]
        unknown = [slot for slot in cleared if slot not in SLOTS]
        if unknown:
            raise ValueError("--clear: unknown slot %s (slots: %s)" % (", ".join(unknown), ", ".join(SLOTS)))
        editing = bool(assignments or cleared)
        if editing and not relative and root_lists_subprojects(root):
            raise ValueError("this repository lists subprojects: the section lives in each subproject's CLAUDE.md (use --subproject <path>)")
        current = read(folder)
        if editing:
            bindings = dict(current["bindings"])
            bindings.update(assignments)
            for slot in cleared:
                bindings.pop(slot, None)
            current = write(folder, bindings)
    except ValueError as error:
        print(str(error), file=sys.stderr)
        return EXIT_INVALID_USAGE
    emit(options, folder, current, project_bindings(root) if not relative and not editing else None)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
