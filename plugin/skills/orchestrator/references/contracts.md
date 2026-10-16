# Return contracts

Every subagent returns structured text with fixed fields. The orchestrator reads those fields and nothing else. If a return comes in the wrong format, treat it as a failure of that dispatch and resend once, citing the expected format; the second time, quarantine.

No contract includes file content. Specs, diffs and logs stay on disk; the return carries a pointer.

## `brownfield-mapper`

```
DOC: .specs/codebase/<NAME>.md
LINES: <n>
SUMMARY: up to 3 lines
GAPS: what could not be determined, or "-"
```

A filled `GAPS` blocks nothing. Note it in `STATE.md` as a known mapping gap.

## `spec-writer`

```
FEATURE: <slug>
SCOPE: Small|Medium|Large|Complex
FILES: .specs/features/<slug>/spec.md[, design.md][, tasks.md]
PATHS: <dir or glob>, <dir or glob>
DEPS: <slug|dir:folder>, ... | none
KIND: back|front|both|generic
SUMMARY: up to 5 lines
OPEN_QUESTIONS:
- <question> | options: a) <...> b) <...>
NOTES: up to 3 lines, or "-"
```

What the orchestrator does with each field:

- `DEPS` goes to `deps=` in the ROADMAP (`none` = omit the field). `slug` = a feature that must be `verified` before this one runs; `dir:<folder>` = a shared folder that is only imported. Check with `python "<app>/harness.py" state validate <root>` (slug exists, no cycle). A missing `DEPS` in the return is not an error for an adopted spec: treat it as `none` and note it in STATE.
- `SCOPE` decides whether the feature goes through the gate. `Small` skips; `Medium` and above go through.
- `PATHS` feeds two decisions: executor routing and collision detection between parallel features. Without plausible `PATHS`, return the spec to the writer.
- `KIND` is the first routing key; `PATHS` breaks ties (see `dispatch.md`).
- `OPEN_QUESTIONS` **are not answered immediately**. Accumulate them and take them in batch to the checkpoint. While there is an open question, the feature does not advance to the gate.
- `NOTES` may contain a request to change the ROADMAP or STATE. The orchestrator applies it.

**Completion mode.** When the spec was adopted from disk (`origin=adopted`) and the gate returned `BLOCKED`, the dispatch to the `spec-writer` carries the instruction to **complete and fix the existing file**, preserving requirements and IDs already written. The return uses the same contract above. An adopted spec is never rewritten from scratch.

## `spec-reviewer`

```
FEATURE: <slug>
VERDICT: APPROVED|BLOCKED
CITED: R1, R3, R7
FINDINGS:
- <id or section>: <problem>. <what needs to change>.
```

Anti-rubber-stamp rule, applied by the orchestrator: `APPROVED` is only valid with `CITED` listing at least three requirement IDs, or all of them when the spec has fewer than three. `APPROVED` without that is treated as `BLOCKED`.

`BLOCKED` goes back to the `spec-writer` with the `FINDINGS` pasted into the prompt. A second rejection of the same spec goes to quarantine.

## `executor-back` / `executor-front` / `executor-generic`

```
FEATURE: <slug>
STATUS: DONE|TOO_BIG|NEEDS_CONFIRM|AMBIGUOUS|REGRESSED
CHANGED:
- <path>:<lines> — <change in up to 10 words>
NOTES: up to 3 lines, or "-"
```

What to do with each status:

| STATUS | Orchestrator action |
|---|---|
| `DONE` | dispatches the `verifier` |
| `TOO_BIG` | **stops and calls the user**. The feature probably needs to be split in the roadmap. |
| `NEEDS_CONFIRM` | **stops and calls the user** with what the executor asked for. Never authorize on your own. |
| `AMBIGUOUS` | goes back to the `spec-writer` as an open question. Second time → quarantine. |
| `REGRESSED` | a single restore attempt via a new dispatch to the same executor with the report. If it fails → quarantine, and warn the user that there is broken code in the tree. |

`CHANGED` feeds the commit message and the report. Check that the listed paths are inside the spec's `PATHS`; if any falls outside, treat it as `NEEDS_CONFIRM`.

## `verifier`

```
FEATURE: <slug>
VERDICT: PASS|FAIL|UNVERIFIED
RAN: <command>, <command>
FAIL:
- <command>: <first useful line of the error> @ <path:line>
NOTES: up to 2 lines, or "-"
```

- `PASS` → `verified`, go to commit.
- `UNVERIFIED` → the repo has no test, build, typecheck or lint. The feature advances, but is marked unverified in `STATE.md` and shown that way in the report. Never present `UNVERIFIED` as approved.
- `FAIL` → goes back to the same executor with the `FAIL` block in the prompt. Second failure → quarantine.

## `verifier-flow`

Full format in `${CLAUDE_PLUGIN_ROOT}/skills/flow-validator/SKILL.md` (Report section). Summary:

```
FLOW: <name or slug>
VERDICT: PASS|FAIL|UNVERIFIED
LAYERS: route | e2e | route+e2e
STEPS: <one per line, with PASS|FAIL|UNVERIFIED>
EVIDENCE: <paths>
LAYERS_NOT_COVERED: <e.g. "UI e2e">, or "-"
```

- Curl alone never counts as E2E; if `LAYERS` does not include `e2e` and the feature has a screen, highlight that in the checkpoint report.
- `UNVERIFIED` is never presented as approved.
- Each `FAIL` carries the evidence path (screenshot, route response) for the resend to the executor.

## `Plan` (native agent, phase 2)

No fixed contract. The orchestrator reads the output and converts it to the `ROADMAP.md` format defined in `state.md`. Every feature is born `pending`, without `PATHS` — the `spec-writer` fills `PATHS`.
