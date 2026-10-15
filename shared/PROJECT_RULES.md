# Project limiting rules (template for `.specs/project/RULES.md`)

An **always-read** file: it does not depend on phase, stage or feature, and it survives compaction and a new conversation, because the
project's `CLAUDE.md` imports it with `@.specs/project/RULES.md` (Claude Code loads CLAUDE.md in every session).
Only the **application's limits** live here: what never/always holds. History, stage decisions and progress live in
`STATE.md` and in `phases/`, not here.

**Who writes:** with `/orchestrator` running, **only the orchestrator** (the guard blocks subagents). Without the loop (standalone
use of `tlc-spec-driven`), the main agent. In any case **only with an explicit statement from the user**; never by
inference. A subagent never edits; if it thinks a rule is missing, it returns it in `NOTES`.

**Precedence:** a rule from here overrides spec, design, task and skill. Conflict = stop and ask (`NEEDS_CONFIRM`).

**Size:** up to ~40 rules and ~600 words. Beyond that, consolidate or move detail to `.specs/codebase/`.

```markdown
# Limiting rules

> Always read. Overrides spec/design/skill. Conflict = ask. Owner: orchestrator (or main agent without loop).

| ID | Rule | Why | How to apply | Origin |
|----|------|-----|--------------|--------|
| R-01 | Never create a tracking table/route | billed per route, real cost | Reject a feature that depends on it; take it to the user | user, 2026-10-13: "never create tracking" |
| R-02 | An existing OrderTemplateId never changes | user_settings is keyed by OrderTemplateId | New customer template = new template_info + new template_mapping rows, same OrderTemplateId | user, 2026-09-02 |
| R-03 | Writing to production only outside the loop | irreversible | Stop at NEEDS_CONFIRM | user |

## Environment limits
- <port, database, service that cannot be touched>

## Architecture limits
- <layer that cannot depend on another, folder that receives no new code, forbidden lib>

## Permanently out of scope
- <what has already been decided this product does not do>
```

**Rule with guard (optional, deterministic):** if the rule can be blocked by command pattern or path, also copy it
to `.specs/project/PERMISSIONS.json` (`deny_bash_regex` / `deny_path_regex`) and cite the ID: it then becomes a
hook block, not just text.

**In each feature's spec:** section `Applicable rules: R-02, R-05` (only the IDs) and, if the feature has its own limit,
`Feature limits: LIM-01 ...` inside `spec.md`. The executor reads the whole RULES.md (it is short) and the spec.
