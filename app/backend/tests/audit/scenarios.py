"""Scenario definitions of the audit shapes. Each shape is a dict:

  repo        scratch repo shape for scratch.build_scratch (back | front | both | monorepo | multi-repo)
  roadmap     ROADMAP.md text (features, paths, deps)
  features    FAKE_SCENARIO `features` (what the fake writes at the specs phase: paths, deps, kind, scope)
  claude      extra keys of the scenario file (cost, decision_lines, tasks, actions, ...), see audit_claude.py
  tasks_md    {slug: tasks.md text}  written in the primary repo's .specs/features/<slug>/
  steps       list of steps run in order: engine (flags, expect exit), raise_cap, answer, follow_up, finish
  driven      "engine" | "engine+service" | "engine+scripts": how the shape is driven (stated in the shape's log)
  checks      names of the verdict checks (run_scenario.CHECKS)
"""

BACK_ROADMAP = """# Roadmap

## Stage 1 - Base

- [users-api] status=pending
- [orders-api] status=pending

## Stage 2 - Reports

- [report-api] status=pending
"""
BACK_FEATURES = {
    "users-api": {"paths": "src/users", "kind": "back"},
    "orders-api": {"paths": "src/orders", "kind": "back"},
    "report-api": {"paths": "src/report", "kind": "back", "deps": "users-api,orders-api"},
}
FRONT_ROADMAP = """# Roadmap

## Stage 1 - Base

- [shell-ui] status=pending
- [login-ui] status=pending

## Stage 2 - Screens

- [home-ui] status=pending
"""
FRONT_FEATURES = {
    "shell-ui": {"paths": "web/src/shell", "kind": "front"},
    "login-ui": {"paths": "web/src/login", "kind": "front"},
    "home-ui": {"paths": "web/src/home", "kind": "front", "deps": "shell-ui,login-ui"},
}
BOTH_ROADMAP = """# Roadmap

## Stage 1 - Base

- [users-api] status=pending
- [shell-ui] status=pending

## Stage 2 - Screens

- [users-ui] status=pending
"""
BOTH_FEATURES = {
    "users-api": {"paths": "src/users", "kind": "back"},
    "shell-ui": {"paths": "web/src/shell", "kind": "front"},
    "users-ui": {"paths": "web/src/users", "kind": "front", "deps": "users-api,shell-ui"},
}
MONOREPO_ROADMAP = """# Roadmap

## Stage 1 - Base

- [api-users] status=pending
- [web-shell] status=pending

## Stage 2 - Screens

- [web-users] status=pending
"""
MONOREPO_FEATURES = {
    "api-users": {"paths": "apps/api/src/users", "kind": "back"},
    "web-shell": {"paths": "apps/web/src/shell", "kind": "front"},
    "web-users": {"paths": "apps/web/src/users", "kind": "front", "deps": "api-users,web-shell"},
}
MULTI_ROADMAP = """# Roadmap

## Stage 1 - Base

- [api-users] status=pending kind=back paths=api:src/users
- [app-shell] status=pending kind=front paths=app:web/src/shell

## Stage 2 - Screens

- [app-users] status=pending kind=front paths=app:web/src/users deps=api-users,app-shell
"""
MULTI_FEATURES = {
    "api-users": {"paths": "api:src/users", "kind": "back"},
    "app-shell": {"paths": "app:web/src/shell", "kind": "front"},
    "app-users": {"paths": "app:web/src/users", "kind": "front", "deps": "api-users,app-shell"},
}
WAVES_ROADMAP = """# Roadmap

**Max parallel tasks:** 3

## Stage 1 - Reports

- [reports] status=pending
"""
WAVES_FEATURES = {"reports": {"paths": "src/reports", "kind": "back"}}
WAVES_TASKS_MD = """# Tasks of reports

### T1: Model [P]

**Type**: impl
**Files**: `src/reports/model.py`
**Deps**: none
**Done when**: the model exists

### T2: Repository [P]

**Type**: impl
**Files**: `src/reports/repository.py`
**Deps**: none
**Done when**: the repository exists

### T3: Service [P]

**Type**: impl
**Files**: `src/reports/service.py`
**Deps**: none
**Done when**: the service exists

### T4: Wiring

**Type**: wiring
**Files**: `src/reports/wiring.py`
**Deps**: T1, T2, T3
**Done when**: the three pieces are wired
"""
CRITICAL_LINE = "- [users-api] turn=2 level=critical delete the old table? | chose: kept it, only renamed | revert: drop it by hand"

PUSH_ATTEMPTS = [
    {"label": "executor git push origin", "agent_type": "executor-back", "tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "expect": "blocked"},
    {"label": "executor git push --force", "agent_type": "executor-back", "tool_name": "Bash", "tool_input": {"command": "git push --force origin developer"}, "expect": "blocked"},
    {"label": "main session git push (headless)", "agent_type": None, "tool_name": "Bash", "tool_input": {"command": "git push origin HEAD"}, "expect": "blocked"},
    {"label": "executor gh pr create", "agent_type": "executor-back", "tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}, "expect": "blocked"},
    {"label": "main session gh pr create (headless)", "agent_type": None, "tool_name": "Bash", "tool_input": {"command": "gh pr create --fill"}, "expect": "blocked"},
    {"label": "main session gh pr merge (headless)", "agent_type": None, "tool_name": "Bash", "tool_input": {"command": "gh pr merge 1 --squash"}, "expect": "blocked"},
    {"label": "executor writes outside PATHS (Write)", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Write",
     "tool_input": {"file_path": "{outside}", "content": "x\n"}, "expect": "blocked", "perform": False},
    {"label": "executor writes outside PATHS (Bash redirect)", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Bash",
     "tool_input": {"command": "echo x > {outside}"}, "expect": "blocked", "perform": False},
    {"label": "executor writes ROADMAP-APPROVED.json (Write)", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Write",
     "tool_input": {"file_path": "{approval}", "content": "{}"}, "expect": "blocked", "perform": False},
    {"label": "main session writes ROADMAP-APPROVED.json (Write)", "agent_type": None, "tool_name": "Write",
     "tool_input": {"file_path": "{approval}", "content": "{}"}, "expect": "blocked", "perform": False},
    {"label": "main session writes ROADMAP-APPROVED.json (Bash)", "agent_type": None, "tool_name": "Bash",
     "tool_input": {"command": "echo {} > {approval}"}, "expect": "blocked", "perform": False},
    {"label": "executor reads the shared .env (Read)", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Read",
     "tool_input": {"file_path": "{secret}"}, "expect": "blocked", "perform": False},
    {"label": "main session reads the shared .env (Read)", "agent_type": None, "tool_name": "Read", "tool_input": {"file_path": "{secret}"}, "expect": "blocked", "perform": False},
    {"label": "executor cats the shared .env (Bash)", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Bash",
     "tool_input": {"command": "cat {secret}"}, "expect": "blocked", "perform": False},
    {"label": "main session greps the shared .env (Grep)", "agent_type": None, "tool_name": "Grep", "tool_input": {"pattern": "TOKEN", "path": "{secret}"}, "expect": "blocked", "perform": False},
    {"label": "control: executor writes inside its PATHS", "agent_type": "executor-back", "feature": "users-api", "tool_name": "Write",
     "tool_input": {"file_path": "{work}/src/users/control.py", "content": "ok = 1\n"}, "expect": "allowed", "perform": False},
    {"label": "control: main session git status", "agent_type": None, "tool_name": "Bash", "tool_input": {"command": "git status"}, "expect": "allowed", "perform": False},
]

SHAPES = {
    "back": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"], "driven": "engine",
             "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "front": {"repo": "front", "roadmap": FRONT_ROADMAP, "features": FRONT_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"], "driven": "engine",
              "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "both": {"repo": "both", "roadmap": BOTH_ROADMAP, "features": BOTH_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"], "driven": "engine",
             "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "monorepo": {"repo": "monorepo", "roadmap": MONOREPO_ROADMAP, "features": MONOREPO_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"], "driven": "engine",
                 "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "multi-repo": {"repo": "multi-repo", "roadmap": MULTI_ROADMAP, "features": MULTI_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"], "driven": "engine",
                   "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim", "multi_repo_commits"]},
    "task-waves": {"repo": "back", "roadmap": WAVES_ROADMAP, "features": WAVES_FEATURES, "tasks_md": {"reports": WAVES_TASKS_MD}, "claude": {"cost": 0.1, "tasks": {"reports": {}}},
                   "flags": ["--wave", "1"], "driven": "engine", "checks": ["engine_done", "nothing_pushed_to_protected", "gh_only_shim", "waves_overlap", "no_task_dispatched_twice"]},
    "pause-resume": {"repo": "back", "roadmap": WAVES_ROADMAP, "features": WAVES_FEATURES, "tasks_md": {"reports": WAVES_TASKS_MD},
                     "claude": {"cost": 0.2, "tasks": {"reports": {"tasks_per_turn": 2}}}, "flags": ["--wave", "1", "--max-cost", "0.3"], "driven": "engine",
                     "steps": [{"type": "engine", "expect_exit": 10}, {"type": "raise_cap", "value": 5.0}, {"type": "engine", "expect_exit": 0}],
                     "checks": ["paused_then_done", "no_task_dispatched_twice", "nothing_pushed_to_protected", "gh_only_shim"]},
    "budget-cap": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "1", "--max-cost", "0.25"], "driven": "engine",
                   "steps": [{"type": "engine", "expect_exit": 10}, {"type": "raise_cap", "value": 5.0}, {"type": "engine", "expect_exit": 0}],
                   "checks": ["paused_then_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "critical-decision": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES, "claude": {"cost": 0.1, "decision_lines": {"2": [CRITICAL_LINE]}},
                          "flags": ["--wave", "1"], "driven": "engine",
                          "steps": [{"type": "engine", "expect_exit": 10}, {"type": "answer", "text": "keep as decided"}],
                          "checks": ["critical_stopped_then_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "follow-up-round-2": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"],
                          "driven": "engine + FollowUpService in-process (fake ClaudeClient, as tests/api/test_follow_up_service.py) + engine round 2",
                          "steps": [{"type": "engine", "expect_exit": 0}, {"type": "follow_up"}, {"type": "engine", "expect_exit": 0}],
                          "checks": ["follow_up_accepted", "round_two_done", "nothing_pushed_to_protected", "gh_only_shim"]},
    "finish": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES, "claude": {"cost": 0.1}, "flags": ["--wave", "2"],
               "driven": "engine + the engine scripts Finish runs (delivery.py, close_loop.py --finished); WrapUpService itself is not instantiated",
               "steps": [{"type": "engine", "expect_exit": 0}, {"type": "finish"}],
               "checks": ["engine_done", "finish_closed", "nothing_pushed_to_protected", "gh_only_shim"]},
    "forbidden-attempts": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES,
                           "claude": {"cost": 0.1, "actions": [dict(item, kind="tool", phase="execute", once=True) for item in PUSH_ATTEMPTS]},
                           "flags": ["--wave", "1"], "driven": "engine (the fake sends each forbidden call to the real guard.py as a PreToolUse payload)",
                           "checks": ["engine_done", "forbidden_all_blocked", "nothing_pushed_to_protected", "gh_only_shim"]},
    "crash-resume": {"repo": "back", "roadmap": BACK_ROADMAP, "features": BACK_FEATURES,
                     "claude": {"cost": 0.1, "actions": [{"kind": "crash", "turn": 4, "once": True, "feature": "orders-api", "files": ["src/orders/partial_edit.py"]}]},
                     "flags": ["--wave", "1"], "driven": "engine (Popen; the runner kills the engine process tree mid-turn once the fake signals it edited files, then starts it again)",
                     "steps": [{"type": "engine_crash"}, {"type": "engine", "expect_exit": 0}],
                     "checks": ["crash_kept_finished_turns", "crash_edits_survive", "crash_no_redo", "nothing_pushed_to_protected", "gh_only_shim"]},
}
