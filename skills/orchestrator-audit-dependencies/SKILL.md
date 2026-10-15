---
name: orchestrator-audit-dependencies
description: Audits whether any ROADMAP feature depends on another without declaring `deps=` (a forgotten dependency), by reading the real code imports (TypeScript/JavaScript, Python, C#) and the spec text, and records the suggestions in .specs/project/DEPENDENCIES.md. Use before running the loop, after writing new specs, before dispatching a parallel wave, or when the user asks "audit the dependencies", "is there a forgotten dependency", "can this run in parallel?". Do NOT activate for package dependencies (npm/NuGet) or for deciding the product delivery order.
---

# Audit dependencies between features

Depends on the `orchestrator` skill (script `${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/audit_deps.py`, ROADMAP in `.specs/project/`).

## Why it exists

The loop runs features **in parallel** and trusts the `deps=` of the ROADMAP. The `spec-writer` fills it in, and may forget. A forgotten dependency = two features changing linked things at the same time. `loop_state.py validate` only checks what is written; this skill looks for what is **missing**.

## How it works

```
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/audit_deps.py <root>             # only shows
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/audit_deps.py <root> --write    # records in DEPENDENCIES.md
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/audit_deps.py <root> --apply   # writes deps= into the ROADMAP and records
```

Evidence, strongest to weakest:
1. **import**: a file in feature B's `paths` imports something located inside feature A's `paths` (not yet `verified`).
2. **text**: B's spec cites A's slug or a path of A, or lists A under "Dependencies".
3. **review**: `deps=A` declared with no evidence at all (may be a legitimate ordering dependency; nothing is removed).

A suggestion that would close a cycle becomes `ciclo` and is not applied. A suggestion `recusada` (refused) by the user does not come back.

## Where it is recorded

`.specs/project/DEPENDENCIES.md`, one line per pair, with the evidence (`file:line`):
```
- [pending] screen -> orders | import | deps=orders | apps/web/screen.ts:12 import { Order } from '../api/orders'
- [applied] coupon -> pix-payment | text | deps=pix-payment | .specs/features/coupon/spec.md:20 ...
- [refused] ui -> api | texto | ...
```
Marks: `pending` (awaiting decision), `applied` (already in the ROADMAP), `refused` (the user said no), `cycle`, `review`. The file is the record: the orchestrator reads it in Phase 0 and the loop's `--check` warns if there is any `pending`.

## Usage rule

- **Interactive:** show the `pending` suggestions to the user with the evidence. They approve one by one (or all). Only then `--apply`; for any they refuse, change it to `[refused]` in the file.
- **Unsupervised loop / autopilot:** run with `--apply` before each wave. Adding `deps=` only makes execution more serial (cheap); erring toward parallel corrupts code. If `--apply` fails ROADMAP validation, nothing is written: take it to `VALIDATE.md`.
- Run again **after feature A's code exists**: the `import` side only sees code that has already been written.

## Limits (tell the user)

- A dependency on code that **does not exist yet** and that the spec does not mention **is not seen**.
- Covers TypeScript/JavaScript (relative + tsconfig alias), Python and C# (`using` by namespace, `ProjectReference`). Other languages: only the text side.
- It is a heuristic: text evidence may be a false positive (the spec just mentions the other feature). That is why the user decides in interactive mode.
