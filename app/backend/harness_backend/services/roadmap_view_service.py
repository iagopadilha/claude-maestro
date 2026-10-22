"""ROADMAP view for the panel: stages, features (with dependency clarity, summary and wave), counts and graph.

The parser is the one in `engine/loop_state.py` (never rewritten). Used by `/state` (current ROADMAP) and by `/turn/<n>`
(the snapshot ROADMAP).
"""
import re

from ..repositories.loop_file_repository import loop_state
from ..repositories.loop_read_formatting import clip_redacted_text, to_text_list

CLOSED_STATUSES = ("verified", "blocked")
IN_PROGRESS_STATUSES = ("building", "speccing", "approved")
SIZES = ("Small", "Medium", "Large", "Complex")
GOAL_LIMIT = 600
STAGE_DESCRIPTION_LIMIT = 600
STAGE_CLOSING_LIMIT = 1200
FEATURE_NAME_LIMIT = 90
SPEC_TITLE_LINES = 15
STAGE_CLOSING_SECTIONS = {"Delivered": "delivered", "User decisions": "decisions", "Lessons": "lessons", "Unverified": "unverified",
                          "Blocked": "blocked", "For the next stage": "next_stage"}
STAGE_TITLE_PATTERN = re.compile(r"^#{1,6}\s*(Stage\s+\S+.*)$", re.I)
SPEC_GOAL_TITLES = (re.compile(r"(?i)^(vision|goal|objective|summary|overview)\b"), re.compile(r"(?i)^(problem|context|background)\b"))
REQUIREMENT_LINE_PATTERN = re.compile(r"^\|\s*([A-Z][A-Z0-9]{0,5}-\d+[a-z]?)\s*\|")
SPEC_SIZE_PATTERN = re.compile(r"(?i)\bSCOPE\b\W{0,6}(Small|Medium|Large|Complex)\b")
SPEC_SIZE_HEAD_LENGTH = 3000
WAVE_REASON_DISJOINT = "disjoint paths, no dependency between them"
WAVE_REASON_TOGETHER = "dispatched together in the same turn"
WAVE_REASON_ALONE = "alone in the turn"
WAVE_REASON_ALONE_REGRESSION = "runs alone in the wave (regression=%s)"
WAVE_REASON_WAITING = "waiting on: "
WAVE_REASON_NO_OPEN_DEPENDENCY = "no open dependency"
STAGE_FALLBACK_TITLE = "Stage %s"
NO_STAGE_TITLE = "No stage"
SOURCE_SPEC = "spec.md"
SOURCE_ROADMAP = "roadmap"
SOURCE_NONE = "none"


def list_stage_titles(text):
    titles = []
    for line in text.splitlines():
        found = STAGE_TITLE_PATTERN.match(line.strip())
        if found:
            titles.append(found.group(1).strip())
    return titles


def split_markdown_sections(text):
    """[(title, body)] of the `#`..`######` headings; the text before the 1st heading comes with title ''."""
    sections, title, body = [], "", []
    for line in text.splitlines():
        found = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if found:
            sections.append((title, "\n".join(body)))
            title, body = found.group(1).strip(), []
        else:
            body.append(line)
    sections.append((title, "\n".join(body)))
    return sections


def extract_first_paragraph(body):
    paragraph = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(("|", "```", "- **", "<!--")):
            if paragraph:
                break
            continue
        paragraph.append(line)
    return " ".join(paragraph)


def are_paths_overlapping(first, second):
    first, second = first.strip("/\\"), second.strip("/\\")
    return first == second or first.startswith(second + "/") or second.startswith(first + "/")


def extract_stage_descriptions(text):
    """{stage label: text between the stage heading and the 1st feature} of the ROADMAP."""
    found_descriptions, label, lines, closed = {}, None, [], True
    for raw in text.splitlines():
        line = raw.strip()
        heading = loop_state.STAGE_LINE.match(line)
        if heading or re.match(r"^#{1,6}\s", line):
            if label is not None and lines:
                found_descriptions[label] = "\n".join(lines)
            label, lines, closed = (heading.group(1) if heading else None), [], False
        elif label is not None and not closed:
            if loop_state.FEATURE_LINE.match(line):
                closed = True
            elif line:
                lines.append(line)
    if label is not None and lines:
        found_descriptions.setdefault(label, "\n".join(lines))
    return found_descriptions


def add_dependency_clarity(features, parsed):
    """depends_on/dep_dirs/unblocks/waiting_on/blocked_by (only slugs go into depends_on; dir:<folder> goes into dep_dirs)."""
    status_by_id = {feature["id"]: feature["status"] for feature in features}
    unblocked = {feature["id"]: [] for feature in features}
    for feature in features:
        slugs, folders = loop_state.split_deps(parsed[feature["id"]])
        feature["depends_on"] = [{"id": dependency, "status": status_by_id.get(dependency)} for dependency in slugs]
        feature["dep_dirs"] = list(folders)
        for dependency in slugs:
            if dependency in unblocked and feature["id"] not in unblocked[dependency]:
                unblocked[dependency].append(feature["id"])
    for feature in features:
        feature["unblocks"] = unblocked[feature["id"]]
        feature["waiting_on"] = [dependency["id"] for dependency in feature["depends_on"] if dependency["status"] != "verified"]
        feature["blocked_by"] = [dependency["id"] for dependency in feature["depends_on"] if dependency["status"] == "blocked"]


def add_wave_summary(features, parsed, waves):
    """features[].wave: last wave that named the feature, who went along and why; not run yet = upcoming, with what it waits on."""
    last = {}
    for number, slugs in waves:
        for slug in slugs:
            last[slug] = (number, slugs)
    by_id = {feature["id"]: feature for feature in features}
    for feature in features:
        slug = feature["id"]
        if slug in last:
            number, slugs = last[slug]
            together = [other for other in slugs if other != slug and other in by_id]
            if together:
                members = [slug] + together
                independent = not any(b in loop_state.split_deps(parsed[a])[0] for a in members for b in members if a != b)
                paths = [[item for item in (parsed[member].get("paths") or "").split(",") if item] for member in members]
                disjoint = all(paths) and not any(are_paths_overlapping(x, y) for i in range(len(members)) for j in range(i + 1, len(members))
                                                  for x in paths[i] for y in paths[j])
                reason = WAVE_REASON_DISJOINT if (independent and disjoint) else WAVE_REASON_TOGETHER
            else:
                reason = WAVE_REASON_ALONE_REGRESSION % feature["regression"] if feature.get("regression") else WAVE_REASON_ALONE
            feature["wave"] = {"turn": number, "together_with": together, "reason": reason, "upcoming": False}
        elif feature["status"] not in CLOSED_STATUSES:
            waiting = feature["waiting_on"]
            feature["wave"] = {"turn": None, "together_with": [], "upcoming": True,
                               "reason": (WAVE_REASON_WAITING + ", ".join(waiting)) if waiting else WAVE_REASON_NO_OPEN_DEPENDENCY}
        else:
            feature["wave"] = None


class RoadmapViewService:
    def __init__(self, loop_file_repository):
        self.loop_file_repository = loop_file_repository

    def build_roadmap_view(self, work_dir, text, up_to_turn=None):
        """(stages, features, counts, graph) from the text of a ROADMAP; empty when there are no features."""
        parsed, _ = loop_state.parse_roadmap(text)
        labels = list(dict.fromkeys(fields.get("stage", "") for fields in parsed.values()))
        titles = list_stage_titles(text)
        number_by_label = {label: int(label) if label.isdigit() else index + 1 for index, label in enumerate(labels)}
        stages = []
        for label in labels:
            title = next((t for t in titles if re.match(r"Stage\s+%s\b" % re.escape(label), t, re.I)), None) if label else None
            stages.append({"n": number_by_label[label], "title": title or (STAGE_FALLBACK_TITLE % label if label else NO_STAGE_TITLE),
                           "features": [slug for slug, fields in parsed.items() if fields.get("stage", "") == label]})
        commits = self.read_commits_from_state(work_dir, list(parsed))
        features = []
        for slug, fields in parsed.items():
            slugs, folders = loop_state.split_deps(fields)
            tries = fields.get("tries", "")
            features.append({
                "id": slug, "name": self.read_feature_name(work_dir, slug, fields), "status": fields.get("status"),
                "stage": number_by_label.get(fields.get("stage", "")),
                "deps": slugs + [loop_state.DIR_PREFIX + folder for folder in folders],
                "paths": [path for path in (fields.get("paths") or "").split(",") if path],
                "kind": fields.get("kind") or "?", "tries": int(tries) if tries.isdigit() else 0, "note": fields.get("note"),
                "commit": fields.get("commit") or (commits.get(slug) if fields.get("status") == "verified" else None),
                "regression": fields.get("regression"), "e2e": fields.get("e2e"),
            })
        add_dependency_clarity(features, parsed)
        waves = self.list_snapshot_waves(work_dir, up_to_turn)
        for feature in features:
            feature["summary"] = self.build_feature_summary(work_dir, feature["id"], parsed[feature["id"]], text)
        add_wave_summary(features, parsed, waves)
        self.add_stage_summaries(work_dir, stages, features, text, waves, labels)
        counts = {status: sum(1 for feature in features if feature["status"] == status) for status in loop_state.STATUSES}
        counts["total"] = len(features)
        edges = [[dependency, slug] for slug, fields in parsed.items() for dependency in loop_state.split_deps(fields)[0] if dependency in parsed]
        return stages, features, counts, {"nodes": list(parsed), "edges": edges}

    def read_feature_name(self, work_dir, slug, fields=None):
        """Readable name of a feature: the `title="..."` of its ROADMAP line (what the user approved), else the first heading of its spec.md
        (without the `Spec:` / slug prefix), else the slug."""
        title = " ".join(str((fields or {}).get("title") or "").split())
        if title:
            return title[:FEATURE_NAME_LIMIT]
        for line in self.loop_file_repository.read_feature_spec_head(work_dir, slug, SPEC_TITLE_LINES):
            if line.startswith("# "):
                title = re.sub(r"^(Spec(ification)?\s*[:\-—]\s*)?(%s\s*[:\-—]\s*)?" % re.escape(slug), "", line[2:].strip(), flags=re.I)
                return title[:FEATURE_NAME_LIMIT] or slug
        return slug

    def read_commits_from_state(self, work_dir, slugs):
        """Commit hash of each feature, when STATE.md names `<slug> ... commit <hash>` on the same line."""
        found_commits = {}
        for line in self.loop_file_repository.read_state_file(work_dir, "STATE.md").splitlines():
            found = re.search(r"commit\s+([0-9a-f]{7,40})", line)
            if found:
                for slug in slugs:
                    if slug in line:
                        found_commits.setdefault(slug, found.group(1)[:7])
        return found_commits

    def build_feature_summary(self, work_dir, slug, fields, roadmap_text):
        """goal, size, requirements and source (spec.md | roadmap | none). No data = null."""
        size = fields.get("scope") if fields.get("scope") in SIZES else None
        spec = self.loop_file_repository.read_feature_spec(work_dir, slug)
        if spec.strip():
            sections = split_markdown_sections(spec)
            goal = None
            for wanted_title in SPEC_GOAL_TITLES:
                for title, body in sections:
                    if title and wanted_title.match(title) and extract_first_paragraph(body):
                        goal = extract_first_paragraph(body)
                        break
                if goal:
                    break
            if goal is None:
                for title, body in sections[1:]:
                    if title and not re.match(r"(?i)^metadata\b", title) and extract_first_paragraph(body):
                        goal = extract_first_paragraph(body)
                        break
            spec_size = SPEC_SIZE_PATTERN.search(spec[:SPEC_SIZE_HEAD_LENGTH])
            requirements = len({found.group(1) for found in (REQUIREMENT_LINE_PATTERN.match(line.strip()) for line in spec.splitlines()) if found})
            return {"goal": clip_redacted_text(goal, GOAL_LIMIT) if goal else None,
                    "size": spec_size.group(1).capitalize() if spec_size else size, "requirements": requirements or None, "source": SOURCE_SPEC}
        description = None
        for raw in roadmap_text.splitlines():
            line = loop_state.FEATURE_LINE.match(raw.strip())
            if line and line.group(1) == slug:
                description = re.sub(r"\b\w+=\S+", "", loop_state.TITLE_FIELD.sub("", line.group(2))).strip(" -:;|") or None
        return {"goal": clip_redacted_text(description, GOAL_LIMIT) if description else None, "size": size, "requirements": None,
                "source": SOURCE_ROADMAP if (description or size) else SOURCE_NONE}

    def list_snapshot_waves(self, work_dir, up_to_turn=None):
        """[(turn, [slugs])] from the `feature` field of the LOOP.json of each snapshots/NNN (ascending order)."""
        waves = []
        for number in self.loop_file_repository.list_snapshot_numbers(work_dir):
            if up_to_turn is not None and number > up_to_turn:
                break
            loop = self.loop_file_repository.read_cached_snapshot_loop_json(work_dir, number)
            if loop is None:
                continue
            slugs = to_text_list(loop.get("feature"))
            if slugs:
                waves.append((number, slugs))
        return waves

    def read_stage_closing(self, work_dir, number):
        text = self.loop_file_repository.read_stage_closing(work_dir, number)
        if not text.strip():
            return None
        closing = {key: None for key in STAGE_CLOSING_SECTIONS.values()}
        for title, body in split_markdown_sections(text):
            key = STAGE_CLOSING_SECTIONS.get(title)
            if key and body.strip():
                closing[key] = clip_redacted_text(body, STAGE_CLOSING_LIMIT)
        return closing

    def add_stage_summaries(self, work_dir, stages, features, text, waves, labels):
        by_id = {feature["id"]: feature for feature in features}
        descriptions = extract_stage_descriptions(text)
        for stage, label in zip(stages, labels):
            members = [by_id[slug] for slug in stage["features"]]
            current = (next((f for f in members if f["status"] in IN_PROGRESS_STATUSES), None)
                       or next((f for f in members if f["status"] not in CLOSED_STATUSES), None))
            stage_waves = [{"turn": number, "features": [slug for slug in slugs if slug in stage["features"]]} for number, slugs in waves
                           if any(slug in stage["features"] for slug in slugs)]
            description = descriptions.get(label)
            stage["summary"] = {"description": clip_redacted_text(description, STAGE_DESCRIPTION_LIMIT) if description else None,
                                "progress": {"verified": sum(1 for f in members if f["status"] == "verified"),
                                             "blocked": sum(1 for f in members if f["status"] == "blocked"), "total": len(members)},
                                "current_feature": current["id"] if current else None, "waves": stage_waves,
                                "closing": self.read_stage_closing(work_dir, stage["n"])}
