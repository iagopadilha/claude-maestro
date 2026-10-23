# Dispatch: routing, collision, parallelism, worktree

## Choosing the executor

Decision order:

1. `KIND` of the spec — `back` → `executor-back`; `front` → `executor-front`; `generic` → `executor-generic`.
2. `KIND: both` — **split into two dispatches**, one per layer, each with the subset of `PATHS` that belongs to it. If the two subsets are disjoint, they may run in parallel. If the feature cannot be cut that way, run serially: back first, front after.
3. `PATHS` breaks ties when `KIND` is doubtful. API, domain, service, repository, migration directories → back. Component, page, hook, UI route, style directories → front. Infra, script, CI, config, docs → generic.

When dispatching `executor-generic`, include in the prompt a line `SKILL: <name>` when a useful domain skill exists (for example `migration` for a schema migration; never `github` — git belongs to the orchestrator). With no suitable skill, omit the line.

## What always goes in the executor prompt

- The first line `FEATURE: <slug>` (exactly one feature per agent, in every dispatch of every role; the panel's pipeline reads it) and the path of the spec and, if they exist, of `design.md` and `tasks.md`. **Path, not content.**
- The line `TOOLKIT: <path>`, in the prompt of EVERY agent you dispatch (executor, spec-writer, verifier, verifier-flow): the claude-dev-toolkit plugin root (the folder holding `skills/` and `shared/`), computed once per turn with `python "<app>/harness.py" paths --json` (field `toolkit`; empty = the toolkit is missing, `harness.py doctor` reports it). The agent runs the toolkit scripts as `<TOOLKIT>/shared/<script>`.
- The literal `PATHS` list, with the instruction not to leave it, and the line `SCOPE_FEATURE: <slug>` (the guard reads it to limit editing to this feature's `PATHS` only, not the union of the wave).
- A warning when another executor is running in parallel on the same tree, with the neighbor's paths.
- **Task wave dispatch** (build phase, `SKILL.md`): one executor per task, the extra line `SCOPE_TASK: <slug>/<Tn>` next to `SCOPE_FEATURE: <slug>`, and the path of `tasks.md` (the executor reads its own task there). Write SCOPE.json `tasks` (`{"<slug>/<Tn>": [files]}`) BEFORE the wave. The tasks of one wave never share a file (`harness.py tasks waves <slug>` guarantees it); tell each executor the files of its wave neighbors. A feature without `tasks.md` is dispatched as ONE task covering the spec (log it).
- Multi-repo project: the worktree path of every repo the `PATHS` touch (from `meta.repos`), the `PATHS` as absolute paths in those worktrees, and the `repo-order=` of the feature. Dispatch the repos of one feature in that order (producer before consumer); the executor edits only inside those worktrees and never in the user's original repo folders.
- The verifier's `FAIL` block or the reviewer's `FINDINGS`, when it is a resend.

## Monorepo: subproject and contract lines (only when `SUBPROJECTS.json` is confirmed)

Before dispatching any agent for a feature, run `python "<engine>/subprojects.py" for-paths <root> --paths <the feature's paths>` and `python "<engine>/system_map.py" pending <root> --slugs <the feature's deps=>`, and **paste their lines** (`SUBPROJECT:`, `SYSTEM:`, `CONTRACT-PENDING:`) into the prompt of every agent of that feature: executors, verifiers, spec-writer and spec-reviewer. No `SUBPROJECT:` line = nothing changes. Agents rely on their loaded `CLAUDE.md` (with its `maestro:context` block) for stack, conformance and run commands, and read PROJECT.md only for the plan and the capabilities; the line only says which subproject applies.

After a feature is VERIFIED the engine merges its `contract.md` into `docs/codebase/SYSTEM.md` between turns (one feature at a time, no lane rule needed) and records it in LOOP.json as `contract_merge: {slug: {changed, warnings}}`. When the loop status shows `changed: true`, the NEXT checkpoint commit adds `docs/codebase/SYSTEM.md`; show any merge warnings in the checkpoint report. Back-end executors never edit `SYSTEM.md`; front-end executors read it and the pending `contract.md` of their `deps=`. See `references/monorepo.md`.

**Contract delta is part of the dispatch (back-end feature in a monorepo).** When the feature's paths fall in a subproject, the executor prompt MUST contain the `for-paths` output lines as printed (`SUBPROJECT:`, `SYSTEM:`) plus the `CONTRACT-PENDING:` lines, AND this instruction, verbatim in substance: "write .specs/features/<slug>/contract.md (routes, env, events it adds/changes) - required for back-end features in a monorepo". A prompt without those lines is why a real run produced no `contract.md` and nothing reached `SYSTEM.md`. Before marking such a feature `verified` the orchestrator (or the verifier it dispatched) checks that `.specs/features/<slug>/contract.md` exists; if it is missing, send the executor back once for it instead of verifying. The engine double-checks after the turn: a back-end feature that became `verified` without the file gets a `WARNING contract` line in `VALIDATE.md` and the warning in `LOOP.json` `contract_merge`. The instruction must also say: "use exactly these lines, no table" (`- [add|change|remove] route|dto|auth|env <identity> — <purpose> (<file>)`), e.g. `- [add] route GET /health — liveness probe (Api/HealthController.cs)`; a contract.md with no such line merges nothing and raises a warning.

## Collision between features

Two features collide when their `PATHS` intersect — the same directory, or one contained in the other.

```
apps/api/orders   x  apps/web/checkout    -> disjoint, parallel OK
apps/api/orders   x  apps/api/orders/card -> collide
packages/ui       x  apps/web/checkout    -> collide if checkout imports packages/ui
```

When in doubt about dependency between packages of a monorepo, treat it as a collision. Erring on the serial side costs time; erring on the parallel side costs corrupted code.

**By code, not by judgment:** the third example above is now declared in the ROADMAP. `checkout` with `deps=dir:packages/ui` collides with whoever has `packages/ui` in `paths`, and `harness.py state disjoint`/`next` already read that. Two features that only import `packages/ui` do not collide. An ordering dependency (B uses what A creates) goes as `deps=<slug-of-A>`: B only runs with A `verified`.

## Isolation decision

| Situation | What to do |
|---|---|
| One feature at a time | No isolation. Current branch. **Default case.** |
| 2+ features, disjoint `PATHS` | Same tree, in parallel. Executors only edit files; **git belongs to the orchestrator**, which removes the index collision. |
| 2+ features touching a shared folder (`packages/`, schema, common types) | Worktree, one per feature. The only case where it pays off. |
| Long refactor in parallel with a normal feature | Worktree for the refactor. |

Do not use `isolation: "worktree"` of the `Agent` tool: it names and cleans up on its own, with no control over name or merge. The protocol below is manual, on purpose.

## Planning the roadmap to run in a wave (default: a single worktree)

User decision: **reuse what exists**, no worktree per feature and no running the loop (`harness.py loop`) more than once on the same repo. Parallelism comes from **designing the roadmap** so the features do not collide, and from running with `--wave N`.

When building the roadmap (Phase 2, and in the `Plan` agent's prompt):
1. **One feature per page, module or service**, each with its own disjoint `paths`. Splitting finer is usually worth more than one big feature.
2. **A shared folder the feature only reads** (e.g. `packages/ui`, common types) goes in `deps=dir:<folder>`: it does not collide with others that also only import. Whoever **edits** the folder is alone in its own feature, and the others depend on it through `deps=<slug>` (e.g. `refactor-shell` before the pages).
3. Check by code, not by eye: `python "<app>/harness.py" state disjoint <root> slug1 slug2`. Collision = becomes a queue (serial), not an error.
4. Tell the user, when presenting the roadmap, **how many features can move together** and suggest `--wave` (2 or 3). Features with `regression=` and `isolation=worktree` move alone in the wave.
5. A collision that cannot be avoided only slows the loop down. **Do not propose a worktree per feature or multiple script runs** by default: in headless there is a single worktree, the engine's, and the manual join is a cost that is only justified after seeing the bottleneck on a real project.

**When to reconsider lanes** (several worktrees, one loop run per lane, join in chat at the end): only if, on a real project, folder collision cannot be avoided **and** the total loop time gets too long because of the queue. That requires `--lane` in `run_loop.py`, a `lane=` field in the ROADMAP, merging of `ROADMAP/STATE/LOOP.json` per lane and a port rule (the `verifier-flow` kills the process on the port; two lanes with E2E at the same time knock each other down). None of that exists today: propose it to the user, do not assume it.

## Worktree protocol

When the orchestrator decides to create a worktree, it **warns the user before creating it**, saying which feature and why there was a collision.

1. Find the repo name and the current branch.
2. `git worktree add ../<repo>-worktree-<slug> -b <slug>` from the current HEAD, never from `main`.
3. Say that it was created and where.
4. Dispatch the executor with `cwd` in the worktree.
5. Verify inside the worktree (the `verifier` runs there).
6. Commit on the worktree branch.
7. Return to the main tree and merge **into the branch the user was on**, never `main`.
8. Merge conflict: **stop and call the user.** Do not resolve it yourself, not even in autopilot.
9. Clean merge: `git worktree remove ../<repo>-worktree-<slug>` and delete the temporary branch.
10. After deleting any worktree (including the `*-worktree-loop` at the end of the loop, or through the panel's `clean_worktrees` action): in the original repo, check out the integration branch and pull, so the next work starts from the merged code.

**When to join:** the merge happens **right after the feature's verification and commit**, into the branch the user was on (in the headless loop, the loop branch), before the next wave and always before `--e2e`: E2E needs everything together, and the next feature may depend on the code. At the end of the loop the single branch is the final one; no surplus worktree is left (step 9 removes each one as soon as the merge closes, and `STATE.md` lists the live ones). **In headless there is no worktree** (it is the engine's): a feature with `isolation=worktree` there runs alone in the wave, in series.

Cost to remember before choosing a worktree: dependencies must be installed in the new tree (expensive in a monorepo), and dev-server ports collide if something comes up. If the path collision is small, serial usually comes out cheaper.

## Parallelism by phase

Every feature is its own lane with ONE agent per step (`SKILL.md`, "Lanes"): never one agent that covers two features, and no feature waits for another to reach the same phase.

| Step | Parallel? |
|---|---|
| Brownfield | Yes, always. Seven disjoint documents, 3 at a time. |
| Specs | Yes, one `spec-writer` per feature. They write only in `.specs/features/<slug>/`, never collide. |
| Gate | Yes, one `spec-reviewer` per feature. Read-only. A feature enters it as soon as its own spec is back. |
| Execution | One executor per feature, in parallel only with disjoint `PATHS` (and deps verified) or a worktree; a colliding feature waits as `approved` while the others go on. |
| Execution (task waves) | Inside one feature, one executor per task of the wave, at most `Max parallel tasks` (ROADMAP header, default 3); waves run in order. Across features the total stays inside `Max spend per turn` and `Max minutes per turn`: shrink the wave when short. |
| Verification | One `verifier` per feature, in parallel by default. Serialize only two that would build the same project or solution output (the other lanes keep moving). |
| Commit | Always serial, always the orchestrator; one feature at a time, as soon as ITS verification passed. |

Before dispatching 2+ executors in parallel, **check disjointness by code**, not by eye: `python "<app>/harness.py" state disjoint <root> <slug1> <slug2>` (exit 1 = `paths` conflict).

Ceiling: one agent per lane, except a `building` lane, which runs up to `Max parallel tasks` executors per task wave; so at most `--wave` × `Max parallel tasks` simultaneous (`--wave` default 3, never more than 8; `Max parallel tasks` default 3, 1 to 5), shrunk when the spend or minutes left in the turn are short; brownfield mappers 3. Dispatch the whole round (one agent per live lane, each at its own step) in a single message, otherwise it runs in series.

## .NET projects

- **Verification (its own phase, `verifier`):** `<TOOLKIT>/shared/verify.py` finds the `.sln`/`.slnx`/`.csproj` closest to the root (up to 3 folders down, ignoring bin/obj) and runs `dotnet build <target>`; `dotnet test` if there is a test project; `dotnet format --verify-no-changes` only when an `.editorconfig` exists (without it, format would fight legacy code). A compile error becomes `FAIL` with the first `CSxxxx` line.
- **Executor:** may run `dotnet build <csproj of what it touched>` (the build does an implicit restore). `dotnet restore` and `dotnet add package` stay blocked by the guard (rule `install`): a new dependency only if the spec asks for it.
- **Starting the app (E2E, `verifier-flow`):** `dotnet run --launch-profile https`, or `dotnet build` and `dotnet run --no-build --project <csproj> --urls http://127.0.0.1:<port>`. See `<TOOLKIT>/skills/flow-validator/references/ENVIRONMENT.md`.
- **Loop worktree:** born without `bin/obj`. The repo needs a `.gitignore` with `bin/` and `obj/` (the preflight warns), otherwise the build dirties the tree and the loop stops.

## Resend

When resending a feature (gate rejected, verifier failed), send it to the **same type of agent**, with the reason pasted in the prompt. Do not change model — the Sonnet ceiling also applies on resend. There is no automatic third attempt: the second failure goes to quarantine.
