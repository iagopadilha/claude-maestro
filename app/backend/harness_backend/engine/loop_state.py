#!/usr/bin/env python3
"""Typed state of the orchestrator graph: grammar, legal transitions, invariants, snapshots and rewind. Stdlib only, no AI.

ROADMAP.md is the "graph state": each feature is a node with a status; each status change is an edge. This module guards
the edges (the orchestrator is the supervisor that decides; here we deterministically check what it wrote).

Usage (CLI):
  python loop_state.py validate <folder>                 # checks the current ROADMAP.md grammar and LOOP.json
  python loop_state.py migrate <folder>                  # old layouts -> .specs/project/loop/ (idempotent; run_loop.py already calls it)
  python loop_state.py disjoint <folder> slug1 slug2 ... # are the PATHS of these features disjoint? (exit 0 yes, 1 conflict)
  python loop_state.py graph <folder>                    # state graph in Mermaid, with the features in each node
  python loop_state.py loop <folder> --status S --turn N [--feature a,b] [--reason T] [--next T] [--options "a|b"] [--to-check "x|y"]
                                                    # WRITES a validated .specs/project/loop/json/LOOP.json (enum, turn, real time). Use this instead of hand-writing the JSON
  python loop_state.py next <folder> [--wave N] [--headless]  # JSON: next phase and the features it must dispatch (deterministic scheduler)

Feature line grammar (ROADMAP.md):
  - [slug] status=<pending|speccing|specced|approved|building|verified|blocked> scope=<Small|Medium|Large|Complex>
    kind=<back|front|both|generic> paths=<p1,p2> [deps=<slug|dir:folder,...>] [isolation=same-tree|worktree]
    [tries=<n>] [origin=adopted] [e2e=ok|failed|not-covered] [regression=yes|baseline|ok|failed] [note=...]
`regression` only exists on refactoring features (slug `refactor-*`) and only when the user asked for it in the ROADMAP: `yes` = requested,
`baseline` = old code captured, `ok`/`failed` = comparison result. A feature with `regression` runs ALONE in the wave.
`deps` = the feature's dependencies, comma-separated. `slug` = another feature that must be `verified` before the
feature runs. `dir:<folder>` = shared folder the feature IMPORTS without editing (editing goes in `paths`): it collides
with whoever EDITS that folder, not with whoever only imports it. A dep slug must exist and cannot form a cycle.
`tries` = accumulated failures of the feature (gate, execution, verification or E2E). With 2 failures it MUST be `blocked`.
Stages: a heading `## Stage N` above the feature lines (or an explicit `stage=` field on the line).
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

STATUSES = ("pending", "speccing", "specced", "approved", "building", "verified", "blocked")
SCOPES = ("Small", "Medium", "Large", "Complex")
KINDS = ("back", "front", "both", "generic")
E2E = ("ok", "failed", "not-covered")
ISOLATIONS = ("same-tree", "worktree")
REGRESSION = ("yes", "baseline", "ok", "failed")
REFACTOR_PREFIX = "refactor-"
DIR_PREFIX = "dir:"
MAX_TRIES = 2
LOOP_STATUS = ("continue", "awaiting_user", "done", "error")
ANSWERS_OK = ("approved", "specced", "pending")  # legal destinations of a `blocked` feature unblocked by the user

# Graph edges (the supervisor decides which to follow). Within one turn the orchestrator may chain several.
FORWARD = {
    "pending": ("speccing", "specced"),
    "speccing": ("specced",),
    "specced": ("approved",),
    "approved": ("building",),
    "building": ("verified",),
    "verified": (),
}
BACK = {  # retry: lost dispatch, gate/verifier/E2E sent the feature back
    "speccing": ("pending",),
    "building": ("approved",),
    "verified": ("approved",),  # E2E failed in the final phase
    "specced": ("specced",),    # gate BLOCKED goes back to the spec-writer and the spec stays specced
}
FEATURE_LINE = re.compile(r"^- \[([^\]]+)\]\s*(.*)$")
STAGE_LINE = re.compile(r"^#{1,6}\s*Stage\s+(\S+)", re.I)


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


# ---------------------------------------------------------------- parse
def parse_roadmap(text):
    """Returns (features, errors). features: {slug: {field: value}} in file order."""
    features, errors, stage = {}, [], ""
    for number, raw in enumerate(text.splitlines(), 1):
        heading = STAGE_LINE.match(raw.strip())
        if heading:
            stage = heading.group(1)
            continue
        match = FEATURE_LINE.match(raw.strip())
        if not match:
            continue
        slug, rest = match.group(1), match.group(2)
        fields = dict(re.findall(r"(\w+)=(\S+)", rest))
        if "status" not in fields:
            errors.append("ROADMAP line %d: [%s] has no status=" % (number, slug))
            continue
        if slug in features:
            errors.append("ROADMAP line %d: duplicate slug [%s]" % (number, slug))
            continue
        fields.setdefault("stage", stage)  # the stage comes from the `## Stage N` heading above (a stage= field on the line wins)
        features[slug] = fields
    return features, errors


def check_fields(features):
    errors = []
    for slug, f in features.items():
        if f["status"] not in STATUSES:
            errors.append("[%s] invalid status %r (valid: %s)" % (slug, f["status"], ", ".join(STATUSES)))
        if f.get("scope") and f["scope"] not in SCOPES:
            errors.append("[%s] invalid scope %r" % (slug, f["scope"]))
        if f.get("kind") and f["kind"] not in KINDS:
            errors.append("[%s] invalid kind %r (valid: %s)" % (slug, f["kind"], ", ".join(KINDS)))
        if f.get("e2e") and f["e2e"] not in E2E:
            errors.append("[%s] invalid e2e %r" % (slug, f["e2e"]))
        if "tries" in f:
            if not f["tries"].isdigit():
                errors.append("[%s] tries must be an integer (got %r)" % (slug, f["tries"]))
            elif int(f["tries"]) >= MAX_TRIES and f["status"] != "blocked":
                errors.append("[%s] tries=%s (cap %d) but status=%s: with %d failures the feature must be blocked" % (slug, f["tries"], MAX_TRIES, f["status"], MAX_TRIES))
        if f["status"] in ("approved", "building", "verified") and not f.get("paths"):
            errors.append("[%s] status=%s without paths= (the hook scope depends on it)" % (slug, f["status"]))
        if f.get("regression"):
            if f["regression"] not in REGRESSION:
                errors.append("[%s] invalid regression %r (valid: %s)" % (slug, f["regression"], ", ".join(REGRESSION)))
            if not slug.startswith(REFACTOR_PREFIX):
                errors.append("[%s] regression= only applies to a refactoring feature (slug %s*)" % (slug, REFACTOR_PREFIX))
        if f.get("isolation") and f["isolation"] not in ISOLATIONS:
            errors.append("[%s] invalid isolation %r (valid: %s)" % (slug, f["isolation"], ", ".join(ISOLATIONS)))
        slugs, _ = split_deps(f)
        for dep in slugs:
            if dep == slug:
                errors.append("[%s] depends on itself" % slug)
            elif dep not in features:
                errors.append("[%s] deps= names a nonexistent feature [%s] (a shared folder is written dir:<folder>)" % (slug, dep))
        if f["status"] == "building":
            waiting = [d for d in slugs if d in features and features[d]["status"] != "verified"]
            if waiting:
                errors.append("[%s] is building but depends on %s which is not verified" % (slug, ", ".join(waiting)))
    cycle = find_cycle(features)
    if cycle:
        errors.append("cycle in deps=: " + " -> ".join(cycle))
    return errors


def split_deps(feature):
    """(slugs, shared_folders) from deps=. Empty when the field is absent."""
    slugs, folders = [], []
    for item in (feature.get("deps") or "").split(","):
        item = item.strip()
        if not item:
            continue
        if item.startswith(DIR_PREFIX):
            folders.append(item[len(DIR_PREFIX):])
        else:
            slugs.append(item)
    return slugs, folders


def find_cycle(features):
    """First dependency cycle between features (list of slugs, closing on the first), or []."""
    state = {}  # slug -> 1 visiting, 2 done

    def visit(slug, trail):
        state[slug] = 1
        for dep in split_deps(features[slug])[0]:
            if dep not in features:
                continue
            if state.get(dep) == 1:
                return trail[trail.index(dep):] + [dep]
            if dep not in state:
                found = visit(dep, trail + [dep])
                if found:
                    return found
        state[slug] = 2
        return []

    for slug in features:
        if slug not in state:
            found = visit(slug, [slug])
            if found:
                return found
    return []


# ---------------------------------------------------------------- edges
def reachable(old, new, has_answer=False):
    """Is the change old->new a legal edge (or chain) of the graph?"""
    if old == new:
        return True
    if old == "blocked":  # only the user unblocks (ANSWERS.md); never blocked straight to verified
        return has_answer and new in ANSWERS_OK
    if new == "blocked":  # quarantine: from any state
        return True
    seen, queue = {old}, [old]
    while queue:
        node = queue.pop(0)
        for nxt in FORWARD.get(node, ()) + BACK.get(node, ()):
            if nxt == new:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def norm(path):
    return path.replace("\\", "/").strip().strip("/").lower()


def overlap(a, b):
    a, b = norm(a), norm(b)
    return a == b or a.startswith(b + "/") or b.startswith(a + "/")


def edits_overlap(first, second):
    """Two features collide if they edit overlapping folders, or if one EDITS what the other imports (dir:).
    Two that only import the same folder do not collide. Returns the reason, or "" if they do not collide."""
    paths_a, paths_b = (first.get("paths") or "").split(","), (second.get("paths") or "").split(",")
    dirs_a, dirs_b = split_deps(first)[1], split_deps(second)[1]
    for mine, theirs in ((paths_a, paths_b), (paths_a, dirs_b), (dirs_a, paths_b)):
        for pa in mine:
            for pb in theirs:
                if pa.strip() and pb.strip() and overlap(pa, pb):
                    return "%s overlaps %s" % (pa, pb)
    return ""


def conflicts(features, slugs):
    """Pairs of features whose paths overlap (or nonexistent slug / no paths)."""
    found = []
    for slug in slugs:
        if slug not in features or not features[slug].get("paths"):
            found.append("[%s] nonexistent or without paths=" % slug)
    ok = [s for s in slugs if s in features and features[s].get("paths")]
    for i, first in enumerate(ok):
        for second in ok[i + 1:]:
            reason = edits_overlap(features[first], features[second])
            if reason:
                found.append("[%s] x [%s]: %s" % (first, second, reason))
    return found


def validate_loop_json(state):
    errors = []
    if not isinstance(state, dict) or not state:
        return ["LOOP.json missing or invalid"]
    if state.get("status") not in LOOP_STATUS:
        errors.append("LOOP.json invalid status %r" % state.get("status"))
    if not isinstance(state.get("turn"), int):
        errors.append("LOOP.json turn must be an integer")
    for key in ("options", "to_check"):
        if key in state and not (isinstance(state[key], list) and all(isinstance(i, str) for i in state[key])):
            errors.append("LOOP.json %s must be a list of strings" % key)
    if state.get("status") == "awaiting_user" and not state.get("reason"):
        errors.append("LOOP.json awaiting_user without reason (the question)")
    return errors


def validate(prev_text, new_text, loop_state=None, new_commits=0, has_answer=False):
    """Checks the new state against the previous one. Returns a list of violations (empty = valid state)."""
    new, errors = parse_roadmap(new_text)
    errors += check_fields(new)
    old, _ = parse_roadmap(prev_text) if prev_text else ({}, [])
    gone = [slug for slug in old if slug not in new]
    if gone:
        errors.append("features removed from the ROADMAP without a request: %s" % ", ".join(gone))
    newly_verified = 0
    for slug, f in new.items():
        before = old.get(slug, {}).get("status")
        if before and f["status"] in STATUSES and before in STATUSES and not reachable(before, f["status"], has_answer):
            errors.append("[%s] illegal transition %s -> %s" % (slug, before, f["status"]))
        if f["status"] == "verified" and before != "verified":
            newly_verified += 1
    if newly_verified > new_commits:
        errors.append("%d feature(s) became verified but there were %d new commit(s): verified requires the feature's commit" % (newly_verified, new_commits))
    building = [s for s, f in new.items() if f["status"] == "building"]
    errors += ["simultaneous building with overlapping paths: " + c for c in conflicts(new, building) if " x " in c]
    if loop_state is not None:
        errors += validate_loop_json(loop_state)
    return errors


# ---------------------------------------------------------------- scheduler (next phase, deterministic)
def ready(features, slug):
    """Are all slug dependencies verified?"""
    return all(features.get(dep, {}).get("status") == "verified" for dep in split_deps(features[slug])[0])


def stuck_by_blocked(features, slug):
    """Direct dependencies that are blocked: the feature does not move until the user unblocks them."""
    return [dep for dep in split_deps(features[slug])[0] if features.get(dep, {}).get("status") == "blocked"]


def pick_wave(features, candidates, wave, headless=False):
    """Greedily, up to `wave` features with paths disjoint from each other and from those already building.
    In headless mode there is no worktree: a feature with isolation=worktree (collides with a shared folder) runs ALONE in the wave.
    A feature with regression= also runs alone (in any mode): the old code's baseline needs a tree only it changes."""
    chosen = []
    busy = [s for s, f in features.items() if f["status"] == "building"]
    for slug in candidates:
        if len(chosen) >= wave:
            break
        if not features[slug].get("paths"):
            continue
        if any(edits_overlap(features[slug], features[other]) for other in chosen + busy):
            continue
        if features[slug].get("regression") in ("yes", "baseline", "failed") or (headless and features[slug].get("isolation") == "worktree"):
            if not chosen and not busy:
                return [slug]
            continue
        chosen.append(slug)
    return chosen


def next_work(features, wave=1, headless=False):
    """Decides the next phase of the turn. Same priority as loop.md: the most advanced work that exists."""
    # only the current stage moves: the first (in file order) that still has an open feature. A dependency between stages
    # needs no deps=: the next stage only starts when this one closes (verified/blocked).
    stages = list(dict.fromkeys(f.get("stage", "") for f in features.values()))
    current = next((st for st in stages if any(f.get("stage", "") == st and f["status"] not in ("verified", "blocked") for f in features.values())), None)
    everything = features
    features = {slug: f for slug, f in everything.items() if f.get("stage", "") == current} if current is not None else everything
    by = lambda status: [s for s, f in features.items() if f["status"] == status]
    approved = [s for s in by("approved") if ready(everything, s)]
    result = {
        "phase": "none", "slugs": [], "waiting_deps": [s for s in by("approved") if not ready(everything, s)],
        "stuck_by_blocked": [], "deferred_by_collision": [], "isolation": {},
    }
    if by("building") or by("speccing"):
        result.update(phase="recover", slugs=by("building") + by("speccing"))  # lost dispatch: Phase 0 resolves it
    elif approved:
        chosen = pick_wave(features, approved, max(1, wave), headless)
        result.update(phase="execute", slugs=chosen, deferred_by_collision=[s for s in approved if s not in chosen])
    elif by("specced"):
        result.update(phase="gate", slugs=by("specced")[:3])
    elif by("pending"):
        result.update(phase="specs", slugs=by("pending")[:3])
    for slug, f in features.items():
        if f["status"] not in ("verified", "blocked") and stuck_by_blocked(everything, slug):
            result["stuck_by_blocked"].append(slug)
    for slug in result["slugs"]:
        result["isolation"][slug] = features[slug].get("isolation", "same-tree")
    result["stage"] = current
    if result["phase"] == "none" and all(f["status"] in ("verified", "blocked") for f in everything.values()):
        result["phase"] = "final"
    return result


# ---------------------------------------------------------------- .specs/project layout
# project/             what belongs to the project and people edit (PROJECT, ROADMAP, STATE, RULES, TEST-ENVIRONMENT, phases/)
# project/loop/        everything the loop owns. At its root only what a person reads: the .md files (ANSWERS, VALIDATE,
#                      DEPENDENCIES, GRAPH) and NOTICE.txt
#   json/              machine state: LOOP.json, LOOP-CMD.json, MONITOR.json, meta.json, SCOPE.json, PERMISSIONS.json,
#                      ROADMAP-APPROVED.json, rules.tests.json, delivery.json
#   internal/          the rest: LOOP.lock, PAUSE, guard.log, monitor*.log, actions.log, turns/NNN.json, snapshots/NNN/,
#                      chat/, jobs/, whiteboard.json, roadmap-goal.md (the last four are built with internal_dir(), not loop_path())
LOOP_FOLDER_NAME = "loop"
JSON_FOLDER_NAME = "json"
INTERNAL_FOLDER_NAME = "internal"
READABLE_NAMES = ("NOTICE.txt",)
# what used to live loose in project/ (public plugin layout) and now lives in project/loop/
LOOP_FILES = ("LOOP.json", "LOOP.lock", "SCOPE.json", "PERMISSIONS.json", "rules.tests.json", "guard.log", ".guard-alert",
              "ANSWERS.md", "DEPENDENCIES.md", "GRAPH.md", "VALIDATE.md")
OLD_LOGS_FOLDER_NAME = "loop-logs"  # public plugin: .specs/loop-logs/ (turn logs, meta.json, NOTICE.txt, delivery.json, snapshots/)

# Old layout names -> current names, used ONLY by migrate_layout() to move projects written by older versions.
# These are the only non-English words in the code base on purpose: they are the literal names found on disk.
LEGACY_NAMES = {
    "loop_script": "loop",
    "interno": "internal",
    "voltas": "turns",
    "RESPOSTAS.md": "ANSWERS.md",
    "VALIDAR.md": "VALIDATE.md",
    "DEPENDENCIAS.md": "DEPENDENCIES.md",
    "GRAFO.md": "GRAPH.md",
    "AVISO.txt": "NOTICE.txt",
    "ESCOPO.json": "SCOPE.json",
    "PERMISSOES.json": "PERMISSIONS.json",
    "regras.testes.json": "rules.tests.json",
    "entrega.json": "delivery.json",
    "ROADMAP-APROVADO.json": "ROADMAP-APPROVED.json",
    "acoes.log": "actions.log",
    "objetivo-roadmap.md": "roadmap-goal.md",
    "REGRAS.md": "RULES.md",
    "TESTE-AMBIENTE.md": "TEST-ENVIRONMENT.md",
    "etapa-NN.md": "stage-NN.md",  # pattern: NN = the stage number (phases/)
}
NUMBER_PLACEHOLDER = "NN"
LEGACY_NAME_PATTERNS = [(re.compile("^" + re.escape(old).replace(NUMBER_PLACEHOLDER, r"(\d+)") + "$"), new)
                        for old, new in LEGACY_NAMES.items() if NUMBER_PLACEHOLDER in old]
LEGACY_LOOP_FOLDER_NAME = next(old for old, new in LEGACY_NAMES.items() if new == LOOP_FOLDER_NAME)
RULES_IMPORT = "@.specs/project/RULES.md"
LEGACY_RULES_IMPORT = "@.specs/project/" + next(old for old, new in LEGACY_NAMES.items() if new == "RULES.md")
PROJECT_FILES_RENAMED = ("RULES.md", "TEST-ENVIRONMENT.md")


def loop_dir(work):
    return os.path.join(work, ".specs", "project", LOOP_FOLDER_NAME)


def internal_dir(work):
    return os.path.join(loop_dir(work), INTERNAL_FOLDER_NAME)


def loop_path(work, name):
    """Where `name` lives inside project/loop: .md and NOTICE.txt at the root, *.json in json/, the rest in internal/."""
    if name.endswith(".md") or name in READABLE_NAMES:
        return os.path.join(loop_dir(work), name)
    return os.path.join(loop_dir(work), JSON_FOLDER_NAME if name.endswith(".json") else INTERNAL_FOLDER_NAME, name)


def turns_dir(work):
    return loop_path(work, "turns")


def state_file(work, name):
    """Path of a state file: loop files live in project/loop/, the others (ROADMAP, STATE, RULES...) in project/."""
    return loop_path(work, name) if name in LOOP_FILES else os.path.join(work, ".specs", "project", name)


# ---------------------------------------------------------------- roadmap approval
# Only the user approves (click in the panel, POST /api/roadmap/approve). The signature covers what the user approved:
# which features exist and in which stage. Status, paths, deps, tries and commit change during the loop and are left out.
APPROVAL_NAME = "ROADMAP-APPROVED.json"


def roadmap_signature(text):
    features, _ = parse_roadmap(text)
    rows = sorted("%s|%s" % (slug, fields.get("stage", "")) for slug, fields in features.items())
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest(), len(rows)


def roadmap_approval(work):
    """Approval status: {"approved": bool, "reason": text|None, "at": ISO|None, "features": N}."""
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md"))
    if not text:
        return {"approved": False, "reason": "ROADMAP.md missing", "at": None, "features": 0}
    signature, count = roadmap_signature(text)
    try:
        with open(loop_path(work, APPROVAL_NAME), encoding="utf-8") as handle:
            saved = json.load(handle)
    except (OSError, ValueError):
        return {"approved": False, "reason": "roadmap not approved in the panel yet", "at": None, "features": count}
    if saved.get("signature") != signature:
        return {"approved": False, "reason": "the roadmap changed after the approval (different features or stages): approve it again in the panel",
                "at": saved.get("at"), "features": count}
    return {"approved": True, "reason": None, "at": saved.get("at"), "features": count}


def approve_roadmap(work, now_iso):
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md"))
    features, errors = parse_roadmap(text)
    if errors or not features:
        raise ValueError("invalid ROADMAP: " + ("; ".join(errors) or "no features"))
    signature, count = roadmap_signature(text)
    path = loop_path(work, APPROVAL_NAME)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump({"signature": signature, "features": count, "at": now_iso}, handle)
    os.replace(temp, path)
    return {"approved": True, "reason": None, "at": now_iso, "features": count}


# ---------------------------------------------------------------- migration of old layouts
def current_name(name):
    """Current name of a file or folder written by an older layout (unchanged when it is already current)."""
    if name in LEGACY_NAMES and NUMBER_PLACEHOLDER not in name:
        return LEGACY_NAMES[name]
    for pattern, new in LEGACY_NAME_PATTERNS:
        found = pattern.match(name)
        if found:
            return new.replace(NUMBER_PLACEHOLDER, found.group(1))
    return name


def _has_legacy_name(folder):
    for _, dirs, names in os.walk(folder):
        if any(current_name(name) != name for name in dirs + names):
            return True
    return False


def _move(source, destination):
    """Moves without overwriting; a folder that already exists at the destination is merged, and every name inside it
    passes through current_name(). Returns the destinations actually created."""
    if not os.path.exists(source) or os.path.abspath(source) == os.path.abspath(destination):
        return []
    if os.path.isdir(source) and (os.path.isdir(destination) or _has_legacy_name(source)):
        made = []
        os.makedirs(destination, exist_ok=True)
        for child in sorted(os.listdir(source)):
            made += _move(os.path.join(source, child), os.path.join(destination, current_name(child)))
        try:
            os.rmdir(source)
        except OSError:
            pass
        return made
    if os.path.exists(destination):
        return []
    os.makedirs(os.path.dirname(destination), exist_ok=True)
    shutil.move(source, destination)
    return [destination]


def _loop_entry_destination(work, name):
    """Destination of an entry found at the root of an old loop folder (the legacy one in LEGACY_NAMES or a flat project/loop/)."""
    name = current_name(name)
    if name in (JSON_FOLDER_NAME, INTERNAL_FOLDER_NAME):
        return os.path.join(loop_dir(work), name)
    return loop_path(work, name)


def _rewrite_rules_import(work):
    """The CLAUDE.md of an older project imports the legacy rules file (LEGACY_RULES_IMPORT): only that exact line becomes the RULES.md import."""
    path = os.path.join(work, "CLAUDE.md")
    try:
        with open(path, encoding="utf-8", newline="") as handle:
            text = handle.read()
    except OSError:
        return []
    lines = text.split("\n")
    changed = [i for i, line in enumerate(lines) if line.strip() == LEGACY_RULES_IMPORT]
    if not changed:
        return []
    for i in changed:
        lines[i] = lines[i].replace(LEGACY_RULES_IMPORT, RULES_IMPORT)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write("\n".join(lines))
    return [path]


def migrate_layout(work):
    """Brings old layouts to .specs/project/loop/ (.md at the root, json/ and internal/): loose files in project/ (public
    plugin and older versions), the .specs/loop-logs folder, the legacy loop folder and its old names (LEGACY_NAMES), a flat project/loop/
    and the project files with old names (RULES.md, TEST-ENVIRONMENT.md, phases/stage-NN.md, the CLAUDE.md import).
    Idempotent and never overwrites: what is already at the destination stays (and so does the source). Returns what moved.
    The CONTENT of the files is not translated."""
    specs = os.path.join(work, ".specs")
    project = os.path.join(specs, "project")
    old_logs = os.path.join(specs, OLD_LOGS_FOLDER_NAME)
    legacy_loop = os.path.join(project, LEGACY_LOOP_FOLDER_NAME)
    current = loop_dir(work)
    moves = []
    if os.path.isdir(legacy_loop):
        moves += [(os.path.join(legacy_loop, name), _loop_entry_destination(work, name)) for name in sorted(os.listdir(legacy_loop))]
    if os.path.isdir(current):
        moves += [(os.path.join(current, name), _loop_entry_destination(work, name)) for name in sorted(os.listdir(current))
                  if name not in (JSON_FOLDER_NAME, INTERNAL_FOLDER_NAME)]
    if os.path.isdir(project):
        for name in sorted(os.listdir(project)):
            renamed = current_name(name)
            if renamed in LOOP_FILES:
                moves.append((os.path.join(project, name), loop_path(work, renamed)))
            elif renamed != name and renamed in PROJECT_FILES_RENAMED:
                moves.append((os.path.join(project, name), os.path.join(project, renamed)))
    phases = os.path.join(project, "phases")
    if os.path.isdir(phases):
        moves += [(os.path.join(phases, name), os.path.join(phases, current_name(name))) for name in sorted(os.listdir(phases))
                  if current_name(name) != name]
    if os.path.isdir(old_logs):
        for name in sorted(os.listdir(old_logs)):
            target = os.path.join(turns_dir(work), name) if re.fullmatch(r"\d+\.json", name) else loop_path(work, current_name(name))
            moves.append((os.path.join(old_logs, name), target))
    moved = []
    for source, destination in moves:
        moved += [os.path.relpath(path, specs) for path in _move(source, destination)]
    moved += [os.path.relpath(path, work) for path in _rewrite_rules_import(work)]
    for emptied in (old_logs, legacy_loop):
        try:
            os.rmdir(emptied)
        except OSError:
            pass
    return moved


# ---------------------------------------------------------------- snapshots and rewind
# LOOP.json and ANSWERS.md live in project/loop/; the other 3 in project/. The snapshot keeps the 5 side by side.
STATE_FILES = ("ROADMAP.md", "STATE.md", "LOOP.json", "ANSWERS.md", "RULES.md")


def snap_dir(work, number):
    return os.path.join(loop_path(work, "snapshots"), "%03d" % number)


def snapshot(work, number, head):
    """Saves the state (.specs/project files + HEAD) right after turn `number`. Number 0 = baseline before anything."""
    target = snap_dir(work, number)
    shutil.rmtree(target, ignore_errors=True)
    os.makedirs(target, exist_ok=True)
    for name in STATE_FILES:
        source = state_file(work, name)
        if os.path.isfile(source):
            shutil.copy2(source, os.path.join(target, name))
    with open(os.path.join(target, "snapshot.json"), "w", encoding="utf-8") as handle:
        json.dump({"turn": number, "head": head}, handle)
    return target


def load_snapshot(work, number):
    target = snap_dir(work, number)
    try:
        with open(os.path.join(target, "snapshot.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return None
    meta["dir"] = target
    return meta


def restore(work, number):
    """Restores the state files from snapshot `number` (deletes the ones that did not exist). The git reset is up to the caller."""
    meta = load_snapshot(work, number)
    if not meta:
        return None
    for name in STATE_FILES:
        source = os.path.join(meta["dir"], name)
        destination = state_file(work, name)
        if os.path.isfile(source):
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            shutil.copy2(source, destination)
        elif os.path.isfile(destination):
            os.remove(destination)
    return meta


# ---------------------------------------------------------------- graph (Mermaid)
def graph(text):
    features, _ = parse_roadmap(text)
    by = {status: [s for s, f in features.items() if f["status"] == status] for status in STATUSES}
    lines = ["flowchart LR"]
    for status in STATUSES:
        label = "%s (%d)" % (status, len(by[status]))
        if by[status]:
            label += "<br/>" + "<br/>".join(by[status][:6]) + ("<br/>..." if len(by[status]) > 6 else "")
        lines.append('  %s["%s"]' % (status, label))
    for src, targets in FORWARD.items():
        for dst in targets:
            lines.append("  %s --> %s" % (src, dst))
    lines += ["  speccing -.retry.-> pending", "  building -.FAIL.-> approved", "  verified -.E2E failed.-> approved",
              "  specced -.gate BLOCKED.-> specced", "  pending & speccing & specced & approved & building -.2 failures.-> blocked"]
    return "\n".join(lines)


def write_loop_json(work, args):
    """Writes LOOP.json from flags, validating the contract. Returns (exit_code, message)."""
    import datetime

    def flag(name, default=""):
        return args[args.index(name) + 1] if name in args and args.index(name) + 1 < len(args) else default

    status, turn = flag("--status"), flag("--turn")
    if status not in LOOP_STATUS:
        return 2, "invalid status %r (valid: %s)" % (status, ", ".join(LOOP_STATUS))
    if not turn.isdigit():
        return 2, "--turn must be an integer (the turn received in the prompt)"
    state = {"status": status, "turn": int(turn), "feature": flag("--feature"), "reason": flag("--reason"), "next": flag("--next"),
             "at": datetime.datetime.now().isoformat(timespec="seconds")}
    for key, option in (("options", "--options"), ("to_check", "--to-check")):
        if flag(option):
            state[key] = [item.strip() for item in flag(option).split("|") if item.strip()]
    errors = validate_loop_json(state)
    if errors:
        return 2, "; ".join(errors)
    target = state_file(work, "LOOP.json")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(state, handle, ensure_ascii=False)
    return 0, "LOOP.json written: status=%s turn=%s" % (status, turn)


def main():
    args = sys.argv[1:]
    if len(args) >= 2 and args[0] == "loop":
        code, message = write_loop_json(os.path.abspath(args[1]), args)
        print(message)
        return code
    if len(args) < 2 or args[0] not in ("validate", "disjoint", "graph", "next", "migrate"):
        print(__doc__)
        return 2
    command, work = args[0], os.path.abspath(args[1])
    if command == "migrate":
        moved = migrate_layout(work)
        print("moved: " + ", ".join(moved) if moved else "nothing to migrate")
        return 0
    text = read(os.path.join(work, ".specs", "project", "ROADMAP.md"))
    if command == "graph":
        print(graph(text))
        return 0
    features, errors = parse_roadmap(text)
    if command == "next":
        wave = int(args[args.index("--wave") + 1]) if "--wave" in args else 1
        print(json.dumps(next_work(features, wave, "--headless" in args), ensure_ascii=False))
        return 0
    if command == "validate":
        errors += check_fields(features)
        state = None
        if os.path.exists(state_file(work, "LOOP.json")):
            try:
                state = json.loads(read(state_file(work, "LOOP.json")))
            except ValueError:
                errors.append("LOOP.json is not valid JSON")
        if state is not None:
            errors += validate_loop_json(state)
        for item in errors:
            print("VIOLATION:", item)
        print("%d feature(s); %s" % (len(features), "valid state" if not errors else "%d violation(s)" % len(errors)))
        return 1 if errors else 0
    found = conflicts(features, args[2:])
    for item in found:
        print("CONFLICT:", item)
    print("disjoint" if not found else "do NOT dispatch in parallel")
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
