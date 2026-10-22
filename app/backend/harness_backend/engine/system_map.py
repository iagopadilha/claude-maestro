#!/usr/bin/env python3
"""Contract merge for the root `docs/codebase/SYSTEM.md` of a monorepo (deterministic, no AI).

Usage:
  python system_map.py merge   <repo-root> --slugs a,b [--json]
  python system_map.py pending <repo-root> --slugs a,b

Executors never edit SYSTEM.md. A feature that adds, changes or removes a route, DTO, auth scheme or env name
writes `.specs/features/<slug>/contract.md` with lines like

  - [add|change|remove] <route|dto|auth|env> <identity> - <one-line purpose> (<file>)

(identity = `METHOD /path`, DTO name, auth scheme name or env variable name; the separator is an em dash or ` - `).
`merge_contract_deltas` upserts them by kind + identity into the `<!-- CONTRACT:BEGIN -->...<!-- CONTRACT:END -->`
section and leaves every byte outside the markers untouched (line endings included).

A ledger (`.specs/project/loop/internal/contract-merged.json`) remembers which contract.md content was merged for each
feature, so `pending` can list the ones not merged yet (printed as `CONTRACT-PENDING: <path>` lines).
"""
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import loop_state  # noqa: E402  (feature_dir / feature_folder_name: the feature folder is the slug with ':' replaced by '-')

SYSTEM_RELATIVE_PATH = os.path.join("docs", "codebase", "SYSTEM.md")
LEDGER_RELATIVE_PATH = os.path.join(".specs", "project", "loop", "internal", "contract-merged.json")
CONTRACT_BEGIN = "<!-- CONTRACT:BEGIN -->"
CONTRACT_END = "<!-- CONTRACT:END -->"

NO_GRAMMAR_MARK = "has no line in the contract grammar"
KIND_ORDER = ["route", "dto", "auth", "env"]
KIND_HEADINGS = {"dto": "#### DTOs", "auth": "#### Auth", "env": "#### Env"}
ROUTE_HEADING_PREFIX = "#### Routes: "
HTTP_METHODS = "GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS"

ENTRY_LINE = re.compile(
    r"^\s*[-*]\s*\[(?P<action>add|change|remove)\]\s+(?P<kind>route|dto|auth|env)\s+"
    r"(?P<identity>.+?)(?:\s+(?:—|-)\s+(?P<purpose>.*?)\s*\((?P<file>[^()]+)\))?\s*$"
)
CONTRACT_CANDIDATE_LINE = re.compile(r"^\s*[-*]\s*\[")
ROUTE_IDENTITY = re.compile(r"^(?P<method>%s)\s+(?P<path>/\S*)$" % HTTP_METHODS, re.IGNORECASE)
RENDERED_LINE = re.compile(r"^- (?P<kind>route|dto|auth|env) (?P<identity>.+?) — (?P<text>.+)$")
CONFLICT_SUFFIX = re.compile(r"\s*\(conflict with (?P<slug>[^)]+)\)\s*$")
GENERATED_HEADING = re.compile(r"^#### (Routes: .+|DTOs|Auth|Env)\s*$")


def contract_path(root, slug):
    return os.path.join(loop_state.feature_dir(root, slug), "contract.md")


def pending_line(slug):
    return "CONTRACT-PENDING: .specs/features/%s/contract.md" % loop_state.feature_folder_name(slug)


def read_text(path):
    with open(path, "rb") as handle:
        return handle.read()


def content_hash(raw_bytes):
    return hashlib.sha256(raw_bytes).hexdigest()


def load_ledger(root):
    try:
        with open(os.path.join(root, LEDGER_RELATIVE_PATH), "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            data.setdefault("merged", {})
            data.setdefault("origins", {})
            return data
    except (OSError, ValueError):
        pass
    return {"merged": {}, "origins": {}}


def save_ledger(root, ledger):
    path = os.path.join(root, LEDGER_RELATIVE_PATH)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = path + ".tmp"
    with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(ledger, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(temporary, path)


def normalize_identity(kind, identity):
    """Returns the canonical identity, or None when it is not valid for the kind."""
    identity = identity.strip()
    if kind == "route":
        match = ROUTE_IDENTITY.match(identity)
        return "%s %s" % (match.group("method").upper(), match.group("path")) if match else None
    return identity if identity and " " not in identity else None


def parse_contract(text, slug):
    """Returns (deltas, malformed_lines). A delta is (action, kind, identity, entry_text or None)."""
    deltas = []
    malformed = []
    for number, line in enumerate(text.splitlines(), 1):
        if not CONTRACT_CANDIDATE_LINE.match(line):
            continue
        match = ENTRY_LINE.match(line)
        if not match:
            malformed.append("%s: line %d is not a valid contract line: %s" % (slug, number, line.strip()))
            continue
        action, kind = match.group("action"), match.group("kind")
        identity = normalize_identity(kind, match.group("identity"))
        purpose, file_name = match.group("purpose"), match.group("file")
        if identity is None or (action != "remove" and not (purpose and file_name)):
            malformed.append("%s: line %d is not a valid contract line: %s" % (slug, number, line.strip()))
            continue
        entry_text = "%s (%s)" % (purpose.strip(), file_name.strip()) if purpose else None
        deltas.append((action, kind, identity, entry_text))
    return deltas, malformed


def split_section(system_text):
    """Returns (before, section, after) around the markers, or None when the markers are missing."""
    begin = system_text.find(CONTRACT_BEGIN)
    if begin < 0:
        return None
    section_start = begin + len(CONTRACT_BEGIN)
    end = system_text.find(CONTRACT_END, section_start)
    if end < 0:
        return None
    return system_text[:section_start], system_text[section_start:end], system_text[end:]


def parse_section(section):
    """Returns (preamble_lines, entries). entries maps (kind, identity) to a list of {text, conflict}."""
    preamble = []
    entries = {}
    for line in section.splitlines():
        stripped = line.strip()
        if not stripped or GENERATED_HEADING.match(stripped):
            continue
        rendered = RENDERED_LINE.match(stripped)
        if not rendered:
            preamble.append(line.rstrip())
            continue
        text = rendered.group("text")
        conflict = CONFLICT_SUFFIX.search(text)
        if conflict:
            text = text[:conflict.start()]
        entries.setdefault((rendered.group("kind"), rendered.group("identity")), []).append(
            {"text": text, "conflict": conflict.group("slug") if conflict else None})
    return preamble, entries


def route_area(identity):
    path = identity.split(" ", 1)[1].strip("/")
    return path.split("/", 1)[0].split("?", 1)[0] or "root"


def entry_line(kind, identity, entry):
    suffix = " (conflict with %s)" % entry["conflict"] if entry["conflict"] else ""
    return "- %s %s — %s%s" % (kind, identity, entry["text"], suffix)


def render_section(preamble, entries, newline):
    lines = list(preamble)
    while lines and not lines[-1].strip():
        lines.pop()
    groups = []
    routes_by_area = {}
    for (kind, identity) in sorted(entries):
        if kind == "route":
            routes_by_area.setdefault(route_area(identity), []).append(identity)
    for area in sorted(routes_by_area):
        groups.append((ROUTE_HEADING_PREFIX + area, [("route", identity) for identity in routes_by_area[area]]))
    for kind in KIND_ORDER[1:]:
        keys = [key for key in sorted(entries) if key[0] == kind]
        if keys:
            groups.append((KIND_HEADINGS[kind], keys))
    for heading, keys in groups:
        if lines:
            lines.append("")
        lines.append(heading)
        for key in keys:
            lines.extend(entry_line(key[0], key[1], entry) for entry in entries[key])
    return newline + newline.join(lines) + newline if lines else newline


def origin_of(origins, key_text, text):
    for recorded_text, recorded_slug in origins.get(key_text, []):
        if recorded_text == text:
            return recorded_slug
    return None


def apply_delta(entries, origins, slug, delta, warnings):
    action, kind, identity, text = delta
    key = (kind, identity)
    key_text = "%s %s" % key
    if action == "remove":
        entries.pop(key, None)
        origins.pop(key_text, None)
        return
    existing = entries.get(key, [])
    if any(entry["text"] == text for entry in existing):
        return
    recorded = [(entry, origin_of(origins, key_text, entry["text"])) for entry in existing]
    foreign = [origin or "an earlier entry" for _, origin in recorded if origin != slug]
    if not existing or not foreign:
        entries[key] = [{"text": text, "conflict": None}]
        origins[key_text] = [[text, slug]]
        return
    other = foreign[0]
    warnings.append("conflict on %s: %s differs from %s; both kept" % (key_text, slug, other))
    entries[key] = existing + [{"text": text, "conflict": other}]
    origins.setdefault(key_text, []).append([text, slug])


def merge_contract_deltas(root, slugs):
    """Merges the contract.md of each listed feature into SYSTEM.md. Returns {changed: bool, warnings: [str]}."""
    root = os.path.abspath(root)
    warnings = []
    ledger = load_ledger(root)
    candidates = []
    for slug in slugs:
        path = contract_path(root, slug)
        if not os.path.isfile(path):
            continue
        raw = read_text(path)
        if ledger["merged"].get(slug) == content_hash(raw):
            continue
        candidates.append((slug, raw))
    if not candidates:
        return {"changed": False, "warnings": warnings}

    system_path = os.path.join(root, SYSTEM_RELATIVE_PATH)
    if not os.path.isfile(system_path):
        warnings.append("SYSTEM.md is missing (%s): contract merge skipped, the deltas stay in contract.md"
                        % SYSTEM_RELATIVE_PATH.replace("\\", "/"))
        return {"changed": False, "warnings": warnings}
    original = read_text(system_path).decode("utf-8")
    parts = split_section(original)
    if parts is None:
        warnings.append("SYSTEM.md has no %s ... %s section: contract merge skipped" % (CONTRACT_BEGIN, CONTRACT_END))
        return {"changed": False, "warnings": warnings}

    before, section, after = parts
    newline = "\r\n" if "\r\n" in original else "\n"
    preamble, entries = parse_section(section)
    origins = ledger["origins"]
    for slug, raw in candidates:
        deltas, malformed = parse_contract(raw.decode("utf-8", errors="replace"), slug)
        warnings.extend(malformed)
        if not deltas and not malformed:  # free-form text or a table: the grammar is lines `- [add|change|remove] <kind> <identity> — <purpose> (<file>)`
            warnings.append("contract.md of %s %s: nothing merged" % (slug, NO_GRAMMAR_MARK))
        for delta in deltas:
            apply_delta(entries, origins, slug, delta, warnings)
        ledger["merged"][slug] = content_hash(raw)

    updated = before + render_section(preamble, entries, newline) + after
    changed = updated != original
    if changed:
        with open(system_path, "wb") as handle:
            handle.write(updated.encode("utf-8"))
    save_ledger(root, ledger)
    return {"changed": changed, "warnings": warnings}


def pending_slugs(root, slugs):
    """Slugs (in the given order) whose contract.md exists and was not merged in its current content."""
    root = os.path.abspath(root)
    merged = load_ledger(root)["merged"]
    pending = []
    for slug in slugs:
        path = contract_path(root, slug)
        if os.path.isfile(path) and merged.get(slug) != content_hash(read_text(path)):
            pending.append(slug)
    return pending


def parse_slugs(args):
    if "--slugs" not in args:
        return []
    position = args.index("--slugs") + 1
    value = args[position] if position < len(args) else ""
    return [slug.strip() for slug in value.split(",") if slug.strip()]


def main():
    args = sys.argv[1:]
    if len(args) < 2 or args[0] not in ("merge", "pending"):
        print(__doc__)
        return 2
    command, root, slugs = args[0], args[1], parse_slugs(args)
    if command == "pending":
        for slug in pending_slugs(root, slugs):
            print(pending_line(slug))
        return 0
    result = merge_contract_deltas(root, slugs)
    if "--json" in args:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("changed: %s" % ("yes" if result["changed"] else "no"))
        for warning in result["warnings"]:
            print("WARNING: %s" % warning)
    return 0


if __name__ == "__main__":
    sys.exit(main())
