# Project Initialization

**Trigger:** "Initialize project", "Setup project", "Start new project"

> **Loop Harness projects:** do not use the long format below. `PROJECT.md` is born from the app's template as a SHORT context file for the loop's subagents (~300 words): one `**Vision:**` line (1-2 sentences, what is being built and for whom; the only `FILL_IN`) and a static `## Where to look` section pointing to `docs/codebase/` (stack, architecture, conventions, structure, tests, integrations, concerns), `RULES.md` (limits and "never X"), `STATE.md`, `ROADMAP.md`, `FLOWS.md` and `TEST-ENVIRONMENT.md`. Stack, goals, scope and constraints are not kept there; do not ask the user for them. The long format below stays valid for standalone greenfield use.

## Process

Extract project vision via iterative Q&A (max 3-5 questions per message):

**Essential questions:**

1. What are you building?
2. Who is it for and what problem does it solve?
3. What tech stack are you using? (if known)
4. What's in scope for v1? What's explicitly excluded?
5. Critical constraints? (timeline, technical, resources)

**Stop when:** Clear understanding of vision, goals, and boundaries.

## Output: .specs/project/PROJECT.md

**Structure:**

```markdown
# [Project Name]

**Vision:** [1-2 sentence description]
**For:** [target users]
**Solves:** [core problem being addressed]

## Goals

- [Primary goal with measurable success metric]
- [Secondary goal with measurable success metric]

## Tech Stack

**Core:**

- Framework: [name + version]
- Language: [name + version]
- Database: [name]

**Key dependencies:** [3-5 critical libraries/frameworks]

## Scope

**v1 includes:**

- [Core capability 1]
- [Core capability 2]
- [Core capability 3]

**Explicitly out of scope:**

- [What is NOT being built]
- [What is NOT being built]

## Constraints

- Timeline: [if applicable]
- Technical: [if applicable]
- Resources: [if applicable]
```

**Size limit:** 2,000 tokens (~1,200 words)

**Validation:**

- Vision clear in 1-2 sentences?
- Goals have measurable outcomes?
- Scope boundaries explicit?
