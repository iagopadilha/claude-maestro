"""`.specs/project/FLOWS.md`: one `## [slug] Title` section per ROADMAP feature with the numbered steps of the flow that
proves the feature works (the verifier-flow checks the `Expected:` steps).

Parse rule (shared with the skill, `references/state.md`): section heading `^##\\s*\\[(slug)\\]\\s*(title)?`; steps =
lines matching `^\\s*\\d+[.)]\\s+\\S` inside the section. A feature "has a flow" when its section exists with at least
MIN_FLOW_STEPS steps. HTML comments (`<!-- ... -->`, where the template keeps its example) are ignored.
"""
import re

MIN_FLOW_STEPS = 2
FLOW_HEADING_PATTERN = re.compile(r"^##\s*\[([^\]]+)\]\s*(.*?)\s*$")
OTHER_HEADING_PATTERN = re.compile(r"^#{1,6}\s")
FLOW_STEP_PATTERN = re.compile(r"^\s*\d+[.)]\s+\S")
HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.S)


def parse_flows(text):
    """{slug: {"title": str, "steps": [str]}} in file order (a repeated slug adds its steps to the first section)."""
    flows, current = {}, None
    for raw in HTML_COMMENT_PATTERN.sub("", text or "").splitlines():
        heading = FLOW_HEADING_PATTERN.match(raw.strip())
        if heading:
            slug = heading.group(1).strip()
            current = flows.setdefault(slug, {"title": heading.group(2).strip(), "steps": []})
            continue
        if OTHER_HEADING_PATTERN.match(raw.strip()):
            current = None
            continue
        if current is not None and FLOW_STEP_PATTERN.match(raw):
            current["steps"].append(raw.strip())
    return flows


def check_flow_coverage(feature_slugs, flows_text):
    """{"missing": [ROADMAP slugs without a flow of >= MIN_FLOW_STEPS steps], "orphans": [FLOWS slugs not in the ROADMAP]}."""
    flows = parse_flows(flows_text)
    slugs = list(feature_slugs)
    missing = [slug for slug in slugs if len(flows.get(slug, {}).get("steps", [])) < MIN_FLOW_STEPS]
    orphans = [slug for slug in flows if slug not in slugs]
    return {"missing": missing, "orphans": orphans}
