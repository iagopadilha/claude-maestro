#!/usr/bin/env python3
"""Scripted fake `claude -p` for the audit scenarios (use: run_loop.py <repo> --claude audit_claude.py).

It EXTENDS tests/engine/fake_claude.py (same roadmap/commit mechanics, same FAKE_SCENARIO keys: cost, decision_lines, features, ...): the
scenario file (env AUDIT_SCENARIO, also given as FAKE_SCENARIO) adds the keys below, which run BEFORE the delegated fake turn:

  "actions": [{"turn": 2, "phase": "execute", ...one of...}]       `turn` / `phase` are optional filters
     {"kind": "tool", "label": "push", "agent_type": "executor-back"|null, "tool_name": "Bash", "tool_input": {...},
      "expect": "blocked"|"allowed", "perform": true}               builds the PreToolUse payload Claude Code would send and runs the REAL guard.py
                                                                    (`--profile auto`, exit 2 = blocked). Never executes anything the guard blocked;
                                                                    `perform` only acts when the guard let a Write/Edit/Bash through. A secret path is
                                                                    NEVER opened: a Read is only ever sent to the guard as a payload.
     {"kind": "tasks_set", "slug": "s", "task": "T1", "status": "done"}     python harness.py tasks set ...
  "tasks": {"slug": {"tasks_per_turn": N}}   wave features: in an execute turn the fake runs `harness.py tasks waves`, dispatches ONLY the tasks not
                                             done (overlapping executor threads inside a wave, timestamps logged), commits their files, marks them
                                             done; when all are done the delegated fake turn commits the feature. `tasks_per_turn` caps the tasks
                                             of one turn (a feature that spans two turns: pause/resume).
  "final_loop_json": {...}                   merged over the LOOP.json the delegated fake wrote (after it).
Placeholders in strings: {work} {scratch} {secret} {outside} {approval} {origin}. Everything is logged under env AUDIT_LOG_DIR (events.jsonl).
Scope is written to SCOPE.json by this fake, as the real orchestrator does at dispatch.
"""
import datetime
import json
import os
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE_TESTS = os.path.join(os.path.dirname(HERE), "engine")
sys.path.insert(0, ENGINE_TESTS)
import engine_env  # noqa: E402,F401  (engine on sys.path)
import fake_claude  # noqa: E402
import loop_state  # noqa: E402

REPO_PREFIX = re.compile(r"^([a-z0-9._-]+):(?![/\\])(.+)$")
LOG_LOCK = threading.Lock()
ROADMAP_PARTS = (".specs", "project", "ROADMAP.md")


def now():
    return datetime.datetime.now().isoformat(timespec="milliseconds")


def log_event(kind, **fields):
    folder = os.environ.get("AUDIT_LOG_DIR", "")
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)
    with LOG_LOCK, open(os.path.join(folder, "events.jsonl"), "a", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(at=now(), kind=kind, **fields)) + "\n")


def expand(value, context):
    if isinstance(value, str):
        for key, replacement in context.items():
            value = value.replace("{%s}" % key, replacement)
        return value
    if isinstance(value, dict):
        return {key: expand(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [expand(item, context) for item in value]
    return value


def guard_path():
    explicit = os.environ.get("AUDIT_GUARD")
    if explicit:
        return explicit
    return os.path.join(os.environ.get("LOOP_SKILL_DIR", ""), "hooks", "guard.py")


def transcript_for(feature, task, context):
    """A subagent transcript holding the dispatch marks the guard reads (SCOPE_FEATURE / SCOPE_TASK), as Claude Code writes it."""
    folder = os.path.join(context["scratch"], "transcripts")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "agent-%s-%s.jsonl" % (feature, task or "x"))
    lines = "SCOPE_FEATURE: %s\n" % feature + (("SCOPE_TASK: %s/%s\n" % (feature, task)) if task else "")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps({"role": "user", "content": lines}) .replace("\\n", "\n") + "\n")
    return path


def call_guard(payload):
    """Exactly what Claude Code does: the payload on stdin of `guard.py --profile auto`; exit 2 = blocked, the reason on stderr."""
    result = subprocess.run([sys.executable, guard_path(), "--profile", "auto"], input=json.dumps(payload), capture_output=True, text=True,
                            encoding="utf-8", errors="replace", cwd=payload.get("cwd") or None, timeout=60)
    return result.returncode, result.stderr.strip()


def perform_tool(tool_name, tool_input, cwd):
    """Only reached when the guard let the call through. Bash runs in the TEMP scratch (push locked by the engine env, `gh` is the shim)."""
    if tool_name in ("Write", "Edit"):
        path = tool_input.get("file_path", "")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(tool_input.get("content", tool_input.get("new_string", "written by the fake\n")))
        return "wrote " + path
    if tool_name == "Bash":
        result = subprocess.run(tool_input.get("command", ""), shell=True, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        return "exit %d: %s" % (result.returncode, (result.stdout + result.stderr).strip()[:300])
    return "not performed (%s is only ever sent to the guard)" % tool_name


def run_tool_action(action, context):
    label, expect = action.get("label", action.get("tool_name", "tool")), action.get("expect", "allowed")
    tool_input = expand(action.get("tool_input", {}), context)
    payload = {"hook_event_name": "PreToolUse", "session_id": "audit-fake", "cwd": context["work"], "tool_name": action["tool_name"], "tool_input": tool_input,
               "transcript_path": os.path.join(context["scratch"], "main.jsonl")}
    if action.get("agent_type"):
        payload["agent_type"] = action["agent_type"]
        payload["agent_id"] = "x"
        if action.get("feature"):
            payload["agent_transcript_path"] = transcript_for(action["feature"], action.get("task"), context)
    code, reason = call_guard(payload)
    blocked = code == 2
    record = {"label": label, "agent_type": action.get("agent_type"), "tool_name": action["tool_name"], "tool_input": tool_input, "exit": code,
              "blocked": blocked, "expect": expect, "ok": blocked == (expect == "blocked"), "reason": reason[:300]}
    if not blocked and action.get("perform", expect == "allowed"):
        record["performed"] = perform_tool(action["tool_name"], tool_input, context["work"])
    log_event("tool", **record)
    return record


def harness_cli(*args):
    app = os.environ.get("LOOP_APP_HOME", "")
    return subprocess.run([sys.executable, os.path.join(app, "harness.py"), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)


def tasks_set(work, slug, task, status):
    result = harness_cli("tasks", "set", slug, task, status, "--root", work)
    log_event("tasks_set", slug=slug, task=task, status=status, exit=result.returncode)


def worktrees_of(work):
    """{repo name: worktree path}; a single-repo project has only the primary."""
    try:
        with open(loop_state.loop_path(work, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        return {}
    return {name: item.get("worktree") for name, item in (meta.get("repos") or {}).items() if isinstance(item, dict) and item.get("worktree")}


def write_scope(work, features, tasks_by_feature, trees):
    """SCOPE.json as the orchestrator writes it at dispatch: roots (worktrees), per-feature paths, per-task files."""
    def absolute(entry):
        match = REPO_PREFIX.match(entry)
        if match and match.group(1) in trees:
            return os.path.join(trees[match.group(1)], match.group(2))
        return entry
    scope = {"roots": sorted({work, *trees.values()}),
             "features": {slug: [absolute(item) for item in feature["paths"].split(",") if item] for slug, feature in features.items()},
             "tasks": {"%s/%s" % (slug, task["id"]): [absolute(path) for path in task["files"]] for slug, tasks in tasks_by_feature.items() for task in tasks}}
    path = loop_state.loop_path(work, "SCOPE.json")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(scope, handle)


def git(cwd, *args):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=audit", "-c", "user.email=audit@localhost", *args], capture_output=True, text=True, encoding="utf-8")


def task_target(work, slug, task):
    relative = (task["files"] or ["%s_%s.txt" % (slug, task["id"].lower())])[0]
    return os.path.join(work, relative)


def run_executor(slug, task, work, context, duration, results):
    """One simulated task executor (thread): starts, works for `duration`, writes its file THROUGH the guard, ends; timestamps are logged."""
    log_event("executor_start", slug=slug, task=task["id"], ts=time.time())
    time.sleep(duration)
    target = task_target(work, slug, task)
    results[task["id"]] = run_tool_action({"label": "executor-write %s/%s" % (slug, task["id"]), "agent_type": "executor-back", "feature": slug, "task": task["id"],
                                           "tool_name": "Write", "tool_input": {"file_path": target, "content": "task %s of %s\n" % (task["id"], slug)},
                                           "expect": "allowed"}, context)
    log_event("executor_end", slug=slug, task=task["id"], ts=time.time())


def run_wave_feature(slug, config, work, context):
    """Dispatches only the tasks not done, wave by wave; returns True when every task of the feature is done."""
    result = harness_cli("tasks", "waves", slug, "--root", work)
    try:
        plan = json.loads(result.stdout)
    except ValueError:
        log_event("waves_error", slug=slug, stdout=result.stdout[-300:], stderr=result.stderr[-300:])
        return False
    log_event("waves_plan", slug=slug, waves=plan["waves"], done=plan["done"], problems=plan["problems"])
    by_id = {task["id"]: task for task in plan["tasks"]}
    budget, dispatched = config.get("tasks_per_turn"), 0
    for number, wave in enumerate(plan["waves"], 1):
        if budget is not None:
            wave = wave[:max(0, budget - dispatched)]
        if not wave:
            break
        log_event("wave_dispatch", slug=slug, wave=number, tasks=wave, ts=time.time())
        for task_id in wave:
            tasks_set(work, slug, task_id, "running")
        results, threads = {}, []
        for index, task_id in enumerate(wave):
            thread = threading.Thread(target=run_executor, args=(slug, by_id[task_id], work, context, 0.4 + 0.15 * index, results))
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
        for task_id in wave:  # the orchestrator (this thread) commits and records the status, one task at a time
            outcome = results.get(task_id) or {}
            if outcome.get("performed"):
                git(work, "add", "--", outcome["tool_input"]["file_path"])
                git(work, "commit", "-q", "-m", "feat(%s): %s" % (slug, task_id))
            tasks_set(work, slug, task_id, "done" if outcome.get("ok") and outcome.get("performed") else "failed")
        dispatched += len(wave)
        if budget is not None and dispatched >= budget:
            break
    after = json.loads(harness_cli("tasks", "waves", slug, "--root", work).stdout or "{}")
    return not after.get("tasks")


def run_multi_repo_execute(slug, feature, work, trees):
    """Executes a feature whose paths are `<repo>:<path>`: writes into THAT repo's worktree and commits there (fake_claude only knows one cwd)."""
    done_any = False
    for entry in [item for item in feature["paths"].split(",") if item]:
        match = REPO_PREFIX.match(entry)
        name = match.group(1) if match else "primary"
        tree, folder = (trees.get(name), match.group(2)) if match else (work, entry)
        if not tree:
            continue
        relative = os.path.join(folder, loop_state.feature_folder_name(slug) + ".txt")
        fake_claude.write(os.path.join(tree, relative), "feature %s\n" % slug)
        git(tree, "add", "--", relative.replace("\\", "/"))
        git(tree, "commit", "-q", "-m", "feat(%s): implement (%s)" % (slug, name))
        log_event("multi_repo_commit", slug=slug, repo=name, tree=tree)
        done_any = True
    return done_any


def run_actions(scenario, turn, phase, context, work):
    for index, action in enumerate(scenario.get("actions", [])):
        if action.get("turn") not in (None, turn) or action.get("phase") not in (None, phase):
            continue
        if action.get("once"):
            marker = os.path.join(os.environ.get("AUDIT_LOG_DIR", work), "once-%d.done" % index)
            if os.path.exists(marker):
                continue
            os.makedirs(os.path.dirname(marker), exist_ok=True)
            open(marker, "w").close()
        if action["kind"] == "tool":
            run_tool_action(action, context)
        elif action["kind"] == "tasks_set":
            tasks_set(work, action["slug"], action["task"], action["status"])
        elif action["kind"] == "crash":
            crash_here(action, context, work)


def crash_here(action, context, work):
    """Mid-turn crash: edits worktree files (uncommitted, through the guard), tells the runner, then hangs until the runner kills the engine tree."""
    for relative in action.get("files", []):
        run_tool_action({"label": "partial edit before the crash", "agent_type": "executor-back", "feature": action.get("feature"), "tool_name": "Write",
                         "tool_input": {"file_path": os.path.join(work, relative), "content": "half-done edit"+chr(10)}, "expect": "allowed"}, context)
    log_event("crash_point", files=action.get("files", []), head=git(work, "rev-parse", "--short", "HEAD").stdout.strip())
    open(os.path.join(os.environ.get("AUDIT_LOG_DIR", work), "crash-ready"), "w").close()
    time.sleep(900)


def main():
    argv = sys.argv[1:]
    prompt = argv[argv.index("-p") + 1] if "-p" in argv else ""
    tokens = prompt.split()
    turn = int(fake_claude.option(tokens, "--turn", "1"))
    phase = fake_claude.option(tokens, "--phase")
    ready = [item for item in fake_claude.option(tokens, "--ready").split(",") if item]
    work = os.getcwd()
    with open(os.environ["AUDIT_SCENARIO"], encoding="utf-8") as handle:
        scenario = json.load(handle)
    scratch = os.environ.get("AUDIT_SCRATCH", work)
    context = {"work": work, "scratch": scratch, "secret": os.path.join(scratch, "home", ".claude", "skills", "_shared", ".env"),
               "outside": os.path.join(work, "outside_scope", "hack.txt"), "approval": os.path.join(work, ".specs", "project", "ROADMAP-APPROVED.json"),
               "origin": os.environ.get("AUDIT_ORIGIN", "origin")}
    log_event("turn", turn=turn, phase=phase, ready=ready, resume="--resume" in argv)

    if phase == "execute" and ready:
        features, _ = loop_state.parse_roadmap(fake_claude.read(os.path.join(*ROADMAP_PARTS)))
        trees = worktrees_of(work)
        wave_config = scenario.get("tasks", {})
        import task_waves
        tasks_by_feature = {slug: task_waves.load_feature_tasks(work, slug)["tasks"] for slug in ready if slug in wave_config}
        write_scope(work, {slug: features[slug] for slug in ready if slug in features}, tasks_by_feature, trees)
        run_actions(scenario, turn, phase, context, work)
        remaining = []
        for slug in ready:
            feature = features.get(slug, {})
            if slug in wave_config:
                if run_wave_feature(slug, wave_config[slug], work, context):
                    remaining.append(slug)  # all tasks done: the delegated fake turn commits the feature and marks it verified
                continue
            if REPO_PREFIX.search((feature.get("paths") or "").split(",")[0]):
                if run_multi_repo_execute(slug, feature, work, trees):
                    roadmap_path = os.path.join(*ROADMAP_PARTS)
                    fake_claude.write(roadmap_path, fake_claude.set_fields(fake_claude.read(roadmap_path), slug, status="verified"))
                continue
            remaining.append(slug)
        log_event("execute_dispatch", turn=turn, ready=ready, delegated=remaining)
        if remaining:
            prompt = re.sub(r"--ready\s+\S+", "--ready " + ",".join(remaining), prompt)
        else:
            prompt = re.sub(r"--ready\s+\S+", "", prompt).replace("--phase execute", "--phase final")
        argv[argv.index("-p") + 1] = prompt
        sys.argv[1:] = argv

    if not (phase == "execute" and ready):
        run_actions(scenario, turn, phase, context, work)
    code = fake_claude.main()
    override = scenario.get("final_loop_json")
    if override:
        path = os.path.join(".specs", "project", "loop", "json", "LOOP.json")
        current = json.loads(fake_claude.read(path) or "{}")
        current.update(override)
        fake_claude.write(path, json.dumps(current))
    return code


if __name__ == "__main__":
    sys.exit(main())
