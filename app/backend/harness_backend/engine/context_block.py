#!/usr/bin/env python3
"""The generated `maestro:context` block of a project's `CLAUDE.md` (stdlib only).

One block between `<!-- maestro:context -->` and `<!-- /maestro:context -->` holds only stable, non-restrictive facts about the
project: its Conformance (one sentence, never a line that starts with `Conformance:`), where the map is, where the system map is
(a monorepo subproject), how it is built, run and tested and the giant-file rule. Nothing about the loop, git or approvals.

  render(folder, facts) -> str                 the block text with "\\n" line ends and no trailing newline
  write(folder, facts, title=None) -> {"written": bool, "error": str}

`write` keeps every byte outside the markers (line endings included), writes the block with the file's own line ending, appends a
missing block at the end, creates a missing `CLAUDE.md` with a `# <name>` title, refuses to touch a file whose markers are damaged
(two blocks, or a start without an end) and does not touch the file when the rendered text is already there (no mtime change).

`facts` (all optional):
  conformance   "STANDARD" | "LEGACY" | None        the Conformance block of the same file ("not set yet" when None)
  map_documents [names in <folder>/docs/codebase]   the map documents present (README.md excluded by the caller)
  system_pointer relative path to the root SYSTEM.md ("" = none; only a monorepo subproject whose root has it)
  commands      {"build","run","test": text}        what `readiness_checks.known_commands` found ("" = unknown)
  giant_lines   int                                 giant-file threshold in effect (default 1500)
"""
import os

START_MARKER = "<!-- maestro:context -->"
END_MARKER = "<!-- /maestro:context -->"
DAMAGED_ERROR = "context block damaged"
DEFAULT_GIANT_LINES = 1500
MAP_RELATIVE_FOLDER = "docs/codebase"
TESTING_DOCUMENT = "TESTING.md"
TEXT_ENCODING = "utf-8"
# The block of run_hints.write_codebase_block: a CLAUDE.md with this context block never keeps it (one block about the map per file).
CODEBASE_START_MARKER = "<!-- CODEBASE:BEGIN -->"
CODEBASE_END_MARKER = "<!-- CODEBASE:END -->"
MAP_GUIDE_SENTENCE = ("They are a guide, not the source of truth: if a document disagrees with the code, the code wins. "
                      "If a change really alters one of them, update the document in the same change.")
DOCUMENT_PURPOSE = {
    "STACK.md": "languages, frameworks, versions and main dependencies",
    "ARCHITECTURE.md": "layers, patterns and how data flows",
    "CONVENTIONS.md": "naming, code style and error handling in use",
    "STRUCTURE.md": "folder layout and where each kind of code lives",
    "TESTING.md": "how tests are written and run",
    "INTEGRATIONS.md": "external APIs, databases, queues and authentication",
    "CONCERNS.md": "tech debt, risks and fragile areas",
}


def render_conformance_sentence(conformance):
    if conformance in ("STANDARD", "LEGACY"):
        return "This project's Conformance is %s; the rules are in the Conformance block of this file." % conformance
    return "This project's Conformance is not set yet; when it is set, the rules are in the Conformance block of this file."


def render_map_lines(map_documents):
    if not map_documents:
        return ["The codebase map is not made yet (it will be in `%s/`)." % MAP_RELATIVE_FOLDER]
    lines = ["The codebase map is in `%s/`. Read one document only when the task needs it, not at the start of every session:" %
             MAP_RELATIVE_FOLDER]
    for name in map_documents:
        lines.append("- `%s/%s`: %s" % (MAP_RELATIVE_FOLDER, name, DOCUMENT_PURPOSE.get(name, "mapping document")))
    lines += ["", MAP_GUIDE_SENTENCE]
    return lines


def remove_codebase_blocks(text):
    """`text` without the `CODEBASE` block(s) of run_hints (only between their markers, plus the line ending after the end marker and
    one surplus blank line); everything else is byte-identical. A start without an end is left alone. Returns (new text, removed)."""
    removed = False
    while True:
        start = text.find(CODEBASE_START_MARKER)
        end = text.find(CODEBASE_END_MARKER, start) if start >= 0 else -1
        if start < 0 or end < 0:
            return text, removed
        end += len(CODEBASE_END_MARKER)
        for line_ending in ("\r\n", "\n"):
            if text.startswith(line_ending, end):
                end += len(line_ending)
                break
        before, after = text[:start], text[end:]
        for line_ending in ("\r\n", "\n"):  # the block was set apart by a blank line: do not leave two
            if before.endswith(line_ending * 2) and after.startswith(line_ending):
                after = after[len(line_ending):]
                break
        if not after and before.endswith(("\r\n\r\n", "\n\n")):  # the block was the last thing in the file
            before = before[:-2] if before.endswith("\r\n\r\n") else before[:-1]
        text, removed = before + after, True


def render_command_lines(commands, map_documents):
    commands = commands if isinstance(commands, dict) else {}
    known = [(label, str(commands.get(slot) or "").strip()) for label, slot in (("Build", "build"), ("Run", "run"), ("Test", "test"))]
    known = [(label, text) for label, text in known if text]
    if not known:
        if TESTING_DOCUMENT in (map_documents or []):
            return ["Build/test command not known: see `%s/%s`." % (MAP_RELATIVE_FOLDER, TESTING_DOCUMENT)]
        return ["Build/test command not known: see `%s/%s` when it exists." % (MAP_RELATIVE_FOLDER, TESTING_DOCUMENT)]
    return ["%s: `%s`" % (label, text) for label, text in known]


def render(folder, facts):
    """The block (markers included). `folder` is only the project the block describes (kept for callers and future facts)."""
    facts = facts if isinstance(facts, dict) else {}
    map_documents = list(facts.get("map_documents") or [])
    try:
        giant_lines = int(facts.get("giant_lines") or DEFAULT_GIANT_LINES)
    except (TypeError, ValueError):
        giant_lines = DEFAULT_GIANT_LINES
    lines = [START_MARKER, "## Project context (generated by the app; rewritten when the map, the Conformance or the project list changes)", "",
             render_conformance_sentence(facts.get("conformance")), ""]
    lines += render_map_lines(map_documents)
    system_pointer = str(facts.get("system_pointer") or "").strip()
    if system_pointer:
        lines += ["", "How this project talks to the others (routes, contracts, flows) is in `%s`; read it only when the task crosses projects."
                  % system_pointer]
    lines += [""] + render_command_lines(facts.get("commands"), map_documents)
    lines += ["", "Files above %d lines: read them by signatures (Grep, then small ranges), never whole; never make a file cross %d lines: "
              "split it instead." % (giant_lines, giant_lines), END_MARKER]
    return "\n".join(lines)


def find_block(text):
    """(start index, end index after the end marker) of the one block, (None, None) when absent, or "damaged"."""
    if text.count(START_MARKER) != text.count(END_MARKER) or text.count(START_MARKER) > 1:
        return "damaged"
    start = text.find(START_MARKER)
    if start < 0:
        return None, None
    end = text.find(END_MARKER)
    if end < start:
        return "damaged"
    return start, end + len(END_MARKER)


def write(folder, facts, title=None):
    """Writes the block into `<folder>/CLAUDE.md`. Never raises: `{"written": False, "error": text}` on a problem."""
    path = os.path.join(folder, "CLAUDE.md")
    try:
        block = render(folder, facts)
        if os.path.exists(path):
            with open(path, "rb") as handle:
                text = handle.read().decode(TEXT_ENCODING)
            created = False
            original = text
            text, _ = remove_codebase_blocks(text)
        else:
            if not os.path.isdir(folder):
                return {"written": False, "error": "folder does not exist: %s" % folder}
            text, created = "", True
            original = text
        found = find_block(text)
        if found == "damaged":
            return {"written": False, "error": DAMAGED_ERROR}
        line_ending = "\r\n" if "\r\n" in text else "\n"
        block = block.replace("\n", line_ending)
        start, end = found
        if created:
            title_line = "# %s" % (title or os.path.basename(os.path.abspath(folder)))
            updated = title_line + line_ending + line_ending + block + line_ending
        elif start is None:
            if not text:
                separator = ""
            elif text.endswith("\n"):
                separator = line_ending  # one blank line between the team's text and the block
            else:
                separator = line_ending * 2
            updated = text + separator + block + line_ending
        else:
            updated = text[:start] + block + text[end:]
        if updated == original:
            return {"written": False, "error": ""}
        with open(path, "wb") as handle:
            handle.write(updated.encode(TEXT_ENCODING))
        return {"written": True, "error": ""}
    except (OSError, UnicodeError) as failure:
        return {"written": False, "error": "%s: %s" % (type(failure).__name__, str(failure)[:200])}
