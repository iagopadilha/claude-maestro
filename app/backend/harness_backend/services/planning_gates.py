"""Planning gates: what must be true before "Approve roadmap" is allowed (checked by the back end on approve and shown by
the planning screen). Pure functions over the facts `ProjectProgressService.read_project_facts` already reads.

Order and ids are a contract with the front end: git, structure, conformance, map, repos (only when an extra repo is not set up), goal (the PROJECT.md context gate), features, fields,
open_questions, flows. The first four are the PREREQUISITES of the planning chat (it refuses to write without them).
"""
GATE_GIT = "git"
GATE_STRUCTURE = "structure"
GATE_CONFORMANCE = "conformance"
GATE_MAP = "map"
GATE_REPOS = "repos"  # extra repos of a multi-repo project: each needs a Conformance line and (with code) a codebase map; not a prerequisite
GATE_GOAL = "goal"  # id kept; it now checks the short PROJECT.md (the subagents' context: only the Vision is filled in)
GATE_FEATURES = "features"
GATE_FIELDS = "fields"
GATE_OPEN_QUESTIONS = "open_questions"
GATE_FLOWS = "flows"
GATE_TEST_ENV = "test_environment"
PREREQUISITE_GATES = (GATE_GIT, GATE_STRUCTURE, GATE_CONFORMANCE, GATE_MAP)
FILL_IN_MARK = "FILL_IN"
ITEMS_SHOWN = 12
ITEM_TEXT_LIMIT = 200


MAP_JOB_TYPE = "map"
# what each document of `docs/codebase` (the brownfield map) is for, shown next to its name
MAP_DOCUMENT_PURPOSE = {
    "STACK.md": "languages, frameworks, versions and main dependencies",
    "ARCHITECTURE.md": "layers, patterns and how data flows",
    "CONVENTIONS.md": "naming, code style and error handling in use",
    "STRUCTURE.md": "folder layout and where each kind of code lives",
    "TESTING.md": "how tests are written and run",
    "INTEGRATIONS.md": "external APIs, databases, queues and authentication",
    "CONCERNS.md": "tech debt, risks and fragile areas",
}


def describe_map_documents(facts):
    """`N document(s) in docs/codebase, mapped at <sha>` (the folder is `.specs/codebase` for a project mapped before the move)."""
    detail = "%d document(s) in %s" % (len(facts["map_files"]), facts.get("map_folder") or "docs/codebase")
    return detail + (", mapped at %s" % facts["map_commit"] if facts.get("map_commit") else "")


def describe_map_files(map_files):
    """One line per written map document: `NAME.md: what it is for`."""
    return ["%s: %s" % (name, MAP_DOCUMENT_PURPOSE[name]) if name in MAP_DOCUMENT_PURPOSE else name for name in map_files]


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
    map_files = facts["map_files"]
    env_problems = [friendly_environment_problem(problem) for problem in facts.get("environment_all_problems") or []]
    running_job = facts.get("running_job")
    map_running = bool(running_job and running_job.get("type") == MAP_JOB_TYPE)  # the gate passes only when the job ended
    unready_repos = facts.get("repos_unready") or []
    gates = [
        create_gate(GATE_GIT, "Git repository", facts["is_git"],
                    None if facts["is_git"] else "the folder is not a git repository (the loop needs git)"),
        create_gate(GATE_STRUCTURE, ".specs/ structure", structure["ok"],
                    None if structure["ok"] else "initialize the .specs/ structure", structure["missing"]),
        create_gate(GATE_CONFORMANCE, "Conformance in CLAUDE.md", facts["conformance"],
                    "Conformance: %s" % facts["conformance"] if facts["conformance"] else "choose STANDARD or LEGACY"),
        create_gate(GATE_MAP, "Codebase map", map_files and not map_running,
                    "mapping now: %d document(s) written so far" % len(map_files) if map_running
                    else describe_map_documents(facts) if map_files
                    else "repo with code and no map: map it before planning" if has_code else "repo without code: nothing to map",
                    describe_map_files(map_files), optional=not has_code),
        create_gate(GATE_GOAL, "Project context (PROJECT.md)", project_text is not None and project_text.strip() and not fill_in_lines,
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
        create_gate(GATE_TEST_ENV, "Test environment (credentials and limits)", not env_problems,
                    describe_test_environment(env_problems, facts.get("regression_required")), env_problems,
                    optional=True),  # never blocks the approval: a regression feature makes the loop preflight (Check) require it
    ]
    if unready_repos:  # only a multi-repo project with an extra repo that is not set up yet gets this gate (right after the map)
        gates.insert(next(index for index, gate in enumerate(gates) if gate["id"] == GATE_MAP) + 1,
                     create_gate(GATE_REPOS, "Other repositories set up", False,
                                 "%d repository(ies) without the minimum setup (Conformance + codebase map)" % len(unready_repos),
                                 [repo["detail"] for repo in unready_repos]))
    return gates


def friendly_environment_problem(problem):
    """The engine preflight words its messages for the regression/e2e case; here the file may simply not exist yet."""
    if "is missing" in problem:
        return "TEST-ENVIRONMENT.md does not exist yet: initialize the .specs/ structure (or send a message in the chat) and the app creates it from its template"
    return problem


def describe_test_environment(problems, required):
    """Why the test environment passes or not. Never blocks the approval; with a regression feature the loop preflight (Check)
    refuses to start without it. Without it the loop only validates by build and unit tests, never by running the real app."""
    if not problems:
        return "complete: where it runs, credential NAMES, test users and what is forbidden are written"
    prefix = "needed before the loop starts (a feature asks for regression or the ROADMAP says E2E: yes): " if required else "optional (no regression feature and E2E: no): "
    tail = " (values: this project's .env.test first, then the machine defaults in ~/.loop-harness/.env)" if required else ""
    return prefix + problems[0] + tail


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


def describe_gates_for_ai(gates):
    """Plain-text status block sent to the planning AI before every user message: what passes and what is missing."""
    lines = ["[Planning status from the app, read just now: the source of truth, newer than your memory. Fix what is MISSING in the "
             "planning documents; do not ask the user for what is already OK.]"]
    for gate in gates:
        mark = "OK" if gate["ok"] else "optional" if gate["optional"] else "MISSING"
        detail = " - %s" % gate["detail"] if gate["detail"] else ""
        lines.append("- [%s] %s: %s%s" % (mark, gate["id"], gate["label"], detail))
        if not gate["ok"]:
            lines.extend("    * %s" % item for item in gate["items"])
    return "\n".join(lines)
