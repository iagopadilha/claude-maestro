# Roadmap Creation

**Trigger:** "Create roadmap", "Plan features", "Map project phases"

## Process

Based on PROJECT.md, decompose vision into:

- Milestones (shippable increments)
- Features (user-facing capabilities)
- Status tracking (planned/in-progress/complete)

## Output: .specs/project/ROADMAP.md

**Structure:**

```markdown
# Roadmap

**Current Milestone:** [milestone name]
**Status:** Planning | In Progress | Complete

---

## [Milestone 1 Name]

**Goal:** [What makes this milestone shippable]
**Target:** [Date or completion criteria]

### Features

**[Feature Name]** - STATUS

- [Capability 1]
- [Capability 2]
- [Capability 3]

**[Feature Name]** - STATUS
_Depends on:_ [Feature Name], dir:[shared-folder]

- [Capability 1]
- [Capability 2]

---

## [Milestone 2 Name]

**Goal:** [What this milestone adds]

### Features

**[Feature Name]** - PLANNED
**[Feature Name]** - PLANNED

---

## Future Considerations

- [Potential future capability]
- [Potential future capability]
```

**Status values:**

- PLANNED: Not started
- IN PROGRESS: Currently implementing
- COMPLETE: Shipped and verified

**Size limit:** 3,000 tokens (~1,800 words)

**Update strategy:**

- Mark features PLANNED → IN PROGRESS when starting
- Mark IN PROGRESS → COMPLETE when verified
- Add new milestones as project evolves

**Dependencies (optional line, same meaning as `DEPS` in the spec — see [specify.md](specify.md)):** `_Depends on:_` lists features that must be done first and `dir:<folder>` shared folders the feature only imports. Omit the line when there is none. Order inside a milestone should already respect it. Dependencies across milestones need no line: a milestone starts when the previous one closes.

**Validation:**

- Dependencies point to features that exist in this roadmap, with no cycles?
- Each milestone has clear shippable outcome?
- Features are user-facing capabilities?
- Status reflects current reality?
