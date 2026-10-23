# The loop panel

The panel is the per-project page of the Loop Harness app (`/p/<project_id>/`): it follows `run_loop.py` and lets you
answer, instruct, chat, act and deliver from the browser. Python standard library on the server; plain HTML, CSS and JS on
the front end (no npm, no build). The fixed API contract is in `API.md`; technical identifiers keep the word `monitor`
(`MONITOR.json`, `monitor_url`, `--no-monitor`, `LOOP_MONITOR`).

## When it comes up on its own
On every real run (not `--check`, `--dry-run` or `--rewind`) `run_loop.py` asks `engine/monitor_proc.py` for the panel
and prints `[loop] monitor: <url>`. Order:
1. `LOOP_MONITOR_URL` (set by the panel that launched this loop) answering `api/health` -> that panel.
2. The work's `.specs/project/loop/json/MONITOR.json` with a live pid and a `health` that reports the same `work` -> reused.
3. A live app (`~/.loop-harness/app.json`, or `LOOP_DATA_DIR`) -> the project is registered in it
   (`POST /api/app/projects/open`) and its URL is used.
4. No live app -> `python <app>/harness.py serve --project <repo>` starts detached and its `MONITOR_URL=` line is read.
   The server output goes to `.specs/project/loop/internal/monitor.log` of the worktree.

Everything is best effort: the loop never fails because of the panel. `--no-monitor`, or `LOOP_MONITOR=off` in the
environment or the app `.env`, turns it off. `LOOP_MONITOR_SERVER=<path>` swaps the server script (tests, custom
installs).

## Starting it by hand
```
python <app>/harness.py serve                         # hub of projects, opens the browser
python <app>/harness.py serve --project <repo>        # opens (or registers) one project and prints MONITOR_URL=<url>
python <app>/harness.py serve --port 47850 --idle-hours 2 --no-browser
```
- One app process serves every project. With an app already alive, `serve` only registers the project, prints its URL
  and exits 0 (no second server, no second browser tab).
- The app writes a compatible `MONITOR.json` (`pid`, `port`, project `url`, `repo`, `work`, `started_at`) in each opened
  project that has `.specs/`, and deletes the ones it wrote when it shuts down.

## Ports
The first free port from **47800 to 47899** (bind test on `127.0.0.1`), or the one given by `--port`. It never kills
the process that holds a port.

## Lifecycle
The app is the **control panel of your projects**: independent of the loop, it does not read stdin, does not die when
`run_loop.py` ends and **stays up without a loop** (status `no_loop` shows the **New loop** panel). From the front end you
check (`loop/check`), start (`loop/start`), answer, resume and pause.
It shuts down only on **Shut down app** (`POST /api/app/shutdown`), on idleness (no loop running in any project, no
claude job running and no activity for `idle_hours`, default 2 h) or when killed from outside. It never shuts down on its
own with a loop `running`. Without a live app (including after the automatic shutdown),
`python <app>/harness.py serve --project <repo>` brings the panel back.

## The default screen
The panel is **the loop's default screen**: it opens in the browser when the loop starts (only when the app starts;
reusing a live app does not open another tab), its URL goes in every notification, and it is where you follow, answer,
instruct, chat, act and deliver. `LOOP_MONITOR_OPEN=off` (environment or the app `.env`) only stops the browser from
opening; `--no-monitor` (or `LOOP_MONITOR=off`) turns the whole panel off.

## Act (objective actions)
**Act mode (current version).** Runs objective actions from a **whitelist**, done by code (not by the AI), always with
**explicit confirmation** from the user: `deliver` (push **only the feature branch** + PR to the integration branch, via
`engine/delivery.py`), `summary` (`engine/loop_summary.py`, written to the `output_dir` of `config.json`, default
`~/Downloads`), `push` (feature branches without a PR) and `clean_worktrees` (with a verified backup first). Free text
goes through a router (`claude -p` haiku, no tools) that can only pick an action from the list; outside it becomes
`none` and the front end offers to send it as an instruction. Act **does not reopen a loop**. Push and PR only exist
through `delivery.py`: the guard and the hard rules still apply, and it refuses protected branches, force and open
features; that is why `deliver` from the panel only works with the loop stopped and no open feature. The click on
confirm is the user authorizing; the orchestrator (Claude) never calls `action/*`.

## Chat and instruct (step by step)
The chat is **read-only** (`claude -p` sonnet, `Read`/`Grep`/`Glob`, no MCP, up to US$ 1 per answer) and independent of
the loop. Old version = read-only; current version = instruct the loop (default) or ask. An instruction becomes a
`[pending]` block in `ANSWERS.md` (`Question: (user instruction from the panel)`, optional `[feature]`, `Attached context:`).
1. Open the panel and click an agent (or the turn you want to understand); open the **Chat** tab.
2. Pick the mode: **Ask** only answers questions (the AI reads the files and answers, changing nothing; up to US$ 1 per
   answer); **Instruct the loop** (default on the current version) sends a request the loop applies. On an old turn only
   Ask exists; use "Bring to the current version" to turn the conversation into an instruction.
3. After sending an instruction, the response says what happened: **note** = the loop is running and applies it on the
   next turn; **answer** = the loop was waiting for you and was relaunched with the text; **note+resume** = the loop was
   stopped, in error or done and was relaunched to apply it. Without a loop, start one from the New loop panel.
4. A request that adds a feature or changes the approved roadmap is not applied on its own: the loop stops and asks,
   with the recommended option marked. Answer in the panel.

## Who triggers the loop
The click on **Start loop** (like Answer and Resume) is **the user triggering the loop**, which keeps the `SKILL.md`
rule that Claude never triggers `run_loop.py`; the orchestrator never calls `loop/*`. `loop/start` runs `run_loop.py`
with `LOOP_MONITOR_URL=<panel url>`: the loop checks `GET <url>api/health` (only `127.0.0.1`/`localhost`, no proxy)
and, when it answers, reuses that panel instead of starting another one (the URL goes to `meta.monitor_url` and to the
notifications); when it does not answer, it falls back to the normal order above.

## Guard and access
The guard (`plugin/skills/orchestrator/hooks/guard.py`) is a `PreToolUse` hook. It runs on the orchestrator, on every plugin subagent and, through the inline `--settings` of the isolation, on every `claude -p` job of the app. The built-in rules and their profiles are in `plugin/skills/orchestrator/hooks/README.md`; this section is what the app adds around them. The panel's guard feed (`guard_events` and `guard_counts` of the state, the Errors tab, the pipeline marks) counts only `blocked` decisions and never returns the raw `guard.log`.

**Two allowlist rules** (both HARD: no `PERMISSIONS.json` entry releases them):

| Rule | Active when | Denies |
|---|---|---|
| `mcp-allowlist` | `LOOP_HEADLESS=1` (loop turns) or `LOOP_ISOLATED=1` (isolated app jobs) | any `mcp__*` tool that is not on the caller profile's list. Grafana read tools pass for every profile (the writers are `grafana-write`). `mcp__mcp-mysql-dev__mysql_query` is allowed to `executor`, `verifier`, `spec`, `orchestrator`, `planning`, `roadmapmap`, `subprojects` and `systemmap`; `mcp__mcp-mysql-prod__mysql_query` only to `orchestrator` and `planning`. The statements still go through `sql-write` / `prod-sql-write`, so production stays read only. |
| `skill-allowlist` | `LOOP_SKILLS_FILE` points to a file (the engine writes `<data dir>/runtime/skills-allowlist.json` and exports it with `LOOP_ISOLATED=1`) | a `Skill` call whose `skill` (a `plugin:` prefix removed, lower case) is not in the list of the caller's profile. Fail closed: the variable set with an unreadable or invalid file, or a profile missing from it, denies every skill. |

Without those variables (your interactive sessions, any use of the plugin outside the app) neither rule is evaluated and the guard behaves as before. The skills of each profile are the defaults of `engine/runtime_isolation.py` plus `loop_extra_skills` and the skills of `SUBPROJECTS.json` (see `HOW_TO_USE.md`, "Isolation and access"). Subagents have an explicit `tools:` list: `executor-back`, `executor-generic`, `verifier` and `spec-writer` list the DEV SQL tool, nobody lists the production one.

**Audit lines.** While the two rules are active, every **allowed** `mcp__*` call and every allowed `Skill` call adds one JSON line to `.specs/project/loop/internal/guard.log` (no line for the other allowed tools, so the log stays small):
```json
{"t": "2026-10-22T20:21:07", "turn": 4, "profile": "executor", "rule": "skill-allowlist", "decision": "allow",
 "detail": "Skill backend-dev", "tool": "Skill", "agent": "executor-back", "subject": "backend-dev"}
```
For an MCP SQL call `rule` is `mcp-allowlist` and `subject` is the statement's first verb in capitals (`SELECT`); for other MCP tools `subject` is empty. A denial is the usual `decision: "blocked"` line with the rule name. `harness.py doctor --access` summarizes the last 200 lines by decision, rule and agent and lists the last denials.

**What the app does around the hook.** Isolation (`--setting-sources project,local`, the bundle of agents and allowlisted skills, the guard passed by `--settings`) is described in `ARCHITECTURE.md`; `harness.py doctor --access` shows what a real isolated `claude -p` loads. Turn it off with `"loop_isolation": false` in `~/.loop-harness/config.json`.

## Security
- Listens **only on 127.0.0.1**; rejects a `Host` other than `127.0.0.1:<port>` or `localhost:<port>` (DNS rebinding).
  No CORS.
- A random 32-byte token created on every start, kept only in memory and injected into `index.html`; every `POST`/`PUT`
  requires `X-Monitor-Token` (missing or wrong: 403). JSON body up to 64 KB unless a route says otherwise.
- Never returns `.env` content, credentials or the raw `guard.log`.
- Relaunching the loop (answer, resume) runs the `argv` of `LOOP-CMD.json`, which `run_loop.py` writes on every run
  without `--answer`/`--option`. Pausing creates the `PAUSE` file, which the loop checks at the top of each turn.
