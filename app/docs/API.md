# Loop Harness: per-project panel API (fixed contract)

Every route below lives under `/p/<project_id>/api/` of the live app (the hub routes `/api/app/*` and the projects
module routes `progress`, `structure/initialize`, `conformance`, `specs/*`, `roadmap*`, `codebase/map` and `jobs` are in
`ARCHITECTURE.md`). The back end is Python standard library only; the front end is plain HTML/CSS/JS (no npm, no build).
The front end reads this API and never embeds mock data.

## Where the loop data lives (layout of `engine/loop_state.py`)
`work` = the loop worktree (`../<project>-worktree-loop`); when it does not exist, the repo itself. Everything is relative
to `work`:
- `.specs/project/ROADMAP.md`, `STATE.md`, `RULES.md`, `phases/stage-NN.md`
- `.specs/project/loop/`: `ANSWERS.md`, `VALIDATE.md`, `DEPENDENCIES.md`, `GRAPH.md`, `NOTICE.txt`
- `.specs/project/loop/json/`: `LOOP.json` (status of the last turn), `LOOP-CMD.json`, `MONITOR.json`, `meta.json` (`turns`,
  `cost_usd`, `branch`, `base`, `pr`, `pr_status`, ...), `ROADMAP-APPROVED.json`
- `.specs/project/loop/internal/`: `LOOP.lock` (PID of the live `run_loop.py`), `PAUSE`, `turns/NNN.json` (result of the
  turn's `claude -p`: `total_cost_usd`, `duration_ms`, `num_turns`, `usage`, `result`, `session_id`,
  `permission_denials`, `exit_code`, `timeout`, `turn`), `turns/NNN.events.jsonl` (optional live agent events),
  `snapshots/NNN/`, `chat/<thread>.json`, `jobs/<job>.json`, `whiteboard.json`, `actions.log`
- ROADMAP parsing reuses `loop_state.parse_roadmap`, `state_file`, `loop_dir`, `turns_dir` and `graph_view`; the parser
  is never rewritten.

## Signal files (in `.specs/project/loop/`)
| File | Who writes | Content |
|---|---|---|
| `json/LOOP-CMD.json` | `run_loop.py` on every run | `{"argv": ["python", "<run_loop.py>", "<repo>", "--wave", "2", ...], "cwd": "<dir>"}`. **Without** `--answer`/`--option`. It is the command used to relaunch. |
| `json/MONITOR.json` | the app, when a project is opened (deleted on shutdown) | `{"pid": 123, "port": 47801, "url": "http://127.0.0.1:47801/p/<id>/", "repo": "...", "work": "...", "started_at": "ISO"}` |
| `internal/PAUSE` | the app (`POST pause`) | empty file. `run_loop.py` checks it at the top of each turn: when it exists, deletes it and ends with `awaiting_user` ("paused by the user in the panel"). |

## Lifecycle
The app is the **project's control panel**: it stays up without a loop and triggers everything from the front end
(check, start a new loop, answer, resume, pause). It shuts down only on `POST /api/app/shutdown`, on idleness (no loop
running in any project, no claude job running and no activity for `idle_hours`, default 2 h) or when killed from outside.
It never shuts down on its own with a loop `running`. Activity = any POST, or `GET state?visible=1` (the front end only
sends `visible=1` while the tab is visible). `work` is **re-evaluated on every request**: a worktree created later by a new
loop is read without reopening the project; without a worktree the repo is read (`no_loop`).

## Security
`index.html` is served with `<meta name="monitor-token" content="TOKEN">` injected (random 32-byte hex token, created on
every start, kept only in memory). Every `POST`/`PUT` requires the `X-Monitor-Token` header; missing or wrong = 403. The
server listens only on 127.0.0.1 and rejects a `Host` other than `127.0.0.1:<port>`/`localhost:<port>` (DNS rebinding).
No CORS header. JSON body up to 64 KB unless a route says otherwise. Never returns `.env` content, credentials or the raw
`guard.log`.

## Endpoints
Every route returns UTF-8 JSON. Error: `{"ok": false, "error": "text"}` with a 4xx/5xx status. An unknown project is
404 `{"ok": false, "error": "project not found"}`.

**Planning mode.** `planning_required` (in `GET state`, and `required` in `GET planning`, see `ARCHITECTURE.md`) is true
when the roadmap was never approved AND the project never ran a loop (no loop worktree, no LOOP.json status, no
`meta.json` turns, no turn file, no live `LOOP.lock`; a project opened on the loop worktree itself counts as looped).
While it is true, every POST of this file that acts on the loop or on the AI is refused with 409
`{"ok": false, "error": "planning mode: ...", "code": "planning_required"}` and runs nothing: `answer`, `resume`, `note`,
`pause`, `loop/check`, `loop/start`, `instruction`, `POST whiteboard`, `chat/send`, `action/propose` and `action/run`.
Reads (`state`, `history`, `whiteboard`, `chat/*` GETs, `actions`...) and the legacy `shutdown` stay open. A project that
already ran loops and lost the approval (rows changed) is NOT in planning mode (the old banner applies).

### GET health
`{"ok": true, "repo": "...", "work": "...", "pid": 123}`

### GET state?visible=1
```json
{
  "now": "ISO",
  "project": "sample-api",
  "project_ref": {"id": "1a2b3c4d5e6f", "name": "sample-api", "repo_path": "..."},
  "repo": "...", "work": "...", "branch": "loop/20261015-0940",
  "loop": {
    "status": "running|continue|awaiting_user|done|error|stopped|no_loop",
    "running": true, "pid": 1234,
    "turn": 12, "feature": ["F-08", "F-09"],
    "reason": "text (the question, or the cause of the error)", "next": "text",
    "options": ["a (recommended)", "b"], "to_check": ["x"], "at": "ISO", "pause_requested": false
  },
  "meta": {"turns": 12, "cost_usd": 6.2, "branch": "...", "pr": null, "pr_status": null},
  "limits": [
    {"key": "cost", "label": "cost", "value": 6.2, "max": 30, "unit": "US$", "pct": 20.7},
    {"key": "turns_without_progress", "label": "without progress", "value": 0, "max": 10, "unit": "", "pct": 0},
    {"key": "turn_minutes", "label": "current turn", "value": 9.5, "max": 60, "unit": "min", "pct": 15.8}
  ],
  "stages": [{"n": 1, "title": "Stage 1", "features": ["slug-a", "slug-b"]}],
  "features": [{
    "id": "slug-a", "name": "Readable title (or the slug)", "status": "pending|speccing|specced|approved|building|verified|blocked",
    "stage": 1, "deps": ["slug-b", "dir:src/x"], "paths": ["src/..."], "kind": "back|front|both|generic|?",
    "tries": 0, "note": "...", "commit": "1c02e9a", "regression": "yes|baseline|ok|failed|null", "e2e": "ok|failed|not-covered|null"
  }],
  "counts": {"pending": 3, "speccing": 0, "specced": 0, "approved": 1, "building": 2, "verified": 5, "blocked": 1, "total": 12},
  "graph": {"nodes": ["slug-a"], "edges": [["slug-b", "slug-a"]]},
  "agents": [{
    "id": "orch|<slug>:<role>", "role": "orchestrator|spec-writer|spec-reviewer|executor-back|executor-front|executor-generic|verifier|verifier-flow",
    "feature": "slug|null", "state": "running|done|idle|queued|blocked|failed", "summary": "short text",
    "tokens": 38200, "cost_usd": 0.61, "duration_s": 1394,
    "log": [{"type": "sys|tool|ai|user|guard", "text": "...", "result": "", "t": "00:04"}]
  }],
  "validate": ["- [slug] doubt | chose: ... | revert: ..."],
  "recent_actions": [{"action": "deliver", "t": "ISO", "ok": true, "summary": "..."}],
  "roadmap_approval": {"approved": true, "reason": null, "at": "ISO", "features": 12},
  "planning_required": false,
  "answers": [{"date": "ISO", "question": "...", "answer": "...", "state": "pending|applied|automatic"}],
  "turns": [{"n": 12, "cost_usd": 0.61, "duration_s": 1394, "status": "continue|awaiting_user|done|error|running", "feature": ["a"], "commits": 1, "has_snapshot": true}],
  "quarantine": [{"id": "slug", "reason": "<note> (2 failures)", "stuck": ["other-feature"], "options": ["try again", "skip and continue"]}],
  "guard_events": [{"t": "ISO", "profile": "executor|spec|verifier|orchestrator", "rule": "path-scope", "detail": "outside the feature's PATHS (src/a)..."}],
  "guard_counts": {"path-scope": 2, "git-push": 1},
  "monitor": {"url": "http://127.0.0.1:47801/p/<id>/", "idle_in_s": null, "shutdown_in_s": null}
}
```
Derivation rules (server):
- `loop.status`: `LOOP.lock` with a live PID = `running` (even if an old LOOP.json says otherwise); no lock and LOOP.json
  present = the LOOP.json status; no lock with LOOP.json in `continue` = `stopped` (the script died), or `error` when
  `meta.json` has `stopped == "error"` written after the LOOP.json `at`; no LOOP.json = `no_loop`.
- `agents`: always `orch` (data of the last `turns/NNN.json`: cost, tokens summed from `usage`, duration, `result`
  clipped to 300 characters as an `ai` log line). Plus one agent per open feature, with the role inferred from the status
  (`speccing` = spec-writer, `specced` = spec-reviewer, `approved`/`building` = executor-<kind>, `building` with a
  verification `note` = verifier). Tokens and cost per agent only when there is an `*.events.jsonl`; otherwise `null`.
  **Never invent numbers.**
- `limits`: `max` comes from `LOOP-CMD.json` (`--max-cost`, `--max-stall`, `--timeout`, with the defaults 30/10/60 when
  the flag is absent); `turn_minutes` only while `running`.
- `quarantine`: `blocked` features; `stuck` = features whose `deps` contain the blocked one; fixed `options` above.
- `answers`: parse of the `## Answer <date> [marker]` blocks of `ANSWERS.md`; `[automatic answer from the loop...]` =
  `automatic`.
- `guard_events`: the last 20 `blocked` decisions of `internal/guard.log` (written by the plugin's `PreToolUse` guard),
  newest first, as `{t, profile, rule, detail}`; `detail` clipped to 200 characters and credential-redacted. Info and
  `allowed-by-user` lines are left out and the raw file is never returned. `guard_counts` = blocks per rule over the whole
  log. No `guard.log` = `[]` and `{}`.
- `agents[].log` items read from `turns/NNN.events.jsonl` come from the turn in progress when its file exists (the turn after
  the last finished one), otherwise from the last finished turn. An event of `type: "guard"` keeps its `rule` and `reason`.
- A field missing at the source = `null` or an empty list, never a 500.

### GET agent/<id>/log
`{"id": "...", "log": [...], "truncated": false}` (same format as `agents[].log`, complete). Missing = empty list.

### GET turn/<n>
`{"n": 12, "turn_log": {...turns/NNN.json without the large raw field...}, "roadmap": "ROADMAP text in the snapshot", "features": [...same format, from the snapshot]}`;
404 `turn without snapshot` when there is no snapshot.

### GET graph
`{"dependencies_mermaid": "...", "states_mermaid": "...", "text": "..."}` (from `graph_view`).

### Agent history (per turn): source = Claude Code transcripts
Each `turns/NNN.json` has a `session_id`. Transcripts live in `<claude_transcripts_dir>/<slug>/<session_id>.jsonl`
(the turn's orchestrator) and `<claude_transcripts_dir>/<slug>/<session_id>/subagents/agent-<agentId>.jsonl` +
`agent-<agentId>.meta.json` (agent type), where `claude_transcripts_dir` defaults to `~/.claude/projects` and `<slug>` =
the absolute path of `work` with every non-alphanumeric character replaced by `-` (e.g.
`C--Users-dev-projects-sample-api-worktree-loop`; the variant with a lowercase drive letter is also tried). They exist
even after the loop ended: it is the real history.
- **GET history** -> `{"turns": [{"n": 16, "session_id": "...", "cost_usd": 0.96, "duration_s": 13, "status": "done", "transcript": true, "agents": [{"id": "v016:orch", "role": "orchestrator", "feature": null, "state": "done", "summary": "...", "tokens": 174400, "duration_s": 13, "started_at": "ISO|null", "finished_at": "ISO|null"}, {"id": "v016:a0dc390cefdb8565c", "role": "spec-writer", "feature": "slug|null", "state": "done|failed", "summary": "first line of the final return, up to 200 characters", "tokens": 21400, "duration_s": 252, "started_at": "ISO", "finished_at": "ISO"}]}]}`,
  newest turn first. `role` comes from `.meta.json` (`agentType`); `feature` from the `SCOPE_FEATURE: <slug>` or
  `FEATURE: <slug>` line of the initial prompt; `tokens` summing `usage` across the transcript; `duration_s` from the
  first to the last timestamp. **Never invent**: what cannot be extracted = `null`. Missing transcript = `agents` with
  only the orchestrator (data of `turns/NNN.json`) and `"transcript": false`.
- **GET agent/<id>/log** also accepts these ids (`v016:a0dc...` and `v016:orch`): returns the full transcript log as
  `[{"type": "sys|tool|ai|user|guard", "text": "...", "result": "...", "t": "HH:MM:SS"}]` (initial prompt = `sys`, tool calls =
  `tool` with the command summary and the start of the result, model text = `ai`; a tool call whose error result carries
  the guard's `BLOCKED by the orchestrator guard [<rules>]: <reason>` stderr = `guard`, with `rule`, `reason` and
  `result` = `exit 2 · [<rules>] <reason>`). Each `text`/`result` is clipped to
  2000 characters and the log to 400 items (`"truncated": true`). **Credentials are redacted** (tokens, passwords, keys)
  with the same filter used for `result` (mark `[redacted]`).
- Cache per file (mtime) so large transcripts are not re-read on every poll; `history` is never part of the 2 s polling
  (only when the History view opens and when `meta.turns` changes).
- `agents` of `state` describes **the current/last turn**; for the last turn, when there is a transcript, the real agents
  (with real tokens) replace the ones inferred from the status.

### Clarity fields
- **Per agent** (in `agents[]` of `state` and in `history[].turns[].agents[]`): `last_action` =
  `{"type": "tool|ai|sys", "text": "up to 160 characters", "t": "HH:MM:SS|null"}` (last tool call or model text before the
  final return); `return_status` = keyword of the agent's final return when present
  (`DONE|APPROVED|BLOCKED|PASS|FAIL|UNVERIFIED|NEEDS_CONFIRM|TOO_BIG`, read from the `STATUS:`/`VERDICT:` lines or the
  first word of the handback, otherwise `null`); `decision` = short sentence (up to 200 characters) of what the agent
  concluded, `null` when unavailable. Never invented.
- **Per feature** (in `features[]` of `state`): `depends_on` = `[{"id": "slug", "status": "verified|..."}]` (only `deps=`
  that are slugs; `dir:<folder>` goes to `dep_dirs: ["folder"]`); `unblocks` = slugs that depend on this one;
  `waiting_on` = ids in `depends_on` whose status is not `verified` yet (empty and not `blocked` = it can run);
  `blocked_by` = ids of `depends_on` with status `blocked`.

### Feature, wave and stage summaries
- **Per feature** (`features[]` of `state` and `turn/<n>`): `summary` = `{"goal": "up to 600 characters", "size": "Small|Medium|Large|Complex|null", "requirements": N|null, "source": "spec.md|roadmap|none"}`,
  from `.specs/features/<slug>/spec.md` (vision/goal/summary sections and the count of requirements with an ID) or, without
  a spec, the ROADMAP line. `wave` = `{"turn": N|null, "together_with": ["slug"], "reason": "short text", "upcoming": bool}`:
  the wave is the set of features dispatched in the same turn (the `feature` field of each snapshot's `LOOP.json`, last
  turn that names the feature); `reason` explains why they run together (`disjoint paths, no dependency between them`) or,
  for a feature not run yet, `upcoming: true` with what it waits on (`waiting on: a, b`). Never invented.
- **Per stage** (`stages[]`): besides `n`, `title`, `features`: `summary` =
  `{"description": "stage text in the ROADMAP or null", "progress": {"verified": N, "blocked": N, "total": N}, "current_feature": "slug|null", "waves": [{"turn": N, "features": ["slug"]}], "closing": {"delivered": "text", "decisions": "text", "lessons": "text", "unverified": "text", "blocked": "text", "next_stage": "text"}|null}`.
  `closing` comes from `.specs/project/phases/stage-NN.md` (fixed headings `Delivered`, `User decisions`, `Lessons`,
  `Unverified`, `Blocked`, `For the next stage`), each section clipped to 1200 characters; a stage without the file =
  `null`. `current_feature` = the first `building`/`speccing`/`approved` feature, otherwise the first open one.

### Whiteboard
A drawable board (tldraw/Excalidraw style) **persisted on the server** (survives a reload, another tab and another day).
File: `<work>/.specs/project/loop/internal/whiteboard.json` (atomic write). The content is opaque to the server (the
front end decides the format).
- **GET whiteboard** -> `{"ok": true, "data": <object|null>, "version": N, "updated_at": "ISO|null"}` (`version` 0 and
  `data` null when never saved).
- **POST whiteboard** `{"data": <object>, "base_version": N}` -> `{"ok": true, "version": N+1, "updated_at": "ISO"}`. A
  `base_version` other than the current one = 409 `{"ok": false, "error": "...", "version": <current>}` (another tab saved
  first). `data` must be a JSON object; body limit **8 MB on this route only** (413 beyond it). Counts as activity.
- There is no DELETE: clearing the board = POST with an empty scene.

### Chat with the AI and instructions to the loop
Product rule: **old version = READ-ONLY**; **current version = two intents**: *Instruct the loop* (default) or *Ask*
(questions only).
- **Old version** = any turn that is not the last one (snapshot) or an agent of an old turn. Only the read-only *Ask*
  chat. *Instruct the loop* is disabled with the explanation "you are on an old version"; the front end offers "Bring to
  the current version" (attaches question + answer as context of the next instruction).
- **Current version** = the last turn and its agents. *Instruct the loop* is the default; *Ask* can be toggled.

**POST chat/send** `{"thread": "<id|null>", "scope": {"type": "agent|general", "agent_id": "vNNN:orch|vNNN:<agentId>|<state id>|null", "turn": N|null}, "text": "..."}`
-> 202 `{"ok": true, "job": "j-...", "thread": "t-..."}`. ALWAYS read-only and independent of the loop (does not touch
`LOOP.lock`, `LOOP.json` or `ANSWERS.md`). Runs a detached `claude -p` with: cwd = `work`; model `sonnet`;
`--max-budget-usd 1`; allowed tools ONLY `Read Grep Glob` (`--allowedTools`) and `--disallowedTools "Bash Edit Write NotebookEdit"`;
**no MCP** (`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`, so nothing reaches production systems, an issue
tracker or a ticket system through this channel); `--output-format json`; the user's text goes through **stdin**, never
on the command line; an environment without secrets (the panel token is never passed). Context built on the server: (a)
`vNNN:orch` with a known `session_id`: `--resume <session_id> --fork-session` (the turn's history is not changed); (b) a
subagent or no session: a prompt with the agent's role, its initial prompt, last actions, final return (from the
transcript, already redacted) and the `.specs/features/<slug>/` files by **path** (it reads them with Read); (c)
`general`: the `.specs` summary (ROADMAP, STATE, VALIDATE, LOOP.json) by path. Fixed safety prefix: `You are in READ-ONLY
mode. Answer questions; do not claim to have changed anything.` In an existing thread the thread's own session continues
(`--resume <thread session_id>`).

**GET chat/job/<job>** -> `{"state": "running|ok|error", "reply": "text|null", "cost_usd": 0.12, "error": null, "thread": "t-..."}`;
`reply` goes through credential redaction; job timeout 180 s (`error`: `timed out`), and the process is killed.
**GET chat/threads?agent_id=<id>&turn=<n>** -> `{"threads": [{"thread": "t-...", "title": "first question (60 characters)", "scope": {...}, "updated_at": "ISO", "message_count": N, "cost_usd": 0.4}]}`.
**GET chat/thread/<thread>** -> `{"thread": "...", "scope": {...}, "messages": [{"role": "user|ai", "text": "...", "t": "ISO", "cost_usd": null, "mode": "question"}]}`;
unknown thread = 404 `thread not found`.
Persistence: `<work>/.specs/project/loop/internal/chat/<thread>.json` (atomic, up to 200 messages; claude's `session_id`
kept in the file and NEVER returned by the API). At most 1 job at a time per thread (409) and 3 in total; user text up to
8000 characters (400/413).

**POST instruction** `{"text": "...", "feature": "slug|null", "attachments": ["attached context 1"]}` -> an instruction to
the loop of the CURRENT version (the server ignores any turn: always current). Behavior by `loop.status`:
- `running`: writes the note in `ANSWERS.md` (`[pending]` block, `Question: (user instruction from the panel)`, prefix
  `[feature] ` when given) -> `{"ok": true, "action": "note"}`; the next turn applies it.
- `awaiting_user`: same as `answer` with the text -> `{"ok": true, "action": "answer", "pid": N}`.
- `error|stopped|done`: writes the `[pending]` block and relaunches like `resume` -> `{"ok": true, "action": "note+resume", "pid": N}`.
- `no_loop`: 409 "start a loop from the New loop panel".
`attachments` (each up to 2000 characters, up to 5) go into the block as `Attached context:`; empty `text` = 400
(`empty text`); 409 with a live lock when relaunching. Token required. It is still the user triggering.

### Objective actions of the current version (without opening another loop)
Third composer mode, only on the CURRENT version: **Act**. Runs **deterministic actions from a whitelist** (code, not
the AI), with **explicit confirmation** from the user first. The AI only serves to *understand a free-text request*.
Whitelist: `deliver`, `summary`, `push` and `clean_worktrees`.
- `deliver` = push **only the feature branch** + PR to the integration branch through `engine/delivery.py` (never a push
  to a protected branch such as `main`/`master`/the integration branch, never a force push; the refusals of
  `delivery.py` and of the guard still apply). The integration branch comes from `LOOP_INTEGRATION_BRANCH` (default
  `developer`) and is shown in `preview.base`.
- `summary` = generate the final loop summary (`engine/loop_summary.py`) into the `output_dir` of `config.json` (default
  `~/Downloads`).
- **GET actions** -> `{"actions": [{"id": "deliver", "title": "Deliver: push the branch + PR to <base>", "available": true, "unavailable_reason": null, "preview": {"branch": "loop/...", "base": "<integration branch>", "existing_pr": "url|null", "open_features": [], "push": "text of delivery.py --dry-run"}}, {"id": "summary", "title": "Generate the loop summary", "available": true, "unavailable_reason": null, "preview": {"destination": "folder path"}}, ...]}`.
  The `deliver` preview comes from `delivery.py <work> --dry-run` (synchronous, 60 s timeout, never a push).
  `available=false` with a reason when: the loop is running (`the loop is running`), there is no loop or worktree
  (`there is no loop in this worktree`, `there is no loop worktree`, `no worktree`), a feature is open, the branch is
  protected, etc.
- **POST action/propose** `{"text": "open the PR and push"}` -> 202 `{"ok": true, "job": "j-..."}`; **GET action/job/<job>**
  -> `{"state", "reply", "cost_usd", "error", "proposal": {"action": "deliver|summary|push|clean_worktrees|none", "explanation": "1-2 sentences", "confidence": "high|low", "targets": []}}`.
  Router: `claude -p` (model `haiku`, no tools: `--allowedTools ""` and empty MCP as in the chat, text through stdin,
  `--max-budget-usd 0.2`, 60 s timeout) with mandatory JSON output; **an answer outside the whitelist or invalid =
  `none`**, and the front end then offers "send as an instruction to the loop" (`instruction`).
- **POST action/run** `{"action": "deliver|summary|push|clean_worktrees", "confirm": true, "targets": [...], "force": false}`
  -> `{"ok": true, "output": "up to 4000 characters, redacted", "pr": "url|null", "pr_status": "...", "push": "..."}`
  (`deliver`/`summary`) or `{"ok": true, "results": [...], "output": "..."}` (`push`/`clean_worktrees`). `confirm` must be
  exactly `true` (otherwise 400). Runs **the script** (`delivery.py` / `loop_summary.py`) as a subprocess with a 180 s
  timeout, **without a shell**, `LOOP_HEADLESS` NOT set (the user is delivering; `delivery.py` still refuses a protected
  branch, force and open features, and its refusal comes back in `output`/`push`). 409 when `running`, with a live lock
  or `no_loop`; 403 without the token. After a successful `deliver`, writes `pr`/`pr_status` to `meta.json` (as
  `run_loop.py` does) so `state` shows the link. Every run (action, when, short result) is recorded in
  `<work>/.specs/project/loop/internal/actions.log` and shows up in `state.recent_actions` (last 10: `{"action", "t", "ok", "summary"}`).
- **`push`** = push **only the current feature branches, without a PR**: for each target worktree (`git worktree list`,
  only `loop/*` branches) it runs `delivery.py <work>` with `LOOP_PR_MODE=off` (reusing its refusals). Optional
  `targets: [path, ...]` (default: every loop worktree). Result item `{"path", "branch", "ok", "push", "output"}`.
- **`clean_worktrees`** = delete the loop worktrees. **Destructive and irreversible for anything not committed/pushed**,
  so: (1) targets only from the `GET actions` list (`preview.worktrees`: `{"path", "branch", "dirty": bool, "unpushed_commits": N, "has_specs": bool}`),
  restricted to worktrees whose branch starts with `loop/` and whose folder is a sibling of the repo named
  `*-worktree-loop*`; **never** the main repo, never another worktree; (2) BEFORE deleting, a **mandatory backup into
  `output_dir`**: run `loop_summary.py` and copy the worktree's `.specs/` (feature specs, ROADMAP, STATE, VALIDATE,
  ANSWERS, GRAPH, phases, `loop/` with turns/snapshots/chat, **without** `node_modules`/`bin`/`obj`/`.git`) to
  `<output_dir>/loop-<project>-<YYYY-MM-DD-HHMM>/specs/` (shown as `preview.backup_dir`); check the copy (file count
  source vs destination); a failure ABORTS without deleting anything; (3) `git worktree remove <path>` **without
  --force**; a `dirty` worktree is only deleted with `force: true` in the body (the front end requires a second
  confirmation typing the branch name) and then uses `--force`; then `git worktree prune`; the branch and its commits are
  **not** deleted (only the folder); (4) returns per target `{"path", "branch", "backup", "deleted": bool, "error": null, "ok"}`.
  When the deleted worktree is the panel's own `work`, the state becomes `no_loop`.
- Safety: `confirm: true` required; a target outside the preview = 400; every run in `actions.log`; the server never
  deletes without a verified backup. The server ignores any `turn`: actions only apply to the current version.

### POST answer   `{"text": "..."}` or `{"option": 2}`
Only with `loop.status == "awaiting_user"` (otherwise 409). `option` N takes `options[N-1]`. Relaunches `run_loop.py`
**detached** with the `argv` of `LOOP-CMD.json` + `--answer "<text>"` (output to
`.specs/project/loop/internal/monitor-relaunch.log`). Response `{"ok": true, "pid": N}`. A live lock = 409.

### POST resume   `{}`
Relaunches without `--answer` (for `error`, `stopped` and `awaiting_user` after a pause). 409 when `running`.

### POST note   `{"text": "..."}`
Loop `running`: appends to `ANSWERS.md` the block `\n## Answer <iso> [pending]\nQuestion: (user note from the panel)\nUser answer: <text>\n`;
the next turn applies it. Relaunches nothing. Empty text = 400.

### POST pause   `{}`
Creates the `PAUSE` file. `{"ok": true}`. 409 when not `running`.

### POST loop/check   `{"wave": 2, "e2e": false, "push": true, "setup": ""}`
Runs `run_loop.py <repo> --check` + flags (same whitelist as start), synchronous, 60 s timeout. Response
`{"ok": true, "ready": true|false, "problems": ["..."], "warnings": ["..."], "command": "ready line"}`, parsing the lines
`[check] PROBLEM:`, `[check] WARNING:` and `[check] READY. Command:`. Creates nothing, spends nothing.

### POST loop/start   `{"wave": 2, "e2e": false, "push": true, "setup": "", "budget": 5, "max_cost": 30, "timeout": 60, "branch": "", "continue_on_blocked": false, "pause_on_blocked": false}`
Starts a **new** loop (or continues it, when the worktree already exists) by launching `run_loop.py <repo> <flags>`
detached (output in `.../internal/monitor-relaunch.log`), with `LOOP_MONITOR_URL=<this panel's url>` so `run_loop.py`
reuses this app instead of starting another one. Runs the internal `--check` first; `ready=false` = 409 with `problems`
and `warnings`. Also 409 with a live lock. Strict whitelist, **no shell**: `wave` int 1..8; `budget`/`max_cost` number
>= 0; `timeout` int 1..600; `branch` only `[A-Za-z0-9._/-]{1,80}` without `..` and not starting with `-`; `setup` string
up to 200 characters passed as ONE argument (`--setup <value>`); booleans become `--e2e`, `--no-push` (when `push` is
false), `--continue-on-blocked`, `--pause-on-blocked`. Unknown field = 400. `run_loop.py` writes `LOOP-CMD.json` itself.
Response `{"ok": true, "pid": N}`. This is the user's click triggering the loop; the orchestrator (Claude) never calls
this endpoint.

### POST shutdown   `{}`
Compatible no-op `{"ok": true}`: shutting down belongs to the app (`POST /api/app/shutdown`).

## Front-end polling
`GET state?visible=1` every 2 s while the tab is visible; hidden, it stops. A network error shows the "panel
disconnected" banner and retries every 5 s without losing what is on screen.

## Screen states the front end covers
`running` (animated); `awaiting_user` (large card with `reason`, `options` as buttons, a free-text field, `to_check`);
`error` (cause + "Try again" = `resume`); `stopped` (notice + "Resume"); `done` (PR, `validate`, summary); `no_loop`
(**New loop** panel: wave/e2e/push/setup/limits form, "Check" = `loop/check` showing PROBLEM/WARNING/READY and the
command, "Start loop" = `loop/start`; the same panel is reachable in `done`, `error` and `stopped` as "Start another
loop"); pause requested (the button becomes "Pausing after the turn..."). Confirmations are inline (no
`alert`/`confirm`).
