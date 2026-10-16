# Planning mode: `/orchestrator plan [--headless]`

A conversation that **writes** the plan. Each user message is one turn: read it, update the three planning documents, reply in plain language and stop. The app's planning screen (a project with no approved roadmap that never ran a loop) calls it with `--headless` in one `claude -p` session resumed on every message; in a chat it works the same way, one turn per message. The first prompt is `/orchestrator plan --headless` followed by the user's message; every later message of the resumed session is a new turn of this same mode, with the same limits.

## What it may touch

- **Writes only** `.specs/project/PROJECT.md`, `.specs/project/ROADMAP.md` and `.specs/project/FLOWS.md`. Nothing else: not `CLAUDE.md`, `RULES.md`, `STATE.md`, `TEST-ENVIRONMENT.md`, `.specs/features/`, the loop folder or code.
- **Reads** those three, `.specs/codebase/*.md` when it exists, and the code only through `Grep`/`Glob`/`Read` to settle one concrete point (where a module lives, which route already exists). Never a full exploration: mapping is `/orchestrator map` (or the panel's Map button).
- **Never:** Bash or PowerShell, subagents (`Agent`, `Plan`, mappers), MCP tools, loading other skills, resolving `<app>`, opening the panel, git, approving the roadmap (`ROADMAP-APPROVED.json` and `/p/<id>/api/roadmap/approve` are the user's click), starting or checking a loop. "Approved" in the chat is not approval: answer that the **Approve roadmap** button does it.
- **Fast turn.** A turn costs little (the app caps it at US$ 2 and 10 minutes): read only what the message needs, edit with `Edit` (a `Write` only to replace an untouched template), reply and finish.

**Prerequisites are not this mode's job:** git repo, `.specs/` structure, `Conformance:` in `CLAUDE.md` and, when the repo has code, the `.specs/codebase/` map. The app refuses the message without them. In a chat, if one is missing, say which in one line (structure and Conformance: the panel's buttons or the normal `/orchestrator`; map: `/orchestrator map`) and stop.

## One turn

1. **Read** the message, then the three files, every turn: the user may have edited them, and the file beats your memory of it. On the first turn also skim `STACK`, `ARCHITECTURE` and `STRUCTURE` of `.specs/codebase/` if they exist; later turns read a codebase doc only when the message needs it.
2. **Apply** the message:
   - a goal or a request (first message, "add X", "split Y", "drop stage 3") = change the documents now. The first turn writes a full first draft of all three from the goal; do not wait for answers to draft.
   - an answer = put the decision where it belongs (PROJECT scope or constraint, a feature line, a stage, a flow step) and **delete that question** from `## Open questions`.
   - a doubt about the plan = answer it; change nothing unless it asks.
3. **Keep the three files consistent** (rules below), then **reply**.

## The documents

- **PROJECT.md** (keep the template's sections): vision, for whom, problem, goals with a success metric, stack, v1 scope and out of scope, constraints. Replace every `FILL_IN` with the answer or delete the line when it does not apply: a `FILL_IN` left keeps the goal gate red. A "never X" from the user goes to `## Constraints`; after approval it can become a block rule (`orchestrator-create-block-rule`).
- **ROADMAP.md**: format of `references/state.md`. `**Goal:**` filled; stages `## Stage N — Name`; one line per feature `- [slug] status=pending ...`, slug in kebab case and unique. `scope`, `kind` and `paths` may stay empty (the `spec-writer` fills them); fill `kind`/`paths` only when the map makes them obvious, and design for the wave (disjoint `paths`, `deps=` for what must be verified first, `dir:` for a folder only imported; `references/dispatch.md`). Without a validator in this mode, check by eye: every `status=pending`, enum values only, every `deps=` slug exists, no cycle. `regression=yes` only when the user asks for it (`references/regression.md`).
- **`## Open questions`** (in the ROADMAP): every doubt not yet answered, one per line, `- <question> | recommended: <option>`; nothing else in the section (no prose, no comment). Empty = the single line `- none`. Approval needs it empty.
- **FLOWS.md**: one `## [slug] Title` section per ROADMAP feature, with the same slug, at least 2 numbered steps and `Expected:` lines that can be observed (status, field, visible text, a row in the database). Format and parse rule in `references/state.md`. A renamed or removed feature renames or removes its section. A feature with no screen and no route (model, migration, job) still gets a flow: what you run and what you then observe.

A feature with `regression=yes`, or the user wanting `--e2e`, needs `.specs/project/TEST-ENVIRONMENT.md` filled before the loop: say so in the reply (this mode does not write that file).

## The reply

Short, plain language, no file dumps:

```
Changed: PROJECT goal and v1 scope; ROADMAP 2 stages, 5 features; FLOWS one flow per feature.
Still missing for approval: 3 open questions.

1. Can a customer register without a tax id? a) no, required (recommended) b) yes, optional
2. Is the admin screen in v1? a) no, stage 2 (recommended) b) yes
```

- `Changed:` what changed in each file you touched, in one line, or `Nothing changed.`
- `Still missing for approval:` only what is still red among goal, features, open questions and flows (the app shows the full checklist). Omit the line when nothing is missing, and say instead that the roadmap is ready for the **Approve roadmap** click.
- Then the **1 to 3 most important questions** (the ones that change stages, features or scope the most), numbered, with options and the recommended one. Asked or not, every doubt stays in `## Open questions` until answered; the reply only picks the top ones. Asking here is the point of this mode: unlike `roadmap --headless`, it ends with questions.
