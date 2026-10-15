# Orchestrator: handoff to the loop, adopting ready-made material, standalone feature vs roadmap

Moved out of SKILL.md to reduce fixed context. Read when the task touches this subject.

## Handoff to the unsupervised loop

Usage flow: **the user decides, the loop executes.** Context, `Conformance:`, roadmap, `RULES.md` and the specs' questions are resolved here, in a normal conversation. When the roadmap is approved, `Conformance:` is saved and no `OPEN_QUESTIONS` remain, **offer the handoff** (once, in one line) instead of continuing on your own if the user wants to run unsupervised:
1. Ask only what is missing: `--wave` (default 1), `--e2e` (validate the flow at the end?), `--setup` (Node projects only: `npm ci`). Pushing the feature branch + PR to `developer` on completion **is already the default** (`--no-push` turns it off). Also confirm that the repo has a `.gitignore` with `bin/`, `obj/`, `node_modules/` (without it the build would dirty the worktree and the loop would stop with "dirty tree"; `--check` warns).
2. Run `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/run_loop.py <repo-root> --check [flags]` and show the result. Fix the `PROBLEM`s that are yours (e.g. saving `Conformance:`); relay the `WARNING`s.
3. Hand over the final command printed by `--check`, and say: run it in a **separate terminal**, outside this conversation; it stops and notifies on its own, and the summary is written to a file. Uncommitted changes in the repo do not go to the worktree: commit first.
4. Write to `STATE.md`: `autopilot`, and that the next step is the loop. After that the source of truth for state becomes the worktree `../<project>-worktree-loop`; to resume a decision, the user opens Claude **inside it**.
Do not run `run_loop.py` on your own (without `--check`) inside this conversation: the loop is the user's, in another terminal. Full guide: `loop/HOW_TO_USE.md`.

## Adopting ready-made material

A spec that already exists on disk **is not rewritten**. The loop adopts what it finds and only creates what is missing. This is the default rule, not an optional mode.

**Scan (part of Phase 0).** Besides reading `PROJECT.md`, `ROADMAP.md` and `STATE.md`, list `.specs/features/*/`. Every folder with `spec.md` is ready-made material. Do not read the content of the specs at this point — only the paths and the matching ROADMAP line.

**Reconciliation with the ROADMAP**, done by the orchestrator before any dispatch:

| State of the folder | What to do |
|---|---|
| Has `spec.md`, no line in the ROADMAP | Create the line with `status=specced origin=adopted`. Read the spec once to fill `scope`, `kind`, `paths` and `deps`: if it has the `## Metadata` block (`tlc-spec-driven` template), copy from there instead of deducing. |
| Has `spec.md`, line `pending` | Promote to `specced` and mark `origin=adopted`. **Do not dispatch a `spec-writer`.** |
| Line at `specced` or above | Leave as is. |
| Line in the ROADMAP, folder empty or nonexistent | Stays `pending`: it is the normal Phase 3. |

If `scope`, `kind` or `paths` could not be taken from the spec, write `scope=Medium` and the reason in `STATE.md`. Erring toward Medium makes the gate run, which is the cheap side.

**An adopted spec always goes through the gate.** Phase 4 reads the field `origin=adopted` and runs even with `scope=Small`. The `Small` exemption only applies to a spec this loop saw being born; outside material does not have that provenance.

**Gate `BLOCKED` on an adopted spec** goes back to the `spec-writer` in completion mode: the prompt says to **complete and fix the existing file** based on the `FINDINGS`, preserving requirements and IDs already written. Never ask for a rewrite from scratch. The two attempts and the quarantine apply the same way.

**Missing companion file.** A missing `design.md` or `tasks.md` does not block. If the gate asks for one of them in the `FINDINGS`, the same completion dispatch creates what is missing.

## Standalone feature or roadmap?

The ready-made material decides where the loop enters. Classify before dispatching anything:

**Standalone feature** — one feature folder, one coherent scope, `PATHS` from a single area. Then: skip Phases 1, 2 and 3 and enter directly at Phase 4 with that feature. Do not generate a roadmap and do not ask for roadmap approval — mandatory stop 7 does not apply here. Record in `STATE.md` that the loop ran in standalone feature mode, with the path of the adopted spec.

**Roadmap format** — several features, stages, phases, milestones, or more than one folder in `.specs/features/`. Then: follow the normal flow from Phase 2, but **derive the `ROADMAP.md` from the existing material instead of dispatching the `Plan` agent**. Features that already have `spec.md` are born `specced`; those the material only mentions are born `pending` and go through Phase 3. Present the roadmap and wait for approval, as always.

Ambiguous between the two: ask in one line, at the checkpoint, before the first dispatch. Do not guess — the wrong choice here generates a duplicate spec or a roadmap nobody asked for.
