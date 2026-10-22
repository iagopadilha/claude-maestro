# The orchestrator as a multi-agent graph (Graph Engineering applied)

This file maps what the orchestrator and the loop already are, in graph terms, and says where each guarantee is **enforced** (by code or hook) and where it is only **prompt**. Code is the authority: `loop_state.py` and `run_loop.py` (the Loop Harness engine, `<app>/backend/harness_backend/engine/`, called through `harness.py`) and `hooks/guard.py` (this skill).

## Map

| Graph concept | Here | Guarantee |
|---|---|---|
| Graph state (typed) | `ROADMAP.md` (one node per feature, `status=`), `LOOP.json`, `ANSWERS.md`, `RULES.md`; `STATE.md` is only the "now" in prose | **Code:** `loop_state.py` validates grammar, enums, `tries`, `paths`, the `LOOP.json` schema |
| Dependency edges between nodes | `deps=<slug>` on the ROADMAP line (the feature only runs with its deps `verified`) and `deps=dir:<folder>` (collision by shared imported folder) | **Code:** `loop_state.py` validates existing slug, cycle, `building` with an open dep; `next`/`disjoint` apply it |
| Scheduler | `loop_state.py next [--wave N] [--headless]` returns the phase and the turn's features; `run_loop.py` passes them in `--phase/--ready/--stuck` | **Code** decides; the orchestrator executes |
| Supervisor / router node | the orchestrator (chat or headless `claude -p`) | prompt + the script checks what it wrote |
| Worker nodes | `spec-writer`, `spec-reviewer`, `executor-*`, `verifier`, `verifier-flow`, `brownfield-mapper` | **Hook:** `guard.py` profiles (scope, git, PROD, credentials) |
| Conditional edges | `APPROVED/BLOCKED` (gate), `DONE/NEEDS_CONFIRM/TOO_BIG` (executor), `PASS/FAIL/UNVERIFIED` (verifier) | prompt decides; **code** checks that the recorded transition is legal |
| Legal transitions | status machine (below) | **Code:** `loop_state.reachable` rejects e.g. `verified -> pending`, `blocked -> verified` |
| Fan-out / fan-in | lanes (`--wave N`): up to N features in parallel, each its own pipeline with its own agent at every step (execution only with disjoint `paths`), **serial fan-in** (a single state writer: each feature is committed one at a time, as soon as ITS verification passed) | **Code:** `loop_state.py disjoint` before dispatching; `validate` rejects 2 `building` with overlapping paths |
| Checkpointer | `.specs/` + 1 commit per feature + snapshot per turn (`project/loop/internal/snapshots/NNN`) + per-turn log with `session_id` (`project/loop/internal/turns/NNN.json`) | **Code:** `run_loop.py` writes and validates every turn |
| Time travel | `run_loop.py --rewind N [--yes]`: restores state and `git reset --hard` of the loop branch to turn N | **Code** (commits to the reflog; nothing really disappears) |
| Human-in-the-loop | mandatory stops; `awaiting_user` + notification + `--answer` or the panel; roadmap approval only by the user's click (`ROADMAP-APPROVED.json`, guard `approval-write`) | **Code + hook** |
| Anti-loop / deadlock | table below | **Code**, not just prompt |

## Status machine (edge = allowed transition)

```
pending -> speccing -> specced -> approved -> building -> verified
   ^          |           ^ (gate BLOCKED)        |          |
   +----------+           +--- specced            +-> approved (FAIL)   verified -> approved (E2E failed)
any state -> blocked (quarantine)                 blocked -> approved|specced|pending ONLY with a user answer (ANSWERS.md)
```
Within a turn the orchestrator may **chain** several edges (e.g. `pending -> verified`). What is illegal is walking backwards outside the edges above, removing a feature, or marking `verified` without a new commit. `python "<app>/harness.py" state graph <folder>` prints the graph in Mermaid with the features at each node.

## Anti-loop (all ceilings)

| Risk | Ceiling | Where |
|---|---|---|
| The same feature fails again and again (gate, executor, verifier, E2E) | `tries=` on the ROADMAP line; **2 failures = `blocked`** and **the loop pauses and notifies the user** (`--continue-on-blocked` turns off the pause); `tries>=2` outside `blocked` is a violation | `loop_state.py` + `run_loop.py` |
| Turn without progress | 10 turns without progress (default `--max-stall`) | `run_loop.py` |
| State does not change | 2 turns with identical ROADMAP+STATE+HEAD = error | `run_loop.py` |
| Stuck turn | `--timeout` (60 min) kills the process tree | `run_loop.py` |
| Spend per turn | `--budget` (US$ 5) | `claude -p --max-budget-usd` |
| Total spend | `--max-cost` (US$ 30, `0` turns it off) | `run_loop.py` |
| Endless "cosmetic" fixing | the reviewer only returns `FINDINGS` citing a requirement ID; 2 rejections = quarantine; the verifier is objective (build/type/lint) | `contracts.md` + `tries` |
| Account usage limit | waits `--limit-wait` and stops after `--limit-retries` | `run_loop.py` |
| Trivial question stops the loop | automatic answer (recommended option, or the first that does not abandon), at most `--max-auto-answers` (12) and `--auto-answers-per-feature` (3); never on access, business rule, discrepancy, production, push/merge | `run_loop.py` + `loop.md` |
| Gate rejects on wording forever | gate tiebreak once per feature (`--gate-tiebreaks`, default 1) | `run_loop.py` + `loop.md` |
| Harmless permission denial | tolerated (and logged in `VALIDATE.md`) only when the turn advanced; risky ones stop at once | `run_loop.py` + `loop.md` |

## Fan-out rules (wave)
1. Before dispatching 2+ executors: `python "<app>/harness.py" state disjoint <root> slug1 slug2`. Exit 1 = conflict: reduce to 1 or use a worktree per feature (`dispatch.md`).
2. Ceiling of 3 simultaneous (`dispatch.md`). Verification and commit: always serial.
3. Only the orchestrator writes `ROADMAP.md` and `STATE.md` (fan-in with a single writer; there is no state merge to resolve).
4. `SCOPE.json` holds each feature's paths; the guard blocks editing outside the **union** of the in-flight paths.

## Layers against improper push (strongest to weakest)
1. **`claude -p` environment** (headless): `pushurl` of every remote disabled by `GIT_CONFIG_*`. Push fails in git itself, even via `python -c`/subshell.
2. **Guard** (`hooks/guard.py`, Bash and PowerShell): `git-push` per profile + hard rules `push-protected` and `push-force` that not even `PERMISSIONS.json` releases; looks at the whole command text.
3. **`harness.py deliver`** (the engine's `delivery.py`): the only push/PR path; refuses a protected branch, force, an open feature, headless and executors.
4. Agent prompts (guidance only).
The hole that remains in the interactive chat (no `claude -p`): only layers 2 to 4. A command the regex does not recognize would still pass; that is why pushing to `developer` should also be protected on the Git host (branch protection).

## Known limits (honesty)
- Executor identity in the guard: **confirmed live**. The hook receives `agent_id` and `transcript_path` (of the session, not of the subagent); the guard builds `<session>/subagents/agent-<id>.jsonl`, reads the `SCOPE_FEATURE: <slug>` line from the dispatch prompt and limits editing to that feature's `PATHS` (`project/loop/internal/guard.log` records `scope-trace slug=... fields=...`). Without the line or without the file, it falls back to the union of scopes and records `scope-identity` once.
- `deps=` is declared by the `spec-writer`; `validate` only checks what is written. `harness.py audit` (skill `orchestrator-audit-dependencies`) covers what is missing with evidence from **imports** (TypeScript/JavaScript, Python, C#) and from **spec text**; a code dependency on code that does not exist yet and that the spec does not mention stays invisible.
- `isolation=worktree` in headless means "runs alone"; worktree per feature with merge only in interactive mode.
- `blocked` scores 0 in `RANK`: a loop that only blocks keeps accumulating turns without progress and stops at `--max-stall`.
- Parallel `claude -p` sessions (several loops) do not exist: one loop per worktree, sequential turns. Parallelism is inside the turn (subagents).
- `--rewind` rolls back state files and git; it does not undo effects outside the repo (server, database, e-mails already sent).
- Rehearsed live so far: a `Small` feature. A wave with 2+ features and the `--e2e` phase have only passed in tests with a simulated `claude`.
- **Zero-cost suites:** `python -W ignore -m unittest discover -s "<app>/backend/tests" -t "<app>/backend" -v` (the app's suite; the engine's is in `backend/tests/engine/`) and `python -m unittest discover -s "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks" -p test_guard.py -v` (the guard: layout fallback, state folder, approval, panel endpoints, state scripts, delivery, fail-open notifier). The engine suite runs the real `run_loop.py` against a simulated `claude` (`backend/tests/engine/fake_claude.py`) in temporary git repos: happy path with deps/stages/wave, collision, blocked dependency, gate, illegal transition, verified without commit, identical state, cost ceiling, question/`--answer`, `--rewind`, `--check`, plus `loop_state.py` and this skill's `guard.py`. Run them after any change to the loop or the guard. It proves the **script** (scheduling, validation, stopping); it does not prove that the real model obeys `--phase/--ready`, only a live rehearsal shows that.
