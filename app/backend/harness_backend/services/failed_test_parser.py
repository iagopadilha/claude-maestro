"""Failed tests read out of a verifier's final return or tool output (pytest, unittest, jest/vitest and plain "expected ... got ..." lines).

Pure text work, no files. What cannot be read stays `None`: a name is never guessed and a missing expected/got is not filled in.
Every text goes through credential redaction and is clipped before it is returned.
"""
import re

from ..repositories.loop_read_formatting import clip_redacted_text

FAILED_TESTS_LIMIT = 20
NAME_LIMIT = 200
DETAIL_LIMIT = 300
PYTEST_SUMMARY_PATTERN = re.compile(r"^FAILED\s+(\S+?)(?:\s+-\s+(.*))?$")
PYTEST_SECTION_PATTERN = re.compile(r"^_{3,}\s+(.+?)\s+_{3,}$")
UNITTEST_HEADER_PATTERN = re.compile(r"^(?:FAIL|ERROR):\s+(\S+)\s+\(([\w.]+)\)")
JEST_HEADER_PATTERN = re.compile(r"^\s*[●✕×]\s+(.+?)(?:\s+\(\d+\s*ms\))?$")
ASSERTION_PATTERN = re.compile(r"^(?:E\s+)?(\w*Error\b.*)$")
PYTEST_ASSERT_PATTERN = re.compile(r"^(?:E\s+)?(?:\w*Error:\s*)?assert\s+(.+?)\s+==\s+(.+)$")
UNITTEST_DIFFERENCE_PATTERN = re.compile(r"(?:AssertionError:\s*)?(.+?)\s+!=\s+(.+)$")
EXPECTED_LINE_PATTERN = re.compile(r"(?i)^\W*expected(?:\s+value)?\s*[:=]\s*(.+)$")
GOT_LINE_PATTERN = re.compile(r"(?i)^\W*(?:received|actual|got)(?:\s+value)?\s*[:=]\s*(.+)$")
EXPECTED_GOT_INLINE_PATTERN = re.compile(r"(?i)expected\s+(.+?),?\s+(?:but\s+)?(?:got|received|was|found)\s+(.+)$")


def new_entry(name):
    return {"name": clip_redacted_text(name, NAME_LIMIT), "why": None, "expected": None, "got": None}


def fill(entry, key, value):
    """Sets a field once (the first reading wins) from a text already known to be non-empty."""
    text = clip_redacted_text(value, DETAIL_LIMIT)
    if text and entry[key] is None:
        entry[key] = text


def read_detail_line(entry, line):
    """Reads one line that follows a test header and fills `why`, `expected` and `got` of that test."""
    stripped = line.strip()
    expected_got = EXPECTED_GOT_INLINE_PATTERN.search(stripped)
    pytest_assert = PYTEST_ASSERT_PATTERN.match(stripped)
    difference = UNITTEST_DIFFERENCE_PATTERN.search(stripped) if "AssertionError" in stripped and "!=" in stripped else None
    if expected_got:
        fill(entry, "expected", expected_got.group(1))
        fill(entry, "got", expected_got.group(2))
    elif pytest_assert:
        fill(entry, "got", pytest_assert.group(1))
        fill(entry, "expected", pytest_assert.group(2))
    elif difference:
        fill(entry, "got", difference.group(1))
        fill(entry, "expected", difference.group(2))
    else:
        for key, pattern in (("expected", EXPECTED_LINE_PATTERN), ("got", GOT_LINE_PATTERN)):
            found = pattern.match(stripped)
            if found:
                fill(entry, key, found.group(1))
    assertion = ASSERTION_PATTERN.match(stripped)
    if assertion:
        fill(entry, "why", assertion.group(1))


def find_entry(entries, name):
    short = name.split("::")[-1]
    return next((entry for entry in entries if entry["name"] == name or entry["name"].endswith("::" + short) or name.endswith("::" + entry["name"])), None)


def parse_failed_tests(text):
    """[{"name","why","expected","got"}] in the order found, at most FAILED_TESTS_LIMIT; empty when nothing is parseable."""
    entries, current = [], None
    for raw in str(text or "").splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        summary = PYTEST_SUMMARY_PATTERN.match(stripped)
        unittest_header = UNITTEST_HEADER_PATTERN.match(stripped)
        section = PYTEST_SECTION_PATTERN.match(stripped)
        jest = JEST_HEADER_PATTERN.match(line) if not (summary or unittest_header or section) else None
        name = None
        if summary:
            name = summary.group(1)
        elif unittest_header:
            name = "%s.%s" % (unittest_header.group(2), unittest_header.group(1))
        elif section:
            name = section.group(1)
        elif jest and len(jest.group(1)) > 2:
            name = jest.group(1)
        if name is not None:
            existing = find_entry(entries, clip_redacted_text(name, NAME_LIMIT))
            current = existing or new_entry(name)
            if existing is None and len(entries) < FAILED_TESTS_LIMIT:
                entries.append(current)
            if summary and summary.group(2):
                read_detail_line(current, summary.group(2))
                fill(current, "why", summary.group(2))
            continue
        if current is not None:
            read_detail_line(current, line)
    return entries
