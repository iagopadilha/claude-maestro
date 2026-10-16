# Specify

**Goal**: Capture WHAT to build with testable, traceable requirements.

If the feature has ambiguous gray areas (multiple valid approaches for user-facing behavior), the agent will automatically trigger the [discuss gray areas](discuss.md) process within this phase. For clear, well-defined features, it goes straight to the next phase.

## Process

### 1. Clarify Requirements

You are a thinking partner, not an interviewer. Start open — let the user dump their mental model. Follow the energy: whatever they emphasize, dig into that.

Ask conversationally (not as a checklist):

- "What problem are you solving?"
- "Who is the user and what's their pain?"
- "What does success look like?"

If needed:

- "What are the constraints (time, tech, resources)?"
- "What is explicitly out of scope?"

**Challenge vagueness.** Never accept fuzzy answers. "Good" means what? "Users" means who? "Simple" means how? Make the abstract concrete: "Walk me through using this." "What does that actually look like?"

**Know when to stop.** When you understand what they're building, why, who it's for, and what done looks like — offer to proceed.

### 1b. Declare Feature Metadata (SCOPE, KIND, PATHS, DEPS)

Every spec opens with a small metadata block. It costs five lines and lets anything downstream (a person, an orchestrator, a hook) route and schedule the feature **without re-reading or guessing**:

- **SCOPE** — `Small | Medium | Large | Complex` (the auto-sizing table in SKILL.md).
- **KIND** — `back | front | both | generic`. First key to pick who implements it.
- **PATHS** — real directories or files the feature will **edit** (not hypothetical ones). Three uses: choose the implementer, detect collisions between features that could run in parallel, and bound what the implementer may touch (see "Scope guard" in SKILL.md). A spec without plausible `PATHS` is incomplete.
- **DEPS** — what must exist before this feature can start, comma-separated, of two kinds:
  - `<feature-slug>`: another feature that must be done first (this one uses something it creates: a route, table, component, type). Use the exact slug from the roadmap. Not in the roadmap? Do not invent a slug; ask.
  - `dir:<folder>`: a shared folder this feature only **imports**, never edits (e.g. `dir:packages/ui`). Editing a folder goes in `PATHS`. An editor of a folder collides with its importers; two features that only import it do not collide.
  - Nothing needed: `DEPS: none`. Forgetting a dependency makes two features run in parallel over code that does not exist yet, so when in doubt, declare it.

DEPS is **between features**. Dependencies between tasks inside one feature stay in `tasks.md` (`Depends on`, `[P]`). The graph of feature DEPS must have no cycles.

### 2. Capture User Stories with Priorities

**P1 = MVP** (must ship), **P2** (should have), **P3** (nice to have)

Each story MUST be **independently testable** - you can implement and demo just that story.

### 3. Write Acceptance Criteria

Use **WHEN/THEN/SHALL** format - it's precise and testable:

- WHEN [event/action] THEN [system] SHALL [response/behavior]

---

## Template: `.specs/[feature]/spec.md`

```markdown
# [Feature Name] Specification

## Metadata

- **SCOPE:** [Small | Medium | Large | Complex]
- **KIND:** [back | front | both | generic]
- **PATHS:** [dir-or-file-the-feature-edits], [another]
- **DEPS:** [feature-slug | dir:shared-folder], ... | none

## Problem Statement

[Describe the problem in 2-3 sentences. What pain point are we solving? Why now?]

## Goals

- [ ] [Primary goal with measurable outcome]
- [ ] [Secondary goal with measurable outcome]

## Out of Scope

Explicitly excluded. Documented to prevent scope creep.

| Feature     | Reason         |
| ----------- | -------------- |
| [Feature X] | [Why excluded] |
| [Feature Y] | [Why excluded] |

---

## User Stories

### P1: [Story Title] ⭐ MVP

**User Story**: As a [role], I want [capability] so that [benefit].

**Why P1**: [Why this is critical for MVP]

**Acceptance Criteria**:

1. WHEN [user action/event] THEN system SHALL [expected behavior]
2. WHEN [user action/event] THEN system SHALL [expected behavior]
3. WHEN [edge case] THEN system SHALL [graceful handling]

**Independent Test**: [How to verify this story works alone - e.g., "Can demo by doing X and seeing Y"]

---

### P2: [Story Title]

**User Story**: As a [role], I want [capability] so that [benefit].

**Why P2**: [Why this isn't MVP but important]

**Acceptance Criteria**:

1. WHEN [event] THEN system SHALL [behavior]
2. WHEN [event] THEN system SHALL [behavior]

**Independent Test**: [How to verify]

---

### P3: [Story Title]

**User Story**: As a [role], I want [capability] so that [benefit].

**Why P3**: [Why this is nice-to-have]

**Acceptance Criteria**:

1. WHEN [event] THEN system SHALL [behavior]

---

## Edge Cases

- WHEN [boundary condition] THEN system SHALL [behavior]
- WHEN [error scenario] THEN system SHALL [graceful handling]
- WHEN [unexpected input] THEN system SHALL [validation response]

---

## Requirement Traceability

Each requirement gets a unique ID for tracking across design, tasks, and validation.

| Requirement ID | Story       | Phase  | Status  |
| -------------- | ----------- | ------ | ------- |
| [FEAT]-01      | P1: [Story] | Design | Pending |
| [FEAT]-02      | P1: [Story] | Design | Pending |
| [FEAT]-03      | P2: [Story] | -      | Pending |

**ID format:** `[CATEGORY]-[NUMBER]` (e.g., `AUTH-01`, `CART-03`, `NOTIF-02`)

**Status values:** Pending → In Design → In Tasks → Implementing → Verified

**Coverage:** X total, Y mapped to tasks, Z unmapped ⚠️

---

## Success Criteria

How we know the feature is successful:

- [ ] [Measurable outcome - e.g., "User can complete X in < 2 minutes"]
- [ ] [Measurable outcome - e.g., "Zero errors in Y scenario"]
```

---

## Tips

- **P1 = Vertical Slice** — A complete, demo-able feature, not just backend or frontend
- **WHEN/THEN is code** — If you can't write it as a test, rewrite it
- **Requirement IDs are mandatory** — Every story maps to trackable IDs
- **Edge cases matter** — What breaks? What's empty? What's huge?
- **Out of Scope prevents creep** — If it's not here, it doesn't get built
- **Metadata is not optional** — `PATHS` and `DEPS` are what make a spec schedulable; a spec adopted by an orchestrator reads them instead of guessing (and falls back to `Medium`, the expensive side, when they are missing)
- **Confirm before Discuss** — User must approve spec before moving to discuss phase
