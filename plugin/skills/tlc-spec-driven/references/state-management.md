# State Management

**Purpose:** Persistent memory across sessions - decisions, blockers, learnings.

## Structure

**Output:** `.specs/project/STATE.md`

```markdown
# State

**Last Updated:** [ISO timestamp]
**Current Work:** [Feature name] - [Task identifier]

---

## Recent Decisions (Last 60 days)

### AD-[NNN]: [Decision title] ([date])

**Decision:** [What was decided]
**Reason:** [Why this choice]
**Trade-off:** [What was sacrificed]
**Impact:** [How this affects implementation]

### AD-[NNN]: [Decision title] ([date])

[Same structure]

---

## Active Blockers

### B-[NNN]: [Blocker description]

**Discovered:** [Date]
**Impact:** [Severity and scope]
**Workaround:** [Temporary solution if available]
**Resolution:** [Path to permanent fix]

---

## Lessons Learned

### L-[NNN]: [Learning description]

**Context:** [Situation that occurred]
**Problem:** [What went wrong]
**Solution:** [How it was resolved]
**Prevents:** [What this knowledge prevents in future]

---

## Quick Tasks Completed

| #   | Description              | Date   | Commit | Status  |
| --- | ------------------------ | ------ | ------ | ------- |
| 001 | [Quick task description] | [date] | [hash] | ✅ Done |

---

## Deferred Ideas

Ideas captured during work that belong in future features or phases. Prevents scope creep while preserving good ideas.

- [ ] [Idea description] — Captured during: [feature/phase]
- [ ] [Idea description] — Captured during: [feature/phase]

---

## Todos

Capture in-progress thoughts and action items that don't fit in active tasks.

- [ ] [TODO: action item]
- [ ] [TODO: action item]
```

## When to Update

| Event                            | Action                                 |
| -------------------------------- | -------------------------------------- |
| Significant architectural choice | Add AD-[NNN]                           |
| Implementation blocked           | Add B-[NNN]                            |
| Important discovery/learning     | Add L-[NNN]                            |
| Quick task completed             | Add row to Quick Tasks table           |
| Scope creep captured             | Add to Deferred Ideas                  |
| In-progress thought              | Add to Todos                           |
| Session end                      | Update "Last Updated" + "Current Work" |

## Limiting Rules (`.specs/project/RULES.md`)

Separate from STATE.md. STATE.md is the "now" (decisions, blockers, lessons, todos). RULES.md is the **permanent limits of the application**: it never expires, is not tied to a phase or feature, and is read at the start of every session, after context compaction and in new conversations (the project `CLAUDE.md` imports it with `@.specs/project/RULES.md`; see `${CLAUDE_PLUGIN_ROOT}/shared/PROJECT_CLAUDE.md`).

- **Write only on an explicit user statement** ("never create tracking", "always use the same OrderTemplateId"). Never infer a rule. Record ID `R-xx`, rule, why, how to apply, origin (quote + date). Model: `${CLAUDE_PLUGIN_ROOT}/shared/PROJECT_RULES.md`.
- **Owner:** the main agent. When `/orchestrator` is running, only the orchestrator writes it and subagents cannot (hook-enforced); they suggest rules in `NOTES`.
- **Precedence:** RULES.md beats spec, design, tasks and skills. On conflict stop and ask.
- **Feature level:** `spec.md` carries `Applicable rules: R-xx` (IDs only) and, when needed, `Feature limits: LIM-xx`.
- **Size:** up to ~40 rules / ~600 words. A decision that expires goes to STATE.md, not here.

## Size Management (Hybrid Strategy)

**Zones:**

- 🟢 <7k tokens: No action
- 🟡 7-10k tokens: Footer note "STATE.md at [X]k. Cleanup recommended."
- 🔴 >10k tokens: Active prompt "STATE.md critical ([X]k). Cleanup now?"

**Cleanup process:**

- Move decisions >60 days to STATE-ARCHIVE.md
- Keep only active blockers
- Preserve recent learnings (<60 days)

**Validation:**

- Decisions have clear rationale?
- Blockers include resolution path?
- Learnings are actionable?

---

## Preferences

Track user-facing behavioral state in STATE.md:

```markdown
## Preferences

**Model Guidance Shown:** [ISO date or "never"]
```

**Update when:**

| Event                       | Action                   |
| --------------------------- | ------------------------ |
| First model tip given       | Set date                 |
| User acknowledges/dismisses | Keep date (don't repeat) |

This prevents repetitive suggestions while maintaining natural, helpful behavior.
