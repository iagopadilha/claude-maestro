# Orchestrator: handoff to the loop, adopting ready-made material, standalone feature vs roadmap

Moved out of SKILL.md to reduce fixed context. Read when the task touches this subject.

## Handoff to the unsupervised loop

Usage flow: **the user decides, the loop executes.** Context, `Conformance:`, roadmap, `RULES.md` and the specs' questions are resolved here, in a normal conversation. When the roadmap is approved, `Conformance:` is saved and no `OPEN_QUESTIONS` remain, **offer the handoff** (once, in one line) instead of continuing on your own if the user wants to run unsupervised:
1. Ask only what is missing: `--wave` (default 3), `--e2e` (validate the flow at the end?), `--setup` (Node projects only: `npm ci`). Pushing the feature branch + PR to the integration branch on completion **is already the default** (`--no-push` turns it off). Also confirm that the repo has a `.gitignore` with `bin/`, `obj/`, `node_modules/` (without it the build would dirty the worktree and the loop would stop with "dirty tree"; `--check` warns). The roadmap must be approved in the panel (`ROADMAP-APPROVED.json`), and, if a feature has `regression=yes` or the loop uses `--e2e`, `.specs/project/TEST-ENVIRONMENT.md` must be filled in (no `FILL_IN`; `SKILL.md`, Phase 2b): the preflight refuses either otherwise.
2. Run `python "<app>/harness.py" check <repo-root> [flags]` (`<app>`: `SKILL.md`, section Runtime) and show the result (or ask the user to press the panel's Check button). Fix the `PROBLEM`s that are yours (e.g. saving `Conformance:`); relay the `WARNING`s.
3. Hand over the final command on one line, `python "<app>/harness.py" loop <repo-root> [flags]` (the same flags `check` printed), open the panel (`python "<app>/harness.py" serve --project <repo-root>` in the background) with the link in one line, and say: run it in a **separate terminal**, outside this conversation, or press Start loop in the panel; it stops and notifies on its own, and the summary goes to the app's output folder (`output_dir`, default `~/Downloads`). Uncommitted changes in the repo do not go to the worktree: commit first.
4. Write to `STATE.md`: `autopilot`, and that the next step is the loop. After that the source of truth for state becomes the worktree `../<project>-worktree-loop`; to resume a decision, the user opens Claude **inside it** (or answers in the panel).
Do not run the loop (`harness.py loop`) on your own inside this conversation (only `harness.py check`): the loop is the user's, in another terminal or in the panel. Hard rule, it holds even with a generic go-ahead ("ok", "go"): only the user launches it, unless they explicitly ask you to run it ("run the loop"); in that case follow the **chat mode** of `SKILL.md` (Hard rules: `check`, warning about the limits, launch in the background, no following). Full guide: `<app>/docs/HOW_TO_USE.md`.

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
