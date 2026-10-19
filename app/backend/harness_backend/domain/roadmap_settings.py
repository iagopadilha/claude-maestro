"""Project-wide settings written in the ROADMAP header (not per feature).

`**E2E:** yes|no` = the loop ends with the flow-validation phase (verifier-flow runs each FLOWS.md flow against the real app,
needs .specs/project/TEST-ENVIRONMENT.md). Absent or unreadable = no. Decided during planning, never in the Start-a-loop card.
"""
import re

HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.S)
E2E_LINE_PATTERN = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}E2E\*{0,2}\s*:\s*\*{0,2}\s*(yes|no|true|false)\b", re.I | re.M)
YES_VALUES = ("yes", "true")


def parse_roadmap_e2e(text):
    """True/False from the first `E2E: yes|no` line outside HTML comments (the template comment documents it); None when absent."""
    found = E2E_LINE_PATTERN.search(HTML_COMMENT_PATTERN.sub("", text or ""))
    return None if not found else found.group(1).lower() in YES_VALUES
