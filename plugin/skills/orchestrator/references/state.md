# State: ownership, format, write order, resuming

The loop's state lives in `.specs/`, in the `tlc-spec-driven` format. This document adds what the loop needs and tlc does not define: machine-readable status and file ownership rules.

## `.specs/` layout

```
.specs/
  (the repo map is not here: it lives in docs/codebase/ at the project root, versioned; a project mapped before may still have .specs/codebase/, read as a fallback)
  features/<slug>/       spec.md (design.md, tasks.md) and, with regression, regression.md, regression-baseline.json, regression-new.json
  project/               what belongs to the project and people edit: PROJECT.md ROADMAP.md FLOWS.md STATE.md RULES.md TEST-ENVIRONMENT.md REPOS.json phases/ archive/
    loop/                everything the loop owns. At its root only what a person reads:
      ANSWERS.md  VALIDATE.md  DEPENDENCIES.md  GRAPH.md  NOTICE.txt
      json/              machine state: LOOP.json LOOP-CMD.json MONITOR.json meta.json SCOPE.json PERMISSIONS.json
                         ROADMAP-APPROVED.json rules.tests.json delivery.json last-run.json
      internal/          LOOP.lock PAUSE guard.log .guard-alert monitor.log actions.log whiteboard.json roadmap-goal.md
                         turns/NNN.json (output of each `claude -p`) snapshots/NNN/ (the `--rewind` return point) chat/ jobs/
```

Path rule (a single one, `loop_state.loop_path` in the engine): `.md` and `NOTICE.txt` at the root of `loop/`, `*.json` in `json/`, the rest in `internal/`.

A project on an old layout (the flat plugin files in `.specs/project/`, or the `.specs/loop-logs/` folder) migrates on its own: `run_loop.py` calls `loop_state.py migrate` when it opens the worktree (`python "<app>/harness.py" state migrate <root>` does it by hand; it never overwrites and is idempotent). The guard reads the old flat layout until then, so a block rule does not vanish midway.

## Who writes what

| File | Writer |
|---|---|
| `.specs/project/PROJECT.md` | the app creates it from the template; the orchestrator fills the `**Vision:**` line (the only `FILL_IN`). Short context for subagents (~300 words): vision plus `## Where to look` pointers (`docs/codebase/`, `RULES.md`, `STATE.md`, `ROADMAP.md`, `FLOWS.md`, `TEST-ENVIRONMENT.md`). No stack, goals, scope or constraints |
| `.specs/project/ROADMAP.md` | **only** the orchestrator |
| `.specs/project/FLOWS.md` | **only** the orchestrator (`plan` and `roadmap` modes, and whenever a feature enters or leaves the ROADMAP); the user may edit it. Read by the app (approval gate) and by `verifier-flow` |
| `.specs/project/STATE.md` | **only** the orchestrator (only the "now", see State by phases) |
| `.specs/project/phases/stage-NN.md` | **only** the orchestrator (stage closing summary) |
| `.specs/project/archive/` | **only** the orchestrator (old state, never read on resume) |
| `.specs/project/TEST-ENVIRONMENT.md` | the orchestrator (Phase 2b) and the user (directly or in the panel); short (4 sections: where it runs, login, sample data, never); `FILL_IN` only where the user must decide (at most 3); NAMES only (`$NAME`), values live in the git-ignored `.specs/project/.env.test` or `~/.loop-harness/.env`. Read by `spec-writer` and `verifier-flow` (they run `testenv.py list` to see which names are defined, never the `.env` files); the preflight refuses it missing or with `FILL_IN` when the loop needs it |
| `.specs/project/loop/json/LOOP.json` | **only** the orchestrator (last action of each headless turn, via `harness.py state loop`; contract in `loop.md`) |
| `.specs/project/loop/` `NOTICE.txt`, `json/meta.json`, `json/delivery.json`, `json/LOOP-CMD.json`, `internal/LOOP.lock`, `internal/turns/NNN.json`, `internal/snapshots/NNN/` | **only** the engine (`run_loop.py`) |
| `.specs/project/loop/json/ROADMAP-APPROVED.json` | **only** the app, on the user's click on **Approve roadmap** (signature of features and stages). Never the orchestrator nor a subagent (guard `approval-write`, hard rule) |
| `.specs/project/loop/ANSWERS.md` | the engine (`--answer`) and the panel (answer, note, instruction) create `[pending]`; the orchestrator marks `[applied]` |
| `.specs/project/loop/json/PERMISSIONS.json`, `json/SCOPE.json`, `internal/guard.log` | orchestrator (permissions and scope) and `guard.py` (log) |
| `.specs/project/REPOS.json` | the app (Workspace area of the panel). The orchestrator and the agents only read it, never write it |
| `.specs/project/loop/json/last-run.json` | the engine (Close loop). Read when the loop re-attaches a branch; Reset specs keeps it |
| `.specs/project/loop/DEPENDENCIES.md` | the orchestrator, **via** `harness.py audit --write/--apply` (skill `orchestrator-audit-dependencies`): `deps=` suggestions with evidence; marks `[pending]`, `[applied]`, `[refused]`, `[cycle]`, `[review]`. Read in Phase 0 |
| `.specs/project/loop/json/rules.tests.json` | the orchestrator, **via** `harness.py rules write` (skill `orchestrator-create-block-rule`): examples that must block/pass for each guard rule; `--check` re-runs the test |
| `.specs/project/loop/GRAPH.md` | generated by `harness.py graph --write` (orchestrator at the checkpoint, `run_loop.py` every turn); do not edit |
| `.specs/project/loop/json/MONITOR.json` | the app (absolute URL of the project's panel, `/p/<id>/`); deleted when the app shuts down |
| `.specs/project/loop/internal/roadmap-goal.md`, `internal/jobs/<id>.json`, `internal/chat/`, `internal/actions.log`, `internal/PAUSE` | the app (goal of Generate roadmap, the `claude -p` jobs launched by the panel, panel chats, Act log, pause request) |
| `.specs/project/loop/internal/decisions-review.json` | the app only (panel review marks of the decisions: `{"items": {"<line id>": {"reviewed", "reviewed_at", "change_requested"}}}`); the orchestrator never writes it |
| `.specs/project/loop/VALIDATE.md` | **only** the orchestrator (and the engine for automatic answers, gate tiebreaks and tolerated denials): one line per doubt that did not block (`- [<slug|general>] turn=<N> level=<important|critical> <doubt> \| chose: <choice> \| revert: <how>`); goes in the final summary, the notification, the panel and the PR |
| `docs/codebase/*.md` | `brownfield-mapper`, one agent per file |
| `.specs/features/<slug>/*` | the `spec-writer` of that feature |
| project code | executors, inside the `PATHS` |

A subagent that needs something in the ROADMAP or STATE asks for it through the `NOTES` field of its return. The orchestrator applies it. Without this, two parallel agents overwrite each other's progress.

## Status machine

```
pending -> speccing -> specced -> approved -> building -> verified
                          |          |          |
                          +----------+----------+--> blocked
```

| Status | Means |
|---|---|
| `pending` | is in the roadmap, nobody started |
| `speccing` | `spec-writer` dispatched, response pending |
| `specced` | spec written or adopted from disk; if `Small` **and written by this loop**, skips the gate and goes straight to `approved`; if adopted, goes through the gate even when `Small` |
| `approved` | gate passed, or size `Small` |
| `building` | executor dispatched, response pending |
| `verified` | verifier gave `PASS` or `UNVERIFIED`; ready for commit |
| `blocked` | quarantine: two failures, or dependency on a production write |

`verified` through `UNVERIFIED` carries the mark `(unverified)` — in STATE and in the report. It never disappears.

## Feature line format in the ROADMAP

One line per feature, deterministic, inside the stage it belongs to:

```
## Stage 2 — Checkout

- [card-payment-authorization] status=verified title="Authorize a card payment for an order" scope=Medium kind=back paths=apps/api/orders
- [checkout-order-summary] status=verified title="Show the order summary before paying" scope=Small kind=front paths=apps/web/checkout note=unverified
- [discount-coupon-redemption] status=blocked title="Redeem a discount coupon at checkout" scope=Large kind=both paths=packages/ui,apps/web/checkout
- [monthly-tax-report] status=specced title="Download the monthly tax report" scope=Small kind=back paths=apps/api/tax origin=adopted
```

**Names (descriptive, never abbreviated).** The `[slug]` is the stable id: kebab case, **3 to 6 meaningful words** that say what the feature delivers (`customer-registration-api`, `invoice-pdf-download`), unique, never an abbreviation, a number or a generic word (`cust-reg`, `feature-2`, `misc`, `cfg`, `new-stuff`), and it never changes once the roadmap is approved (the approval signature covers it). `title="..."` is the readable name the panel shows (`features[].name`): **a short sentence of what the user gets**, at most ~80 characters, in double quotes and with no double quote inside (the only field whose value has spaces; put it right after `status=`). The same title heads the feature's `## [slug] Title` in FLOWS.md and the first heading (`# <title>`, without a `Spec:` or slug prefix) of its `spec.md`. A line without `title=` still works (the panel falls back to the spec's first heading, then to the slug): the orchestrator adds the title from the spec when it marks the feature `specced`. The feature folder is the slug with ':' replaced by '-' (slug `backend:version-endpoint` -> `.specs/features/backend-version-endpoint/`); every path of this document and every `.specs/features/<slug>/` in the skill means that folder name.

Field `tries=<n>` (accumulated failures of the feature: gate `BLOCKED`, executor/verifier `FAIL`, E2E `FAIL`). **On each failure the orchestrator increments `tries` on the line; on reaching 2 the feature becomes `blocked`** (quarantine). `tries>=2` outside `blocked` is a violation.

The line is a **typed grammar**, checked by the engine's `loop_state.py` (enums of `status`, `scope`, `kind`, `e2e`; `title="..."` must close its quotes; `paths` mandatory from `approved` on; unique slug). Legal transitions and invariants (`verified` only with a new commit; a feature never disappears; `blocked` only leaves with a user answer) are in `references/graph.md`. Check before closing the turn: `python "<app>/harness.py" state validate <root>`.

Optional field `deps=<slug|dir:folder,...>`: `slug` = a feature that must be `verified` before this one runs; `dir:<folder>` = a shared folder only imported (collides with whoever edits it, not with whoever also only imports). A nonexistent slug, self-dependency, cycle and `building` with a dep that is not `verified` are violations. Optional field `isolation=same-tree|worktree` (default `same-tree`, i.e. same tree): `worktree` marks a feature that collides on a shared folder; in headless it runs alone in the wave. E.g.: `- [checkout] status=approved scope=Small kind=front paths=apps/web/checkout deps=card-payment,dir:packages/ui`.

Optional field `repo-order=<name,name>` (multi-repo only): the order in which the repos of ONE feature are built, producer first (`repo-order=api,web` builds the API before the web app). Without it the order is the order of the `paths=` entries.

In a multi-repo project `paths=` entries may carry a repo prefix: `paths=<repo>:<path>,<repo>:<path>`, where `<repo>` is the `name` in `REPOS.json`. An entry with no prefix belongs to the primary repo, so today's lines stay valid. An unknown repo name is a grammar error naming the feature. Two features collide only when the same repo and an overlapping path are both declared. Example: `- [orders-api] status=approved scope=Medium kind=both paths=api:src/Orders,web:src/api/orders.ts repo-order=api,web`.

Optional field `e2e=ok|failed|not-covered`: result of the final flow-validation phase (only with `--e2e`, see `loop.md`).

`scope`, `kind` and `paths` are born empty and are filled when the `spec-writer` responds, or read from the spec when the feature is adopted from disk.

`origin=adopted` marks a spec that already existed before this loop. It is the field Phase 4 consults: a feature with `origin=adopted` goes through the gate even with `scope=Small`. Without the field, the feature was born here and the `Small` exemption applies. The roadmap prose (stage objective, completion criterion) stays above the lines, in the tlc format.

**`## Open questions`** closes the ROADMAP: one doubt per line, `- <question> | recommended: <option>`, and nothing else in the section; with no doubt left, the single line `- none`. Every non-empty line in it counts as an open question, and approval needs none.

## Loop settings in the ROADMAP header, decisions and follow-ups

- **Header settings** (decided in planning, one `**Name:** value` line each under `**Goal:**`; the engine and the panel read them from the approved ROADMAP): `E2E`, `Dispatch`, `Max spend per turn`, `Max spend for the loop` (default 50), `Max minutes per turn`, `Max parallel tasks` (executors per wave inside one feature, 1 to 5, default 3), `Branch`, `Setup`, `When a feature gets stuck`, `Push and PR`. Only the planning conversation edits them.
- **`meta.json` (engine only)**: `delivery` (`{push_pr, remove_worktree_on_finish}`), `max_cost_usd` (per-run cap the user raised; wins over the default), `cost_warned_at` (the 80 % warning, once), `decisions_mark` (line count of `VALIDATE.md` before the turn; lines after it are the turn's new decisions), `cost_usd`, `turns`. Reaching the cost cap sets LOOP status `paused` with reason `cost cap reached: US$ X of Y`.
- **Decision line** in `VALIDATE.md`: `- [<slug|general>] turn=<N> level=<important|critical> <doubt> \| chose: <choice> \| revert: <how>`; `critical` pauses the loop after the turn (`awaiting_user`, reason `critical decision: ...`). Review marks: `internal/decisions-review.json` (app-owned).
- **Follow-up stage**: a stage `## Stage Follow-up-<n> - Follow-up <n>` appended at the end of the ROADMAP (e.g. `## Stage Follow-up-1 - Follow-up 1`; the engine takes the stage id from the first word after `## Stage`, so it is unique per round), same feature-line grammar as above; earlier stages are never edited.

## Monorepo files

- `.specs/project/SUBPROJECTS.json` (written by the app, confirmed by the user) and `SUBPROJECTS-SUGGESTED.json` (the AI's proposal, merged by the app). Shape and use in `references/monorepo.md`.
- `.specs/project/loop/internal/subprojects/map-queue.json` (the app's per-subproject map queue) and `.specs/project/loop/internal/inventory/<name>.json` (large-file inventory per subproject, `_root` for a single project). Both are the app's, internal.
- `.specs/features/<slug>/contract.md`: written by the back-end executor of the feature (its own folder); merged into `docs/codebase/SYSTEM.md` by the engine after VERIFIED. There is **no scope rule for `SYSTEM.md`**: executors never edit it, the engine does.

## Multi-repo projects: REPOS.json, SCOPE.json, last-run.json

A project is multi-repo when `.specs/project/REPOS.json` lists more than one repository. The primary repo holds `.specs/` and is the loop's cwd; the other repos are extra worktrees of the same loop branch. Single-repo projects have no `REPOS.json` or one entry, and nothing below applies.

- **`REPOS.json`** (`.specs/project/`, written by the app): `{ "repos": [ { "name", "path", "primary", "role", "worktree_path"?, "copy"? } ] }`. `name` is the short name used in `paths=<repo>:` and `repo-order=`; the primary is listed first; `copy` lists git-ignored config files copied into the worktree so the build runs. What the loop actually created (worktree and branch per repo) is `meta.repos` in `json/meta.json`.
- **`role`** (`edit` or `reference`; missing = `edit`, so an old file keeps working): `edit` repos can be changed by the loop; `reference` repos are read only context. The primary is always `edit`. A project is multi-repo when the file lists more than one repository (`multi` is derived; an old `multi` flag is ignored). Optional `reason` (one sentence, from the planning chat: why the repo is in the project and why this role). `edit` = the loop changes it (own worktree on a new `loop/*` branch from `developer`, PR to `developer` at the end); `reference` = read only, no worktree. The planning chat manages the list; there is no scope-confirmation step.
- **Worktrees only where something is written:** the engine creates a worktree for an `edit` repo only when at least one ROADMAP feature names it in `paths=<repo>:`. An `edit` repo no feature changes has no worktree; a `reference` repo never has one. Reference repos reach the agents through `--add-dir` (their real folder, read only; the guard blocks any write there).
- **`readiness.json`** (`.specs/project/loop/internal/planning/`, written by the app's readiness check, one at a time per project): `{"state": "ready|problems|stale|running|error", "at", "problems": [], "warnings": [], "fingerprint"}`. It is the result of `run_loop.py --check --planning`; Approve needs `ready`. The orchestrator and the agents only read it.
- **Task status and `tasks` in SCOPE.json.** The source of truth for task status is `.specs/project/loop/internal/tasks/<slug>.json` (`pending | running | done | failed`), written only by the orchestrator through `harness.py tasks set`, atomically; `tasks.md` gets a copy of the final status only at the feature commit (`harness.py tasks mirror <slug>`), never mid-wave. SCOPE.json also carries `"tasks": { "<slug>/<Tn>": [<absolute path>, ...] }`, written before each wave: an executor with `SCOPE_TASK: <slug>/<Tn>` may edit only that task's files. Resume: `harness.py tasks waves <slug>` returns only the not-done tasks.
- **`SCOPE.json`** (`json/`, written by the orchestrator before each dispatch): `{ "roots": [<every worktree root, primary first>], "features": { "<slug>": [<absolute path in a worktree>, ...] }, "copied": [<absolute path of each copied config file>] }`. Each `paths=<repo>:<path>` becomes `<that repo's worktree>/<path>`. The guard lets an executor edit only those entries and lets the orchestrator write only inside `roots`; it blocks any read of a `copied` file (they are local secrets; the build still reads them). An old file with no `roots` and relative entries still works: relative means relative to the primary root.
- **`last-run.json`** (`json/`, written by the engine at Close loop): per repo the branch, base, head, PR link and whether the worktree was removed. The engine re-attaches an existing branch from it. The orchestrator does not write it.

## FLOWS.md: the flow that proves each feature

`.specs/project/FLOWS.md` holds, for **every** ROADMAP feature, the user-visible (or API) flow that proves it works. Written with the ROADMAP (`plan` and `roadmap` modes); the panel's **Approve roadmap** refuses while a feature has no flow.

```
# Flows

## [customer-api] Register a customer
1. POST /customers with a valid tax id
2. Expected: 201 and the customer id
3. POST /customers with the same tax id
4. Expected: 409

## [customer-screen] Sign up from the web
1. Open /signup, fill name and tax id, submit
2. Expected: confirmation screen with the customer id
```

- **Parse rule (the app's):** a section starts at `^##\s*\[(slug)\]\s*(title)?`; its steps are the lines matching `^\s*\d+[.)]\s+\S`. A feature **has a flow** when its section exists with **at least 2 steps**. A slug in FLOWS.md that is not in the ROADMAP is an `orphan`: a warning, not a block.
- Same slug and the same title as the ROADMAP line (`## [slug] <title>`). Steps state the intent (route and payload, screen and action), never a selector; `Expected:` lines are observable (status, field, visible text, a database row) and are what `verifier-flow` checks. Include the error path when the feature has one. No secrets. A feature with no screen and no route (model, migration, job) states what is run and what is then observed.
- Renaming or removing a feature renames or removes its section in the same edit.
- **Use in validation:** `verifier-flow` with `FEATURE: <slug>` takes the feature's section as the source of the flow, completes it with the spec's acceptance criteria and expands it into the `flow-validator` script (`.specs/features/<slug>/flows.md`). Without a section, the flow comes from the spec, as before.

## Write order: before dispatch, never after

This chat may be compacted in the middle of the loop. If the state only exists in the context, compaction loses which features are in flight, and the loop reprocesses or skips work.

Mandatory sequence at each dispatch:

1. Write the new status (`speccing` / `building`) in the ROADMAP and the matching entry in STATE.
2. Only then call `Agent`.
3. On receiving the response, write the result.

If the loop restarts and finds a feature in `speccing` or `building`, that dispatch was lost. Return the feature to the previous state (`pending` or `approved`) and dispatch again.

## RULES.md: limits always read (outside phase, outside feature)

`.specs/project/RULES.md` holds **only the application's limiting rules** (never/always), with ID `R-xx`, reason, how to apply and origin (the user's phrase and date). Template: `${CLAUDE_PLUGIN_ROOT}/shared/PROJECT_RULES.md` (same model as `<app>/templates/PROJECT_RULES.md`; the panel's Initialize structure button creates an empty `RULES.md` and adds `@.specs/project/RULES.md` to `CLAUDE.md`). The project's `CLAUDE.md` imports it with `@.specs/project/RULES.md`, so it is loaded in **every session, after compaction and in a new conversation**. Read first in Phase 0 and by every subagent (it is in the agents' prompts). **Only the orchestrator** writes it, and only with the user's explicit phrase (in planning mode too: what the user says cannot be done becomes a row here, never a line in PROJECT.md); the guard blocks subagents. Each feature's spec lists `Applicable rules: R-xx`. A rule that can be blocked by command/path also goes into `PERMISSIONS.json` (`deny_*`), through the `orchestrator-create-block-rule` skill.

## State by phases (lean STATE, history in phase files)

A single `STATE.md` grows without bound and is read in full on every resume. So the state is split:

| File | Content | Read on resume? |
|---|---|---|
| `ROADMAP.md` | only the status lines per feature (index) | yes |
| `STATE.md` | only the **now**: checkpoint mode, current stage, features in flight, active quarantine, open questions, live worktrees. Goal: up to ~300 words | yes |
| `phases/stage-NN.md` | **closing of stage NN**: what was delivered, decisions the user gave, lessons, unverified, blocks that remained, and "what the next stage needs to know" | only the one **before** the current stage |
| `archive/` | `STATE` of old loops and phases more than 1 stage away | never, only on request |

**Phase closing (mandatory at the end of each stage, also if the loop stops midway through it):**
1. Write `phases/stage-NN.md` with the fixed sections: `Delivered`, `User decisions`, `Lessons`, `Unverified`, `Blocked`, `For the next stage`. Short prose; slug and path instead of content.
2. Empty from `STATE.md` everything that belongs to the closed stage; keep only what still holds (active quarantine, open question).
3. Update the stage's line in `ROADMAP.md` with `closed -> phases/stage-NN.md`.
4. Stop at the checkpoint with the stage summary (the same text as `For the next stage`).

**Stop in the middle of a stage** (user interrupted, mandatory stop, context running out): write in `STATE.md` a `STOP` block with `where it stopped`, `exact next step`, `what the user decided since the last checkpoint` and `pending question`. A decision given in chat goes to the file right away, not at the end.

**Spec slice.** When dispatching an executor, pass `SECTIONS:` with only the parts the feature uses (e.g. `design §3.6, spec REQ-12..15`) instead of the whole file. A spec or design above ~4k words: the `spec-writer` splits it into numbered sections to allow that.

## What to record in STATE.md

Besides what tlc already asks for (decisions, blockers, lessons, todos, deferred ideas):

- **Current checkpoint mode** (`step`, `feature`, `autopilot`) and when the user changed it.
- **Current stage** of the roadmap.
- **Quarantine**: one block per `blocked` feature, with slug, phase in which it failed, reason in one line and the two attempts.
- **Open questions not yet taken to the user**, and the already answered ones with the user's answer.
- **Unverified features** and why (`UNVERIFIED`).
- **Live worktrees**, with path and branch, so no garbage is left.
- **Codebase map**: a short `## Codebase map` pointer to `docs/codebase/` (legacy `.specs/codebase/`) naming the documents, so agents load only the one the task needs, never all of them.
- **Brownfield gaps** (`GAPS` from the mappers).
- **Adopted specs**: slug, path, and whether `scope`/`kind`/`paths` were deduced or assumed (`scope=Medium` for lack of data).
- **Entry mode**: adopted standalone feature, or roadmap derived from ready-made material.

Write in short prose. STATE is read in full at the start of every session — that is why only the now stays in it; whatever belongs to a closed stage goes to `phases/`.

## Resuming

When invoked in a project that already has `.specs/`:

1. Read `PROJECT.md`, `ROADMAP.md`, `STATE.md` and **only** the `phases/stage-NN.md` of the stage before the current one (if it exists). Do not read feature specs or older phases.
2. Scan `.specs/features/*/` and reconcile the ready-made material with the ROADMAP (adoption table in `SKILL.md`). A folder with `spec.md` and a `pending` line becomes `specced`; a folder with `spec.md` and no line gets a new line at `specced`. Only in that case read the spec, once, to extract `scope`, `kind` and `paths`.
3. Identify the current stage and the recorded checkpoint mode.
4. Recover lost dispatches (features stuck in `speccing` or `building`).
5. If there is an open question in STATE, it is the first thing that goes to the user.
6. Present a short report of where the loop stopped (the `STOP` block of STATE, if any). In `step` and `feature`, ask whether to continue. In a recorded `autopilot`, **do not ask**: continue from where it stopped, unless there is a mandatory stop or an open question in STATE.

The quality criterion of this file: someone who opened `.specs/` with no conversation context could continue the loop exactly from where it stopped.
