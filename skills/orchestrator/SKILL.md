---
name: orchestrator
description: Turns the current chat into the orchestrator of an engineering loop on top of the tlc-spec-driven engine. Builds the macro roadmap and, at each stage, dispatches subagents to write specs, review them (gate), implement and verify, keeping all state in .specs/. Stops at a checkpoint at the end of each stage with a short report and batched questions. Supports partial use: only map the codebase, only generate the roadmap, only one stage, only one feature. Use when the user asks for /orchestrator, "orchestrate the project", "run the roadmap", "loop engineering", "drive this project end to end", "break it into features and execute", "map this codebase", "document this repo", or asks to drive a whole project instead of an isolated task. Do not use for a single task, a one-off bug or a question — for those use tlc-spec-driven in quick mode or work inline.
disable-model-invocation: true
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: "python \"${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py\" --profile orchestrator"
---

# Orchestrator

The main chat becomes the orchestrator. It does not write specs, implement or verify: it decides, dispatches subagents, keeps state on disk and talks to the user.

The specification engine is still `tlc-spec-driven`. This skill does not replace or reinvent its format — it orchestrates its execution through subagents.

## Testing rule (from the user — overrides the rest of this file and the references)

- **Spec, design and tasks carry no tests.** No "write/run tests" task, no "Tests pass", no test command in "Verify". "Done when" describes observable behavior. CI already covers whatever is automatic.
- **In this skill verification always runs**, but as **its own phase after execution** (Phase 6, `verifier`) — never embedded in a task or at the start of the cycle.
- **Flow validation (`verifier-flow`: curl/E2E) stays opt-in** (Phase 6b): only when the user says they need it. Do not dispatch it on your own initiative.
- **Only exception: refactoring regression**, only for a `refactor-*` feature with `regression=yes` written in the approved ROADMAP (`references/regression.md`). There, the feature's regression file is a flow-validation script (baseline before editing, comparison after), not a unit test, and the "spec carries no tests" rule does not apply to it.
- **The executor writes no tests**, creates no throwaway test script and runs no suite "just in case". Typecheck and lint of the files it touched itself remain its job (cheap).
- Outside the loop (small tweak, one-off fix, visual-only change): do not dispatch a verification agent. The cost (tens to hundreds of thousands of tokens per round) does not pay off.

## Roles

| Agent | Model | Does |
|---|---|---|
| orchestrator (this chat) | the session model | roadmap, dispatch, git, checkpoint, conversation |
| `brownfield-mapper` | sonnet | docs in `.specs/codebase/`; **how many agents depends on repo size** (Phase 1, `loop/scale.py`) |
| `spec-writer` | sonnet | spec of one feature in `.specs/features/<slug>/` |
| `spec-reviewer` | sonnet | spec gate, read-only |
| `executor-back` | sonnet | implements, with `backend-dev` |
| `executor-front` | sonnet | implements, with `frontend-dev` |
| `executor-generic` | sonnet | implements, skill passed in the prompt |
| `verifier` | sonnet | test / build / typecheck / lint |
| `verifier-flow` | sonnet | validates a flow by running the app: routes (curl) and E2E (Playwright), via the `flow-validator` skill. Opt-in |

**Model ceiling: Sonnet.** No subagent goes up to Opus or any model above Sonnet, under any circumstance — not even after a failure. When calling `Agent`, do not pass a `model` other than `sonnet` (or `haiku`, if you want it cheaper). When a feature fails, it escalates to the orchestrator and the user, never to a bigger model.

Never use `subagent_type: "fork"` for executors: a fork inherits the parent's model and breaks the ceiling.

## Checkpoint modes

- `step` — **default**. Stops at the end of each roadmap stage: short report, batched questions, waits for the user.
- `feature` — stops after each completed feature.
- `autopilot` — runs the whole roadmap without stopping at normal checkpoints. Only entered when the user asks explicitly ("run the whole roadmap", "autopilot", "don't ask me").

The mode can change midway: the user says "go on with everything" at a checkpoint and the rest becomes autopilot.

## Partial use

The loop is not all-or-nothing. The user may ask for just a piece, and the skill stops there:

- `/orchestrator map` (map) — runs **only Phase 1** (`brownfield-mapper`s sized by repo size), writes `.specs/codebase/` and stops. No roadmap, no feature, no code touched, no commit. For understanding an unfamiliar repo. Also answers "map this codebase", "document this repo", "what is in this project".
- `/orchestrator roadmap` — assumes the mapping exists (or runs Phase 1 first if `.specs/codebase/` is empty), generates the roadmap and stops for approval. Executes nothing.
- `/orchestrator stage <n>` (stage) — runs only that roadmap stage and stops.
- `/orchestrator continue --headless --turn N --wave W --branch B [--e2e] [--phase F --ready a,b --stuck c]` — **one unsupervised loop round**, called by the script `loop/run_loop.py` (do not type it by hand). See "Headless mode" and `references/loop.md`.
- `/orchestrator <slug>` — runs only that feature, from spec to commit. If `.specs/features/<slug>/spec.md` already exists, the spec is **adopted** instead of rewritten (see Adopting ready-made material).

In any partial use the same hard rules apply: Sonnet ceiling, git only by the orchestrator, no writes to production, state saved before dispatch.

## Headless mode (unsupervised loop)

When the argument has `--headless`, the orchestrator runs **one round** inside `claude -p`, called by `loop/run_loop.py`. Rules that change:
- **Does not ask.** Do not use `AskUserQuestion` and do not end the reply with a question. A mandatory stop that **blocks** = write `awaiting_user` with the batched questions and finish. A doubt that does not block goes to `VALIDATE.md` and the round continues.
- **The round is a wave:** up to `--wave` features (default 1) with disjoint `PATHS`, dispatched together; verification and commit one by one; then it ends. Details in `references/loop.md`.
- **The script already chose the work** (`loop_state.py next`, deterministic): `--phase` (`execute|gate|specs|recover|final|none`) and `--ready` (the wave's features: dependencies `verified`, disjoint `paths`). **Dispatch exactly those**; do not pick others. If `--stuck` lists features, they depend on a `blocked` one: they do not move, take them to the user. `--phase none` with open features = write `awaiting_user` explaining what is blocking. Without `--phase` (manual round or unreadable ROADMAP), decide as in the section The loop.
- **Round Phase 0:** read `.specs/project/ANSWERS.md`; each `[pending]` block is a user decision (coming from `--answer`): apply it to the state and mark it `[applied]` before choosing the work.
- **Last action, always:** write `STATE.md` and then `LOOP.json` **with the command, not by hand**: `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/loop_state.py loop <root> --status <continue|awaiting_user|done|error> --turn <N from the prompt> --feature a,b --reason "..." --next "..." [--options "a|b"] [--to-check "x|y"]`. It validates the enum and stamps the real time. `status` only accepts those four exact values (`em_andamento`, `ok` and the like are rejected by the script and cost a whole round). In `awaiting_user` the `--reason` is the question; add `--options` and `--to-check` when it makes sense. The script sends the notification with both ways to continue: the script's `--answer` command, or replying in the normal chat. The script decides to continue or stop only by that file and the ROADMAP.
- Check `git branch --show-current == --branch`; only commit on that branch; **never push** (the end-of-flow delivery, branch push + PR, belongs to the script). Do not create or delete a worktree.
- `--e2e` = the user authorized the final flow-validation phase (Phase 6b), headless and serial.
- The final summary file is **not yours**: the script generates it (`loop/loop_summary.py`, no Claude) from `phases/stage-NN.md` (fixed headings), ROADMAP, STATE and `git log`.

## Handoff to the loop, adopting ready-made material, standalone feature vs roadmap
Details in `references/entry-adoption-standalone.md`. **Read before Phase 0** (scan of `.specs/features/*/`), when handing over the loop command, and when deciding standalone feature vs roadmap. Rules that always hold: an existing spec is not rewritten (it is adopted with `origin=adopted` and always goes through the gate); never run `run_loop.py` without `--check` inside this conversation.

## Refactoring

A request to refactor ("bring it to standard", `refactor-*` features in the roadmap) follows `references/refactoring.md`: one feature per page or module with disjoint `paths`, `refactor-shell` (shell) as `deps=` of the pages, a search gate on the front end and preserved behavior. The explicit request authorizes refactoring only inside the feature's `PATHS`, even under `LEGACY`. A new or changed front-end screen uses the project's style guide by default, unless explicitly asked otherwise.

## Blocking question vs non-blocking question

**User rule: a one-off doubt never stops the loop.** Classify every `OPEN_QUESTION` and every doubt that appears:
- **Blocks** (mandatory stop, goes to the user): changes architecture, scope, public contract or data; is irreversible or leaves the machine / hits PROD; there are two readings of the spec with incompatible results and the executor cannot choose; a project rule contradicts the spec.
- **Does not block** (move on): small, reversible divergence (a different component than the design, name, text, order, visual detail). Choose the **most conservative and reversible** path, write one line in `.specs/project/VALIDATE.md` (`- [<slug|geral>] <doubt> | escolhi: <what I did> |doubt \| chose: ... \| revert: <how>`) and continue. It is not a question, it is a review item.
`VALIDATE.md` goes in full into the final report, the notification and the PR body as "For you to validate". Only the orchestrator writes it (guard).

## Mandatory stops (also apply in autopilot)

1. A pending `OPEN_QUESTIONS` that **blocks** (see above). A non-blocking question goes to `VALIDATE.md`, it does not stop.
2. Anything that leaves the machine **outside the end of the flow**: opening a PR other than the final one, deploying, sending a message. The only "leaves the machine" allowed is the final delivery (feature branch push + PR to `developer`, `loop/delivery.py`; see Git).
3. **Writing to production.** Always blocked, with no confirm option inside the loop — see the Production section.
4. Merge conflict.
5. Second failure of the same feature (quarantine): stops **only when another feature depends on it** or nothing else can move; otherwise the feature stays `blocked`, the loop continues and it goes into the final report.
6. Executor returned `NEEDS_CONFIRM` or `TOO_BIG`.
7. Roadmap not yet approved by the user — except for an adopted standalone feature, which has no roadmap.
8. Project `CLAUDE.md` without the `Conformance:` line (STANDARD or LEGACY). Ask the user, explaining why, before any spec; save the answer with `${CLAUDE_PLUGIN_ROOT}/shared/classify_structure.py <root> --write ...`. It stays in `STATE.md` as decided.

## The loop

**Phase 0 — orient.** Does `.specs/` exist? If so, read **`RULES.md` first** (limits that always hold, see `references/state.md`), then `PROJECT.md`, `ROADMAP.md`, `STATE.md` and only the previous stage's `phases/stage-NN.md`, scan `.specs/features/*/` and reconcile the ready-made material (see Adopting ready-made material), read `.specs/project/DEPENDENCIES.md` if it exists (`pending` suggestions: see Dependency audit), then resume from where it stopped (see `references/state.md`). If not, proceed.

**Phase 1 — brownfield (existing repo only, once).** The seven documents are STACK, ARCHITECTURE, CONVENTIONS, STRUCTURE, TESTING, INTEGRATIONS, CONCERNS, but **the number of agents depends on the repo**: run `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/scale.py <root>` and follow the tier. `pequeno` (small, up to ~150 code files): **1** agent with all 7 (`DOCS: STACK,ARCHITECTURE,...`); `medio` (medium, up to ~1200): **3** agents by related groups; `grande` (large): 7 agents, 1 per document, at most 3 at a time. Each dispatch carries the line `DOCS: A,B,C`. The files are disjoint between agents, so they do not collide. If the user asks for another number, theirs wins. If `.specs/codebase/` already exists and is populated, skip this phase.

**Phase 2 — roadmap.** Dispatch the `Plan` agent to design the stages and each stage's features. Write the result into `.specs/project/ROADMAP.md` in the format of `references/state.md`, with a status line per feature. Design the features to run in a wave: disjoint `paths`, a folder that is only imported goes in `deps=dir:`, and suggest `--wave` to the user (`references/dispatch.md`, "Planning the roadmap to run in a wave"). No worktree per feature and no multiple script runs by default. **Present it to the user and wait for approval.** This is the highest-leverage gate of the system: a wrong roadmap produces wrong work in parallel. Do not advance without explicit approval, not even in autopilot.

**Phase 3 — stage specs.** For each `pending` feature of the current stage **that has no `spec.md` on disk**, dispatch a `spec-writer`. Up to 3 in parallel. A feature whose spec was adopted in Phase 0 is `specced` and skips this phase entirely. Before each dispatch, mark the feature `speccing` in `STATE.md` (write order in `references/state.md`). Feature that depends on a service outside the project (another backend, e-mail/messaging, a file, a tax lookup, SQL on a customer system): tell the `spec-writer`, in its prompt, to consult an application-map skill (if one exists). With no external service, do not mention it.

Feature with `regression=yes` in the ROADMAP: the `spec-writer` prompt carries the line `REGRESSION: yes` (it writes `regression.md`; see `references/regression.md`). `regression=` only enters the ROADMAP at the explicit request of the user, approved with the roadmap; never on the orchestrator's own initiative.

On receipt: write `PATHS`, `KIND` and `SCOPE` into the ROADMAP, mark `specced` and classify the `OPEN_QUESTIONS` (see "Blocking question vs non-blocking question"): the ones that **block** go in batch to the user at the checkpoint; the ones that do not become a line in `VALIDATE.md` with the conservative choice and the feature moves on.

**Dependency audit (after the specs, before each wave).** Skill `orchestrator-audit-dependencies`: run `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/audit_deps.py <root> --write`. It looks for a forgotten `deps=` (real code imports and citations in specs) and records each suggestion in `.specs/project/DEPENDENCIES.md` (`[pending]`, `[applied]`, `[refused]`, `[cycle]`). **Interactive:** show the `pending` ones with their evidence; run what the user approves with `--apply`; mark what they refuse `[refused]`. **Autopilot in chat:** run with `--apply` (it only makes execution more serial; a cycle or invalid ROADMAP goes to `VALIDATE.md`). **Headless:** `run_loop.py` already applies it before each round (it picks the wave); you only read the result in `DEPENDENCIES.md`. Run it again before each wave: once feature A exists, the `import` side sees more.

**Phase 4 — gate.** For each `specced` feature of size `Medium`, `Large` or `Complex`, dispatch a `spec-reviewer`. A `Small` feature **written by this loop** skips the gate and goes straight to execution; a `Small` feature with `origin=adopted` goes through the gate anyway.

- `APPROVED` with valid `CITED` → `approved`.
- `APPROVED` without valid `CITED` → treat as `BLOCKED`; the stamp is not valid.
- `BLOCKED` → return to the `spec-writer` with the `FINDINGS`. Second rejection of the same spec → quarantine.

**Phase 5 — execution.** For each `approved` feature **whose `deps=` (slugs) are already `verified`**, choose the executor by `KIND` and `PATHS` (see `references/dispatch.md`). Decide parallelism and isolation from the same reference; for the wave use `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/loop_state.py next <root> --wave N`. **Feature with `regression=yes` (baseline before editing):** it moves alone in the wave. Before marking `building`, dispatch the `verifier-flow` with `FEATURE: <slug>`, `MODE: baseline` and `SCRIPT: .specs/features/<slug>/regression.md` (code still old), write `regression=baseline` and only then dispatch the executor. Baseline `UNVERIFIED` or a case that already fails on the old code = stop: `awaiting_user` with the `RG-xx` (do not continue without a baseline; do not fix it in the loop). Details in `references/regression.md`.

Mark `building` before dispatching. Every executor prompt carries the line `SCOPE_FEATURE: <slug>` (the guard uses it to limit editing to the `PATHS` of that feature only).

**Dependencies in the roadmap.** Each line may have `deps=<slug|dir:folder,...>`: `slug` = a feature that must be `verified` first; `dir:<folder>` = a shared folder the feature only IMPORTS (editing goes in `paths`). Whoever edits a folder collides with whoever imports it; two that only import do not collide. The `spec-writer` fills `deps` in the spec (Dependencies section) and the orchestrator copies it to the ROADMAP; `loop_state.py validate` rejects a nonexistent slug, a cycle and `building` with a dep that is not `verified`. `isolation=worktree` marks a feature that collides on a shared folder: interactively it follows the worktree protocol of `dispatch.md`; **in headless it runs alone in the wave** (I never create a worktree there).

`STATUS: DONE` → go to verification. Any other status → see `references/dispatch.md`.

**Phase 6 — verification (its own phase, always runs).** Dispatch the `verifier` on the feature, after execution; see "Testing rule".

- `PASS` → `verified`.
- `UNVERIFIED` → `verified`, with an unverified mark recorded in `STATE.md` and highlighted in the report.
- `FAIL` → return to the same executor with the error. Second failure → quarantine.

**Phase 6a — regression (only with `regression=baseline`).** After the `verifier` gives `PASS` or `UNVERIFIED` and before the commit, dispatch the `verifier-flow` with `MODE: new` (same `SCRIPT`). All `ok` (or `not-covered` explained) -> `regression=ok` and move on. Any `fail` -> `regression=failed`, `tries`+1, the feature goes back to `approved` with `note=regression-failed` and the same executor receives the failed `RG-xx` (expected x obtained); the baseline is not redone (set `regression=baseline` again when re-dispatching). The feature stays `building` until `regression=ok`. Second failure -> quarantine. `verified` only with `regression=ok`.

**Phase 6b — flow validation (opt-in).** Only runs when the user asked ("have a flow validation phase", "validate the flow", "test E2E") or when the prompt indicates a flow to validate. Without a request, skip it. It runs **after** the `verifier` gives `PASS` or `UNVERIFIED`, never in place of it. Dispatch the `verifier-flow` with `FEATURE: <slug>` (flows derived from the spec) or `FLOW: <user text>` / `SCRIPT: <path>`. It starts the applications (tries HTTPS first and falls back to HTTP if there is no answer), one per port, and stops them at the end; do not dispatch in parallel with another `verifier-flow` or with anything that uses the same ports.

The `verifier-flow` may kill and restart local apps that occupy the port it needs (including ones the user left running) and rebuild, without asking the user (standing authorization from them); it records the terminated PID/process in the report. Never on a remote server or production.

- `PASS` → go to commit.
- `UNVERIFIED` → continue, with the mark `(flow not validated)` in `STATE.md` and in the report, together with `LAYERS_NOT_COVERED`.
- `FAIL` → return to the same executor with the failed `STEPS` block and the evidence path. Second failure → quarantine.

**Phase 7 — commit.** Only the orchestrator commits. One atomic commit per verified feature, Conventional Commits message, authored with the configured git identity. Never `push`.

**Phase 8 — checkpoint and phase closing.** At the end of each stage, close the phase (`phases/stage-NN.md` + STATE emptied; see `references/state.md`, "State by phases"). Short report, with the progress drawing (skill `orchestrator-show-progress-graph`: `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py <root>`, and `--write` for `.specs/project/GRAPH.md`), batched questions and the continuity question. In autopilot, emit the report and move on without waiting, except when there is a mandatory stop.

**Phase 9 — delivery (only at the end of the WHOLE roadmap, only the orchestrator).** When no open feature remains (all `verified` or `blocked` resolved by the user), run `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/delivery.py <root>`: it pushes **only the feature branch** (never `developer`/`main`, never force) and opens the PR to `developer` (reuses an already open PR; without `gh`, returns the compare link). Put the line `PR: <url>` in the final report, together with `VALIDATE.md`. In headless, `run_loop.py` delivers (same code), never you.

## Quarantine

A feature that failed twice — at the gate, in execution, in verification or in E2E — does not take the loop down. **Count failures in `tries=` on the ROADMAP line** (1 on the first failure, 2 on the second) and on the second mark `blocked`: the script checks (`tries>=2` outside `blocked` stops the loop). Mark `blocked` in `STATE.md` with the reason in one line, move on to the next feature and list the blocked ones in the checkpoint report. Do not try a third time on your own. **Fixable failure vs unfixable contradiction (user decision, validated live):** a failure the executor can fix (compile error, lint, type) goes back to the same executor with the error, up to 2 attempts. An **unfixable contradiction** (the spec demands something a project rule forbids, e.g. a file the lint/build refuses) **goes to `blocked` on the first failure, with `tries=1` and `note=<reason>`, and asks for the user's decision**: trying again does not solve it and only wastes a round. Never "resolve" the contradiction by deleting the project rule or the requested file. **In the headless loop quarantine only PAUSES when it blocks the operation:** `run_loop.py` detects the feature that just became `blocked` and, **if another feature depends on it** (`presas`), rewrites `LOOP.json` as `awaiting_user` (options "try again" / "skip and continue") and notifies through all channels; the user decides with `--answer`. A blocked feature without dependents **does not stop the loop**: it continues with the others and shows up in the final report and the PR. `--pause-on-blocked` goes back to always pausing; `--continue-on-blocked` never pauses. When `LOOP.json` already comes as `done`, `awaiting_user` or `error`, there is no extra pause.

## Hard rules

**Git.** Only the orchestrator runs write git; subagents are instructed not to. One commit per verified feature, always on a **feature branch** (never directly on `developer`). **Push and PR only at the end of the whole roadmap and only through delivery** (`loop/delivery.py`; in headless, `run_loop.py` calls the same code): push only the feature branch, PR to `developer` with the link in the final report. **Never**, in any flow, push to `developer`/`develop`/`main`/`master`/`release`/`production`/`dev`, no force, no PR merge: the guard blocks it with a hard rule that not even `PERMISSIONS.json` releases. A raw `git push` stays blocked even for the orchestrator; merging into another branch and tagging require an explicit user request.

**Production.** Reading production is allowed: through read-only tools (e.g. a read-only database connection). **Writing to production never happens inside the loop.** Prohibited, with no confirm option: any tool or skill that creates, changes, deletes or assigns something in a production system (permissions, roles, resources, translations, UI configuration, access rules). If a feature depends on such a write, it goes to quarantine with the reason, and the user executes it outside the loop. **Ticket tracker / issue tracker: the loop never writes or edits** (guard `outbound-msg` in every profile, orchestrator included). Subagents inherit the MCP servers, so this rule is repeated in each agent's prompt — do not remove it from there.

**Guard (hook, not just prompt).** The rules above are enforced by `hooks/guard.py`, a `PreToolUse` hook that blocks with exit 2. It runs in the orchestrator chat (this skill's frontmatter, profile `orchestrator`) and in each subagent (frontmatter of the plugin's `agents/*.md`: `executor`, `verifier`, `spec`). It only exists while the loop runs; outside it nothing changes. Rules: `git-write`, `git-push`, `git-add-all`, `pr-release`, `prod-write`, `dev-write`, `outbound-msg`, `secrets`, `install`, `state-write`, `project-deny`, `entrega` (only the orchestrator delivers) and the **hard** ones, which `PERMISSIONS.json` does not release: `push-protected`, `push-force`. Applies to **Bash and PowerShell**; looks at the whole command text (`python -c`, `$(...)`) and at shell writes (`>`, `tee`, `sed -i`, `Set-Content`) to a state or credential file. **A fail-open warns:** an internal guard error lets the command through (to avoid locking up), but logs it and sends a warning through the configured channels (once per hour). In the headless loop `claude -p` also runs with the `pushurl` of every remote disabled (`GIT_CONFIG_*`), so push is impossible even through a regex loophole.
- **The user outranks the hook.** If the user authorizes something explicitly ("you can push", "you can run the install"), write to `.specs/project/PERMISSIONS.json` `{"allow":[{"rule":"git-push","quote":"<user's phrase>","at":"<date>"}]}`. A rule in `allow` never blocks, and each use is recorded in `.specs/project/guard.log`. Only write it with an explicit user phrase in this session; never by inference, never for yourself.
- **Project-specific rule** (e.g. "never create tracking") goes in the same file, in `deny_bash_regex` and `deny_path_regex`. The guard applies it to all agents. **Create it through the `orchestrator-create-block-rule` skill** (`loop/rules.py propose|gravar`): it proposes the regex with examples that must block and that must pass, runs everything in the real guard, only writes with the user's explicit phrase and stores the examples in `rules.tests.json`; the loop's `--check` re-runs the test. Never write a regex without that test.
- A blocked subagent returns `NEEDS_CONFIRM`. Take it to the user at the checkpoint; the block is a mandatory stop like any other.
- Before running unsupervised (long autopilot), confirm the guard responds: `echo '{"tool_name":"Bash","tool_input":{"command":"git push"}}' | python <skills>/orchestrator/hooks/guard.py --profile executor; echo $?` must give `2`.

**Scope per feature (hook).** Before dispatching an executor, and with the line `SCOPE_FEATURE: <slug>` in its prompt, write its `PATHS` to `.specs/project/SCOPE.json` (`{"features":{"<slug>":["<path>", ...]}}`, paths relative to the repo root); remove the entry when the feature leaves `building`. With that file, the guard blocks in the executor any edit outside the `PATHS` and outside the repo; creating a new file **inside** the PATHS is free. An executor that needs a file outside returns `NEEDS_CONFIRM`; the orchestrator only widens the scope if the user agrees (rewriting `SCOPE.json`). Shell commands that write files (`sed -i`, redirection) stay outside the hook: the executor prompt covers that. The `STANDARD`/`LEGACY` policy (minimal edit vs refactor the touched class) is also passed in the executor prompt, read from `CLAUDE.md`.

**State.** Only the orchestrator writes `.specs/project/ROADMAP.md` and `.specs/project/STATE.md`. A subagent writes only inside its own feature folder or, in brownfield, its own document. Write state **before** each dispatch, not after: this chat's context may be compacted, and `.specs/` alone has to be enough to rebuild the loop.

**Concurrency.** At most 3 simultaneous subagents. Dispatch them in a single message, so they really run in parallel.

**Context.** Keep pointers, not content. Never ask a subagent to paste a spec, diff or log in its return; the contracts in `references/contracts.md` already forbid it. If you need the content, read the file.

## Checkpoint report

Short, in the style of the rest of the session. One line per feature:

```
Stage 2 — Checkout | 2/3 verified

pix-payment    verified   apps/api/orders     PASS
checkout-summary  verified   apps/web/checkout   UNVERIFIED (no test/build/lint)
coupon            blocked    packages/ui         gate 2x: acceptance criterion missing

Blocking questions (2):
1. Does the coupon stack with free shipping? a) yes b) no
2. How long until the PIX payment expires? a) 15min b) 30min c) 1h

For you to validate (did not block, I chose the conservative option): 2 items in .specs/project/VALIDATE.md
PR: https://github.com/<org>/<repo>/pull/123   (final report only)

.specs/features/ has the detail.
Shall I continue to stage 3?
```

Do not repeat in the report what is already in the file. Blocked items and questions highlighted; the rest is one line.

## References

- `references/contracts.md` — return format of each agent and how to interpret it.
- `references/entry-adoption-standalone.md` — handoff to the unsupervised loop, adopting ready-made material (`origin=adopted`), standalone feature vs roadmap. Read before Phase 0.
- `references/example-flow.md` — a complete run from the first chat to the PR: files, statuses, a failure, a question, notifications.
- `references/dispatch.md` — executor routing, path collision, parallelism, worktree protocol.
- `references/regression.md` — refactoring regression (`regression=yes` in the ROADMAP, `refactor-*` only): `RG-xx` cases, baseline before editing, comparison after, how to check effect (curl, Playwright, database, logs).
- `references/refactoring.md` — refactoring to the project's standard in the loop: `refactor-*` feature, shell before pages, search gate, scope in a LEGACY project.
- `references/graph.md` — the orchestrator as a graph: map of nodes and edges, legal transitions, anti-loop ceilings, fan-out, checkpoint and `--rewind`, known limits.
- `references/loop.md` — headless mode: arguments, `LOOP.json` contract, wave, final E2E phase, quarantine without dirtying the tree.
- `loop/loop_state.py` — typed state: ROADMAP grammar, legal transitions, `validate`, `disjoint`, `graph` (Mermaid), snapshots and rewind.
- `loop/rules.py` — skill `orchestrator-create-block-rule`: proposes/writes a block rule and proves it in the real guard (`rules.tests.json`).
- `loop/audit_deps.py` — skill `orchestrator-audit-dependencies`: forgotten `deps=` (imports + specs), recorded in `.specs/project/DEPENDENCIES.md`.
- `loop/graph_view.py` — skill `orchestrator-show-progress-graph`: progress as text, short and Mermaid; writes `.specs/project/GRAPH.md`.
- `loop/delivery.py` — end of the flow: feature branch push + PR to `developer` (link in the report). Refuses a protected branch, force, an open feature and headless.
- `loop/scale.py` — sizes Phase 1: how many `brownfield-mapper`s and which documents each one gets.
- `loop/notify.py` — notification (console, toast, file, e-mail, webhook; pluggable notifier); configuration centralized in `${CLAUDE_PLUGIN_ROOT}/shared/.env` (template `.env.example`), read by `${CLAUDE_PLUGIN_ROOT}/shared/envload.py`.
- `loop/HOW_TO_USE.md` — step-by-step of the loop for the user (normal conversation, `--check`, command, what to do at each stop).
- `loop/run_loop.py` and `loop/loop_summary.py` — script of the unsupervised loop and generator of the final summary (Python, no Claude).
- `references/state.md` — status machine, who writes what, write order, resuming after compaction.
