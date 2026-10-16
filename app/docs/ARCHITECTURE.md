# Loop Harness: architecture

## What it is
Loop Harness is a **local control panel for loop engineering and graph engineering**. It runs on your machine and drives the
`/orchestrator` skill of the plugin through the `claude` CLI, so it uses **your own Claude subscription** (no API token,
no hosted service). A Python back end (standard library only) and a front end in plain HTML/CSS/JS (no build step)
**run and show** the engineering loop:

- the **plugin skill** (`plugin/skills/orchestrator/`) is the **brain**: the prompt `claude -p` executes on every turn
  (`SKILL.md` + `references/`), the hook `hooks/guard.py` and the agents in `plugin/agents/`;
- the **app** (this folder) is the **runtime**: the loop engine (`run_loop.py` and its satellite scripts), a local HTTP
  server and a multi-project web panel.

Why a panel (base text of the front's "Why use the panel?" dialog):
- The loop state is dozens of files (`ROADMAP.md`, `STATE.md`, `VALIDATE.md`, `ANSWERS.md`, `LOOP.json`, specs per
  feature, turns, snapshots, agent transcripts). Opening `.md` files one by one does not show the whole.
- The panel puts it in one view: the roadmap as a dependency graph (graph engineering), turns and agents (loop
  engineering), specs, questions, validations, cost and limits.
- The engine commands (`run_loop.py --check`, `--answer`, `--option`, `--rewind`, `loop_state.py`, `delivery.py`) become
  buttons with confirmation: fewer memorized commands, fewer paste errors.
- It works for a project from scratch (creates the folder and `.specs/`), an existing project without `.specs/` (maps it
  and generates the roadmap) and a project with a loop in the middle (opens it and shows where it stopped).

## Construction rules
- Python 3.10+, **standard library only** (no pip). Front end without npm or a build (native ES modules served by the
  back end).
- Layers `api/controllers -> services -> repositories -> infrastructure`, domain in `domain/`. Singular names with a
  layer suffix (`ProjectService`, `ProjectRepository`, `ProjectController`). Abstractions are `typing.Protocol` prefixed
  with `I` (`IProjectRepository`). Injection **through the constructor**; only `composition.py` and each module's
  `build_*_module` instantiate concrete classes. Methods are verb + object (`open_existing_project`,
  `append_pending_answer`), never `process`/`handle`/`helper`/`manage`; booleans read as questions (`is_loop_running`,
  `has_snapshot`). Read, decide and write stay visibly separate. No magic numbers (named constants). Structured logging
  through `StructuredLog` (never `print` in the back end, except the CLI).
- Code, identifiers, messages and docs in English.
- The **engine** (`backend/harness_backend/engine/`) is the loop proven by its test suite: change only what is needed.
  Services call the engine as a subprocess (CLI scripts) or import pure read functions (`loop_state.parse_roadmap`,
  `loop_state.loop_path`, `loop_state.roadmap_approval`, `graph_view`...). Never rewrite the ROADMAP parser.
- Security: listen only on 127.0.0.1, check `Host`, require the token on every method that changes something, JSON body
  up to 64 KB (exceptions per route), never return `.env`, credentials or the raw `guard.log`, read-only chat without
  MCP, actions only from a whitelist with `confirm:true`. Claude (the orchestrator) never calls a trigger endpoint; the
  click is always the user's.
- Tests: `unittest`, no network, no real `claude` (use `backend/tests/engine/fake_claude.py` or a fake script), never
  touching `~/.claude` or the real `~/.loop-harness` (use a temporary `LOOP_DATA_DIR` and `LOOP_SKILL_DIR` pointing to a
  fake skill with `SKILL.md` + `hooks/guard.py`). Run from `app/`: `python -m unittest discover -s backend/tests -t backend`.

## Layout
```
harness.py                          single CLI: serve | demo | loop | check | state | notify | summary | deliver | rules |
                                    audit | graph | classify | scale | doctor | install
backend/harness_backend/
  composition.py                    composition root (wires the three modules and the app controller)
  config/app_config.py              AppConfig + load_app_config (data folder, ports, engine, templates)
  config/skill_locator.py           finds the orchestrator skill and its guard
  config/envload.py                 .env lookup
  domain/project.py                 Project, ProjectContext, ProjectNotFoundError
  domain/project_errors.py          domain errors of the projects module
  api/router.py, http_server.py     router and HTTP server
  api/controllers/                  one file per controller
  services/ repositories/           per module (table below)
  infrastructure/                   structured_log, activity_monitor, process_runner, claude_client, git_client
  engine/                           run_loop, loop_state, notify, delivery, loop_summary, graph_view, audit_deps,
                                    rules, scale, monitor_proc, classify_structure, app_paths
  demo/                             `harness.py demo`: story catalog, workspace, seeder, story player, transcript writer,
                                    animator, runner and the `claude`/engine stubs (never runs a real loop)
backend/tests/                      engine/, infra/, demo/, api/test_read_*.py, api/test_commands_*.py, api/test_projects_*.py
frontend/                           index.html, src/**/*.js, styles/*.css
templates/                          .specs templates (PROJECT_RULES.md, TEST_ENVIRONMENT.md, specs/*.md)
docs/                               ARCHITECTURE.md, API.md, HOW_TO_USE.md, MONITOR.md, SKILL_BRIDGE.md
```

## Modules and their files
| Module | Files |
|---|---|
| Engine, infrastructure and CLI | `harness.py`, `engine/**`, `config/envload.py`, `infrastructure/process_runner.py`, `infrastructure/claude_client.py`, `infrastructure/git_client.py`, `services/app_diagnostics_service.py`, `api/controllers/app_controller.py`, `backend/tests/engine/**`, `backend/tests/infra/**`, `.env.example`, `docs/HOW_TO_USE.md` |
| Loop read | `services/loop_read_module.py`, `services/loop_status_service.py`, `services/loop_panel_service.py`, `services/agent_history_service.py`, `services/guard_event_service.py`, `services/turn_service.py`, `services/roadmap_view_service.py`, `repositories/loop_file_repository.py`, `repositories/claude_transcript_repository.py`, `repositories/loop_read_formatting.py`, `api/controllers/loop_read_controller.py`, `backend/tests/api/test_read_*.py` |
| Loop commands | `services/loop_commands_module.py`, `services/loop_control_service.py`, `services/chat_service.py`, `services/action_service.py`, `services/whiteboard_service.py`, `services/engine_script_service.py`, `services/loop_worktree_service.py`, `repositories/chat_repository.py`, `repositories/whiteboard_repository.py`, `repositories/answers_repository.py`, `repositories/loop_command_file_repository.py`, `repositories/agent_transcript_repository.py`, `api/controllers/loop_commands_controller.py`, `api/controllers/chat_controller.py`, `api/controllers/action_controller.py`, `backend/tests/api/test_commands_*.py` |
| Projects, structure, roadmap and specs | `services/projects_module.py`, `services/project_service.py`, `services/specs_structure_service.py`, `services/project_progress_service.py`, `services/roadmap_service.py`, `services/spec_file_service.py`, `services/claude_job_service.py`, `services/folder_picker_service.py`, `services/planning_service.py`, `services/planning_gates.py`, `services/planning_requirement_service.py`, `repositories/project_repository.py`, `repositories/specs_file_repository.py`, `repositories/planning_thread_repository.py`, `infrastructure/workspace_file_guard.py`, `domain/flows_document.py`, `domain/project_errors.py`, `api/controllers/planning_controller.py`, `api/controllers/project_responses.py`, `api/controllers/project_controller.py`, `api/controllers/roadmap_controller.py`, `api/controllers/specs_controller.py`, `templates/**`, `backend/tests/api/test_projects_*.py` |
| Core | `composition.py`, `config/app_config.py`, `config/skill_locator.py`, `domain/project.py`, `api/router.py`, `api/http_server.py`, `infrastructure/structured_log.py`, `infrastructure/activity_monitor.py` |
| Front end | `frontend/**` |
| Demo | `demo/**` (`harness.py demo`), `backend/tests/demo/**` |

## Contracts between modules (fixed signatures)
`composition.py` calls:
- `build_projects_module(config, log, get_app_url) -> obj` with `project_service`, `planning_requirement_service`,
  `controllers`, `busy_providers`.
- `build_loop_read_module(config, log, project_service, activity_monitor, planning_requirement_service) -> obj` with
  `loop_status_service`, `controllers`, `busy_providers`.
- `build_loop_commands_module(config, log, project_service, loop_status_service, activity_monitor, get_app_url,
  planning_requirement_service) -> obj` with `controllers`, `busy_providers`.

`IPlanningRequirementService` (the projects module implements it, the other modules consume it):
`is_planning_required(context) -> bool`, `ensure_planning_done(context)` (raises `PlanningRequiredError`).
- `AppController(config, log, server, activity_monitor, project_service)` with `register_routes(router)`.

Every controller exposes `register_routes(router)`; every busy provider exposes `is_busy()` (a running loop or claude
job keeps the app from shutting down for idleness).

`IProjectService` (the projects module implements it, every module consumes it):
- `list_projects() -> list[Project]`
- `get_project(project_id) -> Project` (raises `ProjectNotFoundError`)
- `get_project_context(project_id) -> ProjectContext` (raises `ProjectNotFoundError`; controllers turn it into 404
  `{"ok": false, "error": "project not found"}`)
- `open_existing_project(path) -> Project` (registers or updates `last_opened_at`; writes a compatible `MONITOR.json` in
  the `work` dir; idempotent)
- `create_project_from_scratch(parent_path, name, init_git=True, conformance=None) -> Project`
- `forget_project(project_id) -> None` (only removes it from the registry; never deletes a file)
- `release_projects_on_shutdown() -> None` (deletes the `MONITOR.json` files this process wrote)

`ILoopStatusService` (the loop read module implements it, the loop commands module consumes it):
- `get_loop_summary(context) -> dict` (`status`, `running`, `pid`, `json`, `meta`, `reason`).
- `is_any_loop_running() -> bool` (in any registered project); also a busy provider.

`ProjectContext.work_dir` = the loop worktree (`../<project>-worktree-loop`) when it exists, otherwise the repo. It is
re-evaluated on every call: a worktree created later is picked up without reopening the project.

## Data the app keeps
- `~/.loop-harness/` (or `LOOP_DATA_DIR`): `config.json` (`app_root_dir`, `orchestrator_skill_dir`,
  `claude_transcripts_dir`, `output_dir`, `claude_binary`, `idle_hours`), `projects.json` (`{version, projects[]}`, one
  item per project: `id, name, repo_path, opened_at, last_opened_at, favorite`), `app.json` of the live app (`pid, port,
  url, token, started_at, app_root_dir`), an optional `.env` and `logs/app.jsonl`.
- In each project, under `.specs/` (layout of `engine/loop_state.py`): `project/` (`PROJECT.md`, `ROADMAP.md`,
  `STATE.md`, `FLOWS.md`, `RULES.md`, `TEST-ENVIRONMENT.md`, `phases/stage-NN.md`), `project/loop/` (`ANSWERS.md`, `VALIDATE.md`,
  `DEPENDENCIES.md`, `GRAPH.md`, `NOTICE.txt`), `project/loop/json/` (`LOOP.json`, `LOOP-CMD.json`, `MONITOR.json`,
  `meta.json`, `ROADMAP-APPROVED.json`, ...) and `project/loop/internal/` (`LOOP.lock`, `PAUSE`, `turns/`, `snapshots/`,
  `chat/`, `jobs/`, `planning/thread.json`, `whiteboard.json`, `roadmap-goal.md`, logs). Project ids are a short hash of the normalized folder
  path: the same folder always gets the same id.

## HTTP API
Pages: `GET /` (hub) and `GET /p/<project_id>/` (project panel) serve the same `frontend/index.html` with
`<meta name="monitor-token">` injected. Static files at `/static/<file>` -> `frontend/<file>`.

### App (hub), prefix `/api/app`
| Method and route | What it does |
|---|---|
| `GET /api/app/health` | `{"ok": true, "pid": N, "version": "...", "url": "..."}` |
| `GET /api/app/info` | diagnostics: skill folder, guard found and blocking `git push`, `claude` on PATH, configured notification channels (names, never values), data folder, `seconds_until_shutdown`, `open_projects` |
| `POST /api/app/notify/test` | `notify.py --test` (optional `channels`) |
| `POST /api/app/shutdown` | shuts the app down |
| `GET /api/app/projects` | `{"projects": [{id, name, repo_path, opened_at, last_opened_at, favorite, url, folder_exists, progress_summary: {mode, next_action, loop_status, counts}}]}` |
| `POST /api/app/projects/open` `{"path"}` | opens an existing folder -> `{"ok": true, "project": {...}, "url": "/p/<id>/"}`; 400 when it is not a folder |
| `POST /api/app/projects/create` `{"parent_path", "name", "init_git": true, "conformance": "STANDARD\|LEGACY\|null"}` | creates the folder (refused when it exists with content), optional `git init` + initial commit, `.specs/` from the templates |
| `POST /api/app/projects/pick-folder` `{}` | native folder dialog (tkinter in a subprocess, 300 s timeout) -> `{"path": "...\|null", "reason": "...\|null"}` |
| `GET /api/app/system/folders?path=` | in-browser folder browser (fallback without tkinter): `{"path", "parent", "folders": [{name, path, has_specs, has_git}], "truncated"}` |
| `POST /api/app/projects/<id>/forget` | removes it from the registry |

### Project, prefix `/p/<project_id>/api`
The loop read routes (`health`, `state`, `history`, `agent/<id>/log`, `turn/<n>`, `graph`) and the loop command routes
(`whiteboard`, `answer`, `resume`, `note`, `pause`, `loop/check`, `loop/start`, `instruction`, `chat/*`, `actions`,
`action/*`) are described in `API.md`. `POST /p/<id>/api/shutdown` is a compatible no-op `{"ok": true}`: shutting down
belongs to the app. `GET /p/<id>/api/health` returns `{"ok": true, "repo", "work", "pid"}` (the engine's `monitor_proc`
checks `work`).

Projects module routes:
| Route | What it does |
|---|---|
| `GET progress` | project checklist (below) |
| `POST structure/initialize` `{"conformance": "STANDARD\|LEGACY\|null"}` | creates `.specs/{project,codebase,features}`, `PROJECT.md`, `ROADMAP.md` (empty template), `STATE.md`, `FLOWS.md` (empty template), `RULES.md`, `loop/{json,internal}`, puts `.specs/` in `.gitignore`, imports `@.specs/project/RULES.md` in `CLAUDE.md`; never overwrites an existing file; writes `Conformance:` in `CLAUDE.md` when given; creates `TEST-ENVIRONMENT.md` when the ROADMAP asks for regression. Returns `{created, changed, existing, conformance, ok, progress}` |
| `POST conformance` `{"value": "STANDARD\|LEGACY"}` | writes the line in `CLAUDE.md` (through `engine/classify_structure.py`) -> `{ok, conformance}` |
| `GET specs/tree` | `.specs/` tree grouped: `{exists, specs_dir, loop_running, project[], codebase[], features[{slug, status, stage, in_roadmap, files[]}], loop[], other[]}`; file item `{path, name, editable, not_editable_reason, size, modified_at}`; never `loop/json/` or `loop/internal/` |
| `GET specs/file?path=` | text of a `.md` inside `.specs/` -> `{path, text, version, editable, not_editable_reason}` (`version` = sha1) |
| `PUT specs/file` `{"path", "content", "version"}` | writes an editable `.md` (`features/**`, `codebase/**`, `project/PROJECT.md`, `project/FLOWS.md`, `project/RULES.md`, `project/TEST-ENVIRONMENT.md`; `ROADMAP.md`/`STATE.md` only with the loop stopped) -> `{path, version, created, ok}`; a different `version` = 409 with `current_version`; a new file needs an empty `version` |
| `GET roadmap` | `{text, approved, not_approved_reason, approved_at, stages[{n, title, features}], features[{<ROADMAP fields>, slug}], errors, open_questions}` |
| `POST roadmap/approve` `{"confirm": true}` | writes `ROADMAP-APPROVED.json` through `loop_state.approve_roadmap` -> `{ok, approval: {approved, reason, at, features}}`; 409 with the loop running; 422 `{"ok": false, "error", "code": "planning_gates", "gates": [...]}` while a non-optional planning gate (below) is not ok (an invalid or empty ROADMAP fails the `features`/`fields` gates) |
| `POST roadmap/generate` `{"goal", "map_first": bool, "budget_usd"}` | job `claude -p "/orchestrator roadmap --headless --goal-file ..."` (see `SKILL_BRIDGE.md`) -> 202 `{ok, job, detail}`; 409 `code: "planning_required"` in planning mode (the planning chat replaces it) |
| `POST roadmap/refine` `{"text", "budget_usd"}` | resumes the session of the last roadmap job (`--resume`) -> 202; 409 `planning_required` in planning mode |
| `POST codebase/map` `{"budget_usd"}` | job `claude -p "/orchestrator map"` -> 202 |
| `GET jobs` / `GET jobs/<job>` | project jobs: `{id, type: "roadmap\|roadmap_refine\|map\|planning", state: "running\|ok\|error", started_at, finished_at, cost_usd, reply, error}` |
| `GET planning` | planning screen (below) |
| `POST planning/send` `{"text"}` | one message of the planning chat -> 202 `{ok, job_id}`. Refuses: 400 empty text or longer than 8000 characters; 409 `code: "loop_running"`; 409 (no code) when the loop worktree exists; 409 `code: "planning_prerequisites"` (+ `gates`) while the `git`, `structure`, `conformance` or non-optional `map` gate fails; 409 `code: "planning_busy"` while a reply (or any claude job of the project) runs |
| `GET planning/job/<job>` | `{ok, state: "running\|ok\|error", cost_usd, reply (ok only), error (error only)}`; a job of another type = 404 |
| `POST planning/reset` `{"confirm": true}` | clears the conversation (the next message starts a new claude session); the documents stay. 400 without `confirm: true`; 409 `planning_busy` during a reply -> `{ok}` |

`progress`:
```json
{"mode": "new|no_specs|planning|ready_for_loop|in_loop|done",
 "next_action": "structure|conformance|map|roadmap|flows|approve|test_environment|check|start_loop|follow",
 "steps": [{"id": "folder|git|structure|conformance|map|roadmap|flows|approval|test_environment|loop", "title": "...", "ok": true, "optional": false, "detail": "..."}],
 "loop_status": "no_loop|running|stopped|error|continue|awaiting_user|done",
 "counts": {"total": 0, "verified": 0, "blocked": 0},
 "work_dir": "...", "loop_worktree": false,
 "running_job": {"id": "j-...", "type": "roadmap", "started_at": "ISO"}}
```
`running_job` is `null` when no claude job runs. Errors of every route: `{"ok": false, "error": "<text>"}` (+ extras such
as `current_version`, and `code` on the planning refusals).
`steps` has a `flows` step right after `roadmap` (ok when FLOWS.md has a flow for every ROADMAP feature; optional when the
roadmap is already approved). While the roadmap is not approved, `next_action` is `approve` only when every planning gate
passes; otherwise it is the action of the first failing gate (`structure`, `conformance`, `map`, `flows`, or `roadmap` for
the goal, fields and open questions; a failing `git` gate shows only in its step).

`planning` (planning mode: roadmap never approved AND no loop ever ran; see `services/planning_requirement_service.py`):
```json
{"required": true,
 "thread": {"messages": [{"role": "user|ai", "text": "...", "at": "ISO", "cost_usd": 0.42}], "busy": false, "job_id": "j-...|null", "session": true},
 "docs": {"project": "PROJECT.md text|null", "roadmap": "ROADMAP.md text|null", "flows": "FLOWS.md text|null"},
 "gates": [{"id": "git|structure|conformance|map|goal|features|fields|open_questions|flows", "label": "...", "ok": true, "optional": false, "detail": "...|null", "items": ["..."]}],
 "can_approve": false, "approved": false}
```
Gates, in this order: `git`; `structure` (`.specs/project/` exists; items = missing files); `conformance`; `map`
(`.specs/codebase/*.md`; `optional` when the repo has no code, same rule as the checklist); `goal` (PROJECT.md exists with
no `FILL_IN`; items = those lines); `features` (ROADMAP.md with at least 1 feature and no grammar error); `fields`
(`loop_state.check_fields` errors); `open_questions` (the ROADMAP `## Open questions` section is `- none`; items = the
questions; HTML comments ignored); `flows` (a `## [slug]` section with 2+ numbered steps in `.specs/project/FLOWS.md` for
EVERY feature; items = slugs without one; flows of unknown slugs are a warning in `detail`). `can_approve` = every
non-optional gate ok and the loop not running. The first four are the chat prerequisites.

The chat reply is a claude job (type `planning`, one per project with the other jobs): cwd = the repo; stdin =
`/orchestrator plan --headless` + newline + the text on the first message, only the text on the next ones (`--resume
<session>`); the account's default model; `--max-budget-usd 2`; 600 s; no MCP; `--permission-mode dontAsk`;
`--allowedTools Read Grep Glob Skill Edit(/.specs/project/PROJECT.md) Edit(/.specs/project/ROADMAP.md)
Edit(/.specs/project/FLOWS.md)` (an `Edit` rule covers Write too; `/path` in a CLI rule = the working directory);
`--disallowedTools Bash NotebookEdit Agent`; environment without secrets. Because the user's own settings can widen the
allow rules, a file guard snapshots the repo before the reply (except `.git`, `node_modules`, `loop/internal` and
`MONITOR.json`; in-memory copy of files up to 1 MB, 64 MB in total) and afterwards undoes every change outside the three
documents (created = deleted, changed/deleted = restored, larger files only reported), logs `planning_guard_reverted` and
appends a note to the reply. Thread: `.specs/project/loop/internal/planning/thread.json` (messages + the `session_id`,
which never leaves through the API). The chat never approves.

## Bridge with the skill
`SKILL_BRIDGE.md`: how the plugin skill finds the app (`LOOP_APP_HOME`, else `~/.loop-harness/config.json` ->
`app_root_dir`), which commands it uses (`python <app>/harness.py serve --project <repo>`,
`python <app>/harness.py loop <repo> ...`) and the headless roadmap mode the app calls.
