---
name: tlc-spec-driven
description: Project and feature planning with 4 adaptive phases - Specify, Design, Tasks, Execute. Auto-sizes depth by complexity. Creates atomic tasks with verification criteria, atomic git commits, requirement traceability, and persistent memory across sessions. Stack-agnostic. Use when (1) Starting new projects (initialize vision, goals, roadmap), (2) Working with existing codebases (map stack, architecture, conventions), (3) Planning features (requirements, design, task breakdown), (4) Implementing with verification and atomic commits, (5) Quick ad-hoc tasks (bug fixes, config changes), (6) Tracking decisions/blockers/deferred ideas across sessions, (7) Pausing/resuming work. Triggers on "initialize project", "map codebase", "specify feature", "discuss feature", "design", "tasks", "implement", "validate", "verify work", "UAT", "quick fix", "quick task", "pause work", "resume work". Do NOT use for architecture decomposition analysis (use architecture skills) or technical design docs (use create-technical-design-doc).
license: CC-BY-4.0
metadata:
  author: Felipe Rodrigues - github.com/felipfr
  version: 2.0.0
---

> **User rule on tests (takes precedence over `references/tasks.md`, `implement.md` and `validate.md`).**
> Spec, design and tasks **carry no tests**: no test task, no "Tests pass", no test command in "Verify".
> "Done when" describes observable behavior. Verification is not embedded in each task nor run at the start:
> it is a **phase of its own, at the end**, and **only when the spec specifies it or the user says** validation is needed (curl on the routes,
> E2E, suite). Without that it does not run. A reported failure: fix it and stop, without revalidating on your own. CI covers the rest.
> Typecheck/lint of the touched files stay cheap and are part of the delivery.

# Tech Lead's Club - Spec-Driven Development

Plan and implement projects with precision. Granular tasks. Clear dependencies. Right tools. Zero ceremony.

```
┌──────────┐   ┌──────────┐   ┌─────────┐   ┌─────────┐
│ SPECIFY  │ → │  DESIGN  │ → │  TASKS  │ → │ EXECUTE │
└──────────┘   └──────────┘   └─────────┘   └─────────┘
   required      optional*      optional*     required

* Agent auto-skips when scope doesn't need it
```

## Auto-Sizing: The Core Principle

**The complexity determines the depth, not a fixed pipeline.** Before starting any feature, assess its scope and apply only what's needed:

| Scope       | What                     | Specify                                                 | Design                                          | Tasks                         | Execute                                               |
| ----------- | ------------------------ | ------------------------------------------------------- | ----------------------------------------------- | ----------------------------- | ----------------------------------------------------- |
| **Small**   | ≤3 files, one sentence   | **Quick mode** — skip pipeline entirely                 | -                                               | -                             | -                                                     |
| **Medium**  | Clear feature, <10 tasks | Spec (brief)                                            | Skip — design inline                            | Skip — tasks implicit         | Implement + verify                                    |
| **Large**   | Multi-component feature  | Full spec + requirement IDs                             | Architecture + components                       | Full breakdown + dependencies | Implement + verify per task                           |
| **Complex** | Ambiguity, new domain    | Full spec + [discuss gray areas](references/discuss.md) | [Research](references/design.md) + architecture | Breakdown + parallel plan     | Implement + [interactive UAT](references/validate.md) |

**Rules:**

- **Specify and Execute are always required** — you always need to know WHAT and DO it
- **Design is skipped** when the change is straightforward (no architectural decisions, no new patterns)
- **Tasks is skipped** when there are ≤3 obvious steps (they become implicit in Execute)
- **Discuss is triggered within Specify** only when the agent detects ambiguous gray areas that need user input
- **Interactive UAT is triggered within Execute** only for user-facing features with complex behavior
- **Quick mode** is the express lane — for bug fixes, config changes, and small tweaks

**Safety valve:** Even when Tasks is skipped, Execute ALWAYS starts by listing atomic steps inline (see [implement.md](references/implement.md)). If that listing reveals >5 steps or complex dependencies, STOP and create a formal `tasks.md` — the Tasks phase was wrongly skipped.

## Project Structure

```
.specs/
├── project/
│   ├── PROJECT.md      # Vision & goals (Loop Harness: short context for subagents, see references/project-init.md)
│   ├── RULES.md       # Limiting rules of the app: ALWAYS read, any phase/feature (model: ${CLAUDE_PLUGIN_ROOT}/shared/PROJECT_RULES.md)
│   ├── ROADMAP.md      # Features & milestones
│   └── STATE.md        # Memory: decisions, blockers, lessons, todos, deferred ideas
├── codebase/           # LEGACY location of the brownfield map; new maps go to docs/codebase/ at the project root
│   ├── STACK.md
│   ├── ARCHITECTURE.md
│   ├── CONVENTIONS.md
│   ├── STRUCTURE.md
│   ├── TESTING.md
│   ├── INTEGRATIONS.md
│   └── CONCERNS.md
├── features/           # Feature specifications
│   └── [feature]/
│       ├── spec.md     # Requirements with traceable IDs
│       ├── context.md  # User decisions for gray areas (only when discuss is triggered)
│       ├── design.md   # Architecture & components (only for Large/Complex)
│       └── tasks.md    # Atomic tasks with verification (only for Large/Complex)
└── quick/              # Ad-hoc tasks (quick mode)
    └── NNN-slug/
        ├── TASK.md
        └── SUMMARY.md
```

## Workflow

**New project:**

1. Initialize project → PROJECT.md + ROADMAP.md
2. For each feature → Specify → (Design) → (Tasks) → Execute (depth auto-sized)

**Existing codebase:**

1. Map codebase → 7 brownfield docs in `docs/codebase/` at the project root (versioned; generic, stable, NOT a source of truth: the code wins; read on demand, only the one the task needs; update the one a feature really changes, briefly, in the same change). A project that already has `.specs/codebase/` keeps working: read `docs/codebase/` first and fall back to it
2. Initialize project → PROJECT.md + ROADMAP.md
3. For each feature → same adaptive workflow

**Quick mode:** Describe → Implement → Verify → Commit (for ≤3 files, one-sentence scope)

## Context Loading Strategy

**Base load (~15k tokens):**

- RULES.md (if exists, read FIRST: limiting rules; wins over spec/design/tasks; conflict = ask the user)
- PROJECT.md (if exists)
- ROADMAP.md (when planning/working on features)
- STATE.md (persistent memory)

**On-demand load:**

- Codebase docs (when working in existing project)
- CONCERNS.md (when planning features that touch flagged areas, estimating risk, or modifying fragile components)
- spec.md (when working on specific feature)
- context.md (when designing or implementing from user decisions)
- design.md (when implementing from design)
- tasks.md (when executing tasks)

**Never load simultaneously:**

- Multiple feature specs
- Multiple architecture docs
- Archived documents

**Target:** <40k tokens total context
**Reserve:** 160k+ tokens for work, reasoning, outputs
**Monitoring:** Display status when >40k (see [context-limits.md](references/context-limits.md))

## Commands

**Project-level:**
| Trigger Pattern | Reference |
|----------------|-----------|
| Initialize project, setup project | [project-init.md](references/project-init.md) |
| Create roadmap, plan features | [roadmap.md](references/roadmap.md) |
| Map codebase, analyze existing code | [brownfield-mapping.md](references/brownfield-mapping.md) |
| Document concerns, find tech debt, what's risky | [concerns.md](references/concerns.md) |
| Record decision, log blocker, add todo | [state-management.md](references/state-management.md) |
| Record rule, "never/always" project limit | [state-management.md](references/state-management.md) (section Limiting Rules) |
| Pause work, end session | [session-handoff.md](references/session-handoff.md) |
| Resume work, continue | [session-handoff.md](references/session-handoff.md) |

**Feature-level (auto-sized):**
| Trigger Pattern | Reference |
|----------------|-----------|
| Specify feature, define requirements | [specify.md](references/specify.md) |
| Discuss feature, capture context, how should this work | [discuss.md](references/discuss.md) |
| Design feature, architecture | [design.md](references/design.md) |
| Break into tasks, create tasks | [tasks.md](references/tasks.md) |
| Implement task, build, execute | [implement.md](references/implement.md) |
| Validate, verify, test, UAT, walk me through it | [validate.md](references/validate.md) |
| Quick fix, quick task, small change, bug fix | [quick-mode.md](references/quick-mode.md) |

## Feature Dependencies and Scope Guard

Two ideas that make a spec schedulable, whether one person or many agents run it. Both are declared in the spec's **Metadata** block ([specify.md](references/specify.md)); neither needs any tool beyond this skill.

**Dependencies (`DEPS`).** A feature says what must exist before it starts: other features (`<slug>`, must be done first) and shared folders it only imports (`dir:<folder>`). This is the feature-level graph (nodes = features, edges = DEPS, no cycles). It is separate from `Depends on` between tasks inside one feature (`tasks.md`). Practical rules: a feature starts only when its `<slug>` deps are done; two features may run in parallel only if their `PATHS` do not overlap and neither edits a folder the other imports (`dir:`).

**Scope guard (`PATHS`).** The implementer edits only inside the feature's `PATHS`. Out-of-scope file needed? Stop and ask. This is a prompt rule by default.

**Optional: enforce it with a `PreToolUse` hook.** A `PreToolUse` hook is a script the harness runs before every tool call; exiting with code 2 blocks the call and the stderr message goes back to the agent. It turns the scope rule from "the agent should obey" into "the agent cannot disobey". Typical rules: block edits outside the active feature's `PATHS`, block `git push`, block writes to production. It can be set in the agent's or skill's frontmatter (`hooks: PreToolUse`) or in `settings.json`. The `orchestrator` skill ships a ready one (`hooks/guard.py`, which reads the scope file under `.specs/project/` with the `PATHS` per feature); if it is installed you can reuse it, otherwise write a small script with the same contract (JSON on stdin, exit 2 to block). Never assume the hook exists: if it is absent, the prompt rule above is all there is.

**Failure policy (when a verification fails).** A fixable failure (compile, lint, type error) goes back to the implementer with the error, up to 2 attempts. A contradiction that cannot be fixed (the spec requires something a project rule forbids, e.g. a file the lint/build rejects) is not retried: stop on the first failure, record why, and ask the user. Never "fix" it by deleting the project rule or the requested file.

**Repo hygiene for unattended runs.** If the work will run in a separate worktree (orchestrator loop), the repo needs a `.gitignore` covering build output (`bin/`, `obj/`, `node_modules/`, `dist/`) and everything committed first; otherwise a build leaves the tree looking dirty and the run stops.

## Skill Integrations

This skill coexists with other skills. Before specific tasks, check if complementary skills are installed and prefer them when available.

### Frontend / backend → frontend-dev, backend-dev (automatic)

When the feature touches frontend or backend code, **load the matching dev skill from Specify onwards, not only at Execute**: `frontend-dev` (with the project's style-guide skill for visuals, if any) for React/Vite/TS, `backend-dev` for .NET/NestJS. Both are optional: if not installed, follow the project conventions and say so. Effects on each phase:

- **Specify (first step):** read `Conformance:` in the project `CLAUDE.md` (rules N0 of the dev skills). **Missing = stop and ask the user** whether the project is already on the new standard (`STANDARD`) or legacy (`LEGACY`), explaining why; use `${CLAUDE_PLUGIN_ROOT}/shared/classify_structure.py` for the evidence and record the answer with `--write`. Then read the "Reuse map" and `.specs/project/RULES.md`; requirements reuse what already exists instead of re-specifying it. Declare the feature `PATHS`: work happens only inside them and only in repo code.
- **Design:** in a `STANDARD` project add a section `Outside the standard (to refactor in this feature)` listing only the files/classes the feature will touch that are out of the dev skill's pattern (logic in component, giant service, vague names, nested classes), with the planned moves (smell -> technique -> pattern). In a `LEGACY` project there is no such section: old files get a minimal edit, the new code follows the project's existing pattern, and anything that should be refactored is an attention point for the user. Pieces the feature does **not** touch are never planned, in either case.
- **Tasks:** (STANDARD only) each piece in that section becomes a task `refactor <piece> to the standard` placed **before** the task that adds behaviour to it, with `Done when` describing preserved behaviour (regression offered, not a test task).
- **Execute:** new code and touched pieces end in the dev skill's pattern; delivery carries `Verification:` and `Regression:` lines.

### Diagrams → mermaid-studio

Whenever the workflow requires creating or updating a diagram (architecture overviews, data flows, component diagrams, sequence diagrams, etc.), **always** check if the `mermaid-studio` skill is installed in the user's environment before proceeding. If it is installed, delegate all diagram creation and rendering to it. If it is not installed, proceed with inline mermaid code blocks as usual and recommend the user install `mermaid-studio` for richer diagram capabilities (rendering to SVG/PNG, validation, theming, etc.). Display this recommendation at most once per session.

### Code Exploration → codenavi

Whenever the workflow requires exploring or discovering things in an existing repository (brownfield mapping, code reuse analysis, pattern identification, dependency tracing, etc.), **always** check if the `codenavi` skill is installed in the user's environment before proceeding. If it is installed, delegate code exploration and navigation tasks to it. If it is not installed, fall back to the built-in code analysis tools (see [code-analysis.md](references/code-analysis.md)) and recommend the user install `codenavi` for more effective codebase exploration. Display this recommendation at most once per session.

## Knowledge Verification Chain

When researching, designing, or making any technical decision, follow this chain in strict order. Never skip steps.

```
Step 1: Codebase → check existing code, conventions, and patterns already in use
Step 2: Project docs → README, docs/, inline comments, docs/codebase/ (fallback .specs/codebase/; read on demand, the code wins)
Step 3: Context7 MCP → resolve library ID, then query for current API/patterns
Step 4: Web search → official docs, reputable sources, community patterns
Step 5: Flag as uncertain → "I'm not certain about X — here's my reasoning, but verify"
```

**Rules:**

- Never skip to Step 5 if Steps 1-4 are available
- Step 5 is ALWAYS flagged as uncertain — never presented as fact
- Do not assume or invent. If you cannot find an answer, say "I don't know" or "I couldn't find documentation for this". An invented API, pattern, or behavior carries through design → tasks → implementation and fails there.

## Output Behavior

**Model guidance:** After completing lightweight tasks (validation, state updates, session handoff), naturally mention once that such tasks work well with faster/cheaper models. Track in STATE.md under `Preferences` to avoid repeating. For heavy tasks (brownfield mapping, complex design), briefly note the reasoning requirements before starting.

Be conversational, not robotic. Don't interrupt workflow—add as a natural closing note. Skip if user seems experienced or has already acknowledged the tip.

## Code Analysis

Use available tools with graceful degradation. See [code-analysis.md](references/code-analysis.md).
