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
  `cost_usd`, `branch`, `base`, `pr`, `pr_status`, ... and the keys below), `ROADMAP-APPROVED.json`, `last-run.json` (in the
  repository, written when a loop is closed)
- `.specs/project/loop/internal/`: `LOOP.lock` (PID of the live `run_loop.py`), `PAUSE`, `decisions-review.json`,
  `follow-up-draft.json`, `delivery-pending.json` (in the repository, see below), `turns/NNN.json` (result of the
  turn's `claude -p`: `total_cost_usd`, `duration_ms`, `num_turns`, `usage`, `result`, `session_id`,
  `permission_denials`, `exit_code`, `timeout`, `turn`), `turns/NNN.events.jsonl` (optional live agent events),
  `snapshots/NNN/`, `chat/<thread>.json`, `jobs/<job>.json`, `whiteboard.json`, `actions.log`
- Task waves: `internal/tasks/<slug>.json` is the status of each task of a feature's `tasks.md` (`pending|running|done|failed`, optional note), written only by `harness.py tasks set`:
  `harness.py tasks waves <slug> [--max N]` (JSON waves of the tasks not done yet, at most `Max parallel tasks` per wave), `tasks set <slug> <Tn> <status> [--note TEXT]`, `tasks mirror <slug>` (copies the status into the
  `**Status**:` lines of `tasks.md`); `--root DIR` = the folder that holds `.specs`. `GET state` adds a `tasks` block (`{source: tasks.md|none, max_parallel, tasks: [{id, title, status, wave, deps, files, parallel, note}], waves: [{n, task_ids, split_by_limit}]}`) to every feature that has a `tasks.md` and to each feature being built (empty without `tasks.md`; `TaskStatusRepository`); the pipeline shows it in both list and diagram modes. `split_by_limit` = without the `Max parallel tasks` limit a task of the next wave would have run in this one (a dependency or a shared file is not a split). Subagent rows (`history`, `state.agents`) carry `task` (`"T5"` from the prompt line `SCOPE_TASK: <slug>/T5`, else `null`).
- `meta.json` keys added by the loop panel work: `delivery` `{push_pr, remove_worktree_on_finish}` (the two checkboxes of the Start
  dialog, written by the app at start; before the worktree exists they wait in `delivery-pending.json` of the repository and the
  engine merges them), `cost_usd` (total spent by the run, never reset by a start, a resume or a raise), `max_cost_usd` (cap of this
  run, written by `POST loop/raise-limit`; wins over `--max-cost`), `cost_warned_at` and `cost_warned_cap` (the 80 % notice, sent once
  per cap) and `decisions_mark` (line count of `VALIDATE.md` before each turn; a `level=critical` line written after it pauses the loop).
- `decisions-review.json`: `{"items": {"<id>": {"reviewed": bool, "reviewed_at": "ISO", "change_requested": bool}}}`; the id is the sha1
  of the raw `VALIDATE.md` line. `follow-up-draft.json`: `{"text": "", "decisions": ["<id>"]}` (the decisions service writes
  `decisions`, the follow-up service writes `text`; each keeps the other key). Both belong to the app; the engine never reads them.
- `last-run.json` (written by `engine/close_loop.py`) also records the run totals `cost_usd`, `max_cost_usd` and `turns`, and
  `finished: true` (top level and per repo) when the close came from `POST wrap-up/finish`; a finished entry is never re-attached by the next
  start (new branch). `GET loop/settings` `workspace.reattach_branch` names the branch the next start would re-attach to, or null.
- ROADMAP parsing reuses `loop_state.parse_roadmap`, `state_file`, `loop_dir`, `turns_dir` and `graph_view`; the parser
  is never rewritten.

## Signal files (in `.specs/project/loop/`)
| File | Who writes | Content |
|---|---|---|
| `json/LOOP-CMD.json` | `run_loop.py` on every run | `{"argv": ["python", "<run_loop.py>", "<repo>", "--wave", "2", ...], "cwd": "<dir>"}`. **Without** `--answer`/`--option`. It is the command used to relaunch. |
| `json/MONITOR.json` | the app, when a project is opened (deleted on shutdown) | `{"pid": 123, "port": 47801, "url": "http://localhost:47801/p/<id>/", "repo": "...", "work": "...", "started_at": "ISO"}` |
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
`pause`, `loop/check`, `loop/start`, `loop/raise-limit`, `instruction`, `POST whiteboard`, `chat/send`, `action/propose` and `action/run`.
Reads (`state`, `history`, `whiteboard`, `chat/*` GETs, `actions`, `loop/settings`...) and the legacy `shutdown` stay open. A project that
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
    "status": "running|continue|awaiting_user|paused|done|error|stopped|no_loop",
    "running": true, "pid": 1234,
    "turn": 12, "feature": ["F-08", "F-09"],
    "reason": "text (the question, or the cause of the error)", "next": "text",
    "options": ["a (recommended)", "b"], "to_check": ["x"], "at": "ISO", "pause_requested": false
  },
  "meta": {"turns": 12, "cost_usd": 6.2, "max_cost_usd": 50, "branch": "...", "pr": null, "pr_status": null,
           "claude": "cc", "claude_account": "token, cc account claude2", "claude_switches": 2,
           "claude_account_info": {"command": "cc", "role": "main|fallback|''", "method": "login|token|API key|''", "auth_method": "login|token|api_key|''",
                                   "email": "...", "org": "...", "plan": "team", "cc_account": "claude2", "switches": 2, "label": "token, cc account claude2"},
           "auth_method": "token", "auth_email": "", "auth_plan": "", "cc_account": "claude2",
           "claude_usage_view": [{"key": "claude|cc:claudep", "command": "claude", "cc_account": "", "status": "allowed|allowed_warning|rejected|''",
                                  "read_at": "ISO", "age_seconds": 720,
                                  "windows": [{"key": "five_hour|seven_day|<other>", "label": "5-hour|weekly", "pct": 60.0, "reset_at": 1792702800,
                                               "reset_local": "ISO", "exhausted": false}]}],
           "claude_totals": {"by_method": [{"method": "login|token|api_key|''", "turns": 3, "calls": 2, "cost_usd": 1.2}],
                             "by_command": [{"command": "claude|cc|unknown", "turns": 3, "calls": 2, "cost_usd": 1.2}], "turns": 3, "calls": 2, "cost_usd": 1.2},
           "max_parallel_tasks": 3, "max_parallel_tasks_in_header": false},
  "limits": [
    {"key": "cost", "label": "cost", "value": 6.2, "max": 50, "unit": "US$", "pct": 12.4},
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
  "decisions_summary": {"open_critical": 1, "open_important": 4},
  "recent_actions": [{"action": "deliver", "t": "ISO", "ok": true, "summary": "..."}],
  "roadmap_approval": {"approved": true, "reason": null, "at": "ISO", "features": 12},
  "planning_required": false,
  "answers": [{"date": "ISO", "question": "...", "answer": "...", "state": "pending|applied|automatic"}],
  "turns": [{"n": 12, "cost_usd": 0.61, "duration_s": 1394, "status": "continue|awaiting_user|done|error|running", "feature": ["a"], "commits": 1, "has_snapshot": true}],
  "quarantine": [{"id": "slug", "reason": "<note> (2 failures)", "stuck": ["other-feature"], "options": ["try again", "skip and continue"]}],
  "guard_events": [{"t": "ISO", "profile": "executor|spec|verifier|orchestrator", "rule": "path-scope", "detail": "outside the feature's PATHS (src/a)..."}],
  "guard_counts": {"path-scope": 2, "git-push": 1},
  "monitor": {"url": "http://localhost:47801/p/<id>/", "idle_in_s": null, "shutdown_in_s": null}
}
```
**Sign-in of every turn and agent.** `run_loop.py` writes, next to `claude_account`, the structured fields `auth_method` (`login` browser login, `token` sign-in token, `api_key`, or `''`), `auth_email`, `auth_plan` and `cc_account` (never a secret) in `turns/NNN.json` and `meta.json`. The same four fields, plus `claude` and `claude_account`, are on `state.turns[]`, `state.meta`, `pipeline.turns[]`, `history.turns[]` and, **inherited from the turn that spawned them** (an agent runs inside its turn's process, so it has the turn's command and sign-in), on `state.agents[]`, `history.turns[].agents[]` and `feature/<slug>` `agents[]` and `tasks.tasks[].executors[]` (`claude`, `auth_method`, `auth_email`, `cc_account`). A turn written before the fields existed is read from its `claude_account` label (so old turns still show a method); an unknown sign-in is `''`. In the panel: `login` = "subscription · browser login", `token` = "subscription · sign-in token", `api_key` = "API key · billed per token" (shown as a warning).
`meta.claude_totals` sums the loop turns and the panel's own `claude -p` calls (the call log of `GET jobs`) per sign-in method and per command: `{by_method: [{method, turns, calls, cost_usd}], by_command: [{command, turns, calls, cost_usd}], turns, calls, cost_usd}`, biggest cost first. `meta.max_parallel_tasks` is the ROADMAP header `Max parallel tasks` (default 3, 1 to 5; `max_parallel_tasks_in_header` says whether the header has the line): the panel quotes it in the "who spawns the agents" note.
`meta.claude_account_info` (`ClaudeAccountService`) is the account in use: the per-turn label of `meta.json` wins; the `auth status` rows (cached 5 min, refreshed by a background thread, never awaited by the poll) fill method/e-mail/org/plan and give the main command before turn 1 (empty fields until the first refresh ends). `GET /api/app/info` `claude_commands` reads the same cache and the hub diagnostics lists it.

`meta.claude_usage_view` is the 5-hour and weekly usage per command/account (`build_usage_view`), read only from files the loop already wrote and never waiting: per key (`claude`, or `cc:<account>` when the `cc` picker line exists) `meta.json` `claude_usage` first, else the newest turn log with `usage_windows`, else the newest panel call (`claude_calls.jsonl`). `pct` is 0-100, `reset_at` is epoch seconds, `exhausted` = `pct >= 100`, `age_seconds` = time since `read_at` (`null` when unknown). A configured command never seen has `windows: []` ("usage unknown"). Turn rows of `turns[]` also carry `usage_windows` (`{key: {pct, reset_at, reset_local}}`, `{}` for older turns) and `limit_status`. `GET /api/app/info` adds `claude_usage` (the same rows, taken from the newest opened project's loop files; empty windows without a project) and `usage` on every `claude_commands` row (its usage rows, one per `cc` account). `python harness.py doctor [--project <repo>]` prints one line per command/account, e.g. `5-hour 60 % (reset 14:30) · weekly 33 % (reset Mon 27/10 09:00)`.
Derivation rules (server):
- `loop.status`: `LOOP.lock` with a live PID = `running` (even if an old LOOP.json says otherwise); no lock and LOOP.json
  present = the LOOP.json status; no lock with LOOP.json in `continue` = `stopped` (the script died), or `error` when
  `meta.json` has `stopped == "error"` written after the LOOP.json `at`; no LOOP.json = `no_loop`.
- `agents`: always `orch` (data of the last `turns/NNN.json`: cost, tokens summed from `usage`, duration, `result`
  clipped to 300 characters as an `ai` log line). Plus one agent per open feature, with the role inferred from the status
  (`speccing` = spec-writer, `specced` = spec-reviewer, `approved`/`building` = executor-<kind>, `building` with a
  verification `note` = verifier). Tokens and cost per agent only when there is an `*.events.jsonl`; otherwise `null`.
  **Never invent numbers.**
- `limits`: `max` comes from `LOOP-CMD.json` (`--max-cost`, `--max-stall`, `--timeout`, with the defaults 50/10/60 when
  the flag is absent); `turn_minutes` only while `running`. `meta.max_cost_usd` is the cap in force: the one raised in the panel
  (`meta.json`) when set, else `--max-cost`, else 50.
- `loop.status` `paused`: a resumable stop written by the engine. The spend cap writes it with a `reason` that starts with
  `cost cap reached: US$ ` (for example `cost cap reached: US$ 50.12 of 50`). An old project may still show `error` with a reason that
  contains `total cost cap exceeded`; the panel treats both the same way.
- `decisions_summary`: counts of the decisions of `VALIDATE.md` not yet marked as reviewed (`GET decisions`), by level. `validate`
  stays for compatibility.
- `quarantine`: `blocked` features; `stuck` = features whose `deps` contain the blocked one; fixed `options` above.
- `answers`: parse of the `## Answer <date> [marker]` blocks of `ANSWERS.md`; `[automatic answer from the loop...]` =
  `automatic`.
- `guard_events`: the last 20 `blocked` decisions of `internal/guard.log` (written by the plugin's `PreToolUse` guard),
  newest first, as `{t, profile, rule, detail}`; `detail` clipped to 200 characters and credential-redacted. Info,
  `allowed-by-user` and `allow` lines (the audit lines of allowed MCP and Skill calls, see `MONITOR.md`) are left out and the raw file is never returned. `guard_counts` = blocks per rule over the whole
  log. No `guard.log` = `[]` and `{}`.
- `agents[].log` items read from `turns/NNN.events.jsonl` come from the turn in progress when its file exists (the turn after
  the last finished one), otherwise from the last finished turn. An event of `type: "guard"` keeps its `rule` and `reason`.
- A field missing at the source = `null` or an empty list, never a 500.

### GET agent/<id>/log
`{"id": "...", "log": [...], "truncated": false}` (same format as `agents[].log`, complete). Missing = empty list.

### GET turn/<n>
`{"n": 12, "turn_log": {...turns/NNN.json without the large raw field...}, "roadmap": "ROADMAP text in the snapshot", "features": [...same format, from the snapshot]}`;
404 `turn without snapshot` when there is no snapshot.

### GET errors
Global error center: `{"items": [{id, kind: loop|turn|guard|feature|launch, severity: error|warning|info, title, happened, detail, hint, when, turn, turns, turn_counts, recover: ["retry"|"commit_leftovers"|"discard_leftovers"], count, attempts, rule, profile}], "counts": {"error": N, "warning": N, "info": N}, "problems": N, "loop_status", "running"}`.
`problems` = `counts.error + counts.warning` and is the only number the panel chip shows. A PROBLEM is: the loop stop with status error (`loop`), failed or timed-out turns (`turn`, `error`), blocked/quarantined features (`feature`, `error`) and a launch log with a Traceback or "did not start" (`launch`, `warning`).
Everything else is `info` and never a problem: guard blocks (the guard worked), turn stderr notes (including "verifier still running") and a launch log with only noise. Items are sorted errors, warnings, info. Texts are redacted.
- `happened` = what happened, in plain words (the dialog's WHAT HAPPENED). `hint` = what the user can do about it (WHAT YOU CAN DO); `""` when there is nothing to do (a guard block, a turn failure with no known fix).
- `turns` = every turn the event happened in (sorted, unique; `[]` only when truly unknown, never guessed); `turn` = the single turn the item points at (the latest of `turns`, or the turn the stop/turn file names). Grouped items (guard blocks, the last launch) also carry `turn_counts` = `{"<turn>": events}` for the events whose turn is known (`count` stays the total, so the counts can add up to less). Loop stop: the turn named in the reason ("after turn 5") beats LOOP.json's, which can be stale. Blocked feature: the turn of the snapshot where it became `blocked` (`[]` without snapshots).
- Guard items are grouped per profile AND rule over every stored block (no time window; the id is `guard-<profile>-<rule>`): `rule`, `profile`, `count` = total blocks, `attempts` = the first 5 DISTINCT commands the agents tried (the shell command, or `Write <file>`; redacted, up to 200 characters; old log lines without it add nothing). `rule`/`profile` are `null` and `attempts` is `[]` on the other kinds. There is no "older guard blocks" item any more: the whole history is in the guard items, filtered by `turns`.
- Launch item (`launch-last`, problem lines of the LAST launch of `monitor-relaunch.log`): each line is attributed to the turn printed above it (`[loop] turn N | HH:MM:SS`); a line before any turn line goes to the first turn of that launch; a launch that never reached a turn has `turns: []`.

Where the events are saved: building this list appends the guard blocks and the launch problem lines to **`.specs/project/loop/internal/events/errors.jsonl`** (append only, one JSON object per line, one atomic write per batch; the only write of any GET, and a write failure is ignored). The id of an event is a hash of its content plus its ordinal among identical lines, so reading the same line again never duplicates it; a later record with the same `id` only fills fields (this is how an event whose turn was unknown gets its turn once the turn file exists). So the history survives a rotated or truncated `guard.log` and a launch log that starts over. Records: guard `{id, kind: "guard", t, turn, profile, rule, detail, attempt}`; launch `{id, kind: "launch", t, turn, launch, line}`; texts are redacted and clipped before they are stored. Turn items need no copy: they come from `turns/NNN.json`.
How a turn is found, in order: (1) the guard hook writes `turn` in each `guard.log` line (`{t, turn, profile, rule, decision, detail, attempt}`): inside the headless loop (`LOOP_HEADLESS=1`) it is `LOOP_TURN` when the engine exports it, else the last `internal/turns/NNN.json` + 1; outside the loop it is `null`; (2) the launch log's own turn lines; (3) older lines without a turn: by timestamp against the turn windows. A window ends at the mtime of `turns/NNN.json` and starts at the launch log's `[loop] turn N | HH:MM:SS` line, else the end minus the turn log's `duration_ms`, else the end of the previous turn; a window without a start is never used, and the turn in progress (loop running) has an open end. Blocks of the `planning` profile belong to no turn.

### GET pipeline
The six fixed steps of every feature and what each turn did, for the pipeline view. Not part of the 2 s polling: the panel reloads it when `meta.turns`, `loop.status` or `loop.turn` change, and about every 10 s while running. Cached per project (turn files, ROADMAP, LOOP.json, `meta.turns`).
`{"steps": [{key: spec|review|gate|exec|verify|commit, agent, role}], "features": [{id, name, stage, wave, deps, kind, status}], "stages": [{n, title}], "turns": [{n, label, status, cost_usd, duration_s, decision, events, files}], "live_turn": 9|null, "paused": false, "quarantine": ["slug"]}`.
- `wave` = 1 + the highest wave of its slug dependencies (none = 1; `dir:` dependencies are ignored and are not in `deps`).
- `name` (here, in `state` and in `turn/<n>`) = the `title="..."` of the feature's ROADMAP line, else the first heading of its `spec.md`, else the slug. Every agent of a turn serves ONE feature (its prompt's first `FEATURE: <slug>` line), so a turn lists one `spec-writer`, one `spec-reviewer`, one executor and one `verifier` event per feature; features of the same turn may be at different steps (each one is its own lane).
- `turns[].status` = the turn's LOOP.json status (`continue|awaiting_user|done|error`), `error` for a failed `claude -p`, `running` for the turn in progress (only while the loop runs; it has no file yet, so cost/duration/decision are `null` and its events are `RUNNING` for the features in `speccing|building`, agent inferred from the status, `agent_id: null`); `null` when nothing says. `decision` = the orchestrator's result text (redacted, up to 1200) or `null`.
- `events`: `{feature, step (0-5), status: DONE|RUNNING|QUEUED|FAIL|BLOCKED, out (up to 300, redacted)|null, agent_id: "vNNN:<id>"|null, agent}`. From the agents of the turn (role -> step: `spec-writer` 0, `spec-reviewer` 1, `executor-*` 3, `verifier`/`verifier-flow` 4; running = RUNNING; failed or `return_status` FAIL/BLOCKED = FAIL, BLOCKED for the reviewer; else DONE; agents without a feature or role are skipped) plus the status changes between the snapshots of turn N-1 and N: became `approved` = gate (step 2) DONE; became `verified` with a commit (ROADMAP `commit=`, else `commit <sha>` in the snapshot STATE.md) = commit (step 5) DONE with `out` "commit <sha>"; became `blocked` = BLOCKED on the step in progress (the turn's last agent for it, else the gate when the note starts with `gate`, else the step of the previous status). `QUEUED` is never produced today. `agent_id` is `null` on those derived marks.
- `label`: from the steps the turn's agents ran ("spec writing + spec review", "execution + verification"; more than 3 = "mixed steps"; no agent = "orchestration"; the turn in progress without events = "in progress"), plus " · wave N" when all its features share one wave.
- `files`: `[{path, add, del}]` = `git diff --numstat` between the HEADs of snapshots N-1 and N of the primary work folder (up to 200); `[]` when either HEAD is unknown. A binary file counts 0/0.
- `live_turn` = the turn in progress while running, else the last turn number, else `null`. `paused` = `loop.status` is `awaiting_user` and nothing runs. `quarantine` = slugs with status `blocked`.

### GET feature/<slug>
The central view of one feature being executed (the panel's Feature dialog, polled every 3 s while the loop runs). 404 `feature not found` for an unknown project or slug. Read only.
`{"id", "name", "status", "stage", "wave", "deps": ["slug"], "paths": ["src/x"], "scope": "Small"|null, "kind", "current_step": "spec|gate|build|verify|commit"|null, "steps": [{step, state: done|running|pending|failed}], "claude": {command, account}|null, "tasks": {...the state block..., tasks[].executors: [{agent_id, role, turn, state, return_status, duration_s, tokens, started_at, finished_at}]}, "agents": [{agent_id, role, turn, task, state, return_status, duration_s, tokens, started_at, finished_at, summary}]}`.
- `deps` leave out `dir:` entries (as in `GET pipeline`); `wave` = the feature's dependency wave from the pipeline; `claude` = command and account of the last turn record.
- `steps`/`current_step` follow the ROADMAP status (pending/speccing = spec, specced/approved = gate, building = build, verified = commit done) and the agents found: `verify` when the building note mentions the verifier, `build` failed when the newest executor failed and none runs; a blocked feature fails in the step of its last agent (else the step its note shows); a blocked feature with no trace has `current_step: null`.
- `executors` of a task = every subagent whose prompt names this feature and `SCOPE_TASK: <slug>/<Tn>`, oldest first (one per attempt). A task still `running` whose newest executor is not running and returned nothing is `interrupted` (cut off with its turn).
- Live: while the loop runs, the running session (the most recent session of the project modified after the LOOP.lock / last turn file) is read too; its unfinished subagents are `running` (duration, tokens and finish `null`), with ids `v<current turn>:<agent>`, and `GET agent/<id>/log` resolves them through that session. Without a session the tasks still show the status file. Never raises.

### GET quarantine/<slug>
The pause detail of a blocked feature; 404 `feature is not in quarantine` when the feature is unknown or not blocked. Read only, no token.
`{"id", "reason", "step": 4|null, "step_agent": "verifier"|null, "failures": 2, "turn": 8|null, "attempts": [{turn, agent, status: FAIL|BLOCKED, out, agent_id}], "failed_tests": [{name, why, expected, got}], "uncommitted": ["src/x.py"], "skip_impact": ["slug"], "summary": {"text", "source": "ai|facts", "at": "ISO|null"}, "options": ["try again", "skip and continue"]}`.
- `reason`: the LOOP.json pause reason when `awaiting_user` and it names the feature, else the ROADMAP note + failure count. `failures` = ROADMAP `tries`. `attempts` = the feature FAIL/BLOCKED events of the pipeline (oldest first, last 10); `step`/`step_agent`/`turn` come from the last one (`step` 2 when there is none and the note starts with `gate`; else `null`).
- `failed_tests`: parsed (pytest, unittest, jest, "expected ... got ...") from the final return, else the tool output, of the latest verifier attempt that shows any; `[]` when nothing parses; `why`/`expected`/`got` are `null` when not found.
- `uncommitted`: `git status --porcelain -uall` of `work`, relative paths with `/`, **without** `.specs/` (the loop's own state is never listed nor reverted). `skip_impact`: transitive dependents that are not verified (ROADMAP order).
- `summary`: the cached AI text when it was written for this quarantine state (same `failures` and `turn`), else one deterministic sentence built from the facts (`source: "facts"`, `at: null`).

### POST quarantine/<slug>/summary   `{}`
Token required; planning mode = 409; 404 when not blocked. -> 202 `{"ok": true, "job": "j-..."}`. A read-only `claude -p` (same safety as chat: sonnet, budget 1, Read/Grep/Glob only, no MCP, text through stdin, environment without secrets, cwd = `work`) is asked for 2-3 plain sentences: the root cause and what the user must decide. Poll with **GET chat/job/<job>** (`reply` = the text; it shares the 3-in-total limit with the chat: 409 beyond it, and 409 when a summary of the same feature is already running). On success the text is cached in `internal/quarantine/<slug>.summary.json` (`{text, at, failures, turn, cost_usd}`) and the next GET shows it with `source: "ai"`.

### POST quarantine/<slug>/retry   `{"guidance": "text"}`
Token required; planning mode = 409; 409 `repo_in_use` like `answer`; 404 when not blocked; 400 for a non-text `guidance` or one over 4000 characters (`guidance` is optional). Applies "try again" through the same path as **POST instruction** with `feature: <slug>`: the text is the loop's own option wording (LOOP.json `options`, default `try the feature again`) + `. Guidance from the user: <guidance>`. By `loop.status`: `awaiting_user` = answer + relaunch, `running` = pending note, `error|stopped|done` = pending note + relaunch, `no_loop` = 409. Response as `instruction`: `{"ok": true, "action": "answer|note|note+resume", "pid"?: N}`. The guidance ends in `ANSWERS.md` prefixed `[<slug>]`.

### POST quarantine/<slug>/skip   `{}`
Same as `retry` without guidance, with the loop's "skip" option wording (default `skip the feature and carry on with the others`).

### POST quarantine/<slug>/revert   `{"paths": ["src/x.py"]}`
Token required; planning mode = 409. Throws away the uncommitted changes of the listed files: tracked = `git restore --source=HEAD --staged --worktree`, added to the index = `git rm -f`, untracked = deleted; always inside `work`. 400 for a missing/empty/over-200 list, a non-text item, or any path that is not in `uncommitted` right now (`..`, absolute paths and `.specs/` included; a backslash is read as `/`); 404 when not blocked; **409 while the loop runs** (a live lock or relaunch). -> 200 `{"ok": true, "reverted": ["..."], "failed": []}`; some files failing = 500 with the same fields and `ok: false`.

### GET graph
`{"dependencies_mermaid": "...", "states_mermaid": "...", "text": "..."}` (from `graph_view`).

### GET jobs (project route)
`{"jobs": [...], "calls": [{"at": "ISO", "kind": "chat|roadmap|roadmap_refine|map|planning|follow_up|action|quarantine|catalog|panel", "claude": "claude|cc", "auth_method": "login|token|api_key|''", "auth_email": "...", "auth_plan": "team", "cc_account": "claude2|''", "cost_usd": 0.02|null, "ok": true}]}`.
`calls` is the call log of every `claude -p` the panel itself runs (not the loop turns): one line per `ClaudeClient.run_prompt`, newest first, up to 50 here. It is stored in `<work>/.specs/project/loop/internal/claude_calls.jsonl`, bounded (trimmed to the newest 400 lines once it passes 500). A line never holds the prompt, the reply or a secret. The sign-in method comes from the account cache of the loop panel (`auth status`); it is `''` until that cache has been read once. Recording never fails a call.

### GET, POST /api/app/settings (app route, Settings dialog)
`GET /api/app/settings` -> `{"ok": true, "settings": [{"key", "label", "description", "type": "bool|int|number|text|list|choice", "value", "default", "restart_required": bool, "customized": bool, "choices"?: [{"value", "label"}], "available"?: bool, "note"?: "..."}], "info": [{"key", "label", "value"}], "caveman": {"recommendation", "on": {"turns", "average_cost_usd"}, "off": {...}, "enough_data": bool, "message"}}`. Keys: `loop_isolation`, `loop_caveman`, `loop_extra_skills`, `orchestrator_model`, `claude_binary`, `claude_fallback_binaries`, `max_parallel_maps`, `idle_hours`, `inventory_giant_lines`, `inventory_giant_kb`, `output_dir`. `value` is what the app really uses (an invalid stored value shows the default); `customized` = the key is in `config.json`; `available` and `note` only on `loop_caveman` (false = no `SessionStart` hook with `caveman` in the user's settings). `caveman` compares the average `total_cost_usd` of the recorded turn logs (`turns/NNN.json`, every project) with `caveman: true` and without (a turn log with no flag counts as off); `enough_data` needs 3 turns on each side, else `message` is `Not enough data yet (N on / M off)`.

`POST /api/app/settings` (token required) `{"key", "value", "confirm": true}` validates by type and range (bool must be a boolean, int a whole number >= 1, `idle_hours` a number >= 0, list items are trimmed text, skill names letters/digits/dot/dash/underscore, choice one of `choices`), writes `config.json` atomically keeping every other key, and returns `{"ok": true, "setting": <item>}`. `{"key", "reset": true, "confirm": true}` removes the key (back to the default). 400: unknown key, invalid value, missing `confirm: true`, or turning `loop_caveman` on without the hook; 403 without the token; 409 when `config.json` exists but is not a JSON object (nothing is written). `loop_isolation`, `loop_caveman` and `loop_extra_skills` apply to the next `claude -p`; the others are read at app start (`restart_required`).

### Chat sends and jobs: what changed in the contract (instant chat)
The chat sends answer at once: `POST planning/send`, `planning/import/<id>/send`, `follow-up/send` and `quarantine/<slug>/summary` do only the cheap work in the request (validation, the refusals below and persisting the user message) and return 202 with the job id. The safety snapshot, the prerequisites check, the credential names file, the MCP access and the prompt are built in the job thread (`ClaudeJobCommand.before_start`). `chat/send` was already fast and is unchanged.
- **Synchronous refusals keep their status and `code`:** `loop_running`, the loop worktree already exists (planning sends only), a job already running (`planning_busy` on the planning sends), invalid text (400). For the follow-up send they are the ones listed under it. The planning prerequisites refusal (409 `planning_prerequisites`) is synchronous only when the last gates computed within 10 s are red; otherwise it is checked in the job thread.
- **A refusal can now be a job error.** When `before_start` raises a coded error the job ends `state: "error"` with the message in `error` and a stable `code` (today: `planning_prerequisites`); the user message the request appended is taken back out of the thread, and claude never starts. The job record in `internal/jobs/<id>.json` carries `code` only for such a job.
- **Poll responses.** `GET planning/job/<id>` returns `{ok, state, cost_usd}` plus `reply` (when `ok`) or `error` and, when the job has one, `code`. The panel maps `code` with the same table it uses for the old 4xx. While `before_start` runs the job record (and `GET jobs`) says `progress: "preparing the safety snapshot"` and `state` stays `running`.
- **`GET jobs`** items carry `progress` (only jobs with a preparation step) and `code` (only a job that ended with a coded error), next to the existing fields.

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

**POST chat/send** `{"thread": "<id|null>", "scope": {"type": "agent|general|error|quarantine|plan", "agent_id": "vNNN:orch|vNNN:<agentId>|<state id>|null", "turn": N|null}, "text": "..."}`
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
New scopes for the dialogs: `{"type": "error", "error_id": "<id of an item of GET errors>"}` and `{"type": "quarantine", "feature": "<slug>"}` (the thread stores `error_id` / `feature` in its scope). The server builds the context itself, never from browser text: for `error` the item (title, kind, severity, when, turn, detail, hint, recovery buttons) and the pipeline events of its turn; for `quarantine` the same facts as `GET quarantine/<slug>`, plus the feature folder and the `.specs` files by path. No session is resumed; same read-only safety and redaction. Scope `{"type": "plan"}` ("Ask about the plan" in the Plan & files dialog): the same read-only job (Sonnet, US$ 1, `Read Grep Glob`, no MCP) with a fixed prefix that limits the answers to the plan, the specs, the codebase maps, the AI decisions and the loop state; the plan files are read from the work folder (the loop worktree when it exists), by path, never pasted from the browser. An unknown error id or a feature that is not blocked = 404; a malformed `error_id`/`feature` = 400.

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
- `no_loop`: 409 "start a loop with Start loop in the navbar".
`attachments` (each up to 2000 characters, up to 5) go into the block as `Attached context:`; empty `text` = 400
(`empty text`); 409 with a live lock when relaunching. Token required. It is still the user triggering.

### Objective actions of the current version (without opening another loop)
Third composer mode, only on the CURRENT version: **Act**. Runs **deterministic actions from a whitelist** (code, not
the AI), with **explicit confirmation** from the user first. The AI only serves to *understand a free-text request*.
Whitelist: `deliver`, `summary`, `push` and `clean_worktrees`.
- `deliver` = push **only the feature branch** + PR to the integration branch through `engine/delivery.py` (never a push
  to a protected branch such as `main`/`master`/the integration branch, never a force push; the refusals of
  `delivery.py` and of the guard still apply). The integration branch is the ROADMAP `**Branch base:**` when set, else `LOOP_PR_BASE` / `LOOP_INTEGRATION_BRANCH` (default
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

### POST resume   `{}` or `{"leftovers": "commit"|"discard"}`
Relaunches without `--answer` (for `error`, `stopped` and `awaiting_user` after a pause). 409 when `running`.

### POST note   `{"text": "..."}`
Loop `running`: appends to `ANSWERS.md` the block `\n## Answer <iso> [pending]\nQuestion: (user note from the panel)\nUser answer: <text>\n`;
the next turn applies it. Relaunches nothing. Empty text = 400.

### POST pause   `{}`
Creates the `PAUSE` file. `{"ok": true}`. 409 when not `running`.

### GET loop/settings
What the Start dialog shows, read-only: the values are decided in planning (the ROADMAP header) and read here. Works in any mode.
```json
{
  "settings": {"dispatch": "auto", "e2e": false, "budget_turn": 5.0, "budget_loop": 50.0, "minutes_turn": 60, "branch": "",
               "setup": "", "when_stuck": "pause", "push_pr": true},
  "problems": [{"name": "Max minutes per turn", "value": "soon", "message": "... using the default (60)"}],
  "missing": ["setup"], "defaults": {"dispatch": "auto", "e2e": false, "...": "..."},
  "integration_branch": "developer",
  "delivery": {"push_pr": true, "remove_worktree_on_finish": true},
  "workspace": {"repos": [{"name": "app", "uncommitted_context": [{"path": "docs/codebase/STACK.md", "kind": "map", "state": "modified"}]}],
                "worktree_created": false, "branch": "", "uncommitted": true},
  "models": {"orchestrator": "opus", "agents": "sonnet"}
}
```
`settings` come from `domain/roadmap_settings.parse_loop_settings` over the ROADMAP of the work folder; the header names are
`Dispatch`, `E2E`, `Max spend per turn`, `Max spend for the loop`, `Max minutes per turn`, `Branch`, `Branch base`, `Setup`,
`When a feature gets stuck`, `Push and PR` and `Max parallel tasks` (executors per wave inside one feature, 1 to 5, default 3; `settings.max_parallel_tasks`). The payload also has `base_branch_resolved`
`{"name", "source": "roadmap|env|default"}`: by default the loop branches from `developer`. To change it, write `Branch base: main` (or `release/2.0`) in the ROADMAP header.
Order: ROADMAP, then LOOP_INTEGRATION_BRANCH, then developer. The PR goes to the same branch. (grammar `**Name:** value`, outside HTML comments). A line that is absent is listed
in `missing` and takes its default; an invalid value takes the default and adds an item to `problems`. `delivery` is what
`LoopControlService.read_delivery` returns: the worktree meta, else `delivery-pending.json`, else the defaults (push per the ROADMAP,
remove on Finish). `models.orchestrator` is the `orchestrator_model` key of `config.json` (default `opus`; `""` = no `--model` flag).

### POST loop/check   `{"push": true, "remove_worktree_on_finish": true}`
Runs `run_loop.py <repo> --check` with the options built from the ROADMAP header (plus `push` when given), synchronous, 60 s timeout.
The body only carries the two run choices (both optional booleans; any other key is ignored, a non-boolean is 400). Response
`{"ok": true, "ready": true|false, "problems": ["..."], "warnings": ["..."], "command": "ready line", "dispatch": {"wave": 3, "auto": true, "reason": "..."}}`,
parsing the lines `[check] PROBLEM:`, `[check] WARNING:` and `[check] READY. Command:`. An invalid ROADMAP setting adds a `WARNING`
(the default is used; it never blocks). Creates nothing, spends nothing. The response also has
`"repos": [{"name", "uncommitted_context": [{"path", "kind": "map|claude|system", "state"}]}]`: the map documents, `CLAUDE.md` and `SYSTEM.md` of each repo that are not
committed (the worktree is born from the last commit and would not have them); the same facts are `WARNING:` lines. A confirmed monorepo adds
`PROBLEM`/`WARNING` lines for its subprojects (unconfirmed list, a subproject without Conformance or map, giant files a feature touches; see Subprojects below).

### POST loop/start   `{"push": true, "remove_worktree_on_finish": true}`
Starts a **new** loop (or continues it, when the worktree already exists) by launching `run_loop.py <repo> <options>`
detached (output in `.../internal/monitor-relaunch.log`), with `LOOP_MONITOR_URL=<this panel's url>` so `run_loop.py`
reuses this app instead of starting another one. The body keeps only the two run choices of the Start dialog; every other value
(dispatch, E2E, budgets, minutes, branch, setup, what to do with a stuck feature) comes from the ROADMAP header (`GET loop/settings`).
`push` false becomes `--no-push`; a value equal to the engine default is not repeated; `--orchestrator-model <config value>` is
always passed. Runs the internal `--check` first; `ready=false` = 409 with `problems` and `warnings`. Also 409 with a live lock,
and 409 `repos_not_defined` when the scope was never defined. `setup` goes as ONE argument, **no shell**. `run_loop.py` writes
`LOOP-CMD.json` itself. The two choices are saved before the engine starts: in `meta.json` `delivery` when the worktree already
exists, else in `delivery-pending.json` of the repository (the engine merges it into the new `meta.json`); the wrap-up reads them
back. Response `{"ok": true, "pid": N}`. This is the user's click triggering the loop; the orchestrator (Claude) never calls
this endpoint.

### POST loop/raise-limit   `{"max_cost_usd": 80}`
The spend cap of THIS run goes up and the same loop resumes. The run stops as `paused` with a reason `cost cap reached: US$ x of y`
when the total spent (`meta.cost_usd`, summed over every turn and never reset) passes the cap (default `--max-cost 50`, `0` = no cap)
or when a turn would start already over it; at 80 % of the cap one notice is sent. The route writes `meta.json` `max_cost_usd`
(which wins over `--max-cost`) and relaunches like `resume` (it keeps the spent total and every other meta key). 202
`{"ok": true, "cost_usd": <spent>, "max_cost_usd": 80.0}`. 400 when `max_cost_usd` is not a number above 0 or is not above the
spent total (`{"error": "the new limit must be above the US$ 50.12 already spent", "cost_usd": 50.12}`); 409 with a live loop or
lock; refused in planning mode; 409 `repo_in_use` like a start. Token required.

### Critical decisions pause the loop
Not a route. The engine reads `VALIDATE.md` after each turn; a line with `level=critical` written during the turn stops the loop
with `LOOP.json` `awaiting_user`, `feature` = the slug, `reason` = `critical decision: <doubt> | chose: <choice> | revert: <how>`
(`| N more critical decision(s) in VALIDATE.md` when several) and `options` `["keep as decided", "change it: <write how>"]`. It is never
answered automatically and notifies like any open question (exit 10). `POST answer` resumes it; `important` lines never stop the loop.

### AI decisions (project routes)
Grammar of a `VALIDATE.md` line, owned by `engine/decisions.py`: `- [<slug>|general] turn=<n> level=<important|critical> <doubt> | chose: <choice> | revert: <how>`.
An older line without `turn=`/`level=` reads as turn `null`, level `important`; a line that does not parse is returned in `unparsed`.
`VALIDATE.md` is never edited by the app; what the user did lives in `decisions-review.json`.
- **GET decisions[?turn=N]** -> `{"items": [{"id", "slug", "turn", "level", "doubt", "chose", "revert", "reviewed", "reviewed_at", "change_requested"}], "unparsed": [{"unparsed": true, "raw", "line_no", "id"}], "counts": {"open_critical": 1, "open_important": 4}}`.
  `id` = sha1 of the raw line; `counts` always cover the whole loop, not only the filtered turn. `turn` not a non-negative integer = 400.
- **POST decisions/<id>/review** `{}` -> `{"ok": true}` (marks it reviewed). Unknown id = 404 `decision not found`.
- **POST decisions/<id>/change** `{}` -> `{"ok": true, "draft": {"text": "...", "decisions": ["<id>"]}}`: flags it `change_requested` and adds the id to
  `follow-up-draft.json` (no duplicate, the draft `text` is kept). 404 for an unknown id.

### Codebase maps (read-only)
- **GET codebase/tree** -> `{"groups": [{"folder": "", "label": "...", "files": [{"path": "docs/codebase/STACK.md", "name": "STACK.md"}]}]}`: the map of the root and, in a
  confirmed monorepo, of each subproject that is not skipped. Groups with no files are left out.
- **GET codebase/file?path=<path>** -> `{"path", "text", "version", "editable": false, "not_editable_reason": "read-only view"}`. Only a path listed by `codebase/tree`;
  `..`, a drive or a leading slash = 400 `invalid path`; any other path = 404.

### Wrap-up and Finish (project routes)
**GET wrap-up** (also in the Wrap-up view of the LOOP column, when the loop is `done` or `stopped`)
```json
{
  "status": "done",
  "summary": {"features_done": 12, "features_total": 12, "pr_url": "https://.../pull/7", "compare_url": null, "cost_usd": 18.4, "max_cost_usd": 50, "turns": 14},
  "delivery": {"push_pr": true, "remove_worktree_on_finish": true, "done": true, "integration_branch": "developer"},
  "check": {"repos": [{"name": "app", "repo": "app", "branch": "loop/...", "pushed": true, "uncommitted": [], "unpushed": [],
                       "pr_url": "...", "compare_url": null, "reasons": [], "notes": []}]},
  "unreviewed_critical": 0
}
```
`delivery.integration_branch` is the PR target (BR-04: the ROADMAP `**Branch base:**`, else `LOOP_PR_BASE` / `LOOP_INTEGRATION_BRANCH`, default `developer`), shown in the Wrap-up and the receipt; `GET loop/settings` carries the same `integration_branch` for the Start dialog.
`check` is the same per-repo check as `GET close-loop/check`, plus the lists the panel shows (`uncommitted` files, `unpushed` commits as `<sha> <subject>`).
A repo with a pushed branch but no PR (no `gh`, a non-GitHub remote) gets a non-blocking note in `notes`. No worktree = `{"repos": []}`.

**POST wrap-up/finish** `{"mode": "safe|keep_worktree|remove_anyway", "confirm_text"?: "<repo folder name>", "accept_unreviewed"?: false}`
- `safe`: delivery first when pending and the run asked for it (`delivery.py`: push of the `loop/*` branch + PR, never a merge), then the normal close
  (`engine/close_loop.py`: verified backup, plan copied back, `last-run.json`, `git worktree remove` without `--force`) only when the run asked to remove
  the worktree. Anything that would be lost refuses the whole call.
- `keep_worktree`: delivery only; the worktree stays ("Commit and push them first" in the dialog).
- `remove_anyway`: needs `confirm_text` equal to the repository folder name; no delivery; the backup still runs, then the worktree is removed with `--force`. The local
  branch and its commits stay.
- Every mode refuses unreviewed critical decisions unless `accept_unreviewed` is `true`.

200 `{"ok": true, "mode": "...", "receipt": {"pr_url", "compare_url", "branch", "backup_dir", "cost_usd", "max_cost_usd", "turns", "worktree_removed": true, "closed": [...], "kept": [...]}}`.
Refusals: 400 for an invalid `mode`, `confirm_text` or `accept_unreviewed` (a wrong confirm text included); 409 when the loop runs (`loop_running`), when a follow-up job runs
(`follow_up_running`) or without a worktree (`no_worktree`); and 409 `wrap_up_blocked` when it is not safe to continue, with the reasons per repo (nothing was removed):
```json
{"ok": false, "error": "nothing was removed: app: 2 uncommitted file(s) would be lost", "code": "wrap_up_blocked",
 "reasons": [{"repo": "app", "kind": "uncommitted|unpushed|unreviewed_critical|close_failed|<other check code>",
              "files": ["src/a.py"], "commits": ["1c02e9a fix x"], "message": "app: 2 uncommitted file(s) would be lost", "decisions": ["<id>"]}]}
```
(`decisions` only on `unreviewed_critical`; a `close_failed` 409 also carries `closed` and `backup`.) Token required.

### Follow-up (project routes)
A follow-up is a chat that plans new work on the same branch after a finished loop, then runs it as a normal round. It needs the loop worktree (after Finish removes it,
use Back to planning instead). One thread per round in `.specs/project/loop/internal/follow-up/round-<n>.json` (`round`, `session_id`, `spent_usd`, `summary_note`, `baseline`, `messages`, `closed_at`);
every message resumes the round's claude session (the `session_id` is never returned).
- **GET follow-up** -> `{"round": 1, "thread": {"messages": [...], "spent_usd": 0, "summary_note": ""}, "stage": {"id": "Follow-up-1", "n": 1, "name": "Follow-up 1", "features": [{"slug", "status"}]}|null, "last_job": {...}|null,
  "caps": {"message_usd": 1, "round_usd": 5}, "can_send": {"ok", "reason"}, "can_continue": {"ok", "reason"}, "finished": false, "draft": {"text": "...", "decisions": ["<id>"]}}`
  (`stage` = the stage of the current round; `finished` = the loop worktree is gone; `reason` says why a button is off).
- **POST follow-up/send** `{"text": "...", "decisions": ["<id>"]}` -> 202 `{"ok": true, "job": {...}}`. A `claude -p` job of type `follow_up`: `--model sonnet`, hook
  profile `planning`, no MCP, `Read Grep Glob` plus `Edit` only on `ROADMAP.md` and `FLOWS.md`, run in the loop worktree. The cap is fixed, US$ 1 per message and US$ 5 per round (a `budget_usd` in the body is ignored).
  The prompt carries the instructions and the chosen decisions and asks for ONE stage `## Stage Follow-up-<n> - Follow-up <n>` with new features in the ROADMAP grammar plus FLOWS entries; later messages of the round may edit that stage.
  When the job ends the app accepts ONLY edits of the round's stage (and appended FLOWS text): anything else is undone, both files go back to their text before the turn and the message ends as `error` with the reason.
  The text is kept in the draft until a send succeeds. 400 without text and without decisions, with text over 8000 characters or an unknown decision id. 409 with `code`: `loop_running`, `follow_up_busy` (another claude job runs),
  `no_worktree`, `follow_up_budget` (the round already spent US$ 5).
- **POST follow-up/run** (**Continue loop**) `{}` -> the answer of `loop/start` (`{"ok": true, "pid": N}`, with its 400/409 refusals): closes the round (the next send is round n+1 with spend 0), renews the roadmap approval for the new feature list
  (clicking it is the approval of the reviewed stage), then starts a round on the same worktree with the delivery choices of the first start and the ROADMAP header settings. 409 `no_follow_up_stage` when the round has no stage with open
  features; 409 `repo_in_use` like a start.

### POST shutdown   `{}`
Compatible no-op `{"ok": true}`: shutting down belongs to the app (`POST /api/app/shutdown`).

### GET workspace (project route; planning and loop)
`{"ok", "multi", "explanation", "repos": [repo], "job", "integration_branch": "developer", "plan_folder": "<work dir>/.specs/project", "defined", "rejected": [{path, reason}]}`.
`integration_branch` is the ROADMAP `**Branch base:**`, else `LOOP_PR_BASE` / `LOOP_INTEGRATION_BRANCH` (environment or the app `.env`, default `developer`), the PR target; `plan_folder` is where the plan is written.
`repo` = `{name, path, primary, folder, worktree, worktree_exists, branch, base, state, dirty, uncommitted_context, copy, setup_command, notes, role, reason, changed_by_feature, read_only, current_branch, base_branch, base_sha, upstream, setup, setup_files, other_changes}`:
- `state`: `not_created | ready | loop_running | dirty | missing | read_only` (`read_only` = a `reference` repository: read by the agents, never given a worktree).
- `current_branch` = the branch the user's folder is on now; `base_branch` = where the loop worktree would start (BR-01: the integration branch when it exists as a local branch, else the current branch); `base_sha` = the short sha of that branch; `upstream` = the remote branch the current branch tracks, or null. `null`s for a missing folder.
- `setup` = `{"state": "not_set_up|set_up|map_missing|mapping", "monorepo": bool}`: the installer's progress of that repository. A `reference` repository needs no Conformance and a missing map is only `map_missing`; a confirmed monorepo is judged per non-skipped subproject; `mapping` while a map job or a subproject map queue runs.
- `setup_files` = the uncommitted setup files of the user's folder (`docs/codebase/*.md` of the repo and of each non-skipped subproject, the root `SYSTEM.md`, `CLAUDE.md` files that carry an app block, `.specs/project/**` and `.specs/features/**`; never `.specs/project/loop/**` and never an `.env*`); `other_changes` = the other changed (tracked) files, which are NOT carried. Both are `[]` for a `reference` repository and once the loop worktree exists. `reason` = the sentence the chat (or the user) wrote about why the repository is in the project (null when none).

### Repository list from the Workspace dialog (project routes, token needed)
The same service as the hub's `repos` routes (`RepoRegistryService`), under the panel's prefix:
- `POST workspace/repos` `{"path", "role"?: "edit|reference", "reason"?, "copy"?, "setup"?}` -> `{ok, repo, repos, multi}`. Role defaults to `edit`; `reason` is one sentence (up to 400 characters, blanks collapsed). 400 for a folder that does not exist or is not a git repository, a detached HEAD, a repeated folder (`already_listed`), a bad role.
- `PATCH workspace/repos/<name>` `{"role"?, "reason"?}` (at least one) -> the `GET repos` body. The primary is always `edit` (409 `primary`); `edit` -> `reference` is refused while that repository's loop worktree exists; 409 while a loop runs.
- `DELETE workspace/repos/<name>` -> `{ok, removed, note}`: only takes it off the list, no folder or worktree is deleted; 409 for the primary or while a loop runs.

### POST workspace/repos/<name>/commit-setup   `{"confirm_branch": "<current branch>"}`
"Commit setup files and push" (`SetupCommitService`). Commits ONLY the repository's uncommitted setup files (`setup_files` above) on the CURRENT branch of the user's folder with the message
`chore(setup): codebase maps and agent context` (`git commit -- <paths>`: anything else already staged stays out) and pushes that branch to its upstream (`git push <remote> HEAD:refs/heads/<upstream branch>`, never forced, never another branch).
`confirm_branch` must be the branch the dialog named (400 otherwise, with `branch`). Response `{"ok": true, "repo", "branch", "commit", "pushed_to": "origin/<branch>", "files": ["docs/codebase/STACK.md", ...]}`.
409 refusals carry `code` and `reason`: `detached_head`, `protected_branch` (`main` / `master`), `no_upstream` (the message says `git push -u origin <branch>`), `busy` (a loop or a job runs), `nothing_to_commit`, `commit_failed`, `push_failed` (the commit stays; extras `committed`, `branch`).
The Workspace dialog shows it per repository with the file list, the branch and its upstream, behind a confirmation that names the branch.

### POST workspace/repos/<name>/commit-push   `{"message"?: "one line", "push"?: true}`
Legacy route, now ONLY for a **loop worktree**: `git add -A`, commit and (when `push` is true, the default) push in `<repo>-worktree-loop` on its `loop/*` branch, never forced. The user's own folder is never saved here:
409 `user_folder_refused` when the repo has no worktree (the message sends the user to Commit setup files and push), 409 `not_loop_branch` when the worktree is on `main` / `master` or on any branch that does not start with `loop/`.
Also 409 `busy` while a loop runs, 409 `commit_push_failed` when git fails (extras `committed`, `branch`: the commit may exist even if the push failed); 400 for an invalid message (one line up to 200 characters; empty = default message), a non-boolean `push` or a missing folder.
Response `{"ok": true, "repo", "branch", "committed", "pushed", "commit"}`.

### Planning: import a spec and readiness (project routes `/p/<id>/api/...`)
- `POST planning/import/preview` `{"path": "<spec feature folder>"}` -> `{slug, files, skipped, counts, items, warnings, refused, already_imported}`: what an import would take (requirement/decision/task ids, core sections, warnings).
  Nothing is copied and no AI is called; `refused` is `{reason, counts}` when a limit is broken.
- `POST planning/import` `{"path": "<spec feature folder>"}` -> `{import_id, files, main: "spec.md", at, source: "folder"}`. Only a tlc-spec-driven
  feature folder with `spec.md` is imported: every `.md` inside it (subfolders too), read only, credentials redacted. Limits: depth 3, 30 files, 400 KB per file, 2 MB in total.
  The source is copied twice: to `.specs/project/loop/internal/planning/imported/<stamp>-<feature>/` (with `import.json` and `IMPORT-LOG.md`, the log of what was read, skipped and detected) and to
  `.specs/features/<slug>/` of the work folder; it sends nothing. 400 for a file path, pasted `text`, a folder without `spec.md`, an empty or oversized `.md`, a folder over the limits, a missing folder.
  409 `already_imported` (the same source folder or slug was imported; the response carries the earlier `import_id` and `log_path`) or `feature_folder_exists` (`.specs/features/<slug>/` exists and was not imported by the app).
- `GET planning/imports` -> `{imports: [{import_id, files, main, at, source, sent_at}]}` newest first (the chat shows them as attachments).
- `POST planning/import/<import_id>/send` -> 202 `{ok, job_id}` like `planning/send`: ONE fixed planning message that tells the AI to build ROADMAP, FLOWS,
  PROJECT and the repository scope from the copies, and to write `.specs/project/IMPORT-COVERAGE.md`. Another feature adds to the conversation; nothing is wiped.
  **Coverage.** Nothing of the source may be lost: each ROADMAP feature built from it carries `reqs=ORIG-01..06,CRIA-01..08` (comma list of ids or ranges, no spaces) and points to `.specs/features/<slug>/`.
  `IMPORT-COVERAGE.md` has one `## <slug>` heading per import and under it a table `| Item | Kind | Destination |` with a row for EVERY requirement/decision/task id of the source (or, without ids, every core section; a range like
  `ORIG-01..06` counts as its ids). Destination is a location (`ROADMAP:[slug] reqs`, `RULES.md#...`, `FLOWS.md#...`) or `dropped: <reason>`; `domain/import_coverage.py` parses it.
  After a send, the planning gate **`import_coverage`** is not optional: it fails when `IMPORT-COVERAGE.md` is missing or an item has no row (`missing: <id>`); `dropped: <item> - <reason>` lines are shown and never block.
  The count (`N covered, N dropped, N missing`) is appended to the `IMPORT-LOG.md` of the import.
- `GET planning/readiness` -> the cached "Ready to run the loop" result `{state: ready|problems|stale|running|error, at, problems: [...], warnings: [...], fingerprint, error?}`.
  It is `stale` when ROADMAP, REPOS, TEST-ENVIRONMENT or `.env.test` changed since it ran; `running` while a check is scheduled.
- `POST planning/readiness/run` -> 202 with the live state (`running`); poll `GET planning/readiness`. It runs `run_loop.py <repo> --check --planning` (timeout
  120 s; a timeout or script error is `state: error`, "readiness check did not run"). The result may also carry `uncommitted`: ONE block per **edit** repository whose worktree is not created yet and that
  has uncommitted changes, `[{"kind": "uncommitted", "repo", "setup_files": [paths], "other_changes": [paths]}]` (RD-01; the text line of the same check is not repeated in `warnings`). Its text reads "N setup files not
  committed: they will be carried into the worktree (or commit them from Workspace): ..." and, separately, "M other changed files will NOT be carried: ..."; `engine/readiness_checks.uncommitted_blocks`. A `reference`
  repository has none. Ordering (RD-02): while a real earlier step is open (the installer, a missing Conformance or map, no usable `claude`) the check never says "generate and APPROVE the roadmap first"; the first
  PROBLEM is that step.
- The planning gates (`GET planning`, `gates`) end with `ready` ("Ready to run the loop"). **`POST roadmap/approve` answers 422** (`code: planning_gates`) with the
  gates, the `ready` one failed and its first problem as the reason, while the readiness is not `ready`.
- **Readiness items.** The result of `GET planning/readiness` also carries `items: [{kind, level: problem|warning, repo, files: [paths], text, fix}]`, the structured twin of `problems` / `warnings`: one `uncommitted` item per repository
  (its setup and other changed files merged, `fix` = "Commit these files (the worktree is born from the last commit)") and one item per other line (`kind` such as `other`, with a `fix` sentence when known). The check runs with `--json`:
  `harness.py check <repo> --json` prints, after the text lines, one `[check] JSON: {"ready", "problems", "warnings", "items"}` line the service parses (without it, the items are built from the text lines).

### POST projects/pick-folder (hub route)
`{"initial"?: "<existing folder>"}` -> `{"path": "...|null", "reason": "...|null"}`. Native-first folder picker: the OS dialog (tkinter in a subprocess, 300 s timeout) opens in `initial` when it is an existing folder (anything else is ignored).
`path` is null with a `reason` when the user cancelled, there is no tkinter or the dialog failed; only then the panel falls back to its own folder browser (`GET system/folders`). Every folder picker of the panel (open project, repositories, spec import) goes through it.

### Test environment: Access card (project routes)
`GET /p/<id>/api/test-environment` returns `{ok, ready, names, fill_in, access, shared_paths}`. `access` is data for the **Access** card, never a value:
`{credential_layers: [{name: project|machine|shared, label, path, exists, names: [{name, source, winning}]}], mcp: [{server, tools, configured}]}`. The shared
layers (plugin `shared/.env`, `~/.claude/skills/_shared/.env`) are always read, so `shared-blocked` no longer exists and an old `ACCESS.json` is migrated
(its `grafana_read` / `shared_fallback` keys are dropped). `PUT test-environment/access` was removed (404). `PUT test-environment/values` is unchanged.

### What the agents may use, by default
- **MCP read access** (`engine/mcp_access.py`): the servers `grafana`, `mcp-mysql-dev` and `mcp-mysql-prod` that the user defined in `~/.claude.json` are given to the
  planning reply and to every loop `claude -p` with their read tools only (Grafana: the Loki/datasource read list, writers denied by name; MySQL: `mysql_query`).
  A server that is not configured is simply absent. No per-project toggle.
- **Guard** (`plugin/skills/orchestrator/hooks/guard.py`): `sql-write` blocks a write statement sent through a MySQL MCP tool (the agent writes a `.sql` script in the
  feature folder `sql/` and returns `NEEDS_CONFIRM: apply <script> on DEV, then answer`); `migration-target` blocks a migration command (`dotnet ef database update`
  ...) whose target is production, while the same commands on DEV are allowed; `capabilities-block` (planning profile) reverts an edit inside the generated block of
  PROJECT.md.
- **PROJECT.md block** (`services/project_capabilities_service.py`): the section between `<!-- maestro:capabilities -->` and `<!-- /maestro:capabilities -->` lists the
  repositories with role and path, what agents may read, what they may edit, and what is reserved (commit = orchestrator, push/PR = `harness.py deliver`). It is
  rewritten when repositories, the MCP configuration or the roadmap change; text outside the markers is never touched.
- **Production database script** (`engine/prod_database_script.py`): at deliver and at close, when a feature added a migration or a `.sql` script, the engine writes
  `~/Downloads/<project>-prod-database-<YYYY-MM-DD>.sql` (idempotent migration script per edit repo, then the manual scripts in ROADMAP order, header "run on
  PRODUCTION before deploying; review first"). The result carries `prod_database`; a failure is a warning, never a block. Nothing runs on production.

### Scope of the repositories: defined in the planning chat (hub routes, keyed by project id)
The repositories a loop will touch come from the main planning chat: the AI writes `.specs/project/REPOS-SUGGESTED.json` and, when the reply ends, the app validates
and applies it to `REPOS.json` (a folder that does not exist or is not a git repository is refused, with the reason shown in the next status block and as a warning
chip). There is **no confirm step** (RP-01): the list is managed by the chat and by hand in the **Workspace** dialog (routes `POST|PATCH|DELETE workspace/repos`, above), and a
project always has at least one repository the loop may change (the primary is always `edit`). Each repo has a **role**: `edit` (the loop changes it; a worktree is
created only when a ROADMAP feature has `paths=<repo>:...`) or `reference` (read only, no worktree, given to the agents with `--add-dir`) and a **reason**: ONE sentence
written by the chat (or the user) saying why the repository is in the project and why that role (`REPOS.json` `reason`, up to 400 characters; the planning skill tells the
chat to write it whenever it adds a repository or changes a role). `GET /api/app/projects/<id>/repos` carries `defined` (now always true: `project_repos.is_scope_defined`
is "there is an edit repository"; an old `REPOS.json` with `scope_defined` keeps working), per repo `role` (`edit|reference`), `reason` (one line or
`null`) and `changed_by_feature` (a ROADMAP feature names the repo in `paths=`), `suggested`, and `rejected` (`[{path, reason}]`, the last suggestions of the chat
that were refused).
- `PATCH /api/app/projects/<id>/repos/<name>` `{"role"?: "edit|reference", "reason"?: "one sentence"}` -> the `GET repos` body. `PUT .../repos/<name>/role` is an alias with the same body (role only). 400 for
  another role or a reason that is not text or is too long; 409 `loop_running`.
- Removed: `POST .../repos/multi` (multi is derived: more than one repo) and `POST .../repos/scope/propose` (the separate AI proposal).
- `POST /api/app/projects/<id>/repos/scope/confirm` `{}` still answers (compatible no-op that stores the list as it is); the panel no longer shows or needs it. 409 `loop_running`.
- `POST loop/start` no longer waits for a confirmation: `repos_not_defined` stays in the code for a bare module without the multi-repo wiring and is not answered in the app.
- Start creates the worktrees that are missing (the primary plus each `edit` repo a ROADMAP feature changes, `ensure_worktrees`; `reference` repos get none) before the first turn, idempotently (an existing
  one is `reuse`d). A failure prints `[check] PROBLEM: worktree not created: <reason>` and stops the start.
- **Where the worktree starts (BR-01).** From the base branch (the ROADMAP `**Branch base:**` passed to the engine as `--base-branch`, else `LOOP_PR_BASE` / `LOOP_INTEGRATION_BRANCH` from the environment or the app `.env`, default `developer`) when that branch exists
  as a LOCAL branch of the repository, else from the folder's current branch, with the warning ``the integration branch `developer` does not exist locally: the worktree starts from
  the current branch `<x>` ``. It never starts from another branch because the user's folder happens to be on it. The new loop worktree's `meta.json` (and `repos.<name>` of a multi-repo
  project) records `base` (the branch it started from), `base_sha` (its short sha then) and `carried_setup_files`. `GET workspace` shows it as `base_branch` / `base_sha`.
- **Setup files are carried (BR-02).** `engine/setup_files.py`: when a NEW worktree is created, the setup files of that repository that the worktree does not have (`docs/codebase/**` of the
  repo and of each non-skipped subproject, `CLAUDE.md` files with an app block, in a confirmed monorepo the root `CLAUDE.md` and root `docs/codebase/*.md`, and, for the primary repository,
  `.specs/project/**` and `.specs/features/**`) are COPIED from the user's folder, committed on its branch or not, and never over a file the branch already has; the engine prints
  `carried uncommitted setup files: ...`. The first loop commit commits them on the `loop/*` branch and they reach `developer` through the PR. The pre-turn "uncommitted work" check ignores
  `carried_setup_files`. Other uncommitted files are never carried: one warning names them (`N other changed files in your folder NOT carried`).
  `GET workspace` repo fields `setup_files` / `other_changes` and `uncommitted_context` (`[{"path", "kind": "map|claude|system", "state"}]`, only while the worktree is not created; the Start dialog notice) show them.

### Per-repo readiness (hub route, keyed by project id)
Extra repos of a multi-repo project need a `Conformance:` line and, when they have code, a codebase map (`services/repo_readiness_service.py`).
In the panel this is the per-repository **Set up <repo>** dialog (the same installer as the main repository, opened from the **Workspace** dialog and
right after a repository is added; a monorepo repository uses the `subprojects` routes with `?repo=<name>`). A **reference** repository needs no Conformance and
its map is optional ("Skip, read the raw code"): it never blocks, a missing map is a WARNING (`SU-02`); every **edit** repository needs both like the primary.
`loop/check` also reports a `PROBLEM` for every extra edit repo whose `CLAUDE.md` has no `Conformance:` line.
- `GET /api/app/projects/<id>/repos-readiness` -> `{ok, multi, repos: [{name, primary, path, folder, exists, has_code, conformance, suggestion, map: {files, folder, commit, running, job}, missing: ["conformance"|"map"], ready, detail}], ready}`. The folder read is the repo's loop worktree when it exists, else the repo folder.
- `PUT /api/app/projects/<id>/repos/<name>/conformance` `{"value": "STANDARD|LEGACY"}` -> `{ok, name, conformance}`; writes the line in that repo's `CLAUDE.md`.
- `POST /api/app/projects/<id>/repos/<name>/map` `{"budget_usd"?}` -> 202 `{ok, name, job, detail}`; starts the codebase map job in that repo (no spend cap: `budget_usd` is ignored).
The last two refuse the primary repo (409 `primary`: it uses the project checklist) and a running loop (409 `loop_running`); a missing folder is
`folder_missing`, and `map` also refuses 409 `worktree_exists`. They feed the planning gate `repos` (see `ARCHITECTURE.md`).

### Subprojects of a monorepo (project routes `/p/<id>/api/subprojects`)
A repo can be a monorepo: several projects in one repo, each with its own `CLAUDE.md`, Conformance and map. The list lives in
`.specs/project/SUBPROJECTS.json` of the primary repo, `{"version": 1, "repos": {"<repo folder name>": entry}}` (`engine/subprojects.py`).
Every route takes an optional `?repo=<name>` (default: the primary repo); mutating routes need the token.
`entry` = `{monorepo, confirmed, validated_by: "script|ai", validated_at, confirmed_at, projects: [{path, name, stack, skill, skip, reason, has_code}], notes: [str], rejected: [{path, reason}], differences: [{path, change}], validation_error?}`.
The AI validation writes `.specs/project/SUBPROJECTS-SUGGESTED.json`; the job thread checks each suggested entry, merges the valid ones into `SUBPROJECTS.json` and deletes the file.
- `GET subprojects` (a read stores the detection only when `.specs/` already exists; it never creates `.specs/`) -> 200 `{ok, repo, monorepo, confirmed, validated_by, validated_at, confirmed_at, reason, projects: [row], rejected, notes, differences, validation_error, root_conformance_leftover, root_map_leftover, queue, error, system, validation_job}`.
  With no stored entry it runs the free detection (no AI) and stores it unconfirmed (`validated_by: "script"`). `row` = `{path, name, stack, skill, skip, reason, has_code, warning, exists, conformance|null, suggestion: {suggestion, stack, evidence}|null, map: {files, folder, commit, running, job}, giant_files, giant: [{path, lines}] (top 5), missing: ["conformance"|"map"|"folder"], ready}`.
  `queue` and `map_queue` (the installer's name; the same payload) = the **map queue** or null, see below; `system` = `{exists, mapped_at, older_than_list, enabled, reason}`; `validation_job` = the running (else the last) job of type `subprojects`, or null.
  `root_conformance_leftover` is `true` only until the list is confirmed: confirming a monorepo list REMOVES the legacy Conformance block the app itself wrote between its markers in the root `CLAUDE.md` (text outside the markers is never touched, the user is never asked).
  **Map queue payload** (`SubprojectMapService.public_queue`, file `.specs/project/loop/internal/subprojects/map-queue.json`):
  `{"state": "idle|running|done|stopped", "items": {"<subproject path>": {"state": "queued|running|done|failed", "started_at", "finished_at", "error", "job_id", "documents": <files in <path>/docs/codebase>}}, "system": {"state": "idle|waiting|running|done|failed", "error"}, "started_at", "include_system": bool, "queue": [queued paths], "current": <first running path|null>, "done": [paths], "failed": {path: reason}, "budget_usd": null}`.
  The last five keys are the old sequential queue's, kept for compatibility (`budget_usd` is always null: no spend cap). `system.state` is `waiting` while the maps run, then `running` and `done`/`failed` once the system map starts by itself.
- `POST subprojects/detect` `{"force"?: false}` -> 200 description (detection run again, unconfirmed). 409 `list_confirmed` when the list is confirmed and `force` is not true.
- `PUT subprojects` `{"monorepo"?, "projects"?: [{path, name?, stack?, skip?}], "confirmed"?}` -> 200 description + `warnings`. `monorepo: false` is **Not a monorepo** (stored as confirmed). Confirming needs one non-skipped subproject whose folder exists. 400 for a bad body, an absolute path, a path that leaves the repo, a duplicated or nested path, a repeated name or an unknown stack.
- `POST subprojects/validate` `{"budget_usd"?}` -> 202 `{ok, job}`: one `claude -p` session (`--model sonnet`, **no `--max-budget-usd`**: `budget_usd` is accepted and ignored, never a 400) that checks the detected list against the real layout. A failure keeps `validation_error` in the entry and ends the job in error.
- `POST subprojects/conformance` `{"path", "value": "STANDARD|LEGACY"}` -> 200 `{ok, path, conformance, written}` (writes `<path>/CLAUDE.md`). `{"all": true}` -> 200 `{ok, written: [{path, value}], kept: [{path, value}]}`: each non-skipped subproject without a Conformance gets its own suggestion; a set one is never overwritten. `POST conformance` on the root of a confirmed monorepo answers 409 `monorepo_root_conformance`.
- `POST subprojects/map` `{"paths"?: ["backend"], "budget_usd"?}` -> 202 `{ok, job, queue}` (`job` = the first job started, `queue` = the payload above). **One `claude` job per subproject, all at the same time** (`--model sonnet`, **no spend cap**: `budget_usd` is ignored). Each job runs `/orchestrator map --subproject <path> --inventory <path>` in `<path>` and may write only `<path>/docs/codebase/` and `<path>/CLAUDE.md`.
  The one-job-per-repository rule is relaxed ONLY here: a job carries a `parallel_key` (its subproject folder), so map jobs on DIFFERENT subproject folders of the same confirmed monorepo run together, while every other job (planning chat, system map, validate, follow-up, workspace create, a root map) still refuses while any job runs and is refused by a running map (409, "a claude job is already running in this project"). At most `max_parallel_maps` run at once (`config.json`, default 6, a positive integer; the rest stay `queued` and start as others end).
  Without `paths` it maps every non-skipped subproject with code and no map, or, after a stopped run, the failed and not-started items (a finished item is kept); with `paths` each one must be listed, not skipped and exist (400 otherwise). A failed item never stops the others: the run ends `stopped`, calling again retries only the failed. After an app restart a `running` file with no job alive is read as `stopped` and its running items become `failed` ("interrupted: ..."), shown with **Retry**. 409 while a previous queue is still `running`; 400 when nothing is left to map.
  **System map by itself:** with `paths` omitted (or a retry of such a run), when EVERY item is `done` the system map starts without a click (`include_system`; `system.state` goes `waiting` -> `running` -> `done|failed`).
- `POST subprojects/system-map` `{"budget_usd"?}` -> 202 `{ok, job}`: writes `docs/codebase/SYSTEM.md` and the root map index, never the root `CLAUDE.md` (`--model sonnet`, no spend cap, `budget_usd` ignored). The file has a section **Who talks to whom** right after the subproject list: one line per relation `<consumer> -> <provider>: <how> (<env or base URL name, never a value>; <main routes, topics or tables>)` (a guess is marked `(inferred)`) and the shared resources (database, queue, cache, storage) with the subprojects that use each; the prompt is in the orchestrator skill `references/monorepo.md`. 400 for a non-monorepo or with fewer than 2 mapped subprojects (`system.reason` says why).
- Errors: 422 `subprojects_not_confirmed` (conformance, map and system-map need a confirmed list); 409 while a loop runs, while another claude job runs in the repo, when the loop worktree exists, and the cases above; 400 invalid request; 404 unknown project.
- Every map job ends by refreshing the `maestro:context` block of the folder it mapped (see `ARCHITECTURE.md`). A `CLAUDE.md` lists the map once: the `maestro:context` block carries it, and the older `<!-- CODEBASE:BEGIN -->` block is written only where there is no context block (and removed where there is one).
- No setup job has a spend cap: the subproject maps, the system map, Validate with AI and the root map (`POST codebase/map`) run without `--max-budget-usd`, and a `budget_usd` in any of those bodies is ignored (the loop's own budgets, roadmap generate/refine and the follow-up are unchanged).
- The installer card (panel) is built from these routes: monorepo = **1 Subprojects** (list, Map/Skip, Confirm), **2 Conformance** (STANDARD/LEGACY per subproject, `POST subprojects/conformance`), **3 Maps** (one button for the maps and the system map, a line per subproject from `map_queue.items`); single repository = **1 Conformance** (`POST conformance`, `GET progress` carries `suggestion` in the conformance step) and **2 Map** (`POST codebase/map`). The panel's Initialize structure dialog (`POST structure/initialize`) only creates `.specs/` and sends no Conformance (the body still accepts an optional `conformance` for old clients); the Conformance is the installer's step.

`POST codebase/map` now answers 202 `{ok, job: "<id>", detail}` at once: the safe snapshot and the free inventory run inside the job thread (the job's `progress`
says which), then `claude -p "/orchestrator map --inventory .specs/project/loop/internal/inventory/_root.json"` (path relative to the repo root).
`GET progress` adds `monorepo: true` and `subprojects: [{path, stack, conformance, has_code, map_files}]` only for a confirmed monorepo; the planning gates then
add `subprojects`, one conformance and one map gate per subproject, and the status lists giant files per subproject and the `SYSTEM.md` state.

**Inventory files.** `python harness.py inventory <folder> [--json] [--giant-lines N] [--giant-kb N] [--changed a,b] [--root R] [--name N] [--out F] [--no-write]`
(`engine/code_inventory.py`) walks a folder for free and writes `.specs/project/loop/internal/inventory/<name>.json` (`_root`, or the subproject path with `/` as `-`;
gitignored, outside the map snapshot, only when `.specs` exists): `{folder, generated_at, totals, by_extension, entry_points, giant: [{path, lines, bytes, hint}], thresholds}`.
A file is giant above `inventory_giant_lines` (default 1500) lines or `inventory_giant_kb` (default 200) KB, both keys of `config.json`; a value that is not a positive number is
ignored with a note. `--changed` measures only the listed paths and writes nothing (verifiers use it).

**Contract ledger.** `python engine/system_map.py merge <root> --slugs a,b [--json]` merges each feature's `.specs/features/<slug>/contract.md`
(`- [add|change|remove] <route|dto|auth|env> <identity> - <purpose> (<file>)`) into the `<!-- CONTRACT:BEGIN -->...<!-- CONTRACT:END -->` section of `docs/codebase/SYSTEM.md`, by kind + identity;
the bytes outside the markers stay. `.specs/project/loop/internal/contract-merged.json` keeps a sha256 per slug, so `pending <root> --slugs a,b` prints only what changed
(`CONTRACT-PENDING: <path>`). The engine runs the merge between turns for verified features and records `contract_merge` in `LOOP.json`. A missing `SYSTEM.md` or marker section is a
warning and the delta stays pending.

### Automatic worktree preparation (hub routes, keyed by project id)
In a multi-repo project each repo's worktree is prepared automatically (`engine/detect_setup.py`); the repo objects of
`GET /api/app/projects/<id>/repos` say what will happen:
- `detected_setup`: the command found from the repo's root files (lockfiles first, then `.sln`/`.csproj`, then `requirements.txt`, `poetry.lock`,
  `go.mod`), or `null`. `setup_configured` is true when a custom `setup` is stored (it always wins); `PUT .../repos/<name>/setup` `{"setup"}`
  with an empty string goes back to automatic.
- `copy_configured`: `REPOS.json` `copy` key absent = automatic (the git-ignored local config files are copied), `[]` = copy nothing, a list =
  exactly that list. `PUT .../repos/<name>/copy` `{"copy": [...]}` stores a list; `{"copy": null}` goes back to automatic. The response carries
  `copy` and `copy_configured`.
- `GET .../repos/<name>/copy-suggestions` -> `{ok, name, copy_configured, suggestions: [{path, size, secret, ticked}]}`; `ticked` is true for
  every suggestion while the copy is automatic, otherwise only for the listed ones.
The panel shows them in **Advanced: files and setup** (**Save setup**, **Save files to copy**, **Use automatic**). A single-repo project
without `REPOS.json` is unchanged: nothing is copied or installed unless `setup` is passed to `loop/start`.

### Reset specs and the loop worktree (hub routes, keyed by project id)
- `GET /api/app/projects/<id>/reset-specs/preview` -> `{ok, worktrees: [{repo, primary, path, branch, unpushed_commits}], skipped: [{repo, path, reason}]}`.
  Read only; the hub calls it BEFORE the confirmation. `worktrees` are the loop worktrees the reset would delete (a repo's loop worktree path,
  registered in `git worktree list`, not the main checkout, branch `loop/*`); `unpushed_commits` is an integer (against the upstream, else
  `origin/<base>`/`<base>`) or `null` when git has nothing to compare with. `skipped` are folders at that path that will NOT be touched
  (not a git worktree, branch is not `loop/*`).
- `POST /api/app/projects/<id>/reset-specs` `{"confirm": "<project name>"}` moves the specs of the PRIMARY checkout to `.specs/.trash/<stamp>/`
  (even when a loop ran and the work dir was the worktree) and then deletes every loop worktree: `git worktree remove --force`, `git worktree
  prune` and `git branch -D loop/...` run from the main repo, for each repo of a multi-repo project. No merge, no commit, no push, no backup:
  to keep the changes, finish the loop (Close loop) or push the branch first. Response
  `{ok, moved, trash, kept, worktrees_removed: [{repo, path, branch, branch_deleted, folder_left}], worktree_failures: [{repo, path, branch, error}]}`;
  a failure (or a skipped folder) is reported and never aborts the reset. "Removed" means git no longer registers the worktree: on Windows
  `git worktree remove` can empty the folder and fail to delete the folder itself while a program holds it, so the branch is deleted anyway
  and the emptied folder is deleted with retries (`folder_left: true` + a failure line when it is still held). An EMPTY folder left at the
  loop worktree path by an earlier removal is deleted too (item with `branch: null`); a folder with content is never touched. Same guards as before: 400 without the exact name, 409
  `loop_running` / `planning_busy`.

## Front-end polling
`GET state?visible=1` every 2 s while the tab is visible; hidden, it stops. A network error shows the "panel
disconnected" banner and retries every 5 s without losing what is on screen.

## Screen states the front end covers
`running` (animated); `awaiting_user` (large card with `reason`, `options` as buttons, a free-text field, `to_check`);
`error` (cause + "Try again" = `resume`); `stopped` (notice + "Resume"); `done` (PR, `validate`, summary); `no_loop`
(navbar **Start loop** opens the Start dialog: the settings decided in planning (`loop/settings`, read-only), the two checkboxes,
"Check only" = `loop/check` showing PROBLEM/WARNING/READY and the command, "Start loop" = `loop/start`); `paused` (cost cap: the
status dialog offers `loop/raise-limit`); `done` and `stopped` with a worktree (the Wrap-up view and the navbar **Finish**:
`wrap-up`, `wrap-up/finish`, `follow-up*`); pause requested (the button becomes "Pausing after the turn..."). Confirmations are inline (no
`alert`/`confirm`).
