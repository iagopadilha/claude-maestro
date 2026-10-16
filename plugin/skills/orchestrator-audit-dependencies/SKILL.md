---
name: orchestrator-audit-dependencies
description: Audits whether any ROADMAP feature depends on another without declaring `deps=` (a forgotten dependency), by reading the real code imports (TypeScript/JavaScript, Python, C#) and the spec text, and records the suggestions in .specs/project/loop/DEPENDENCIES.md. Use before running the loop, after writing new specs, before dispatching a parallel wave, or when the user asks "audit the dependencies", "is there a forgotten dependency", "can this run in parallel?". Do NOT activate for package dependencies (npm/NuGet) or for deciding the product delivery order.
---

# Audit dependencies between features

Depends on the `orchestrator` skill and on the Loop Harness app (engine script `audit_deps.py`, called through `python "<app>/harness.py" audit`; ROADMAP in `.specs/project/`). `<app>` = the app folder, resolved as in the `orchestrator` skill's `SKILL.md` (section Runtime: `LOOP_APP_HOME`, or the `app_root_dir` field of `~/.loop-harness/config.json`; with no app, ask the user to clone the repository and run `python <repo>/app/harness.py install` once, and stop).

## Why it exists

The loop runs features **in parallel** and trusts the `deps=` of the ROADMAP. The `spec-writer` fills it in, and may forget. A forgotten dependency = two features changing linked things at the same time. `harness.py state validate` only checks what is written; this skill looks for what is **missing**.

## How it works

```
python "<app>/harness.py" audit <root>             # only shows
python "<app>/harness.py" audit <root> --write     # records in DEPENDENCIES.md
python "<app>/harness.py" audit <root> --apply     # writes deps= into the ROADMAP and records
```

Evidence, strongest to weakest:
1. **import**: a file in feature B's `paths` imports something located inside feature A's `paths` (not yet `verified`).
2. **text**: B's spec cites A's slug or a path of A, or lists A under "Dependencies".
3. **review**: `deps=A` declared with no evidence at all (may be a legitimate ordering dependency; nothing is removed).

A suggestion that would close a cycle becomes `cycle` and is not applied. A suggestion `refused` by the user does not come back.

## Where it is recorded

`.specs/project/loop/DEPENDENCIES.md`, one line per pair, with the evidence (`file:line`):
```
- [pending] screen -> orders | import | deps=orders | apps/web/screen.ts:12 import { Order } from '../api/orders'
- [applied] coupon -> card-payment | text | deps=card-payment | .specs/features/coupon/spec.md:20 ...
- [refused] ui -> api | text | ...
```
Marks: `pending` (awaiting decision), `applied` (already in the ROADMAP), `refused` (the user said no), `cycle`, `review`. The file is the record: the orchestrator reads it in Phase 0 and `harness.py check` warns if there is any `pending`. The panel shows the same dependencies live.

## Usage rule

- **Interactive:** show the `pending` suggestions to the user with the evidence. They approve one by one (or all). Only then `--apply`; for any they refuse, change it to `[refused]` in the file.
- **Unsupervised loop / autopilot:** run with `--apply` before each wave. Adding `deps=` only makes execution more serial (cheap); erring toward parallel corrupts code. If `--apply` fails ROADMAP validation, nothing is written: take it to `VALIDATE.md`.
- Run again **after feature A's code exists**: the `import` side only sees code that has already been written.
- Only the orchestrator runs `--write`/`--apply`: the guard blocks it in subagents (`state-script`).

## When to run

| Moment | Run? | How |
|---|---|---|
| After writing or adopting the specs, before approving the roadmap | yes | `--write`, read everything, decide one by one |
| Before each parallel wave (`--wave` greater than 1) | yes | interactive: `--write` and review; headless: the loop engine (`harness.py loop`) already applies it |
| After feature A was implemented and verified | yes | the `import` side only sees code that already exists |
| Roadmap with 1 feature or serial wave (`--wave 1`) | not needed | there is no parallelism to protect |
| To decide product delivery order or a package dependency | no | outside this skill's scope |

## Limits (tell the user)

- A dependency on code that **does not exist yet** and that the spec does not mention **is not seen**.
- Covers TypeScript/JavaScript (relative + tsconfig alias), Python and C# (`using` by namespace, `ProjectReference`). Other languages: only the text side.
- It is a heuristic: text evidence may be a false positive (the spec just mentions the other feature). That is why the user decides in interactive mode.

## Known noise in C# (lesson from a real run)

- The `import` side matches by **whole namespace**. A `using Company.Domain.Interfaces;` or `using Company.Data.Services;` exists in almost every file, so any pair of features that has files in those namespaces becomes a suggestion. In a C# project with few namespaces, almost all of that is a false positive.
- An aggregator file (DI registration, `Program.cs`) imports everything and produces `[cycle]` suggestions against every feature. If a roadmap feature splits that file (e.g. `refactor-di-by-area`), the noise disappears after it runs.
- **Read every suggestion before concluding.** Never cut the output with `head`: in a real run half was left unread and the conclusion "they are all false positives" had no basis.
- How to judge: an `import` only counts when the file imports a **type** defined in the other feature's `paths` (a class, interface or extension it creates), not just the namespace. `text` (the spec cites the other's slug or a path) is usually reliable. When in doubt, applying `deps=` is cheap (more serial); refusing means writing `[refused]` in `DEPENDENCIES.md`.
- When reporting to the user, separate: "real", "namespace noise" and "aggregator cycle", with the count of each.
