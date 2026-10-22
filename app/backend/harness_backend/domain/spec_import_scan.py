"""Scan of a spec feature folder for the planning import: which `.md` files are read (recursive, depth 3), the size and count
limits, what each file holds (ids, sections) and the warnings the Preview shows. Pure file reading and regex; nothing is
written and no AI is called. A folder that is not a feature folder raises `InvalidRequestError` (400); a folder over the limits
comes back with `refused` filled (the Preview shows it, the import raises it)."""
import hashlib
import os
import re

from .project_errors import InvalidRequestError

MAIN_SPEC_NAME = "spec.md"
MARKDOWN_EXTENSION = ".md"
MAX_FILE_BYTES = 400 * 1024
MAX_FILES = 10  # only the older top-level collect_from_folder
DEFAULT_LIMITS = {"depth": 3, "files": 30, "file_bytes": MAX_FILE_BYTES, "total_bytes": 2 * 1024 * 1024}
SKIPPED_FOLDER_NAMES = ("node_modules",)
STATUS_READ = "read"
STATUS_SKIPPED = "skipped"
CORE_SECTIONS = ("Goals", "Out of Scope", "Edge Cases", "Success Criteria", "Requirement Traceability")
OPEN_QUESTION_HEADINGS = ("open questions", "perguntas abertas")
EMPTY_ANSWER = re.compile(r"^(none|no|nenhuma?|n/?a|-)$", re.IGNORECASE)

HEADING_LINE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
REQUIREMENT_ID = re.compile(r"\b[A-Z]+-\d+\b")
REQUIREMENT_RANGE = re.compile(r"\b([A-Z]+)-(\d+)\.\.(\d+)\b")
DECISION_ID = re.compile(r"\bD-\d+\b")
TASK_ID_PREFIX = re.compile(r"^T-\d+$")
TASK_HEADING = re.compile(r"^#{2,6}\s+(T\d+)\b", re.MULTILINE)
LIST_ITEM = re.compile(r"^\s*(?:[-*]|\d+[.)])\s+\S")
TABLE_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$")
CONFORMANCE_LINE = re.compile(r"^\W*Conform(?:ance|idade)\W*:\W*(STANDARD|LEGACY|PADRAO|LEGADO)\b", re.MULTILINE | re.IGNORECASE)
CONFORMANCE_ALIASES = {"PADRAO": "STANDARD", "LEGADO": "LEGACY"}
COVERAGE_LINE = re.compile(r"^.*\bCoverage\b.*$", re.MULTILINE | re.IGNORECASE)
UNMAPPED_COUNT = re.compile(r"(\d+)\s+unmapped", re.IGNORECASE)
ZERO_MAPPED = re.compile(r"(?<![\w.])0\s+mapped", re.IGNORECASE)


# ------------------------------------------------ scan
def scan_folder(path, limits=DEFAULT_LIMITS):
    """{root, files:[{relative_path, size, sha256, status, reason, text}], counts:{files, bytes, skipped}, refused}.

    `text` is only on read files. `refused` is None or {reason, counts}: spec.md missing at the root, a read file empty or over
    the per-file limit, or totals over the file/size limits."""
    path = os.path.expanduser(str(path))
    if os.path.isfile(path):
        raise InvalidRequestError("pick the feature folder, not a file: the folder that has spec.md")
    if not os.path.isdir(path):
        raise InvalidRequestError("folder not found: %s" % path)
    files = []
    walk_markdown_folder(path, "", 0, limits, files)
    read_files = [entry for entry in files if entry["status"] == STATUS_READ]
    counts = {"files": len(read_files), "bytes": sum(entry["size"] for entry in read_files),
              "skipped": len(files) - len(read_files)}
    return {"root": path, "files": files, "counts": counts, "refused": find_refusal(files, read_files, counts, limits)}


def walk_markdown_folder(root, relative_folder, depth, limits, files):
    folder = os.path.join(root, relative_folder) if relative_folder else root
    try:
        names = sorted(os.listdir(folder))
    except OSError as failure:
        raise InvalidRequestError("could not read %s: %s" % (folder, failure))
    for name in names:
        relative = "%s/%s" % (relative_folder, name) if relative_folder else name
        full_path = os.path.join(folder, name)
        if os.path.islink(full_path):
            files.append(skipped_entry(relative, full_path, "symbolic link"))
        elif os.path.isdir(full_path):
            if name.startswith(".") or name in SKIPPED_FOLDER_NAMES:
                continue
            if depth >= limits["depth"]:
                files.append(skipped_entry(relative + "/", full_path, "deeper than %d levels" % limits["depth"]))
                continue
            walk_markdown_folder(root, relative, depth + 1, limits, files)
        elif not name.lower().endswith(MARKDOWN_EXTENSION):
            files.append(skipped_entry(relative, full_path, "not .md"))
        else:
            files.append(read_entry(relative, full_path, limits))


def skipped_entry(relative, full_path, reason):
    size = os.path.getsize(full_path) if os.path.isfile(full_path) and not os.path.islink(full_path) else 0
    return {"relative_path": relative, "size": size, "sha256": "", "status": STATUS_SKIPPED, "reason": reason}


def read_entry(relative, full_path, limits):
    try:
        with open(full_path, "rb") as handle:
            raw = handle.read(limits["file_bytes"] + 1)
    except OSError as failure:
        return skipped_entry(relative, full_path, "could not be read: %s" % failure)
    size = os.path.getsize(full_path)
    text = raw.decode("utf-8", errors="replace")
    reason = file_content_problem(relative, raw, text, limits["file_bytes"])
    if reason:
        return {"relative_path": relative, "size": size, "sha256": "", "status": STATUS_SKIPPED, "reason": reason,
                "refusal": reason}
    return {"relative_path": relative, "size": size, "sha256": hashlib.sha256(raw).hexdigest(), "status": STATUS_READ,
            "reason": "", "text": text}


def file_content_problem(label, raw, text, file_bytes=MAX_FILE_BYTES):
    """The refusal reason of a file (too large, empty) or ''."""
    if len(raw) > file_bytes:
        return "%s is larger than %d KB: split it or import a smaller file" % (label, file_bytes // 1024)
    if not text.strip():
        return "%s is empty" % label
    return ""


def find_refusal(files, read_files, counts, limits):
    """The first refusal as {reason, counts}, or None."""
    reason = ""
    if not any(entry["relative_path"] == MAIN_SPEC_NAME for entry in files):
        reason = "the folder has no spec.md: pick the folder of a spec feature (spec.md, design.md, tasks.md)"
    for entry in files:
        reason = reason or entry.get("refusal") or ""
    if not reason and len(read_files) > limits["files"]:
        reason = "the folder has %d .md files: the limit is %d" % (len(read_files), limits["files"])
    if not reason and counts["bytes"] > limits["total_bytes"]:
        reason = "the .md files add up to %d KB: the limit is %d KB" % (counts["bytes"] // 1024, limits["total_bytes"] // 1024)
    return {"reason": reason, "counts": counts} if reason else None


# ------------------------------------------------ single-file helpers (kept for the older callers)
def collect_from_folder(path):
    """([(file name, text)], default import name = the folder name): the top-level `.md` files of a feature folder."""
    path = os.path.expanduser(path)
    if os.path.isfile(path):
        raise InvalidRequestError("pick the feature folder, not a file: the folder that has spec.md")
    if not os.path.isdir(path):
        raise InvalidRequestError("folder not found: %s" % path)
    names = sorted(name for name in os.listdir(path)
                   if name.lower().endswith(MARKDOWN_EXTENSION) and os.path.isfile(os.path.join(path, name)))
    if MAIN_SPEC_NAME not in names:
        raise InvalidRequestError("the folder has no spec.md: pick the folder of a spec feature (spec.md, design.md, tasks.md)")
    if len(names) > MAX_FILES:
        raise InvalidRequestError("the folder has %d .md files: the limit is %d" % (len(names), MAX_FILES))
    return ([(name, read_markdown(os.path.join(path, name))) for name in names],
            os.path.basename(os.path.normpath(path)))


def read_markdown(path):
    label = os.path.basename(path)
    if not label.lower().endswith(MARKDOWN_EXTENSION):
        raise InvalidRequestError("%s is not a Markdown file (.md): only Markdown specs are imported" % label)
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError as failure:
        raise InvalidRequestError("could not read %s: %s" % (label, failure))
    text = raw.decode("utf-8", errors="replace")
    check_content(label, raw, text)
    return text


def check_content(label, raw, text):
    reason = file_content_problem(label, raw, text)
    if reason:
        raise InvalidRequestError(reason)


# ------------------------------------------------ detect
def detect_items(files):
    """What the read files hold, by regex and headings only. `files`: [{relative_path, text}]."""
    texts = [(entry["relative_path"], entry.get("text") or "") for entry in files]
    joined = "\n".join(text for _, text in texts)
    sections = {name: False for name in CORE_SECTIONS}
    counts = {"out_of_scope_rows": 0, "edge_cases": 0, "success_criteria": 0}
    for _, text in texts:
        parsed = split_sections(text)
        for name in CORE_SECTIONS:
            sections[name] = sections[name] or any(heading_names(title, name) for title, _ in parsed)
        for title, body in parsed:
            if heading_names(title, "Out of Scope"):
                counts["out_of_scope_rows"] += count_table_rows(body)
            elif heading_names(title, "Edge Cases"):
                counts["edge_cases"] += count_list_items(body)
            elif heading_names(title, "Success Criteria"):
                counts["success_criteria"] += count_list_items(body)
    tasks = []
    for relative_path, text in texts:
        if os.path.basename(relative_path).lower() == "tasks.md":
            tasks.extend(TASK_HEADING.findall(text))
    return {"requirements": find_requirement_ids(joined), "decisions": sort_ids(set(DECISION_ID.findall(joined))),
            "tasks": sort_ids(set(tasks)), "sections": sections, **counts}


def find_requirement_ids(text):
    found = set()
    for prefix, first, last in REQUIREMENT_RANGE.findall(text):
        if prefix not in ("D", "T") and int(first) <= int(last) and int(last) - int(first) < 1000:
            width = len(first)
            found.update("%s-%s" % (prefix, str(number).zfill(width)) for number in range(int(first), int(last) + 1))
    for identifier in REQUIREMENT_ID.findall(text):
        if not DECISION_ID.fullmatch(identifier) and not TASK_ID_PREFIX.match(identifier):
            found.add(identifier)
    return sort_ids(found)


def sort_ids(identifiers):
    return sorted(identifiers, key=lambda identifier: [int(part) if part.isdigit() else part
                                                       for part in re.split(r"(\d+)", identifier)])


def split_sections(text):
    """[(heading title, body text of the section including its sub-sections)] for every heading of the document."""
    lines = text.splitlines()
    headings = []
    for index, line in enumerate(lines):
        match = HEADING_LINE.match(line)
        if match:
            headings.append((index, len(match.group(1)), match.group(2)))
    sections = []
    for position, (index, level, title) in enumerate(headings):
        end = len(lines)
        for next_index, next_level, _ in headings[position + 1:]:
            if next_level <= level:
                end = next_index
                break
        sections.append((title, "\n".join(lines[index + 1:end])))
    return sections


def heading_names(title, name):
    return name.lower() in title.lower()


def count_table_rows(body):
    rows = [line for line in body.splitlines() if line.strip().startswith("|")]
    if any(TABLE_SEPARATOR.match(line) for line in rows):
        return max(0, len([line for line in rows if not TABLE_SEPARATOR.match(line)]) - 1)
    return len(rows)


def count_list_items(body):
    return sum(1 for line in body.splitlines() if LIST_ITEM.match(line))


# ------------------------------------------------ warnings
def build_warnings(scan, items, project_conformance=None):
    """The Preview warnings (strings)."""
    texts = [entry.get("text") or "" for entry in scan.get("files", []) if entry.get("status") == STATUS_READ]
    joined = "\n".join(texts)
    warnings = ["missing section: %s" % name for name in CORE_SECTIONS if not items["sections"].get(name)]
    if has_open_questions(texts):
        warnings.append("open questions are not empty: the AI will carry them to the ROADMAP")
    unmapped = sum(int(count) for line in COVERAGE_LINE.findall(joined) for count in UNMAPPED_COUNT.findall(line))
    if unmapped:
        warnings.append("traceability reports %d unmapped requirements" % unmapped)
    source_conformance = find_conformance(joined)
    if source_conformance and project_conformance and source_conformance != project_conformance:
        warnings.append("the spec says Conformance %s but the project is %s" % (source_conformance, project_conformance))
    if not items["requirements"]:
        warnings.append("no IDs: coverage by section only")
    if any(ZERO_MAPPED.search(line) for line in COVERAGE_LINE.findall(joined)):
        warnings.append("Coverage: 0 mapped")
    return warnings


def has_open_questions(texts):
    for text in texts:
        for title, body in split_sections(text):
            if any(heading_names(title, heading) for heading in OPEN_QUESTION_HEADINGS):
                lines = [line.strip() for line in body.splitlines() if line.strip() and set(line.strip()) - set("-*_ ")]
                first_sentence = re.split(r"\.(?:\s|$)", " ".join(lines))[0].strip()
                if lines and not EMPTY_ANSWER.match(first_sentence):
                    return True
    return False


def find_conformance(text):
    match = CONFORMANCE_LINE.search(text)
    if not match:
        return ""
    value = match.group(1).upper()
    return CONFORMANCE_ALIASES.get(value, value)
