# Standard project CLAUDE.md (template)

Copy to the root of each project as `CLAUDE.md` and fill it in. It is a **pointing summary**, not documentation: it says
where the reusable generic methods/components are and what each structure contains, so the skill (and the agent) can **find
before creating**. Generate the draft of the map with `python "${CLAUDE_PLUGIN_ROOT}/shared/map_reuse.py" <root>` and then
curate it by hand (one description line per item). Keep it to about 150 lines.

```markdown
# <Project>

@.specs/project/RULES.md

<!-- CONFORMANCE:BEGIN -->
## Conformance
Conformance: STANDARD|LEGACY   (do not fill by hand: `python "${CLAUDE_PLUGIN_ROOT}/shared/classify_structure.py" <root> --write STANDARD|LEGACY`)
<!-- CONFORMANCE:END -->

## How to run and verify
- Build/typecheck/lint/test: `python "${CLAUDE_PLUGIN_ROOT}/shared/verify.py"` (or: <project commands>)
- Run locally: the harness app writes a generated `How to run` block (between `RUN:BEGIN` and `RUN:END`) from `launchSettings.json` and the front end's `package.json`: .NET with `--launch-profile https`, https first, http only if it does not answer. Do not hand-write a `dotnet run` line without the https profile.

## Reuse map (read BEFORE creating a method, function, hook, component or type)
Look here; if you found it, **reuse or generalize**, do not create a twin. Created something reusable? Add the line.

| I need to... | Use | Where | Signature / note |
|---|---|---|---|
| Format a tax ID / document number | `FormatDocument` | `src/Common/Formatters/DocumentFormatter.cs` | `string Format(string, DocumentType)` |
| Call an API with handled errors | `ErrorHandler` | `src/lib/error-handler.ts` | use in the service's `onError` |
| Save several entities at once | `BatchSaver` | `src/Infra/Persistence/` | takes `Add/Update/Remove` |

## Structure (what exists in each folder)
| Folder | What it has | Can receive new code? |
|---|---|---|
| `src/Common/` | generic helpers and extensions | yes, if truly generic |
| `src/Domain/` | entities and rules, no I/O | yes |
| `src/Infra/` | repositories, savers, integrations | yes |
| `<legacy folder>` | <description> | no (only spot fixes) |

## Local conventions (only what differs from the skill)
- <e.g. logs with BeginScope, toasts only via @/components/toast>

## Where the rest is
`.specs/project/RULES.md` (limits, always read) · `.specs/project/STATE.md` (only the now) · `.specs/features/<slug>/` (specs)
```

**How the skill uses it:** `backend-dev` and `frontend-dev` (N1) read the "Reuse map" section before naming or
creating; then they confirm with Grep in the code (the map may be stale). Project without the file: the skill offers to generate the
draft with `map_reuse.py`. Owner of the file: the main agent (with `/orchestrator`, only the orchestrator;
a subagent suggests a line in `NOTES`).
