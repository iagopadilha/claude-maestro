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
    """[{"id", "n", "features": [{"slug", "status", "title", "deps", "paths"}], "lines": {slug: feature line text}}] of the `Follow-up` stages,
    in file order. `lines` lets `describe_stage_change` see an edit of any field of a feature line."""
    features, _ = loop_state.parse_roadmap(roadmap_text)
    stages, current = [], None
    for raw in normalize_newlines(roadmap_text).splitlines():
        line = raw.strip()
        if ANY_STAGE_HEADING.match(line):
            heading = STAGE_HEADING.match(line)
            current = {"id": heading.group("id").replace(" ", "-"), "n": int(heading.group("n")) if heading.group("n") else None,
                       "features": [], "lines": {}} if heading else None
            if current:
                stages.append(current)
            continue
        found = FEATURE_LINE.match(line)
        if current is not None and found and found.group(1) in features:
            fields = features[found.group(1)]
            current["features"].append({"slug": found.group(1), "status": fields.get("status"), "title": fields.get("title", ""),
                                        "deps": fields.get("deps", ""), "paths": fields.get("paths", "")})
            current["lines"][found.group(1)] = line
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


def _heading_index(lines, round_number):
    """Index of the first `Follow-up <round_number>` heading line, or None."""
    for index, raw in enumerate(lines):
        heading = STAGE_HEADING.match(raw.strip())
        if heading and heading.group("n") is not None and int(heading.group("n")) == round_number:
            return index
    return None


def validate_round_stage(baseline_roadmap, after_roadmap, baseline_flows, after_flows, round_number, executed_slugs):
    """Why the round's edit is NOT 'only the Follow-up <n> stage changed (+ appended FLOWS text)'; None when it is."""
    before_lines = normalize_newlines(baseline_roadmap).rstrip().split("\n")
    after_lines = normalize_newlines(after_roadmap).rstrip().split("\n")
    before_cut = _heading_index(before_lines, round_number)
    frozen = before_lines if before_cut is None else before_lines[:before_cut]
    while frozen and not frozen[-1].strip():
        frozen = frozen[:-1]
    if [line.rstrip() for line in after_lines[:len(frozen)]] != [line.rstrip() for line in frozen]:
        return "a line above the Follow-up %d stage was changed; only that stage may be edited" % round_number
    headings = [raw.strip() for raw in after_lines if ANY_STAGE_HEADING.match(raw.strip())]
    own = [h for h in headings if (STAGE_HEADING.match(h) and STAGE_HEADING.match(h).group("n") is not None
                                   and int(STAGE_HEADING.match(h).group("n")) == round_number)]
    if not own:
        return "the Follow-up %d stage is missing" % round_number
    if len(own) > 1:
        return "the Follow-up %d stage appears more than once" % round_number
    if headings[-1] != own[0]:
        return "the Follow-up %d stage must be the last stage of the ROADMAP" % round_number
    stage = next((s for s in list_follow_up_stages(after_roadmap) if s["n"] == round_number), None)
    executed = set(executed_slugs or [])
    for feature in (stage or {}).get("features", []):
        if feature["slug"] in executed:
            return "feature slug `%s` of Follow-up %d collides with an already executed feature" % (feature["slug"], round_number)
    if not normalize_newlines(after_flows).rstrip().startswith(normalize_newlines(baseline_flows).rstrip()):
        return "an earlier part of FLOWS.md was changed; only new entries may be appended"
    return None


def _feature_lines(stage_text_lines):
    return {found.group(1): line.strip() for line in stage_text_lines for found in [FEATURE_LINE.match(line.strip())] if found}


def describe_stage_change(before_stage, after_stage):
    """{"added", "removed", "edited"} slug lists between two stages (dicts of list_follow_up_stages, or None).

    A stage dict carries only slug/status, so `edited` compares the feature entries (any field) of the same slug;
    an optional `lines` key ({slug: feature line text}) makes the comparison exact on the line text.
    """
    def entries(stage):
        stage = stage or {}
        lines = stage.get("lines")
        if isinstance(lines, dict):
            return {slug: text for slug, text in lines.items()}
        return {f["slug"]: sorted((k, str(v)) for k, v in f.items()) for f in stage.get("features", [])}
    before, after = entries(before_stage), entries(after_stage)
    return {"added": [s for s in after if s not in before],
            "removed": [s for s in before if s not in after],
            "edited": [s for s in after if s in before and after[s] != before[s]]}
