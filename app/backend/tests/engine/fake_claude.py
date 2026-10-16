#!/usr/bin/env python3
"""Simulated `claude -p` to test run_loop.py WITHOUT spending tokens (use: run_loop.py <repo> --claude fake_claude.py).

Plays the headless orchestrator, deterministically, following what the script says in the prompt
(--phase / --ready / --stuck / --e2e): changes the ROADMAP, creates files in the paths, commits one by one, writes STATE and LOOP.json.
The behavior of each feature comes from a JSON pointed to by FAKE_SCENARIO:

  {"cost": 0.1,                         # US$ per turn (goes in total_cost_usd)
   "idle": false,                       # true = does nothing (tests "identical state")
   "output": "json",                    # json | noisy (warning line before the JSON, text after) | garbage (no JSON)
   "cheat": {"turn": 2, "type": "illegal|no_commit"},
   "features": {"slug": {"paths": "apps/a", "deps": "x,dir:y", "scope": "Small|Medium", "kind": "back",
                         "exec": "ok|fail", "gate": "ok|block", "ask": false}}}
"""
import json
import os
import re
import subprocess
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(HERE)), "harness_backend", "engine"))
import loop_state  # noqa: E402

SPECS = os.path.join(".specs", "project")


def read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def write(path, text):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def option(tokens, name, default=""):
    return tokens[tokens.index(name) + 1] if name in tokens and tokens.index(name) + 1 < len(tokens) else default


def set_fields(text, slug, **fields):
    lines = text.splitlines()
    for number, line in enumerate(lines):
        if re.match(r"^- \[%s\]\s" % re.escape(slug), line.strip()) or line.strip() == "- [%s]" % slug:
            for key, value in fields.items():
                if re.search(r"\b%s=\S+" % key, line):
                    line = re.sub(r"\b%s=\S+" % key, "%s=%s" % (key, value), line, count=1)
                else:
                    line += " %s=%s" % (key, value)
            lines[number] = line
    return "\n".join(lines) + "\n"


def git(*args):
    return subprocess.run(["git", *args], capture_output=True, text=True, encoding="utf-8")


def main():
    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    tokens = prompt.split()
    turn = int(option(tokens, "--turn", "1"))
    phase = option(tokens, "--phase")
    ready = [s for s in option(tokens, "--ready").split(",") if s]
    stuck = [s for s in option(tokens, "--stuck").split(",") if s]
    e2e = "--e2e" in tokens

    scenario = json.loads(read(os.environ["FAKE_SCENARIO"]) or "{}")
    plan = scenario.get("features", {})
    state_path = os.path.join(SPECS, "fake_state.json")
    memory = json.loads(read(state_path) or "{}")
    memory.setdefault("tries", {})
    roadmap_path = os.path.join(SPECS, "ROADMAP.md")
    roadmap = read(roadmap_path)

    answers = os.path.join(SPECS, "loop", "ANSWERS.md")
    if "[pending]" in read(answers):
        write(answers, read(answers).replace("[pending]", "[applied]"))
        memory["answered"] = True

    status, reason, options = "continue", "", []
    features, _ = loop_state.parse_roadmap(roadmap)
    done_slugs = []

    if scenario.get("idle"):
        pass
    elif phase == "specs":
        for slug in ready:
            sc = plan.get(slug, {})
            if sc.get("ask") and not memory.get("answered"):
                status, reason = "awaiting_user", "1. %s: may I go with option a) or b)? | options: a) a b) b" % slug
                options = ["a", "b"]
                break
            scope = sc.get("scope", "Small")
            fields = {"scope": scope, "kind": sc.get("kind", "back"), "paths": sc.get("paths", "src/" + slug),
                      "status": "approved" if scope == "Small" else "specced"}
            if sc.get("deps"):
                fields["deps"] = sc["deps"]
            if sc.get("isolation"):
                fields["isolation"] = sc["isolation"]
            roadmap = set_fields(roadmap, slug, **fields)
    elif phase == "gate":
        for slug in ready:
            if plan.get(slug, {}).get("gate") == "block":
                tries = memory["tries"].get(slug, 0) + 1
                memory["tries"][slug] = tries
                roadmap = set_fields(roadmap, slug, tries=tries, **({"status": "blocked"} if tries >= 2 else {}))
            else:
                roadmap = set_fields(roadmap, slug, status="approved")
    elif phase == "execute":
        for slug in ready:  # the wave writes everything first; verification and commit afterwards, one by one
            if plan.get(slug, {}).get("exec", "ok") == "ok":
                folder = features[slug]["paths"].split(",")[0]
                write(os.path.join(folder, slug + ".txt"), "feature %s turn %d\n" % (slug, turn))
        for slug in ready:
            if plan.get(slug, {}).get("exec", "ok") == "ok":
                folder = features[slug]["paths"].split(",")[0]
                git("add", os.path.join(folder, slug + ".txt").replace("\\", "/"))
                git("commit", "-q", "-m", "feat(%s): implement" % slug)
                roadmap = set_fields(roadmap, slug, status="verified")
                done_slugs.append(slug)
            else:
                tries = memory["tries"].get(slug, 0) + 1
                memory["tries"][slug] = tries
                roadmap = set_fields(roadmap, slug, tries=tries, **({"status": "blocked"} if tries >= 2 else {}))
    elif phase == "recover":
        for slug in ready:
            roadmap = set_fields(roadmap, slug, status="approved" if features[slug]["status"] == "building" else "pending")
    elif phase == "final":
        if e2e and not scenario.get("skip_e2e_mark"):
            for slug, f in features.items():
                if f["status"] == "verified" and not f.get("e2e"):
                    roadmap = set_fields(roadmap, slug, e2e="ok")
    elif phase == "none":
        status, reason = "awaiting_user", "nothing moves: features stuck by a blocked dependency: %s" % (",".join(stuck) or "?")

    cheat = scenario.get("cheat") or {}
    if cheat.get("turn") == turn:
        parsed, _ = loop_state.parse_roadmap(roadmap)
        if cheat.get("type") == "illegal":
            target = next((s for s, f in parsed.items() if f["status"] == "verified"), None)
            target = target or next((s for s, f in parsed.items() if f["status"] == "approved"), None)
            if target:
                roadmap = set_fields(roadmap, target, status="pending")
        elif cheat.get("type") == "no_commit":
            target = next((s for s, f in parsed.items() if f["status"] == "approved"), None)
            if target:
                roadmap = set_fields(roadmap, target, status="verified")

    write(roadmap_path, roadmap)
    after, _ = loop_state.parse_roadmap(roadmap)
    closed = all(f["status"] in ("verified", "blocked") for f in after.values())
    if status == "continue" and closed and (not e2e or phase == "final"):
        status, reason = "done", "everything verified or blocked"
    write(state_path, json.dumps(memory))
    if not scenario.get("idle"):
        write(os.path.join(SPECS, "STATE.md"), "turn %d phase %s done %s\n" % (turn, phase, ",".join(done_slugs)))
    write(os.path.join(SPECS, "loop", "json", "LOOP.json"), json.dumps({
        "status": "in_progress" if scenario.get("bad_status") else status, "turn": turn, "feature": ",".join(ready), "reason": reason, "next": "phase " + phase,
        "at": datetime.datetime.now().isoformat(timespec="seconds"), **({"options": options} if options else {}),
    }))
    result = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "ok",
                         "total_cost_usd": scenario.get("cost", 0.1), "permission_denials": scenario.get("denials", [])})
    output = scenario.get("output", "json")  # json | noisy (warning line before, text after) | garbage (no JSON at all)
    if output == "noisy":
        print("Warning: a newer CLI version is available")
        print(result)
        print("Session saved.")
    elif output == "garbage":
        print("<html><body>502 Bad Gateway</body></html>")
    else:
        print(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
