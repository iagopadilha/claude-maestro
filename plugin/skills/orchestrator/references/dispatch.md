# Dispatch: routing, collision, parallelism, worktree

## Choosing the executor

Decision order:

1. `KIND` of the spec — `back` → `executor-back`; `front` → `executor-front`; `generic` → `executor-generic`.
2. `KIND: both` — **split into two dispatches**, one per layer, each with the subset of `PATHS` that belongs to it. If the two subsets are disjoint, they may run in parallel. If the feature cannot be cut that way, run serially: back first, front after.
3. `PATHS` breaks ties when `KIND` is doubtful. API, domain, service, repository, migration directories → back. Component, page, hook, UI route, style directories → front. Infra, script, CI, config, docs → generic.

When dispatching `executor-generic`, include in the prompt a line `SKILL: <name>` when a useful domain skill exists (for example `migration` for a schema migration; never `github` — git belongs to the orchestrator). With no suitable skill, omit the line.

## What always goes in the executor prompt

- Path of the spec and, if they exist, of `design.md` and `tasks.md`. **Path, not content.**
- The literal `PATHS` list, with the instruction not to leave it, and the line `SCOPE_FEATURE: <slug>` (the guard reads it to limit editing to this feature's `PATHS` only, not the union of the wave).
- A warning when another executor is running in parallel on the same tree, with the neighbor's paths.
- Multi-repo project: the worktree path of every repo the `PATHS` touch (from `meta.repos`), the `PATHS` as absolute paths in those worktrees, and the `repo-order=` of the feature. Dispatch the repos of one feature in that order (producer before consumer); the executor edits only inside those worktrees and never in the user's original repo folders.
- The verifier's `FAIL` block or the reviewer's `FINDINGS`, when it is a resend.

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

| Phase | Parallel? |
|---|---|
| Brownfield | Yes, always. Seven disjoint documents, 3 at a time. |
| Specs | Yes. They write only in `.specs/features/<slug>/`, never collide. |
| Gate | Yes. Read-only. |
| Execution | Only with disjoint `PATHS` or a worktree. |
| Verification | Serial by default. Two simultaneous builds on the same repo fight over cache and artifacts. |
| Commit | Always serial, always the orchestrator. |

Before dispatching 2+ executors in parallel, **check disjointness by code**, not by eye: `python "<app>/harness.py" state disjoint <root> <slug1> <slug2>` (exit 1 = `paths` conflict).

Ceiling of 3 simultaneous in any phase. Dispatch the whole batch in a single message, otherwise it runs in series.

## .NET projects

- **Verification (its own phase, `verifier`):** `verify.py` finds the `.sln`/`.slnx`/`.csproj` closest to the root (up to 3 folders down, ignoring bin/obj) and runs `dotnet build <target>`; `dotnet test` if there is a test project; `dotnet format --verify-no-changes` only when an `.editorconfig` exists (without it, format would fight legacy code). A compile error becomes `FAIL` with the first `CSxxxx` line.
- **Executor:** may run `dotnet build <csproj of what it touched>` (the build does an implicit restore). `dotnet restore` and `dotnet add package` stay blocked by the guard (rule `install`): a new dependency only if the spec asks for it.
- **Starting the app (E2E, `verifier-flow`):** `dotnet run --launch-profile https`, or `dotnet build` and `dotnet run --no-build --project <csproj> --urls http://127.0.0.1:<port>`. See `flow-validator/references/ENVIRONMENT.md`.
- **Loop worktree:** born without `bin/obj`. The repo needs a `.gitignore` with `bin/` and `obj/` (the preflight warns), otherwise the build dirties the tree and the loop stops.

## Resend

When resending a feature (gate rejected, verifier failed), send it to the **same type of agent**, with the reason pasted in the prompt. Do not change model — the Sonnet ceiling also applies on resend. There is no automatic third attempt: the second failure goes to quarantine.
