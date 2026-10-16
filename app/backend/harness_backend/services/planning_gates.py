"""Planning gates: what must be true before "Approve roadmap" is allowed (checked by the back end on approve and shown by
the planning screen). Pure functions over the facts `ProjectProgressService.read_project_facts` already reads.

Order and ids are a contract with the front end: git, structure, conformance, map, goal, features, fields,
open_questions, flows. The first four are the PREREQUISITES of the planning chat (it refuses to write without them).
"""
GATE_GIT = "git"
GATE_STRUCTURE = "structure"
GATE_CONFORMANCE = "conformance"
GATE_MAP = "map"
GATE_GOAL = "goal"
GATE_FEATURES = "features"
GATE_FIELDS = "fields"
GATE_OPEN_QUESTIONS = "open_questions"
GATE_FLOWS = "flows"
PREREQUISITE_GATES = (GATE_GIT, GATE_STRUCTURE, GATE_CONFORMANCE, GATE_MAP)
FILL_IN_MARK = "FILL_IN"
ITEMS_SHOWN = 12
ITEM_TEXT_LIMIT = 200


def create_gate(gate_id, label, ok, detail=None, items=None, optional=False):
    return {"id": gate_id, "label": label, "ok": bool(ok), "optional": bool(optional), "detail": detail,
            "items": [str(item)[:ITEM_TEXT_LIMIT] for item in (items or [])][:ITEMS_SHOWN]}


def list_fill_in_lines(text):
    return [line.strip() for line in (text or "").splitlines() if FILL_IN_MARK in line]


def build_planning_gates(facts):
    """[gate] in the contract order. `facts` = ProjectProgressService.read_project_facts(context)."""
    structure = facts["structure"]
    has_code = facts["has_code"]
    project_text = facts["project_text"]
    fill_in_lines = list_fill_in_lines(project_text)
    features = facts["features"]
    parse_errors, field_errors = facts["roadmap_parse_errors"], facts["roadmap_field_errors"]
    questions = facts["open_questions"]
    coverage = facts["flow_coverage"]
    return [
        create_gate(GATE_GIT, "Git repository", facts["is_git"],
                    None if facts["is_git"] else "the folder is not a git repository (the loop needs git)"),
        create_gate(GATE_STRUCTURE, ".specs/ structure", structure["ok"],
                    None if structure["ok"] else "initialize the .specs/ structure", structure["missing"]),
        create_gate(GATE_CONFORMANCE, "Conformance in CLAUDE.md", facts["conformance"],
                    "Conformance: %s" % facts["conformance"] if facts["conformance"] else "choose STANDARD or LEGACY"),
        create_gate(GATE_MAP, "Codebase map", facts["map_files"],
                    "%d document(s) in .specs/codebase" % len(facts["map_files"]) if facts["map_files"]
                    else "repo with code and no map: map it before planning" if has_code else "repo without code: nothing to map",
                    optional=not has_code),
        create_gate(GATE_GOAL, "Project goal (PROJECT.md)", project_text is not None and project_text.strip() and not fill_in_lines,
                    "PROJECT.md missing" if project_text is None
                    else "%d line(s) still with FILL_IN" % len(fill_in_lines) if fill_in_lines else "filled in", fill_in_lines),
        create_gate(GATE_FEATURES, "Roadmap features", facts["roadmap_exists"] and features and not parse_errors,
                    "ROADMAP.md missing" if not facts["roadmap_exists"] else "outside the grammar" if parse_errors
                    else "%d feature(s)" % len(features) if features else "ROADMAP.md has no feature line", parse_errors),
        create_gate(GATE_FIELDS, "Feature fields", not field_errors, "%d error(s)" % len(field_errors) if field_errors else None,
                    field_errors),
        create_gate(GATE_OPEN_QUESTIONS, "Open questions answered", not questions,
                    "%d open question(s) in the ROADMAP" % len(questions) if questions else None, questions),
        create_gate(GATE_FLOWS, "A flow for every feature (FLOWS.md)",
                    facts["flows_exists"] and features and not coverage["missing"],
                    describe_flows(facts, coverage), coverage["missing"]),
    ]


def describe_flows(facts, coverage):
    if not facts["flows_exists"]:
        return "FLOWS.md missing"
    parts = []
    if coverage["missing"]:
        parts.append("%d feature(s) without a flow of 2+ steps" % len(coverage["missing"]))
    elif facts["features"]:
        parts.append("every feature has a flow")
    else:
        parts.append("no feature in the ROADMAP yet")
    if coverage["orphans"]:
        parts.append("warning: flows of unknown features: %s" % ", ".join(coverage["orphans"][:ITEMS_SHOWN]))
    return "; ".join(parts) or None


def are_prerequisites_ok(gates):
    return all(gate["ok"] or gate["optional"] for gate in gates if gate["id"] in PREREQUISITE_GATES)


def are_gates_ok(gates):
    return all(gate["ok"] or gate["optional"] for gate in gates)
