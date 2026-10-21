"""Parsing and validation of the `Follow-up` stages of the ROADMAP (pure text rules, no I/O).

The stage id is the first word of the heading (loop_state.STAGE_LINE), so the heading is `## Stage Follow-up-<n> - Follow-up <n>`;
`Follow-up <n>` with a space is accepted too. The ROADMAP grammar itself comes from the engine (`loop_state.parse_roadmap`).
"""
import os
import re
import sys

ENGINE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "engine"))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import loop_state  # noqa: E402  (same module object the repositories use)

STAGE_HEADING = re.compile(r"^#{1,6}\s*Stage\s+(?P<id>Follow-up(?:[-\s]+(?P<n>\d+))?)", re.I)
ANY_STAGE_HEADING = re.compile(r"^#{1,6}\s*Stage\s+\S+", re.I)
FEATURE_LINE = re.compile(r"^-\s*\[([^\]]+)\]")
OPEN_FEATURE_STATUSES = ("pending", "speccing", "specced", "approved", "building")


def normalize_newlines(text):
    return (text or "").replace("\r\n", "\n")


def list_follow_up_stages(roadmap_text):
    """[{"id", "n", "features": [{"slug", "status"}]}] of the `Follow-up` stages, in file order."""
    features, _ = loop_state.parse_roadmap(roadmap_text)
    stages, current = [], None
    for raw in normalize_newlines(roadmap_text).splitlines():
        line = raw.strip()
        if ANY_STAGE_HEADING.match(line):
            heading = STAGE_HEADING.match(line)
            current = {"id": heading.group("id").replace(" ", "-"), "n": int(heading.group("n")) if heading.group("n") else None,
                       "features": []} if heading else None
            if current:
                stages.append(current)
            continue
        found = FEATURE_LINE.match(line)
        if current is not None and found and found.group(1) in features:
            current["features"].append({"slug": found.group(1), "status": features[found.group(1)].get("status")})
    return stages


def validate_appended_stage(roadmap_before, roadmap_after, flows_before, flows_after, expected_number):
    """Why the two files are NOT 'the old text + one appended Follow-up stage (+ appended FLOWS text)'; None when they are."""
    before, after = normalize_newlines(roadmap_before).rstrip(), normalize_newlines(roadmap_after).rstrip()
    if not after.startswith(before) or after == before:
        return "an earlier part of the ROADMAP was changed (or nothing was appended); only a new Follow-up stage may be added"
    appended_lines = [line.strip() for line in after[len(before):].splitlines() if line.strip()]
    heading = STAGE_HEADING.match(appended_lines[0]) if appended_lines else None
    if heading is None or heading.group("n") is None or int(heading.group("n")) != expected_number:
        return "the appended stage must be headed `## Stage Follow-up-%d - Follow-up %d`" % (expected_number, expected_number)
    if any(ANY_STAGE_HEADING.match(line) for line in appended_lines[1:]):
        return "only ONE stage may be appended"
    appended_slugs = [found.group(1) for found in (FEATURE_LINE.match(line) for line in appended_lines) if found]
    if not appended_slugs:
        return "the appended stage has no feature"
    features_before, _ = loop_state.parse_roadmap(before)
    features_after, errors_after = loop_state.parse_roadmap(after)
    if len(set(appended_slugs)) != len(appended_slugs) or any(slug in features_before for slug in appended_slugs):
        return "a new feature reuses a slug that already exists"
    if len(features_after) != len(features_before) + len(appended_slugs):
        return "the appended features do not parse as the ROADMAP grammar"
    if errors_after and len(errors_after) > len(loop_state.parse_roadmap(before)[1]):
        return "the appended stage breaks the ROADMAP grammar: %s" % "; ".join(errors_after[:3])
    if not normalize_newlines(flows_after).rstrip().startswith(normalize_newlines(flows_before).rstrip()):
        return "an earlier part of FLOWS.md was changed; only new entries may be appended"
    return None
