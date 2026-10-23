#!/usr/bin/env python3
"""Automatic answers of the headless loop: a question that does not need the user is answered by the script with the recommended option, and a spec stuck on wording at the gate gets a tiebreak.

Moved out of `run_loop.py` as it was.
"""
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
import datetime
import re

import decisions  # noqa: E402  (AI decisions log: the one grammar owner of VALIDATE.md)
import loop_state  # noqa: E402  (typed state: grammar, legal edges, snapshots)
from loop_io import load_json, read, save_json  # noqa: E402

CRITICAL_DECISION_PREFIX = "critical decision: "  # LOOP.json reason of a critical decision stop; the panel and the notice key on it


def write_answer(work, text):
    """Records the user's answer in .specs/project/loop/ANSWERS.md. The orchestrator's next turn reads it first and applies it."""
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    if state.get("status") != "awaiting_user":
        sys.exit("--answer only applies when the loop stopped at awaiting_user (current status: %s)" % (state.get("status") or "no LOOP.json"))
    path = loop_state.state_file(work, "ANSWERS.md")
    block = "\n## Answer %s [pending]\nQuestion: %s\nUser answer: %s\n" % (
        datetime.datetime.now().isoformat(timespec="seconds"), str(state.get("reason") or "").strip()[:1500], text.strip())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(block)
    print("[loop] answer recorded in %s (the next turn applies it and carries on)" % path)


# ---------------------------------------------------------------- automatic answer
# The headless loop exists to run for hours without stopping. A question the orchestrator itself returns with options and that
# does NOT need the user (a small, reversible dilemma, or "try the quarantined feature again") is answered by the SCRIPT with the
# recommended option (or the first one that does not abandon the feature), noted in VALIDATE.md, and the loop carries on. A rule
# in the skill text alone did not hold (the agent asked anyway); code holds.
# It stops (and leaves the question to the user) when the text talks about authorization, production, something irreversible,
# push/merge/deploy, a secret, a credential, billing or payment, when the only options abandon the feature, and when the
# automatic answer limits are reached.
HARD_STOP_RE = re.compile(
    r"authoriz|production|\bprod\b|irreversib|delete|\bdrop\b|truncate|\bpush\b|\bmerge\b|deploy|secret|password|credential|billing|payment|access|business rules?|discrepan|inconsisten|real data",
    re.I,
)


ABANDON_RE = re.compile(r"\bend\b|finish without|stop|skip|give up|abandon|accept .{0,40}blocked|do not do|cancel", re.I)


RECOMMENDED_RE = re.compile(r"recommended", re.I)


def pick_auto_answer(state):
    """Picks the answer to a question that does not need the user. Returns (number, text, why) or None when it must stop."""
    options = [str(option) for option in (state.get("options") or [])]
    if len(options) < 2 or str(state.get("reason") or "").startswith(CRITICAL_DECISION_PREFIX):
        return None  # a critical decision needs the user: it is never answered by the script
    if HARD_STOP_RE.search(str(state.get("reason") or "") + " " + " ".join(options)):
        return None
    for number, option in enumerate(options, 1):
        if RECOMMENDED_RE.search(option) and not ABANDON_RE.search(option):
            return number, option, "recommended"
    for number, option in enumerate(options, 1):
        if not ABANDON_RE.search(option):
            return number, option, "first option that does not abandon the feature"
    return None


def try_auto_answer(work, turn, opts, meta, auto_state):
    """Answers the pending LOOP.json question on its own, when it can. Returns True when it answered (the loop carries on) and False when it must stop."""
    if not getattr(opts, "auto_answer", True):
        return False
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    if state.get("status") != "awaiting_user":
        return False
    pick = pick_auto_answer(state)
    if not pick:
        return False
    key = str(state.get("feature") or "general")
    used_here = auto_state["per_feature"].get(key, 0)
    if auto_state["total"] >= opts.max_auto_answers or used_here >= opts.auto_answers_per_feature:
        print("[loop] automatic answer limit reached (%d in total, %d on %s): stopping for you to decide" % (auto_state["total"], used_here, key))
        return False
    number, text, why = pick
    question = re.sub(r"\s+", " ", str(state.get("reason") or "")).strip()
    write_answer(work, "%s [automatic answer from the loop: option %d, %s]" % (text, number, why))
    auto_state["total"] += 1
    auto_state["per_feature"][key] = used_here + 1
    meta.setdefault("auto_answers", []).append({"turn": turn, "feature": key, "option": number})
    decisions.append(work, key, turn, "important", "question answered automatically: \"%s\"" % question[:160],
                     "option %d (%s): %s" % (number, why, text[:140]), "--answer with another option")
    print("[loop] warning: question answered automatically (option %d, %s); noted in .specs/project/loop/VALIDATE.md" % (number, why))
    return True


# The gate tiebreak is for spec wording points. A feature blocked by access, a business rule or a discrepancy goes to the user.
NO_TIEBREAK_RE = re.compile(r"business|discrep|inconsisten|access|credential|secret|password", re.I)


def gate_blocked_features(specs):
    """Features quarantined because they failed the gate (note=gate-...): only the spec is on paper, nothing was implemented yet."""
    features = loop_state.parse_roadmap(read(os.path.join(specs, "ROADMAP.md")))[0]
    return [slug for slug, fields in features.items()
            if fields.get("status") == "blocked" and "gate" in str(fields.get("note") or "") and not NO_TIEBREAK_RE.search(str(fields.get("note") or ""))]


def try_gate_tiebreak(work, turn, opts, meta, auto_state):
    """Gate tiebreak. The spec-reviewer may reject the same spec round after round over wording points (marking a case as
    not-covered, adjusting PATHS, classifying an RG) that the orchestrator itself solves, and the loop then ends `done` without
    the feature. The real check is build + regression, not the spec wording: after the gate rounds the script tells the
    orchestrator to APPROVE the spec, treat the remaining findings as notes for the executor and move on to execution. Once per
    feature (--gate-tiebreaks). Returns True when it answered (the loop carries on)."""
    if not getattr(opts, "auto_answer", True):
        return False
    specs = os.path.join(work, ".specs", "project")
    used = auto_state.setdefault("tiebreaks", {})
    stuck = [slug for slug in gate_blocked_features(specs) if used.get(slug, 0) < getattr(opts, "gate_tiebreaks", 1)]
    if not stuck:
        return False
    slugs = ", ".join(stuck)
    state = load_json(loop_state.state_file(work, "LOOP.json"))
    state.update(
        status="awaiting_user",
        reason="Gate tiebreak: %s failed the gate several times on spec wording points." % slugs,
        options=["approve the spec and run it, remaining findings become notes for the executor", "finish without the feature"],
    )
    save_json(loop_state.state_file(work, "LOOP.json"), state)
    write_answer(
        work,
        "Gate tiebreak (automatic loop decision): after several rejections, APPROVE the spec of %s (status=approved) without redoing the gate. "
        "Treat the remaining findings as mandatory notes for the executor; a case that depends on data, a secret or a write that "
        "TEST-ENVIRONMENT.md does not cover becomes not-covered in regression.md. Record it in VALIDATE.md and move on to execution with "
        "baseline and regression: build and regression decide, not the spec wording." % slugs,
    )
    for slug in stuck:
        used[slug] = used.get(slug, 0) + 1
    meta.setdefault("gate_tiebreaks", []).append({"turn": turn, "features": stuck})
    decisions.append(work, slugs, turn, "important", "gate tiebreak: spec approved without a new gate after several rejections",
                     "run with the remaining findings as notes; build and regression decide", "--answer asking to redo the gate")
    print("[loop] warning: gate tiebreak for %s (spec approved, findings become notes); noted in .specs/project/loop/VALIDATE.md" % slugs)
    return True
