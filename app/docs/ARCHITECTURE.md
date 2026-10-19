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
                                    rules, scale, monitor_proc, classify_structure, app_paths, project_repos (REPOS.json),
                                    close_loop, repo_catalog
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
| Multi-repo | `services/multi_repo_module.py`, `services/repo_registry_service.py`, `services/repo_readiness_service.py`, `services/workspace_service.py`, `services/repo_catalog_service.py`, `api/controllers/repo_controller.py`, `api/controllers/repo_readiness_controller.py`, `api/controllers/workspace_controller.py`, `api/controllers/repo_catalog_controller.py`, `engine/project_repos.py`, `engine/close_loop.py`, `engine/repo_catalog.py` (the close and repo-in-use logic also touches `loop_worktree_service.py` and `loop_status_service.py`) |
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
- `~/.loop-harness/` (or `LOOP_DATA_DIR`): `repo-catalog.json` (the repo catalog, written only by the catalog job), `config.json` (`app_root_dir`, `orchestrator_skill_dir`,
  `claude_transcripts_dir`, `output_dir`, `claude_binary`, `idle_hours`), `projects.json` (`{version, projects[]}`, one
  item per project: `id, name, repo_path, opened_at, last_opened_at, favorite`), `app.json` of the live app (`pid, port,
  url, token, started_at, app_root_dir`), an optional `.env` and `logs/app.jsonl`.
- In each project, under `.specs/` (layout of `engine/loop_state.py`): `project/` (`PROJECT.md`, `ROADMAP.md`,
  `STATE.md`, `FLOWS.md`, `RULES.md`, `TEST-ENVIRONMENT.md`, `phases/stage-NN.md`), `project/loop/` (`ANSWERS.md`, `VALIDATE.md`,
  `DEPENDENCIES.md`, `GRAPH.md`, `NOTICE.txt`), `project/loop/json/` (`LOOP.json`, `LOOP-CMD.json`, `MONITOR.json`,
  `meta.json`, `ROADMAP-APPROVED.json`, ...) and `project/loop/internal/` (`LOOP.lock`, `PAUSE`, `turns/`, `snapshots/`,
  `chat/`, `jobs/`, `planning/thread.json`, `whiteboard.json`, `roadmap-goal.md`, logs). Multi-repo projects add `project/REPOS.json` (kept by Reset specs) and, in `project/loop/json/`, `SCOPE.json` (`roots`), one `delivery.json` per repo and `last-run.json` (kept by Reset specs). Project ids are a short hash of the normalized folder
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
| `POST /api/app/projects/<id>/reset-specs` `{"confirm": "<project name>"}` | "Reset specs" (`SpecsResetService`): MOVES the planning/loop state (`features/`, ROADMAP, FLOWS, STATE, `phases/`, `archive/`, the loop folder) to `.specs/.trash/<YYYYMMDD-HHMMSS>/` (paths relative to `.specs/`), then recreates ROADMAP/FLOWS/STATE from `templates/specs` and an empty `features/`. Keeps RULES, PROJECT, TEST-ENVIRONMENT, `.env.test`, `ACCESS.json`, `rules.tests.json`, `SCOPE.json`, `PERMISSIONS.json`, `docs/codebase`, `CLAUDE.md`. The lists live in `RESET_MOVED_PATHS` / `RESET_KEPT_PATHS`. -> `{"ok", "moved": [...], "trash": ".specs/.trash/<stamp>\|null", "kept": [...]}`; 400 without the exact name in `confirm`; 409 (`code` `loop_running` / `planning_busy`) while a loop or claude job runs. Idempotent (nothing to move = empty `moved`, `trash` null). Never follows a link, only touches `<work>/.specs/`; `.specs/.trash/` is git-ignored and never listed or read by the app |
| `GET /api/app/overview` | the running loops of every project (only projects whose `LOOP.lock` PID is alive): project, stage, feature, turn, cost, repos and the pending question, for the hub cards |
| `GET`, `POST /api/app/projects/<id>/repos` | list the project's repos / add one (`{"path", "copy"?}`; refused when it is not a git repo, has a detached HEAD or is already listed) |
| `POST /api/app/projects/<id>/repos/multi` `{"multi"}` | the "This project uses several repositories" checkbox; 409 `loop_running` while a loop runs |
| `DELETE /api/app/projects/<id>/repos/<name>` | removes a repo from the list; never touches a folder or a worktree; 409 for the primary repo or while a loop runs |
| `PUT /api/app/projects/<id>/repos/<name>/copy` `{"copy"}` | stores the copy list (files to copy into the worktree); `{"copy": null}` = back to automatic (the key leaves `REPOS.json`). Repo objects carry `detected_setup`, `setup_configured`, `copy_configured` |
| `GET /api/app/projects/<id>/repos/<name>/copy-suggestions` | git-ignored files that look like local configuration (`.env`, `.env.*`, `appsettings.*.json`, `*.local.*`); `node_modules`, `bin`, `obj`, `dist` are never listed |
| `GET /api/app/catalog`, `GET /api/app/catalog/sheet?path=` | repo catalog: the index (name, purpose, capability ids, `weak`, `stale`) and one repo's sheet |
| `POST /api/app/catalog/scan` `{"paths"}`, `GET /api/app/catalog/job/<job>` | scan job that writes `~/.loop-harness/repo-catalog.json` |

### Project, prefix `/p/<project_id>/api`
The loop read routes (`health`, `state`, `history`, `agent/<id>/log`, `turn/<n>`, `graph`) and the loop command routes
(`whiteboard`, `answer`, `resume`, `note`, `pause`, `loop/check`, `loop/start`, `instruction`, `chat/*`, `actions`,
`action/*`) are described in `API.md`. `POST /p/<id>/api/shutdown` is a compatible no-op `{"ok": true}`: shutting down
belongs to the app. `GET /p/<id>/api/health` returns `{"ok": true, "repo", "work", "pid"}` (the engine's `monitor_proc`
checks `work`).

Multi-repo routes: `GET workspace`, `POST workspace/create` `{"repos"?}` (a job; 409 `busy`), `GET workspace/job/<job>` (the steps to draw),
`POST workspace/repos/<name>/commit-push` `{"message"?, "push"?}` (`WorkspaceService.commit_and_push` -> `GitClient.commit_all_and_push`: `git add -A`,
commit and push of the user's folder, current branch only, never forced; 409 `busy`, `worktree_exists`, `commit_push_failed`),
`GET close-loop/check` (per repo: can close and the reasons), `POST close-loop` `{"confirm_compare_link"?, "repos"?}`. Hub routes of the per-repo
readiness guard (`RepoReadinessService`, `RepoReadinessController`): `GET /api/app/projects/<id>/repos-readiness`,
`PUT .../repos/<name>/conformance`, `POST .../repos/<name>/map` (details in `API.md`). Starting a loop (or
answering, which relaunches it) on a repo that another running project uses is refused with 409 `repo_in_use`, naming that project.

Projects module routes:
| Route | What it does |
|---|---|
| `GET progress` | project checklist (below) |
| `POST structure/initialize` `{"conformance": "STANDARD\|LEGACY\|null"}` | creates `.specs/{project,features}`, `PROJECT.md` (short context for the subagents), `ROADMAP.md` (empty template), `STATE.md`, `FLOWS.md` (empty template), `RULES.md`, `TEST-ENVIRONMENT.md` and `.env.test` (test credentials, names and values), `loop/{json,internal}`, puts `.specs/` in `.gitignore`, imports `@.specs/project/RULES.md` in `CLAUDE.md`; never overwrites an existing file; writes `Conformance:` in `CLAUDE.md` when given; `TEST-ENVIRONMENT.md` and `.env.test` are born with the rest (the first planning message only re-creates them when missing). Returns `{created, changed, existing, conformance, ok, progress}` |
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
 "next_action": "structure|conformance|map|repos|roadmap|flows|approve|test_environment|check|start_loop|follow",
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
passes; otherwise it is the action of the first failing gate (`structure`, `conformance`, `map`, `repos`, `flows`, or `roadmap` for
the project context, fields and open questions; a failing `git` gate shows only in its step).

`planning` (planning mode: roadmap never approved AND no loop ever ran; see `services/planning_requirement_service.py`):
```json
{"required": true,
 "thread": {"messages": [{"role": "user|ai", "text": "...", "at": "ISO", "cost_usd": 0.42}], "busy": false, "job_id": "j-...|null", "session": true},
 "docs": {"project": "PROJECT.md text|null", "roadmap": "ROADMAP.md text|null", "flows": "FLOWS.md text|null"},
 "gates": [{"id": "git|structure|conformance|map|repos|goal|features|fields|open_questions|flows", "label": "...", "ok": true, "optional": false, "detail": "...|null", "items": ["..."]}],
 "can_approve": false, "approved": false}
```
Gates, in this order: `git`; `structure` (`.specs/project/` exists; items = missing files); `conformance`; `map`
(`docs/codebase/*.md` at the project root, falling back to the legacy `.specs/codebase/*.md`; the gate detail adds `mapped at <sha>` from the app-written `docs/codebase/README.md`; `optional` when the repo has no code, same rule as the checklist); `repos` (only present when an extra repo of a multi-repo project lacks a Conformance line or, with code, a map; items = `RepoReadinessService.list_unready`; it blocks roadmap approval but is not a chat prerequisite); `goal` (label "Project context (PROJECT.md)": PROJECT.md exists, is non-empty and has no `FILL_IN` line, which in the short template is only the Vision; items = those lines); `features` (ROADMAP.md with at least 1 feature and no grammar error); `fields`
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

## Multi-repo projects
One project can work on several git repositories. A single-repo project is the same thing with one entry: no `REPOS.json`, no
migration, and its files and output stay as they were.

**Repo list.** `.specs/project/REPOS.json` of the primary repo holds `{"multi", "repos": [{name, path, primary, copy[]}]}`
(`engine/project_repos.py` reads it; the engine never imports app services). Names are short, lowercase and never renamed. The primary
repo is the one the project was opened on: it holds `.specs/` and is the loop's working directory. File absent, `multi: false` or no repos
= one primary repo. Unchecking the multi box keeps the listed repos in the file, but the engine then sees only the primary. Reset specs
keeps `REPOS.json`. Removing a repo only edits the list.

**Workspace.** For each repo the loop works in a sibling folder `../<repo>-worktree-loop` (a git worktree) on a `loop/<date>` branch
(the same name in every repo). The user's folder and branch are never touched. `ensure_worktrees` in `run_loop.py` creates them **at loop
start**, so nothing is silent: it prints one line per step (`[loop] [<repo>] check|warn|worktree-add|reattach|copy|setup|ready|reuse|error: ...`;
a single-repo project keeps its old lines without the `[<repo>]` tag). `WorkspaceService` parses those lines into steps. The
**Create workspace** button runs the same code as a job, so the panel can stream the steps before the first loop. State per repo:
`not_created | ready | loop_running | dirty | missing`. A repo with uncommitted changes in the user's folder gets a warning: the worktree
is born from the last commit. `.specs/`, `CLAUDE.md` and `.claude` are copied only into the primary worktree. In a multi-repo project each
worktree is prepared automatically: `resolve_setup` takes the stored setup (or the CLI `--setup`) and otherwise `detect_setup.detect_setup`
(lockfiles, `.sln`/`.csproj`, `requirements.txt`...), `resolve_copy` takes the stored list and otherwise the git-ignored local config files;
a single-repo project without `REPOS.json` keeps no automatic copy or setup. `--check` reports a PROBLEM for every extra repo without a
`Conformance:` line. `meta.json` gets a `repos` map only when the project is multi-repo (`branch`, `base` and
`worktree` stay the primary's, so old readers work). The panel shows this as the **Workspace** band (`frontend/src/pages/panel/workspace.js`;
the repo list and the checkbox are in `pages/repo_manager.js`).

**Roadmap grammar.** `paths=<repo>:<path>` (no prefix = primary repo) and `repo-order=<name,name>` (build and commit order). A repo name
that is not in `REPOS.json` is an error naming the feature. Two features collide only when they use the same repo and an overlapping
path, so features in different repos can share a wave.

**Copy list.** Git-ignored files the build or the run needs (`.env.local`, `appsettings.Development.json`) are listed per repo in
`REPOS.json` (`copy`: key absent = automatic, the suggested files are copied; `[]` = copy nothing; a list = as is). They are copied into the worktree after it is created, and the sha256 of each is stored in `meta.json` so Close
loop can see a change. Heavy folders are never copied. The guard `secrets` rule also matches these names inside the worktrees
(`SCOPE.json` `copied`), so agents cannot read them; the build, which only loads the file, is not blocked.

**Commit and delivery.** Only the orchestrator commits: one commit per repo changed by a verified feature, same slug and subject, files
added by name, only inside the listed worktrees. Push and PR happen only in `delivery.py`, on the user's action (Act, Deliver), never
inside `claude -p`: one push of the `loop/*` branch and one PR per repo against the integration branch (`developer` by default; the
integration and other protected branches are never pushed). Each PR body gets a **Related PRs** block with the other repos' links. Each
worktree keeps its own `delivery.json`; the primary's also holds the aggregate under `repos`. A failure in one repo is reported and
nothing is rolled back.

**Close loop.** A button in the Workspace band that runs `engine/close_loop.py` outside `claude -p`. A repo can close only when the loop is
not running, the push is confirmed (`delivery.json` and the remote hold HEAD), a PR exists (`PR created` or `PR already existed`; a compare
link alone needs a confirmation), the tree is clean outside `.specs/` and no copied file changed. Order, fixed: (1) verified backup of each
worktree, (2) the durable `.specs/` files go back to the primary repo (ROADMAP with statuses, FLOWS, STATE, `features/*/{spec,design,tasks}.md`,
`phases/`, `VALIDATE.md`, the summary; never `loop/internal/` or the rest of `loop/json/`), (3) `last-run.json` in the primary repo (branch,
base, head and PR per repo), (4) `git worktree remove` without `--force` (the primary, which holds `.specs/`, last). Branches and commits
stay. One failing repo does not stop the others; each ends `closed`, `kept` or `failed`. **Re-attach:** if `last-run.json` names a branch
that still exists and the worktree folder is gone, the next start runs `git worktree add <path> <branch>` (no `-b`) and prints `reattach`,
instead of creating a new branch.

**Repo catalog.** The skill `plugin/skills/repo-catalog/` defines a sheet per repo: a purpose of at most 200 characters and capabilities,
each citing an `evidence` file that must exist in the repo. `engine/repo_catalog.py` checks the schema and the evidence (a capability with
a missing file is rejected by id), caches per HEAD sha and marks a repo without `docs/codebase` as **weak** (no purpose, no capabilities;
map it first). `RepoCatalogService` runs the scan as a job and is the only writer of `~/.loop-harness/repo-catalog.json`. The planning
chat reads only the index (name, purpose, capability ids) and **suggests** a repo for each feature; the user confirms, and only then is
`paths=<repo>:` written to the ROADMAP.

**Confinement.** The guard (`hooks/guard.py`) reads `SCOPE.json`: `roots` (every worktree of the project) and absolute feature paths (an
old relative file still works). The orchestrator profile has `repo-only`: it writes and commits only inside the listed worktrees and the
primary `.specs/`, and a `git` command is judged by the repo of its `-C` or cwd. `git push`, PR and release stay blocked for every profile
and `git add -A` for the orchestrator. `claude_command` grants each non-primary worktree with one `--add-dir=<worktree>` (the `=` form,
because the flag is variadic). A probe on claude 2.1.303 showed that the PreToolUse hook fires for a file in a granted directory, and that
a directory that is not granted is not writable while the hook still sees the attempt. The command also passes
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'` (no MCP in a turn) and
`--disallowedTools WebFetch,WebSearch,NotebookEdit,Bash(ssh *),Bash(scp *),Bash(sftp *)`.

**Not covered.**
- Two loops on the same repo: the app refuses a start when another project's running loop uses the repo (`repo_in_use`), nothing more.
- The `secrets` protection matches a tool call that names a copied file; a broad `Grep` or `Glob` over a worktree is not caught.
- Shell redirects are not confined to the worktrees.

## Test environment: layers, access and what the AI sees
`TEST-ENVIRONMENT.md` holds NAMES (`$NAME`); values live only in `.env` files. `engine/credential_check.py` resolves each name in the
SAME order as the skills' `testenv.py`, first hit wins:

| # | Layer | Counts |
|---|---|---|
| 1 | real environment variable | always |
| 2 | `<project>/.specs/project/.env.test` | always |
| 3 | `~/.loop-harness/.env` (`LOOP_DATA_DIR`) | always |
| 4 | plugin `shared/.env` (when the plugin is installed) | only if the project accepted the shared fallback |
| 5 | user-level shared `~/.claude/skills/_shared/.env` (the generic DEV account) | only if the project accepted the shared fallback |

Layers 4 and 5 are the user's SHARED account. A project accepts them with `.specs/project/ACCESS.json`, key `shared_fallback: true`.
A name that only a shared layer defines, without acceptance, has the source `shared-blocked` and stops the run like a missing one.
A blank `NAME=` line never hides a lower layer. The user-level shared folder is found through `LOOP_USER_SHARED_DIR` (tests), then
`user_shared_dir` in `~/.loop-harness/config.json`, then the default `~/.claude/skills/_shared`; the home is never hardcoded. `envload.load()`
copies `.env` files into `os.environ`, so an environment value identical to a file layer's is treated as that file's (a copied shared value
cannot jump the acceptance gate). `;`-lists (`TEST_USER_EMAILS` ...) also give `TEST_USER_<n>_EMAIL/_PASSWORD/_ROLE`, with the origin of the list.

**`ACCESS.json`** (`engine/access_settings.py`): `{"grafana_read": bool, "shared_fallback": bool}`, both false by default; tolerant read
(only a JSON `true` counts), atomic write. It is a per-person choice, so `.specs/project/ACCESS.json` is added to the `.gitignore` lines the
app maintains (`ENV_IGNORE_LINES`) and Reset specs keeps it.

**Endpoints** (`TestEnvironmentController` -> `TestEnvironmentService` -> `TestEnvironmentRepository`; errors `{"ok": false, "error", "code"?}`):
- `GET /p/<id>/api/test-environment` -> `{ok, ready, names: [{name, source: missing|env|project|machine|shared|shared-blocked, set, secret,
  path_label}], fill_in: [FILL_IN lines], access: {grafana_read, shared_fallback}, shared_paths: [labels of the shared files found]}`.
  `names` = every `$NAME` the document mentions (comments included, like the preflight). `ready` = no missing name, no `FILL_IN`, no
  `shared-blocked` name. Never a value.
- `PUT .../test-environment/values` `{"values": {"NAME": "value"}, "target": "project"|"shared"}` -> `{ok, saved: [names]}`. A name outside the
  document is a 400 `unknown_name`; values are one line, at most 500 characters. `project` writes `.specs/project/.env.test` (created, kept
  git-ignored); `shared` writes the user-level shared `.env` (created when missing). Only the `KEY=value` line of a saved name changes; every
  other byte (comments, blank lines, other keys, CRLF/LF, BOM) stays; the write is atomic. A value is never echoed, logged or put in an error.
- `PUT .../test-environment/access` `{"grafana_read"?: bool, "shared_fallback"?: bool}` -> `{ok, access}`.

**Preflight.** `run_loop.test_environment_problems` (runs when the roadmap has `regression=yes` or `--e2e`) uses the same resolution: a
missing name asks to "define it in .specs/project/.env.test", a `shared-blocked` one to "accept the shared fallback in the Test environment
tab". It stops the loop before the first turn and names every name.

**What the AI can and cannot see.** Before each planning message the app writes `.specs/project/loop/internal/credential-names.json`
(`[{name, source, set}]`, no values) and tells the planning prompt it exists and that it must never ask for or write a value. The file is
readable (`Read`) but not in `PLANNING_EDITABLE_PATHS`. Values are visible to the app and to `claude -p` only through the environment of the
real tests the project runs; they are never in a document, a response, a log or a prompt.

**Grafana read-only opt-in** (`grafana_read: true`; `engine/grafana_access.py`). The planning reply and the loop's `claude -p` (the spec-writer
and verifier-flow subagents run inside it) get the user's `grafana` MCP server and only these tools: `mcp__grafana__query_loki_logs`,
`list_loki_label_names`, `list_loki_label_values`, `query_loki_stats`, `query_loki_patterns`, `find_error_pattern_logs`, `list_datasources`.
The server definition is the user's `mcpServers.grafana` of `~/.claude.json` (override `LOOP_CLAUDE_CONFIG_FILE` in tests); only that entry is
copied into a minimal config at `<data dir>/.secrets/grafana-mcp.json` (credentials stay in that file, under the folder the guard's `secrets`
rule blocks; the argv carries only its path), together with `--strict-mcp-config`, so no other MCP joins it. The 7 tools go in
`--allowedTools`; the known Grafana writers (`create_annotation`, `update_dashboard`, `create_incident`, ...) are also in `--disallowedTools`.
No usable definition = the feature stays off (empty MCP config, as always). With `grafana_read` false nothing changes. Spec subagents
declare an explicit `tools:` list: to use these MCP tools their frontmatter must list them (a plugin change, see the report of this feature).

## Bridge with the skill
`SKILL_BRIDGE.md`: how the plugin skill finds the app (`LOOP_APP_HOME`, else `~/.loop-harness/config.json` ->
`app_root_dir`), which commands it uses (`python <app>/harness.py serve --project <repo>`,
`python <app>/harness.py loop <repo> ...`) and the headless roadmap mode the app calls.
