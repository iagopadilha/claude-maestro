# How the plugin skill finds and uses the app

The `/orchestrator` skill of the plugin is the **brain** (the prompt `claude -p` executes: `SKILL.md` + `references/`),
and it owns the hook `hooks/guard.py` and the agents in `plugin/agents/`. The Loop Harness app is the **runtime**: the
loop engine (`run_loop.py` and its satellite scripts), the local HTTP server and the multi-project panel. Both run on your
machine with your own Claude subscription through the `claude` CLI (no API token).

This document says how the skill locates the app, which commands it runs, what stays in the plugin and the headless
roadmap and planning modes the app calls.

## 1. How the skill finds the app

Order (the first one that exists wins):

1. Environment variable `LOOP_APP_HOME` = the app root folder (where `harness.py` is).
2. `~/.loop-harness/config.json`, field `app_root_dir` (written by `python <app>/harness.py install`; `LOOP_DATA_DIR`
   replaces the `~/.loop-harness` folder).

One line for the skill to resolve `<app>` (PowerShell or bash, Python only):

```
python -c "import json,os;d=os.environ.get('LOOP_DATA_DIR') or os.path.join(os.path.expanduser('~'),'.loop-harness');print(os.environ.get('LOOP_APP_HOME') or json.load(open(os.path.join(d,'config.json'),encoding='utf-8'))['app_root_dir'])"
```

Neither exists: the skill tells the user to clone this repository and run `python <repo>/app/harness.py install` once,
and stops. It does not look for the app anywhere else.

The live app: `~/.loop-harness/app.json` (`pid`, `port`, `url`, `token`, `started_at`, `app_root_dir`), written by the
app when it starts and deleted when it stops. The skill **never** reads the token and never calls a trigger or approval
endpoint (`/p/<id>/api/loop/*`, `/p/<id>/api/roadmap/approve`, `/p/<id>/api/action/*`, `answer`, `resume`,
`instruction`): the click is always the user's, and the guard blocks both. To open a project's panel the skill only runs
`harness.py serve --project`.

## 2. `harness.py install`

```
python <app>/harness.py install [--data-dir D] [--skill-dir S]
```

Writes (or updates) `<data dir>/config.json` with:
- `app_root_dir`: the app folder, so the skill and the guard can find `harness.py`;
- `orchestrator_skill_dir`: the skill folder (`--skill-dir`, else detected: `LOOP_SKILL_DIR` >
  `config.json orchestrator_skill_dir` > `$CLAUDE_PLUGIN_ROOT/skills/orchestrator` > `plugin/skills/orchestrator` next to
  `app/` in this repository > the installed plugin cache under `~/.claude/plugins/cache/` > `~/.claude/skills/orchestrator`).

It exits 1 when the skill folder is not found (run it again with `--skill-dir`). It never writes under `~/.claude`.
`python <app>/harness.py doctor` then shows what the app sees: skill folder, guard found and blocking `git push`,
`claude` on PATH, notification channels and a live app.

## 3. Commands the skill runs

Every loop command is `python "<app>/harness.py" <subcommand>`, on one line, with the path in quotes.

| Subcommand | Engine script | What for |
|---|---|---|
| `serve --project <root>` | the app | opens the project's panel (reuses a live app and only registers the project; prints `MONITOR_URL=<project url>`) |
| `loop <root> [flags]` | `run_loop.py` | the unattended loop (`--wave`, `--answer`, `--option`, `--rewind`, `--dry-run`...) |
| `check <root> [flags]` | `run_loop.py --check` | the preflight (the same as the panel's **Check** button) |
| `state <args>` | `loop_state.py` | `validate`, `migrate`, `disjoint`, `graph`, `loop`, `next` |
| `notify <args>` | `notify.py` | notifications (`--test` checks every channel) |
| `summary <args>` | `loop_summary.py` | final loop summary |
| `deliver <args>` | `delivery.py` | push of the feature branch + PR to the integration branch |
| `rules <args>` | `rules.py` | `propose`, `write`, `test`, `list` of block rules |
| `audit <args>` | `audit_deps.py` | forgotten dependencies between features (`--write`, `--apply`) |
| `graph <args>` | `graph_view.py` | progress graph (`--ascii`, `--short`, `--mermaid`, `--write`) |
| `classify <root> [--write STANDARD\|LEGACY]` | `classify_structure.py` | the `Conformance:` section of `CLAUDE.md` (or the panel's Conformance button) |
| `scale <root>` | `scale.py` | how many mapping agents the repo needs |
| `doctor [--json]` | the app | diagnostics |
| `install` | the app | section 2 |

A script name in the skill text (`run_loop.py`, `loop_state.py`, `delivery.py`, `notify.py`...) is an engine script in
`<app>/backend/harness_backend/engine/`, always called through `harness.py`. The engine scripts keep their own CLI, so
`python <app>/backend/harness_backend/engine/<script>.py <args>` works too.

`.specs` templates live in `<app>/templates/` (`PROJECT_RULES.md`, `TEST_ENVIRONMENT.md`, `specs/PROJECT.md`,
`ROADMAP.md`, `FLOWS.md`, `STATE.md`); the panel's **Initialize structure** button creates `.specs/` from them without overwriting
anything, links `@.specs/project/RULES.md` from `CLAUDE.md` and puts `.specs/` in `.gitignore`. `PROJECT.md` is a short context file for the loop's subagents (a Vision line plus pointers to `docs/codebase/`, `RULES.md`, `STATE.md`, `ROADMAP.md`, `FLOWS.md`, `TEST-ENVIRONMENT.md`); the project's limits live in `RULES.md`. `TEST-ENVIRONMENT.md` and `.env.test` are created together with the other files.

Test suites: `python -m unittest discover -s "<app>/backend/tests" -t "<app>/backend"` (engine and app) and
`python -m unittest discover -s "<plugin>/skills/orchestrator/hooks" -p test_guard.py` (guard).

## 4. What stays in the plugin

- `SKILL.md` and `references/*.md`: the prompt of every turn and of the interactive use.
- `hooks/guard.py`: the app finds it through `orchestrator_skill_dir` in `config.json` (or `LOOP_SKILL_DIR`, or
  `$CLAUDE_PLUGIN_ROOT/skills/orchestrator`); the preflight checks that it blocks `git push`.
- The agents in `plugin/agents/*.md` (the guard's profiles).
- The satellite skills (block rules, dependency audit, progress graph) call the same engine scripts through
  `harness.py rules`, `harness.py audit` and `harness.py graph`.

The engine, the panel and the `.specs` templates live only in the app.

## 5. The headless roadmap mode: `/orchestrator roadmap --headless --goal-file <file> [--map-first]`

Who calls it: the app (`POST /p/<id>/api/roadmap/generate`, the panel's **Generate roadmap** button), in a `claude -p`
inside the **repo** folder (never the loop worktree), with `--permission-mode auto`, the normal MCP configuration,
`--max-budget-usd` (default 5, at most 50) and a 30-minute timeout. One claude job per project at a time.

- The goal the user typed is written to `.specs/project/loop/internal/roadmap-goal.md`:
  ```
  # Roadmap goal

  <goal text>

  ---
  Map first: yes|no
  Written by the harness app on <ISO date>
  ```
- `--map-first` is added when the user ticked the option. The skill runs Phase 1 (mapping) first when it is there, or
  when the repo has code and `docs/codebase/` (or the legacy `.specs/codebase/`) is empty.
- The skill writes `.specs/project/PROJECT.md`, `.specs/project/ROADMAP.md` (stages `## Stage N — Name`, one line per
  feature, `status=pending`) and `.specs/project/FLOWS.md` (one `## [slug]` flow per feature), puts every doubt in the `## Open questions` section of the ROADMAP instead of asking,
  **never approves** the roadmap (never writes `ROADMAP-APPROVED.json`), never runs the loop, does not start the panel
  and does not commit.
- **Refine** (`POST /p/<id>/api/roadmap/refine`, the **Refine roadmap** button) resumes the SAME claude session
  (`--resume <session_id>` of the last roadmap job) with the user's request through stdin, after this fixed prefix:
  "Continue in this session's `/orchestrator roadmap --headless` mode: adjust .specs/project/ROADMAP.md and
  .specs/project/FLOWS.md (one flow per feature), and PROJECT.md if needed, following the request below, in the grammar of references/state.md. Do not ask (doubts go to the
  `## Open questions` section of the ROADMAP), never approve the roadmap, and finish." followed by `User request:`.
- **Map** (`POST /p/<id>/api/codebase/map`, the **Map** button) runs `claude -p "/orchestrator map"` without MCP.
- Jobs are recorded in `.specs/project/loop/internal/jobs/<id>.json`; the claude `session_id` stays in that file and is
  never returned by the API.
- While the project is in planning mode (below) generate and refine refuse with `planning_required`: the planning chat
  replaces them.

## 5b. The planning mode: `/orchestrator plan --headless`

Who calls it: the app's **planning screen** (`POST /p/<id>/api/planning/send`), shown instead of the panel while the
project has no approved roadmap and never ran a loop. One `claude -p` session per conversation: the first message is
`/orchestrator plan --headless` followed by the user's text, every next message `--resume`s the same session (thread in
`.specs/project/loop/internal/planning/thread.json`; **New conversation** clears it, the documents stay). Account default
model, US$ 2 per message, 10-minute timeout, no MCP, no Bash; the only writes allowed are the three documents.

- Each turn the skill (`references/planning.md`) re-reads `PROJECT.md`, `ROADMAP.md` and `FLOWS.md` (and
  `docs/codebase/`, or the legacy `.specs/codebase/`, when present), updates them incrementally and replies with what changed and the 1 to 3 most
  important questions. Unlike `roadmap --headless`, it **asks**: the reply is the conversation. Unanswered doubts stay in
  the ROADMAP's `## Open questions`; an answer moves into the documents and the question leaves the list.
- It never approves, never starts or checks a loop, runs no subagent and writes nothing else.
- The app owns the gates (`GET /p/<id>/api/planning`): prerequisites (git, `.specs/`, `Conformance:`, the codebase map
  when the repo has code), project context (PROJECT.md), features, fields, open questions and a flow (≥ 2 steps) per feature. **Approve
  roadmap** is refused (`planning_gates`) until all of them pass. Format of `FLOWS.md`: the skill's `references/state.md`.

## 6. What the app guarantees

- `ROADMAP-APPROVED.json` is only written by `POST /p/<id>/api/roadmap/approve` (token + `confirm: true`), through the
  engine function `loop_state.approve_roadmap`: the same signature `harness.py check` verifies. No job approves.
- The checklist (`GET /p/<id>/api/progress`) uses the engine checks: `loop_state.roadmap_approval`,
  `loop_state.parse_roadmap`/`check_fields` and `run_loop.test_environment_problems`.
- The `MONITOR.json` written by the app (`pid`, `port`, absolute project `url`, `repo`, `work`, `started_at`) is what
  `engine/monitor_proc.py` and the skill use to find the panel.
