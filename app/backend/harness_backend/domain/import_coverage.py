"""Coverage of an import: every requirement/decision/task (or, without IDs, every section) of the source document must appear in
the coverage table with a destination or an explicit `dropped: <reason>`. Pure functions, no I/O.

Grammar of the coverage text: `## <slug>` headings, each followed by a Markdown table `| Item | Kind | Destination |`.
An item can be a range (`ORIG-01..03` -> ORIG-01, ORIG-02, ORIG-03; zero padding is kept). Rows before any heading go under "".
"""
import re

HEADING_PATTERN = re.compile(r"^##\s+(.+?)\s*#*\s*$")
SEPARATOR_CELL_PATTERN = re.compile(r"^:?-{2,}:?$")
RANGE_PATTERN = re.compile(r"^(?P<prefix>.*?)(?P<first>\d+)\s*\.\.\s*(?P<second_prefix>[^\d]*)(?P<last>\d+)$")
HEADER_ITEM_WORDS = frozenset({"item", "items"})
DROPPED_PREFIX = "dropped:"
MAX_RANGE_SIZE = 1000
ITEM_COLUMN = 0
DESTINATION_COLUMN = 2


def split_table_cells(line):
    inner = line.strip()
    inner = inner[1:] if inner.startswith("|") else inner
    inner = inner[:-1] if inner.endswith("|") else inner
    return [cell.strip() for cell in inner.split("|")]


def is_separator_row(cells):
    return bool(cells) and all(SEPARATOR_CELL_PATTERN.match(cell) for cell in cells if cell) and any(cells)


def expand_item_range(item):
    """`ORIG-01..03` -> ['ORIG-01','ORIG-02','ORIG-03']; anything that is not a valid range comes back as [item]."""
    found = RANGE_PATTERN.match(item)
    if not found:
        return [item]
    first_text, last_text = found.group("first"), found.group("last")
    first, last = int(first_text), int(last_text)
    if last < first or last - first >= MAX_RANGE_SIZE:
        return [item]
    width = len(first_text) if first_text.startswith("0") else 0
    return [found.group("prefix") + str(number).zfill(width) for number in range(first, last + 1)]


def parse_coverage(text):
    """{slug: {item: destination}} from the coverage text (header and separator rows ignored, ranges expanded)."""
    coverage = {}
    slug = ""
    for line in (text or "").splitlines():
        stripped = line.strip()
        heading = HEADING_PATTERN.match(stripped)
        if heading:
            slug = heading.group(1).strip().strip("`")
            continue
        if not stripped.startswith("|"):
            continue
        cells = split_table_cells(stripped)
        if is_separator_row(cells) or len(cells) <= ITEM_COLUMN:
            continue
        item = cells[ITEM_COLUMN].strip("`* ").strip()
        if not item or item.lower() in HEADER_ITEM_WORDS:
            continue
        destination = cells[DESTINATION_COLUMN].strip("`* ").strip() if len(cells) > DESTINATION_COLUMN else ""
        for expanded in expand_item_range(item):
            coverage.setdefault(slug, {})[expanded] = destination
    return coverage


def merge_destinations(coverage):
    merged = {}
    for table in (coverage or {}).values():
        merged.update(table)
    return merged


def collect_expected_items(items):
    identifiers = []
    for key in ("requirements", "decisions", "tasks"):
        identifiers.extend(str(value).strip() for value in (items or {}).get(key) or [])
    identifiers = [value for value in identifiers if value]
    if identifiers:
        return identifiers
    return [name for name, present in ((items or {}).get("sections") or {}).items() if present]


def check_coverage(items, coverage):
    """{"covered": [...], "dropped": [{"item","reason"}], "missing": [...]} for the source's items against the merged tables."""
    destinations = merge_destinations(coverage)
    covered, dropped, missing = [], [], []
    for item in collect_expected_items(items):
        destination = (destinations.get(item) or "").strip()
        if not destination:
            missing.append(item)
        elif destination.lower().startswith(DROPPED_PREFIX):
            dropped.append({"item": item, "reason": destination[len(DROPPED_PREFIX):].strip()})
        else:
            covered.append(item)
    return {"covered": covered, "dropped": dropped, "missing": missing}
