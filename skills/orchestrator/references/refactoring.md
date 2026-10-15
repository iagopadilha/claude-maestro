# Refactoring in the loop

Refactoring an app or an area to the project's standard is not an ordinary feature: it has its own order (shell before pages), its own definition of done (searches that return zero) and requires proving that behavior did not change. This file says how the loop handles it **without changing the ROADMAP grammar**: refactoring is a normal feature (`kind=front` or `back`) marked by the slug prefix.

## When it applies

- The user asks to "refactor", "bring it to standard", "bring the legacy code to standard", or the roadmap has `refactor-*` features.
- **An explicit refactoring request changes the scope**, even in a `LEGACY` project: the minimal-edit rule stops applying **only inside that feature's `PATHS`**. Record in `STATE.md` the user's phrase that authorized it. Outside the `PATHS` nothing changes.
- Without an explicit request, the usual `Conformance:` applies (STANDARD refactors only what it touched; LEGACY minimal edit).

## How it appears in the ROADMAP

Slug with the `refactor-` prefix. No new field.

```
- [refactor-shell] status=pending kind=front paths=src/layout,src/components/toast.tsx,src/lib
- [refactor-orders-page] status=pending kind=front paths=src/pages/orders deps=refactor-shell
- [refactor-customers-page] status=pending kind=front paths=src/pages/customers deps=refactor-shell
```

- **One feature per page** (front) or per service/module (back), with disjoint `paths`: that way the scheduler runs in parallel those that do not collide.
- **Shell first** (front): `refactor-shell` is a `deps=` of all pages. It brings i18n, toast, dialog/popover/sheet, layout, header, states and the base grid, copying from the style guide's templates. Internal order in the style guide's refactoring reference.
- A page is a **whole** feature: never leaves a page half-done nor mixes old and new on the same screen.
- Back: one feature per large class or flow (fetch, build, save), with the split proposed by the `spec-writer` (smell, strength, moves, risks, as in `backend-dev` N3).

## What the spec of a `refactor-*` feature must have

The `spec-writer` writes, besides the normal content:
1. **Survey of the piece**: what the page/class does today (route, what a row does on click, business rule, permission, quirks that stay). Existing behavior **stays**.
2. **List of what is outside the standard** (smells or deviations from the style guide) and what will change.
3. **Observable definition of done**: for front, the gate searches below returning zero in the page's files; for back, names and structure conforming to the skill. No test command (orchestrator testing rule).
4. Regression **only if the ROADMAP has `regression=yes` on the feature's line** (explicit user request, approved with the roadmap): the `spec-writer` writes `regression.md` and the loop does a baseline before editing and a comparison after (`references/regression.md`). Without the field, there is no automatic regression; the delivery only offers it, as in the dev skills.

## Search gate (front, cheap)

The executor runs, on the page files it touched, the search from the style guide's refactoring reference (its checklist section). It is the same kind of thing as the typecheck/lint it already runs: it is not a test. What is left over is fixed before returning `DONE`. Report in `NOTES`: "gate searches: zero".

## What the loop does not do here

- Does not refactor outside the feature's `PATHS` nor migrate the whole codebase "along the way".
- Does not change business rules. A business bug found during refactoring: fix it only if the spec says so, otherwise it becomes a line in `VALIDATE.md`.
- Worktree: a long refactor in parallel with a normal feature uses a worktree in interactive mode (`dispatch.md`). In headless there is no worktree: the feature runs alone in the wave.
