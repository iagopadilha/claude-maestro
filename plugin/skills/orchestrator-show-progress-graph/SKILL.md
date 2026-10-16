---
name: orchestrator-show-progress-graph
description: Shows the orchestrator roadmap progress as a drawing (features per stage, dependencies, what is done, in progress, to do, blocked and what is stuck), as plain text for chat and e-mail, Mermaid for PR/GitHub and a short line for short-message channels. Writes .specs/project/loop/GRAPH.md. Use in every checkpoint report, when the user asks "how is the loop going", "what is blocking", "show the graph/progress", or when assembling the loop notification and PR. Do NOT activate for code, database or architecture graphs.
---

# Show the progress graph

Depends on the `orchestrator` skill and on the Loop Harness app (engine script `graph_view.py`, called through `python "<app>/harness.py" graph`; reads `.specs/project/ROADMAP.md`). `<app>` = the app folder, resolved as in the `orchestrator` skill's `SKILL.md` (section Runtime: `LOOP_APP_HOME`, or the `app_root_dir` field of `~/.loop-harness/config.json`; with no app, ask the user to clone the repository and run `python <repo>/app/harness.py install` once, and stop). It only reads and draws: it changes nothing except `GRAPH.md`.

## Usage

```
python "<app>/harness.py" graph <root>             # plain text (default)
python "<app>/harness.py" graph <root> --short     # one line per stage
python "<app>/harness.py" graph <root> --mermaid   # for PR / .md
python "<app>/harness.py" graph <root> --write     # writes .specs/project/loop/GRAPH.md
```

Example output:
```
Stage 1  (2/3 closed)
  [x] api    done
  [x] ui     done
  [!] coupon  BLOCKED   (2 failures)
Stage 2  (0/2 closed)
  [>] screen building <- api(done) ui(done)
  [.] end    pending  <- screen(building)   STUCK on: coupon

Legend: [x] done  [>] in progress  [.] to do  [!] blocked  (<- waits for)
```

(The output labels are the script's own: `Stage`, `closed`, `done`, `BLOCKED`, `building`, `pending`, `STUCK on`, `Legend`, `to do`, `in progress`, `waits for`; in the short line, stages are prefixed `S<n>`.)

## Where each format is used (already wired)

| Where | Format |
|---|---|
| Checkpoint report in chat | plain text |
| Loop e-mail notification | plain text in a block |
| Short-message notification (toast, webhook) | `--short` (fits the ~1000-character limit) |
| Delivery PR body and `loop-summary-*.md` | Mermaid |
| `.specs/project/loop/GRAPH.md` | text + Mermaid, rewritten by the engine every turn |

## When the orchestrator uses it

- **Every checkpoint** (Phase 8) and before asking "shall I continue?": paste the plain text into the report.
- When answering "how is it going?": run it and show it; do not describe from memory.
- `STUCK on:` indicates a feature that depends on a blocked one: take it to the user.
- Only the orchestrator runs `--write` (the guard blocks it in subagents: `state-script`).

## Web panel

The same drawing (dependencies, what is done, in progress and stuck) appears live in the Loop Harness panel (`python "<app>/harness.py" serve --project <root>`; the URL also comes in the loop's notification). This skill remains the way to produce the text for chat, e-mail and the PR.
