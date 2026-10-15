---
name: orchestrator-show-progress-graph
description: Shows the orchestrator roadmap progress as a drawing (features per stage, dependencies, what is done, in progress, to do, blocked and what is stuck), as plain text for chat and e-mail, Mermaid for PR/GitHub and a short line for short-message channels. Writes .specs/project/GRAPH.md. Use in every checkpoint report, when the user asks "how is the loop going", "what is blocking", "show the graph/progress", or when assembling the loop notification and PR. Do NOT activate for code, database or architecture graphs.
---

# Show the progress graph

Depends on the `orchestrator` skill (script `${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py`; reads `.specs/project/ROADMAP.md`). It only reads and draws: it changes nothing except `GRAPH.md`.

## Usage

```
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py <root>             # plain text (default)
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py <root> --short     # one line per stage
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py <root> --mermaid   # for PR / .md
python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/graph_view.py <root> --write    # writes .specs/project/GRAPH.md
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
| Short-message notification | `--short` (fits the ~1000-character limit) |
| Delivery PR body and `loop-summary-*.md` | Mermaid |
| `.specs/project/GRAPH.md` | text + Mermaid, rewritten by the script every round |

## When the orchestrator uses it

- **Every checkpoint** (Phase 8) and before asking "shall I continue?": paste the plain text into the report.
- When answering "how is it going?": run it and show it; do not describe from memory.
- `STUCK on:` indicates a feature that depends on a blocked one: take it to the user.
