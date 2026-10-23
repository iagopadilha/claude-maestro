#!/usr/bin/env python3
"""Checks and pauses the loop applies between turns: scheduler flags, forgotten dependencies, quarantine and critical-decision pauses, spend cap, E2E marks and the contract merge of verified features.

Moved out of `run_loop.py` as it was. `warn_near_cost_cap` stays in `run_loop.py` (it calls its `notify_wait`, which tests replace).
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import subprocess

import decisions  # noqa: E402  (AI decisions log: the one grammar owner of VALIDATE.md)
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
import subprojects  # noqa: E402  (monorepo list: owner of a path, confirmed or not)
import system_map  # noqa: E402  (contract.md deltas of a verified feature -> docs/codebase/SYSTEM.md of a monorepo)
from auto_answers import CRITICAL_DECISION_PREFIX  # noqa: E402
from loop_io import git, read, save_json, save_meta  # noqa: E402
from worktree_state import SYSTEM_DOCUMENT, roadmap_score  # noqa: E402

HERE = ENGINE_DIR
COST_CAP_REASON_PREFIX = "cost cap reached: US$ "
COST_WARNING_RATIO = 0.8


# ---------------------------------------------------------------- one turn
def plan_flags(work, wave, primary_name=""):
    """Deterministic scheduler: the script computes the turn's lanes (loop_state.next_work); the orchestrator executes them.
    A lane is ONE feature at ITS OWN step (`--lanes slug:step,...`; step = spec|gate|execute|recover): every lane gets its own agent at every
    step and advances by itself, so no feature waits for another one to reach the same phase. `--phase` is the common step when all the lanes
    are at the same one (and `lanes` when they differ); `--ready` lists every lane's slug (kept for older prompts).
    With no readable ROADMAP or nothing to dispatch, returns empty and the orchestrator decides as before."""
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    if not features:
        return ""
    plan = loop_state.next_work(features, wave, headless=True, primary_name=primary_name)
    flags = " --phase %s" % plan["phase"]
    if plan["lanes"]:
        flags += " --lanes %s" % ",".join("%s:%s" % (lane["slug"], lane["step"]) for lane in plan["lanes"])
        flags += " --ready %s" % ",".join(plan["slugs"])
    if plan["stuck_by_blocked"]:
        flags += " --stuck %s" % ",".join(plan["stuck_by_blocked"])
    return flags


def audit_before_turn(work):
    """Before choosing the wave: writes forgotten `deps=` into the ROADMAP (audit_deps.py --apply). It only increases serialization; the script does it
    because it picks `--ready` before claude runs. Cycle or invalid ROADMAP: nothing is written. Returns ["feature deps+=other", ...]."""
    try:
        done = subprocess.run([sys.executable, os.path.join(HERE, "audit_deps.py"), work, "--apply"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line[len("APPLIED: "):] for line in done.stdout.splitlines() if line.startswith("APPLIED: ")]


def e2e_unmarked(specs):
    """With --e2e, `done` requires the final phase result on the line of EACH verified feature: e2e=ok, or e2e=not-covered when
    there is no surface to test (pure lib). `e2e=failed` with `done` is a contradiction. Returns the list of problems."""
    features = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    problems = []
    for slug, f in features.items():
        if f["status"] == "verified" and not f.get("e2e"):
            problems.append("%s without e2e=" % slug)
        elif f.get("e2e") == "failed" and f["status"] != "blocked":
            problems.append("%s with e2e=failed" % slug)
    return problems


def missing_backend_contract(work, slug, feature):
    """True for a back-end (kind back/both) feature whose paths fall in a subproject of a confirmed monorepo and that has no contract.md."""
    if str(feature.get("kind") or "").lower() not in ("back", "both"):
        return False
    if os.path.isfile(system_map.contract_path(work, slug)):
        return False
    for path in str(feature.get("paths") or "").split(","):
        owner = subprojects.owner_of(work, path.strip()) if path.strip() else None
        if owner and owner.get("skip") is not True:
            return True
    return False


def merge_verified_contracts(work, meta, state, specs):
    """Contract merge hook (SYS-04, SYS-09, SYS-10, design D22). Only for a CONFIRMED monorepo; otherwise nothing at all (SYS-07).

    Moment chosen: right after a turn is validated, for every feature the ROADMAP now shows as `verified` that was not handled yet,
    one feature at a time in ROADMAP order. The orchestrator verifies and makes its checkpoint commit INSIDE its turn, so the
    engine cannot run before that commit; instead it merges between turns, records `contract_merge: {slug: {changed, warnings}}`
    in LOOP.json (the next turn's orchestrator reads the status) and leaves SYSTEM.md for the NEXT checkpoint commit, as
    dispatch.md says. When no next turn will happen (done / everything closed) the engine commits SYSTEM.md itself so it is not lost.
    Conflict and missing-file warnings go to the loop status, the console and VALIDATE.md. No SCOPE.json change."""
    if not subprojects.monorepo_confirmed(work):
        return
    features, _ = loop_state.parse_roadmap(read(os.path.join(work, ".specs", "project", "ROADMAP.md")))
    handled = meta.setdefault("contract_merged_slugs", [])
    recorded = {}
    for slug, feature in features.items():
        if feature.get("status") != "verified" or slug in handled:
            continue
        result = system_map.merge_contract_deltas(work, [slug])
        handled.append(slug)
        recorded[slug] = {"changed": bool(result["changed"]), "warnings": list(result["warnings"])}
        if missing_backend_contract(work, slug, feature):
            warning = "back-end feature verified without .specs/features/%s/contract.md: SYSTEM.md was not updated" % loop_state.feature_folder_name(slug)
            recorded[slug]["warnings"].append(warning)
            print("[loop] WARNING (contract merge, %s): %s" % (slug, warning))
            decisions.append(work, slug, state.get("turn"), "important", "WARNING contract: " + warning,
                             "went on without the contract delta", "write the contract.md (routes, env, events) and merge it into SYSTEM.md")
        if result["changed"]:
            meta["system_md_uncommitted"] = True
        for warning in result["warnings"]:
            print("[loop] WARNING (contract merge, %s): %s" % (slug, warning))
            append_validate_line(specs, "WARNING contract merge (%s): %s" % (slug, warning))
            if system_map.NO_GRAMMAR_MARK in warning:
                decisions.append(work, slug, state.get("turn"), "important", "WARNING contract: " + warning,
                                 "went on without merging the contract delta",
                                 "rewrite contract.md as lines `- [add|change|remove] route|dto|auth|env <identity> — <purpose> (<file>)` (no table) and merge it into SYSTEM.md")
    if not recorded:
        return
    state["contract_merge"] = recorded
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    score, total, done = roadmap_score(work)
    if meta.get("system_md_uncommitted") and (state.get("status") == "done" or (total and done == total)):
        code, _, err = git(work, "add", "--", SYSTEM_DOCUMENT)
        if code == 0:
            code, _, err = git(work, "commit", "-m", "chore(loop): merge feature contracts into docs/codebase/SYSTEM.md", "--no-verify")
        if code == 0:
            meta["system_md_uncommitted"] = False
        else:
            print("[loop] WARNING: SYSTEM.md contract merge could not be committed: " + err[-200:])
    save_meta(loop_state.loop_path(work, "meta.json"), meta)


def pause_on_quarantine(specs, prev, state, opts):
    """A feature that just entered quarantine (2 failures) PAUSES the loop and notifies the user, instead of carrying on alone.
    Returns the pause text (and rewrites LOOP.json as awaiting_user, so --answer works) or "" if there is nothing to pause.
    Only acts on a turn the orchestrator left as `continue`: `done`/`awaiting_user`/`error` already notify on their own."""
    if opts.continue_on_blocked or not prev or state.get("status") != "continue":
        return ""
    # Default (user decision): a feature that gets stuck ALWAYS pauses the loop and notifies the user. --continue-on-blocked opts out.
    old = loop_state.parse_roadmap(read(os.path.join(prev["dir"], "ROADMAP.md")))[0]
    new = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    fresh = [slug for slug, f in new.items() if f["status"] == "blocked" and old.get(slug, {}).get("status") != "blocked"]
    if not fresh:
        return ""
    why = str(state.get("reason") or "").strip()
    reason = ("Quarantine after 2 failures: %s. The loop paused for you to decide (try again with guidance from you, "
              "or skip and carry on with the others)." % ", ".join(fresh)) + ((" Orchestrator detail: " + why[:600]) if why else "")
    state.update(status="awaiting_user", reason=reason, options=["try the feature again", "skip the feature and carry on with the others"],
                 next="apply the user's decision about: " + ", ".join(fresh))
    save_json(os.path.join(specs, loop_state.LOOP_FOLDER_NAME, loop_state.JSON_FOLDER_NAME, "LOOP.json"), state)
    return reason


# ---------------------------------------------------------------- critical decisions
def pause_on_critical_decision(work, state, mark):
    """A turn that wrote a `level=critical` line in VALIDATE.md (after line `mark`) stops the loop like an open question: LOOP.json becomes
    awaiting_user with the decision as the question. `important` lines never stop it. A turn that ended in `error` stays an error.
    Returns the reason (and rewrites LOOP.json) or "" when there is nothing to stop for."""
    if state.get("status") == "error":
        return ""
    found = decisions.new_critical(work, mark)
    if not found:
        return ""
    first = found[0]
    reason = "%s%s | chose: %s | revert: %s" % (CRITICAL_DECISION_PREFIX, first["doubt"], first["chose"], first["revert"])
    if len(found) > 1:
        reason += " | %d more critical decision(s) in VALIDATE.md" % (len(found) - 1)
    state.update(status="awaiting_user", reason=reason, feature=first["slug"],
                 options=["keep as decided", "change it: <write how>"],
                 next="apply the user's answer about the critical decision, then continue")
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    return reason


# ---------------------------------------------------------------- spend cap
def effective_cost_cap(meta, opts):
    """Cap of US$ of this run: the one raised from the panel (meta max_cost_usd) wins over --max-cost; 0 = no cap."""
    try:
        raised = float(meta.get("max_cost_usd") or 0)
    except (TypeError, ValueError):
        raised = 0.0
    return raised if raised > 0 else float(opts.max_cost or 0)


def cost_cap_reason(cost, cap):
    return "%s%.2f of %.2f" % (COST_CAP_REASON_PREFIX, cost, cap)


def append_validate_line(specs, line):
    path = os.path.join(specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md")
    current = read(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(("" if not current or current.endswith("\n") else "\n") + line + "\n")
