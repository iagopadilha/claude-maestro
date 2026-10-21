"""Planning gates: what must be true before "Approve roadmap" is allowed (checked by the back end on approve and shown by
the planning screen). Pure functions over the facts `ProjectProgressService.read_project_facts` already reads.

Order and ids are a contract with the front end: git, structure, subprojects (only for a detected monorepo or an unreadable
SUBPROJECTS.json), conformance, map, repos (only when an extra repo is not set up), goal (the PROJECT.md context gate), features, fields,
open_questions, flows, test_environment, ready (not optional; only when the facts carry `readiness`, the cached result of
`readiness_service`). The first four are the PREREQUISITES of the planning chat (it refuses to write without them).
"""
import json
import os

from ..domain.import_coverage import check_coverage, parse_coverage
from ..domain.roadmap_settings import DEFAULT_BASE_BRANCH, LOOP_SETTING_HEADER_NAMES, parse_loop_settings
from .loop_worktree_service import get_environment_integration_branch

GATE_GIT = "git"
GATE_STRUCTURE = "structure"
GATE_SUBPROJECTS = "subprojects"  # monorepo only: the list of subprojects must be confirmed before Conformance and the map
GATE_CONFORMANCE = "conformance"
GATE_MAP = "map"
GATE_REPOS = "repos"  # extra repos of a multi-repo project: each needs a Conformance line and (with code) a codebase map; not a prerequisite
GATE_GOAL = "goal"  # id kept; it now checks the short PROJECT.md (the subagents' context: only the Vision is filled in)
GATE_FEATURES = "features"
GATE_FIELDS = "fields"
GATE_OPEN_QUESTIONS = "open_questions"
GATE_FLOWS = "flows"
GATE_TEST_ENV = "test_environment"
GATE_IMPORT_COVERAGE = "import_coverage"  # only after a spec import was sent: every source item is in IMPORT-COVERAGE.md; not optional
IMPORT_COVERAGE_MISSING = "IMPORT-COVERAGE.md missing"
GATE_READY = "ready"  # last: the cached readiness check (`readiness_service`); only present when the facts carry `readiness`
PREREQUISITE_GATES = (GATE_GIT, GATE_STRUCTURE, GATE_SUBPROJECTS, GATE_CONFORMANCE, GATE_MAP)  # `subprojects` exists only for a monorepo (or an unreadable SUBPROJECTS.json)
FILL_IN_MARK = "FILL_IN"
READINESS_NOT_RUN = "readiness check did not run"
REPOS_REJECTED_PARTS = (".specs", "project", "loop", "internal", "planning", "repos_rejected.json")
ITEMS_SHOWN = 12
ITEM_TEXT_LIMIT = 1200  # long enough for the uncommitted-files warning (12 names + "and N more")
SUBPROJECTS_UNREADABLE = "SUBPROJECTS.json unreadable"
SUBPROJECTS_BLOCKED = "blocked: confirm the list of subprojects first"


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
    gates = build_code_prerequisite_gates(facts)
    gates.extend(build_repos_gates(facts))  # right after the map
    gates.extend(build_plan_gates(facts))
    gates.extend(build_import_coverage_gates(facts))  # only after a spec import was sent
    gates.extend(build_readiness_gates(facts))
    return gates


def read_subproject_state(facts):
    """(detected, confirmed, active, error) from `facts["subprojects"]` (`SubprojectsService.describe`): a detected monorepo, its
    confirmation, the non-skipped subprojects and the read error of SUBPROJECTS.json. A file that cannot be read counts as a single
    project (not detected) and only the `subprojects` gate reports it."""
    described = facts.get("subprojects") or {}
    detected = described.get("monorepo") is True
    confirmed = detected and described.get("confirmed") is True
    active = [project for project in described.get("projects") or [] if not project.get("skip")] if detected else []
    return detected, confirmed, active, str(described.get("error") or "")


def is_confirmed_monorepo(facts):
    return read_subproject_state(facts)[1]


def describe_subproject_line(project):
    return "%s (%s)%s" % (project.get("path"), project.get("stack") or "unknown", "" if project.get("has_code") else ": no code yet")


def build_subprojects_gates(facts):
    """[subprojects] for a detected monorepo (fails until the list is confirmed) or an unreadable SUBPROJECTS.json, else []."""
    detected, confirmed, active, error = read_subproject_state(facts)
    if error:
        return [create_gate(GATE_SUBPROJECTS, "Subprojects", False, SUBPROJECTS_UNREADABLE, [error])]
    if not detected:
        return []
    skipped = [project for project in facts["subprojects"].get("projects") or [] if project.get("skip")]
    items = [describe_subproject_line(project) for project in active]
    items += ["%s: skipped%s" % (project.get("path"), " (%s)" % project["reason"] if project.get("reason") else "") for project in skipped]
    return [create_gate(GATE_SUBPROJECTS, "Subprojects", confirmed,
                        "%d subproject(s) confirmed" % len(active) if confirmed
                        else "confirm the list of %d subproject(s) before Conformance and the map" % len(active), items)]


def build_subproject_conformance_gate(facts):
    """Aggregate: one line per non-skipped subproject; ok when every one has its `Conformance:` line."""
    _, confirmed, active, _ = read_subproject_state(facts)
    if not confirmed:
        return create_gate(GATE_CONFORMANCE, "Conformance per subproject", False, SUBPROJECTS_BLOCKED)
    items = ["%s: %s" % (project["path"], project.get("conformance") or "missing") for project in active]
    missing = [project["path"] for project in active if not project.get("conformance")]
    return create_gate(GATE_CONFORMANCE, "Conformance per subproject", not missing,
                       "%d of %d subproject(s) with Conformance" % (len(active) - len(missing), len(active)) if missing
                       else "every subproject has a Conformance line", items)


def build_subproject_map_gate(facts):
    """Aggregate: ok when every non-skipped subproject with code has a codebase map; optional when none has code."""
    _, confirmed, active, _ = read_subproject_state(facts)
    if not confirmed:
        return create_gate(GATE_MAP, "Codebase map per subproject", False, SUBPROJECTS_BLOCKED)
    running_job = facts.get("running_job")
    map_running = bool(running_job and running_job.get("type") == MAP_JOB_TYPE)
    with_code = [project for project in active if project.get("has_code")]

    def map_documents(project):
        return (project.get("map") or {}).get("files") or []

    items = ["%s: %s" % (project["path"], "%d document(s)" % len(map_documents(project)) if map_documents(project)
                         else "no map" if project.get("has_code") else "no code") for project in active]
    missing = [project["path"] for project in with_code if not map_documents(project)]
    mapped = [project for project in active if map_documents(project)]
    system = (facts.get("subprojects") or {}).get("system")  # only present where the system map status is attached
    system_pending = bool(isinstance(system, dict) and not missing and len(mapped) >= 2 and not system.get("exists")
                          and ((facts["subprojects"].get("map_queue") or {}).get("system") or {}).get("state") != "done")
    detail = ("mapping now" if map_running
              else "the system map is not made yet" if system_pending
              else "%d of %d subproject(s) with code are mapped" % (len(with_code) - len(missing), len(with_code)) if missing
              else "every subproject with code has a map" if with_code else "no subproject has code: nothing to map")
    return create_gate(GATE_MAP, "Codebase map per subproject", not missing and not map_running and not system_pending, detail, items,
                       optional=not with_code and not system_pending)


def build_code_prerequisite_gates(facts):
    """[git, structure, (subprojects), conformance, map]: the PREREQUISITES of the planning chat (see PREREQUISITE_GATES). For a
    detected monorepo Conformance and the map are per subproject (the root Conformance and the root map are not asked for)."""
    structure = facts["structure"]
    has_code = facts["has_code"]
    map_files = facts["map_files"]
    running_job = facts.get("running_job")
    map_running = bool(running_job and running_job.get("type") == MAP_JOB_TYPE)  # the gate passes only when the job ended
    gates = [
        create_gate(GATE_GIT, "Git repository", facts["is_git"],
                    None if facts["is_git"] else "the folder is not a git repository (the loop needs git)"),
        create_gate(GATE_STRUCTURE, ".specs/ structure", structure["ok"],
                    None if structure["ok"] else "initialize the .specs/ structure", structure["missing"]),
    ]
    gates.extend(build_subprojects_gates(facts))
    if read_subproject_state(facts)[0]:
        gates.extend([build_subproject_conformance_gate(facts), build_subproject_map_gate(facts)])
        return gates
    gates.extend([
        create_gate(GATE_CONFORMANCE, "Conformance in CLAUDE.md", facts["conformance"],
                    "Conformance: %s" % facts["conformance"] if facts["conformance"] else "choose STANDARD or LEGACY"),
        create_gate(GATE_MAP, "Codebase map", map_files and not map_running,
                    "mapping now: %d document(s) written so far" % len(map_files) if map_running
                    else describe_map_documents(facts) if map_files
                    else "repo with code and no map: map it before planning" if has_code else "repo without code: nothing to map",
                    describe_map_files(map_files), optional=not has_code),
    ])
    return gates


def build_repos_gates(facts):
    """[repos] for every extra EDIT repository that is not set up yet (blocks), or, when only reference repositories lack a map, an
    optional passing gate whose items are WARNINGs (a reference repository never blocks). [] when there is nothing to say."""
    unready_repos = facts.get("repos_unready") or []
    warnings = ["warning: %s" % text for text in facts.get("repos_reference_warnings") or []]
    if not unready_repos:
        return [create_gate(GATE_REPOS, "Other repositories set up", True, "%d warning(s) about reference repositories" % len(warnings), warnings,
                            optional=True)] if warnings else []
    return [create_gate(GATE_REPOS, "Other repositories set up", False,
                        "%d repository(ies) without the minimum setup (Conformance + codebase map)" % len(unready_repos),
                        [repo["detail"] for repo in unready_repos] + warnings)]


def build_plan_gates(facts):
    """[goal, features, fields, open_questions, flows, test_environment]: the state of the planning documents."""
    project_text = facts["project_text"]
    fill_in_lines = list_fill_in_lines(project_text)
    features = facts["features"]
    parse_errors, field_errors = facts["roadmap_parse_errors"], facts["roadmap_field_errors"]
    questions = facts["open_questions"]
    coverage = facts["flow_coverage"]
    env_problems = [friendly_environment_problem(problem) for problem in facts.get("environment_all_problems") or []]
    return [
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


def check_sent_imports_coverage(facts):
    """[{covered, dropped, missing}] of every SENT import (newest first) against `facts["import_coverage_text"]`; [] without one."""
    coverage = parse_coverage(facts.get("import_coverage_text") or "")
    return [check_coverage(meta.get("items") or {}, coverage) for meta in facts.get("imports") or [] if meta.get("sent_at")]


def build_import_coverage_gates(facts):
    """[import_coverage] when a spec import was sent (nothing otherwise); not optional: ok only when no source item is missing from
    `.specs/project/IMPORT-COVERAGE.md`. Items: `missing: <id>` first, then `dropped: <item> - <reason>` (shown, never blocking)."""
    results = check_sent_imports_coverage(facts)
    if not results:
        return []
    if not (facts.get("import_coverage_text") or "").strip():
        return [create_gate(GATE_IMPORT_COVERAGE, "Import coverage (IMPORT-COVERAGE.md)", False, IMPORT_COVERAGE_MISSING,
                            [IMPORT_COVERAGE_MISSING])]
    covered = sum(len(result["covered"]) for result in results)
    dropped = [entry for result in results for entry in result["dropped"]]
    missing = [item for result in results for item in result["missing"]]
    items = ["missing: %s" % item for item in missing]
    items += ["dropped: %s — %s" % (entry["item"], entry["reason"] or "no reason given") for entry in dropped]
    detail = "%d covered, %d dropped, %d missing" % (covered, len(dropped), len(missing))
    return [create_gate(GATE_IMPORT_COVERAGE, "Import coverage (IMPORT-COVERAGE.md)", not missing, detail, items)]


def build_readiness_gates(facts):
    """[ready] only when the facts carry the cached readiness result, else []. Invalid loop settings of the ROADMAP header are added
    as `warning:` items (never a problem: the loop starts with the default of each one)."""
    if facts.get("readiness") is None:
        return []
    gate = create_readiness_gate(facts["readiness"])
    gate["items"] = gate["items"] + ["warning: %s" % item for item in describe_loop_settings_problems(read_roadmap_loop_settings(facts.get("work_dir")))]
    return [gate]


def read_roadmap_loop_settings(work_dir):
    """`parse_loop_settings` of `<work_dir>/.specs/project/ROADMAP.md` (a missing file = every default, no problem)."""
    try:
        with open(os.path.join(work_dir, ".specs", "project", "ROADMAP.md"), encoding="utf-8", errors="replace") as handle:
            return parse_loop_settings(handle.read())
    except (OSError, TypeError):
        return parse_loop_settings("")


def describe_loop_settings_problems(loop_settings):
    return ["ROADMAP setting \"%s: %s\" is invalid (%s)" % (item["name"], item["value"], item["message"]) for item in loop_settings["problems"]]


def format_loop_setting_value(key, value):
    if isinstance(value, bool):
        return "yes" if value else "no"
    if key == "branch":
        return value or "automatic"
    if key == "setup":
        return value or "(none)"
    if key in ("budget_turn", "budget_loop"):
        return "US$ %s" % (int(value) if float(value).is_integer() else value)
    return str(value)


def resolve_base_branch(loop_settings):
    """{"name", "source": "roadmap|env|default"}: the ROADMAP `Branch base` line (valid), then LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH, then
    `developer`. Same order as the engine; the panel payload (`loop_control_service.get_loop_settings`) adds it as `base_branch_resolved`."""
    invalid_names = {item["name"] for item in loop_settings["problems"]}
    if "base_branch" not in loop_settings["missing"] and LOOP_SETTING_HEADER_NAMES["base_branch"] not in invalid_names:
        return {"name": loop_settings["settings"]["base_branch"], "source": "roadmap"}
    name = get_environment_integration_branch()
    return {"name": name, "source": "default" if name == DEFAULT_BASE_BRANCH else "env"}


def describe_loop_settings_for_ai(loop_settings):
    """The loop settings as the loop will read them (the ROADMAP header; a missing line shows the default), plus one WARNING per
    invalid value. The AI sets them in the ROADMAP header with the exact names; the Start dialog only shows them."""
    settings, problems = loop_settings["settings"], loop_settings["problems"]
    invalid_names = {item["name"] for item in problems}
    resolved_base = resolve_base_branch(loop_settings)
    if "base_branch" in loop_settings["missing"] or LOOP_SETTING_HEADER_NAMES["base_branch"] in invalid_names:
        settings = dict(settings, base_branch=get_environment_integration_branch())  # no usable line: the environment, then developer (as the engine resolves it)
    lines = ["- [OK] loop_settings: decided here in planning (ROADMAP header lines `**Name:** value`), read by the loop at Start; a missing line = the default"]
    for key, header_name in LOOP_SETTING_HEADER_NAMES.items():
        mark = " (invalid in the ROADMAP: the default is used)" if header_name in invalid_names else ""
        lines.append("    * %s: %s%s" % (header_name, format_loop_setting_value(key, settings[key]), mark))
    lines.append("    * base_branch_resolved: %s (source: %s)" % (resolved_base["name"], resolved_base["source"]))
    lines.append("    * Branch base: by default the loop branches from `developer`. To change it, write `Branch base: main` (or `release/2.0`) in the "
                 "ROADMAP header. Order: ROADMAP, then LOOP_INTEGRATION_BRANCH, then developer. The PR goes to the same branch.")
    lines.append("    * Max parallel tasks: executors per wave inside one feature (1..5, default 3)")
    lines.extend("- [WARNING] loop_settings: %s" % text for text in describe_loop_settings_problems(loop_settings))
    return lines


def create_readiness_gate(readiness):
    """The `ready` gate from a readiness result ({state, at, problems, warnings, error?}); its reason is the first problem,
    "readiness check did not run" or "stale"."""
    state = readiness.get("state")
    problems, warnings = list(readiness.get("problems") or []), list(readiness.get("warnings") or [])
    if state == "ready":
        detail = "ready at %s" % readiness.get("at") + ("; %d warning(s)" % len(warnings) if warnings else "")
    elif state == "problems" and problems:
        detail = problems[0] + (" (+%d more)" % (len(problems) - 1) if len(problems) > 1 else "")
    elif state == "running":
        detail = "the readiness check is running"
    elif state == "error":
        detail = READINESS_NOT_RUN + (": %s" % readiness["error"] if readiness.get("error") else "")
    else:
        detail = "stale: the plan changed since the last readiness check (run it again)" if readiness.get("at")             else "stale: the readiness check has not run yet"
    return create_gate(GATE_READY, "Ready to run the loop", state == "ready", detail,
                       problems + ["warning: %s" % item for item in warnings])


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


def load_rejected_repos(work_dir):
    """[{path, reason}] of the last repository suggestions the app refused (`repos_rejected.json`); tolerant: anything odd = none."""
    try:
        with open(os.path.join(work_dir, *REPOS_REJECTED_PARTS), encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    if isinstance(data, dict):
        data = data.get("rejected")
    return [{"path": str(item.get("path") or ""), "reason": str(item.get("reason") or "")}
            for item in data if isinstance(item, dict) and item.get("path")] if isinstance(data, list) else []


INVENTORY_PARTS = (".specs", "project", "loop", "internal", "inventory")
SYSTEM_DOCUMENT_PARTS = ("docs", "codebase", "SYSTEM.md")
GIANT_FILES_SHOWN = 5
GIANT_INSTRUCTION = ("Giant files (past the line limit) are risky to change: when a feature would touch one, add a `refactor-*` open question "
                     "with a recommended answer (split the file first); when the user declines, write `note=giant-accepted` on that feature.")


def read_root_giants(work_dir):
    """Giant files of the last root inventory (`_root.json`): (count, [{path, lines}] top 5). (0, []) when there is no inventory."""
    if not work_dir:
        return 0, []
    try:
        with open(os.path.join(work_dir, *INVENTORY_PARTS, "_root.json"), encoding="utf-8") as handle:
            document = json.load(handle)
        giants = [item for item in document.get("giant") or [] if isinstance(item, dict) and item.get("path")]
    except (OSError, ValueError, AttributeError, TypeError):
        return 0, []
    return len(giants), [{"path": item["path"], "lines": item.get("lines", 0)} for item in giants[:GIANT_FILES_SHOWN]]


def format_giant_files(giants):
    return ", ".join("%s (%s lines)" % (item.get("path"), item.get("lines")) for item in giants[:GIANT_FILES_SHOWN])


def describe_root_giants_for_ai(work_dir):
    """Lines for a single project: its giant files from the last root inventory (none when there is no inventory)."""
    count, top = read_root_giants(work_dir)
    if not count:
        return []
    return ["[Giant files]", "- %d giant file(s): %s" % (count, format_giant_files(top)), GIANT_INSTRUCTION]


def describe_subprojects_for_ai(described, work_dir=None):
    """Lines of the planning status block for a detected monorepo (path, stack, Conformance, map) and the rule for the ROADMAP `paths=`;
    [] for a single project (nothing is added to what the AI reads today)."""
    if not described or described.get("monorepo") is not True:
        return describe_root_giants_for_ai(work_dir)
    confirmed = described.get("confirmed") is True
    projects = described.get("projects") or []
    lines = ["[Subprojects of this monorepo%s]" % ("" if confirmed else " (list NOT confirmed yet: the user confirms it in the app)")]
    for project in projects:
        if project.get("skip"):
            lines.append("- %s: skipped%s" % (project.get("path"), " (%s)" % project["reason"] if project.get("reason") else ""))
            continue
        map_files = (project.get("map") or {}).get("files") or []
        map_state = "map: %d document(s)" % len(map_files) if map_files else "map: missing" if project.get("has_code") else "map: no code"
        lines.append("- %s (stack %s): Conformance %s, %s" % (project.get("path"), project.get("stack") or "unknown",
                                                                project.get("conformance") or "missing", map_state))
        giant_count = project.get("giant_files") or 0
        if giant_count:
            lines.append("    giant files: %d - %s" % (giant_count, format_giant_files(project.get("giant") or [])))
    if confirmed:
        example = next((project["path"] for project in projects if not project.get("skip")), "backend")
        lines.append("In the ROADMAP write every feature's `paths=` with the subproject folder as the prefix (for example `%s/...`), "
                     "never a path relative to the repository root without it." % example)
    if work_dir:
        system_exists = os.path.isfile(os.path.join(work_dir, *SYSTEM_DOCUMENT_PARTS))
        lines.append("docs/codebase/SYSTEM.md (system map): %s" % ("exists" if system_exists else "missing"))
    if any((project.get("giant_files") or 0) for project in projects):
        lines.append(GIANT_INSTRUCTION)
    return lines


def describe_gates_for_ai(gates, work_dir=None, rejected_repos=None, subprojects=None, loop_settings=None):
    """Plain-text status block sent to the planning AI before every user message: what passes and what is missing. With `work_dir`
    (or `rejected_repos`) it also lists the repository suggestions the app refused, so the AI fixes them. `loop_settings` (the result of
    `parse_loop_settings`) adds the loop settings block with a WARNING per invalid value."""
    lines = ["[Planning status from the app, read just now: the source of truth, newer than your memory. Fix what is MISSING in the "
             "planning documents; do not ask the user for what is already OK.]"]
    for gate in gates:
        mark = "OK" if gate["ok"] else "optional" if gate["optional"] else "MISSING"
        detail = " - %s" % gate["detail"] if gate["detail"] else ""
        lines.append("- [%s] %s: %s%s" % (mark, gate["id"], gate["label"], detail))
        if not gate["ok"]:
            lines.extend("    * %s" % item for item in gate["items"])
    rejected = rejected_repos if rejected_repos is not None else load_rejected_repos(work_dir) if work_dir else []
    if rejected:
        lines.append("- [MISSING] repos_rejected: the last repository suggestions were NOT applied; fix REPOS-SUGGESTED.json")
        lines.extend("    * %s: %s" % (item["path"], item["reason"]) for item in rejected[:ITEMS_SHOWN])
    if loop_settings is not None:
        lines.extend(describe_loop_settings_for_ai(loop_settings))
    lines.extend(describe_subprojects_for_ai(subprojects, work_dir))
    return "\n".join(lines)
