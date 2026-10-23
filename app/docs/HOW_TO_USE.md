# How to use Loop Harness (unsupervised loop + panel)

Core idea: **you decide, the loop executes.** What needs judgment (context, roadmap, rules, STANDARD or LEGACY, what goes in and what does not) happens in a normal conversation with `/orchestrator`. Only after your approval does the script take over and repeat turns until it finishes or gets stuck. You do not type loop commands nor follow it step by step.

The app is the **runtime** (loop engine, HTTP server, multi-project web panel). The `orchestrator` skill of the plugin stays the **brain** (the prompt each `claude -p` turn runs) and owns the `hooks/guard.py` hook. Everything goes through a single CLI, `harness.py`, at the root of `app/`. In the examples, `<app>` is that folder (e.g. `/home/dev/loop-engineering-harness/app`).

## Install (once per machine)
```
python <app>/harness.py install     # writes ~/.loop-harness/config.json (app folder + detected skill folder)
python <app>/harness.py doctor      # checks the skill, the guard blocking `git push`, `claude` on PATH, .env and notify channels
```
- Python 3.10+ only (standard library). No pip, no npm.
- `install` is how the skill finds the app (`app_root_dir`). It does not touch `~/.claude`. The skill is found in this order: `LOOP_SKILL_DIR`, `orchestrator_skill_dir` in `config.json`, the plugin next to the app in this clone (`<repo>/plugin/skills/orchestrator`), the installed plugin (Claude Code plugin cache). Skill somewhere else: `--skill-dir <folder>` or `LOOP_SKILL_DIR`.
- `doctor` shows only channel **names** and whether they are configured, never values. It exits with code 1 when there is a problem.
- `doctor --access` is a separate read-only report of what the `claude -p` of the app can reach (MCP, skills, bundle, guard log); see "Isolation and access" below.

## In 1 minute (read this first)

```
CONVERSATION (with Claude)                      APP / SCRIPT (you)
1 map the project                               6 Check (harness.py check)   checks everything, spends nothing
2 Conformance STANDARD or LEGACY                7 Start loop (button) or the command the check prints
3 plan: roadmap + flows + rules  -> approve     8 the loop runs on its own, in turns, in a worktree
4 test environment (TEST-ENVIRONMENT.md)        9 it STOPS and NOTIFIES (toast, email, webhook, panel)
5 feature specs                                10 you answer in the panel, or with --answer / --option
```

- **Who does what:** Claude prepares and checks. **You** start the loop (panel button or terminal). Claude only starts it if you ask ("run the loop", "you run it"): that is **chat mode**, below.
- **Chat mode (optional):** you say "run the loop" and Claude runs the check, states the limits and starts it in the background, without following it; when the process ends, the system wakes it up and it summarizes. The loop spend (the `claude -p` sessions) is the same both ways. The difference is in the chat: the process is tied to it (close it and the loop dies) and each wake-up rereads the whole context, which weighs on a long chat. For a long chat or a loop of hours, prefer the panel or the terminal.
- **What a turn is:** a new `claude -p "/orchestrator continue --headless --turn N ..."` (clean context) that runs up to `--wave` features in parallel, each one as its own **lane**: every feature has its own agent at every step (spec-writer, spec-reviewer, executor, verifier; never one agent for two features), moves to its next step as soon as its own previous step passed, and is committed as soon as it is verified, without waiting for the others. A feature that fails or goes to quarantine only holds the features that depend on it. State is written in `.specs/`.
- **What `--wave N` is:** how many features (lanes) move in parallel per turn. Executions only run together when the features edit different files and do not depend on each other (a colliding one waits as `approved` while the others go on); specs and gates have no such limit. A feature with `regression=yes` moves alone. The engine tells the orchestrator which lane starts at which step (`--lanes a:spec,b:gate,c:execute`).
- **Feature names:** each ROADMAP line is `- [slug] status=pending title="..."`. The slug is a kebab-case id of 3 to 6 meaningful words (`customer-registration-api`, never an abbreviation) and `title` a short sentence of what the user gets; the panel shows the title (`features[].name`), falling back to the spec's first heading and then to the slug.
- **Where it works:** in a copy of the repo (`../<project>-worktree-loop`, branch `loop/<date>`). Your folder and your branch are not touched. `--no-push` = nothing goes to the remote.
- **When it stops:** finished (`done`), needs you (`awaiting_user`), or an error. In every case it notifies through the configured channels and prints the resume command. **A permission denial no longer stops it by itself:** a denied common command, on a turn that advanced, becomes a line in `VALIDATE.md` and the loop carries on; it only stops if the denied command is risky (push, force, `rm -rf`, destructive SQL, a production word from `GUARD_PROD_HINTS`, a command forbidden by the project's `PERMISSIONS.json`, any MCP tool) or if the turn did not advance. **A trivial question does not stop it either:** if the orchestrator returns a question with options that does not talk about authorization, production, something irreversible, push/merge, a secret or billing, the script answers it (the recommended option, or the first one that does not abandon the feature), notes it in `VALIDATE.md` and carries on. Cap of 12 per run and 3 per feature; `--no-auto-answer` turns it off. **A spec rejected at the gate over wording does not hold the feature either:** when the loop was about to finish with a feature blocked at the gate, the script tells the orchestrator to approve the spec, treat the findings as notes for the executor and run it; build and regression decide, once per feature.
- **Stopped for lack of a credential or key?** It should not: that is settled earlier, in step 4. The check refuses to run without a complete `TEST-ENVIRONMENT.md`.
- **Where to see what happened:** in the panel (roadmap as a graph, turns, agents, cost) or in the files: `.specs/project/loop/GRAPH.md`, `ROADMAP.md`, `.specs/project/loop/NOTICE.txt` and the summary in the app's output folder (`output_dir` in `config.json`, default `~/Downloads`).

## Part 1 - normal conversation (you take part)

Open Claude in the project folder and use `/orchestrator` as usual, or the panel (Map, Conformance, the planning chat):

1. Context and mapping (`/orchestrator map` in an existing repo).
2. Set `Conformance: STANDARD` or `LEGACY` (the orchestrator asks, explains why and writes it to `CLAUDE.md`; `python <app>/harness.py classify <repo>` shows the evidence).
3. Roadmap: plan it by conversation and **approve** it in the panel (button **Approve roadmap**, which writes `ROADMAP-APPROVED.json`). Without that approval the check refuses to start; changed a feature or a stage afterwards, approve again. Limiting rules (`RULES.md`) come in here, with your phrase.
   - **Planning mode.** A project with no approved roadmap that never ran a loop opens in the panel's **planning screen**: checklist on the left, a live chat in the middle, the documents on the right (PROJECT / ROADMAP / FLOWS). Each message runs `/orchestrator plan --headless` in one session resumed every time: it writes only `.specs/project/PROJECT.md`, `ROADMAP.md` and `FLOWS.md`, then tells you what changed and asks the 1 to 3 questions that matter most. Answer them in the chat; the rest wait in the ROADMAP's `## Open questions`. The chat needs the prerequisites first (git, `.specs/` structure, `Conformance:`, and the codebase map when the repo has code); each failing one has its button on the left. **New conversation** starts a fresh session and keeps the documents.
   - **Import a spec feature.** The **Import a spec feature** button in the chat header (next to **New conversation**) takes the folder of one tlc-spec-driven feature (`spec.md` required; `design.md`, `tasks.md`, `context.md` when present), for example `.specs/features/<feature>/` of another project. A single file or pasted text is not imported. The plan is written in the current project's `.specs/project`, with descriptive feature slugs (3 to 6 words naming what is delivered), a title sentence per feature and stage names that say the outcome. The folder is picked with the system dialog (the in-app browser only when it is unavailable). A **preview** first lists what would be taken (the `.md` files down to depth 3, up to 30 files, 400 KB each and 2 MB in all, with the requirement, decision and task ids found) and the warnings. The files are copied read only, credentials redacted, twice: to `.specs/features/<slug>/` (the complete spec the loop keeps working from) and to the conversation's import folder with an `IMPORT-LOG.md`; they are shown as an attachment and, when you confirm, sent as ONE message: the AI builds ROADMAP (each feature lists the ids it covers as `reqs=ORIG-01..06`), FLOWS, PROJECT and the repositories from them and puts what the spec leaves open in `## Open questions`. Nothing is lost silently: the AI also writes `.specs/project/IMPORT-COVERAGE.md`, one row per requirement, decision and task (or core section) with where it went or `dropped: <reason>`, and the **import coverage** gate keeps Approve roadmap disabled while any item has no row. A file path, a folder without `spec.md`, a folder over the limits, or an empty or too large `.md` is refused and nothing is sent; the same feature cannot be imported twice (`already_imported`), and an existing `.specs/features/<slug>/` that the app did not import is refused (`feature_folder_exists`). Another feature adds to the conversation; it never wipes the plan.
   - **Repositories come from the chat.** Say in the planning chat which repositories to change and which to only read (by name, path or description); the chat adds them, gives each one a role and writes ONE sentence saying why it is in the project and why that role. There is no "confirm these repositories" step. Open the **Workspace** button of the navbar to see each repository with its **edits** or **reference** tag (hover for what it means), that sentence, its branches and its setup. A repository that does not exist or is not a git repo is not applied and shows a warning with the reason. An `edit` repository that no feature changes shows "edit · no feature changes it yet (no worktree)". If a feature changes a `reference` repository, the readiness check reports it: tell the chat to promote it to `edit` or to change the feature.
   - **Setup installer.** The checklist's setup step is a small installer card with **Back** and **Next**, a step indicator on top and a ✓ on every finished step. Single repository: **1 Conformance** (STANDARD or LEGACY, the suggestion preselected and tagged "suggested") and **2 Map** (one button **Map the project**, then **Continue to planning**). Monorepo: **1 Subprojects**, **2 Conformance**, **3 Maps** (see "Monorepos"). The git repository and the `.specs/` structure stay automatic checks shown before the installer; **Initialize structure** only creates `.specs/` and never asks for the Conformance. Mapping runs without a spend cap (the confirmation says so).
   - **Ready to run the loop.** The checklist has a last item that runs the same check as **Check** (plus: worktrees can be created for every edit repo a feature changes, feature paths belong to edit repos, MCP servers the plan uses are configured, the guard is installed, credentials resolve, a build/test command is known) in the background after every change. It shows READY with the time, or the PROBLEM and WARNING lines as items (level, repository, files and a one-line fix, for example "Commit these files"; the same data is `harness.py check <repo> --json`); a later change marks it stale until it runs again. If the check cannot run, approval stays blocked with "readiness check did not run".
   - **Approve roadmap** stays disabled (and the back end refuses it) until every gate is green: prerequisites, `PROJECT.md` (the short context for the subagents) without `FILL_IN`, i.e. its Vision filled in, at least one feature with no grammar or field error, `## Open questions` at `- none`, in `FLOWS.md` a flow of at least 2 steps for every feature, and the **Ready to run the loop** item (below). Until then the loop, the chat with the agents, Act and the whiteboard are not reachable.
   - `FLOWS.md` holds, per feature (`## [slug] Title`), the numbered steps and `Expected:` results that prove it works; the flow validation (`verifier-flow`, `--e2e`) uses it. `/orchestrator roadmap` writes it too.
4. **Test environment:** the orchestrator gathers the NAMES of headers, DEV users, URLs and secrets (and where each comes from) in `.specs/project/TEST-ENVIRONMENT.md`; **values never go in the markdown**: they live in `.specs/project/.env.test` or `~/.loop-harness/.env` (generic DEV account), both ignored by git, with users as `;`-separated lists (`TEST_USER_EMAILS`, `TEST_USER_PASSWORDS`) (template in `<app>/templates/TEST_ENVIRONMENT.md`) and asks you, in one batch, only what is missing. This comes **before** the approval because it is what stops the loop most.
5. Answer the open questions of the specs.
6. When all of that is approved, the orchestrator **stops and hands you the ready command**, in a single line.

## Part 2 - the panel (the default way)
```
python <app>/harness.py serve --project <project-folder>
```
- Starts the app (only `127.0.0.1`, a free port from 47800 to 47899), prints `MONITOR_URL=<project panel url>` and `APP_URL=<hub url>` and opens the browser. If the app **is already up**, it only registers the project in it, prints the URL and exits (one app for every project).
- Flags: `--port N` (fixed port), `--idle-hours H` (hours without use until it shuts down on its own; `0` = never; default 2), `--no-browser`, `--data-dir D` (default `~/.loop-harness` or `LOOP_DATA_DIR`).
- The hub (`APP_URL`) lists the projects, opens an existing folder, creates a project from scratch (folder + `git init` + `.specs/`, landing in the planning screen) and shows the diagnostics.
- **Reset specs** (button on each project card, next to Forget): starts new features from zero. After you type the project name, it moves the plan, roadmap, flows, features, `phases/`, `archive/` and the whole loop state (approval, answers, turns, snapshots, chats, planning conversation) to `.specs/.trash/<date-time>/` and recreates ROADMAP, FLOWS and STATE from the templates. `RULES.md`, `PROJECT.md`, the test environment (`TEST-ENVIRONMENT.md`, `.env.test`, `rules.tests.json`), `SCOPE.json`, `PERMISSIONS.json`, the codebase map (`docs/codebase`), `CLAUDE.md`, your code and git stay. The spec files are never deleted (move the folders back to undo); it is refused while a loop or a Claude job runs in the project, and the roadmap has to be approved again. **If a loop worktree exists** (`<repo>-worktree-loop`, branch `loop/...`; one per repo in a multi-repo project), the confirmation says so, with its path, branch and any commits not pushed yet, and the reset **deletes it too, with its local branch**: no merge, no push, no backup. To keep its changes, finish the loop (**Close loop**) or push the branch before resetting; otherwise everything is deleted and cleaned. Your own checkout and other worktrees or branches are never touched.
- **Several repositories:** see "Projects with several repositories" below.
- The project panel (`/p/<id>/`) shows roadmap, turns, agents, specs, questions, validations, cost and limits, and has the loop buttons. It has two scopes: the **GLOBAL** zone (blue, left: valid for the whole loop) and the **LOOP** zone (amber, right: depends on the selected turn).

The loop also ensures the panel on its own: when it starts, `run_loop` reuses the panel that launched it (`LOOP_MONITOR_URL`), or the work's live `MONITOR.json`, or registers the project in the live app (`app.json`), or starts `harness.py serve --project <repo>` detached. It prints `[loop] monitor: <url>` and the URL goes in every notice.

### In the panel
- **Layout.** A navbar on top (see Navbar), then two zones side by side:
  - **GLOBAL** (blue aside, "valid for the whole loop"; drag its right edge to resize between 320 and 560 px, 400 by default, the width is remembered; the **◀ / ▶** button in the turn strip hides or shows it, and on narrow screens it becomes a drawer). Tabs: **Context** (the project trail as a read-only vertical checklist, monorepo subprojects as one line each), **Plan & files** (stages, counts by status, the AI decisions to review, and **Open plan & files**), **Dependencies** (features by wave, and **Open dependency diagram**) and **Errors (N)** (every problem; click one to open its dialog). There is no Files tab, no editor and no Workspace zone in loop mode: the plan changes only through **Back to planning**.
  - **LOOP** (amber main area, scrolls vertically; everything in it depends on the selected turn): the pause banner, the turn strip, the turn header, the tabs and, on the right, the inspector.
- **Planning navbar.** In planning mode the status pill (**Planning**) sits on the left, right after the project/repository block (never centered), and the right side has the **Workspace** button (next to **Shut down app**). There is no WORKSPACE band and no "Writing the plan in <path>" line any more: all of it, and the uncommitted-files chip, is in the **Workspace** dialog (see "Projects with several repositories").
- **Navbar.** One bar with everything you can click, in this order. Left: the brand and the **repo chip** (short repo name; hover for the full path and branch, **copy** copies the path). Center: the **status indicator** (`no loop`, `running`, `paused`, `waiting for you`, `stopped`, `error`, `done`, plus the turn; hover for a plain sentence; when you have to act it gets an attention style and a click opens the **status dialog** with the question, the error or the summary and its buttons: answer, resume, raise the limit) and two compact **meters**: spend against the limit of this run (attention color from 80 %, red once passed) and verified features against the total. Right, in this order: the PR chip when there is one, **Pause after the turn** (only while running), **Start loop** (not while running; opens the Start dialog), **Back to planning** (disabled with the reason while a loop runs), **Finish** (only when the loop is done or stopped and a worktree exists) and **Shut down app**. A new question from the loop opens the status dialog once by itself.
- **Pause banner.** When a feature is in quarantine (or the loop waits for you) one line says "NEEDS YOU", which feature, why and that the loop is paused, with ONE button, **Understand and resolve**: it opens the pause dialog (AI summary, attempts, failed tests, files left uncommitted, what a skip would also block, a place to write guidance, **Try again with this guidance** and **Skip and continue**, and a chat about the problem). Nothing is decided in the banner itself. A plain question from the loop is answered in the status dialog of the navbar (see Answer).
- **Turn strip.** One card per turn (`T<n>`, what the turn did, a dot with the worst status of its steps, how many steps, cost). Each turn is a snapshot: picking one shows the loop **as it was** at the end of that turn (the state accumulated up to it). **● go to latest** returns to the live turn.
- **Turn header and tabs.** "Turn N · label", its status, the decision of the orchestrator, and DURATION, COST and STEPS IN TURN. Tabs: **Pipeline** (one lane per feature always showing the 6 steps: spec, review, gate, exec, verify, commit; a step not run yet is dashed; a `T<n>` tag marks what changed in that turn; **Diagram** or **List**; click a node to select it, double-click to open the agent dialog), **Decision & events** (what the orchestrator decided and every step of the turn), **Files** (what the turn changed) and **Errors (n)** (the failures of that turn). **⤢ dependencies** opens the dependency diagram.
- **Inspector.** **NOW · T<last>**: the orchestrator (state, what it does, its note) and **RUNNING · k**, the agents running right now (click one to select it). **SELECTED · STATE AT T<N>**: the picked node (agent, status, role, feature, who calls it, what it calls, how many runs, its last output) and **Open history and ask the agent**, a dialog with every run of that agent in that feature across turns and a chat. At the bottom, **Instruct the loop** and **Chat** (see below); a chat answer replaces the two cards until you click **back to the loop state**.
- **Dialogs.** Every error, the quarantine pause, an agent and the dependency diagram open in a big dialog (Esc or a click outside closes it), with the full detail on the left and a chat on the right to ask the AI about it (read-only).
- **Follow:** loop status, features, agents, cost and limits.
- **Guard:** guard blocks are listed in the **Errors** tab of the GLOBAL zone (section **Guard**, "it worked, nothing to fix") and the header shows a red `N problems` chip when there is a real problem. In an agent's log, a blocked call shows up as a red `blocked by the guard · <rule>` item with the reason.
- **Start loop (dialog).** The navbar **Start loop** opens a dialog with no free fields. **Decided in planning** lists, read-only, the nine settings the ROADMAP header holds (see "Loop settings in the ROADMAP header"), each marked `(default)` when the line is missing or `invalid in the ROADMAP` when its value was refused (the default is used); to change one, use **Back to planning**. Under it the models (**Orchestrator: Opus; agents: Sonnet**), the workspace state (per repository "starts from `<branch>` (last commit `<sha>`)" and the PR target), and the uncommitted-files notices, and two checkboxes **For this run**: **Push the branch and open a PR to <integration branch> at the end** (starts from the ROADMAP `Push and PR` line) and **Remove the loop worktree when I click Finish**. **Check only** = `harness.py check` (nothing is created, nothing is spent); **Start loop** checks again if needed and launches. Claude never starts it through the panel. The two checkboxes are remembered for the Finish step.
- **Plan & files (dialog).** **Open plan & files** (GLOBAL tab, or the navbar flow) opens a big read-only dialog: on the left one tree (the plan by stage and feature with a plain title, the status in plain words and each feature's spec, design, tasks and contract; the project files; the codebase maps `docs/codebase` and, in a monorepo, each subproject's; the loop files), on the right the selected file as Markdown with one sentence on what the file is for. There is no Edit, Refresh or Change with AI here. **Ask about the plan** is a chat in the same dialog (read-only, answers only about the plan, the specs, the maps, the decisions and the loop state; up to US$ 1 per answer).
- **AI decisions.** While it works the AI records the choices it made on its own in `VALIDATE.md`, each with its turn, a level, the doubt, what it chose and how to undo it. **important** = a real choice you may want to look at; **critical** = a choice that is expensive to undo (data, security, public contract, scope): the loop **stops at the end of that turn** and asks, with `keep as decided` or `change it: <write how>`. Routine choices are not recorded. The GLOBAL tab shows the count (`AI decisions to review`, critical and important); click it for the dialog, grouped by turn, with **Mark as reviewed** and **Ask for a change** (the change goes to the follow-up draft). Each turn's **Decisions** section shows the ones of that turn. The panel never edits `VALIDATE.md`; what you reviewed is kept apart in `decisions-review.json`.
- **Spend limit.** The run stops (does not fail) when the total spent passes the limit of the run: US$ 50 by default (`Max spend for the loop` in the ROADMAP header, `--max-cost`). At 80 % one notice is sent; at the limit the status is `paused` with `cost cap reached: US$ x of y`. The status dialog shows spent and limit, a **New limit (US$)** field and **Raise the limit and continue**; the new limit must be above what was already spent, and the spent total is never reset. **Stop here** only closes the dialog (the loop stays paused: use **Finish** or **Back to planning**).
- **Finish and follow-up.** With the loop `done` or `stopped` and its worktree still there, the LOOP column shows the **Wrap-up** (features done, PR or compare link, spend, turns, the unreviewed critical decisions and per repo what is pushed, uncommitted or unpushed) and the navbar offers **Finish**. **Finish** does, in order: the push and PR (when you left that checkbox on), the verified backup, the plan copied back to the repository and `last-run.json`, and the worktree removal (when you left that one on). It refuses, and lists what would be lost (uncommitted files, unpushed commits, unreviewed critical decisions), instead of deleting anything. From that refusal you may **Commit and push them first** (keeps the worktree), **Keep the worktree**, accept the unreviewed decisions, or **Remove the worktree and lose that work** (you type the repository name; the backup still runs and the branch stays). The **follow-up** is a chat next to it: each message (and the decisions you asked to change) has a short AI job write or edit ONE stage `Follow-up <n>` of the ROADMAP (the app undoes anything else and says why), at most US$ 1 per message and US$ 5 per round; you read the stage in Plan & files and answer again to adjust it. **Continue loop** approves the stage and runs a new round on the same worktree and branch with the same settings; the next message then opens round n+1. After Finish removed the worktree there is no follow-up: use **Back to planning**. The thread of a round is `.specs/project/loop/internal/follow-up/round-<n>.json`.
- **Back to planning.** Goes back to the planning screen to change the roadmap, the loop settings or the repositories. Refused while a loop runs; asks for confirmation when the worktree has uncommitted or unpushed work.
- **Answer:** when the loop stops at `awaiting_user`, click an option or write the answer. The panel relaunches the loop with your answer (the same as `--answer`). **Resume** / **Try again** relaunch without an answer (`error`, `stopped` or pause). **Add note** leaves a note for the next turn to apply without stopping the loop. **Pause** makes the loop stop before the next turn (the turn in progress finishes first).
- **Act (deliver and summary):** pick `Deliver` (push of the feature branch only + PR to the integration branch: the ROADMAP `**Branch base:**`, else `LOOP_INTEGRATION_BRANCH`) or `Summary`, or describe it in free text; the panel shows what it will do and only runs it after you **confirm**. Only with the loop stopped and no open feature; a protected branch, force and the guard rules still refuse. Your click is the authorization; Claude never triggers it.
- **Chat with the AI and instruct the loop:**
  1. Pick the turn and the node you want to understand and use the **Chat** option of the composer at the bottom of the inspector (or the chat of a dialog).
  2. **Ask** only clears doubts (the AI reads the files and answers, changing nothing; read-only tools, no MCP; up to US$ 1 per answer). **Instruct the loop** (default on the current version) sends a request the loop applies. On an old turn only Ask exists; use "Bring to the current version" to turn it into an instruction.
  3. The answer says what happened: **note** = the loop is running and applies it on the next turn; **answer** = the loop was waiting for you and was relaunched with the text; **note+resume** = the loop was stopped, in error or done and was relaunched to apply it.
  4. A request that adds a feature or changes the approved roadmap is not applied on its own: the loop stops and asks, with the recommended option marked.
- **Shut down:** button in the hub (`POST /api/app/shutdown`). On its own the app only shuts down after `--idle-hours` without use and with **no** loop running (nor chat/roadmap in progress). It **does not die when the loop ends**, on purpose. Without a live app, `harness.py serve --project <repo>` brings the panel back.
- **Do not want the panel:** `--no-monitor` on the loop command (or `LOOP_MONITOR=off` in the `.env` for every loop). `LOOP_MONITOR_OPEN=off` only avoids opening the browser. If the panel does not start, the loop carries on normally and says so in the console.
- Security: only `127.0.0.1`, Host checked and a token on every method that changes state (the token is in `app.json`, readable only by your user).

### Loop settings in the ROADMAP header
The limits and options of a run are decided in planning, as lines under the Goal of `.specs/project/ROADMAP.md` (the template documents them; the planning chat writes them with you):
```
**Dispatch:** auto                      auto, or how many features run per turn
**E2E:** no                             yes = the loop ends with the flow validation
**Max spend per turn:** 5               US$
**Max spend for the loop:** 50          US$ (0 = no limit)
**Max minutes per turn:** 60
**Branch:** automatic                   or a name
**Setup:**                              one command for the new worktree (empty = detected)
**When a feature gets stuck:** pause    pause, or continue (carry on with the others)
**Push and PR:** yes                    default of the Start dialog checkbox
**Max parallel tasks:** 3               executors per wave inside ONE feature (1 to 5)
**Branch base:** developer              optional: the branch the loop starts from and the PR target
```
**Branch base.** By default the loop branches from `developer`. To change it, write `Branch base: main` (or `release/2.0`) in the ROADMAP header. Order: ROADMAP, then LOOP_INTEGRATION_BRANCH, then developer. The PR goes to the same branch.
**Task waves.** A feature with a `tasks.md` is built by one executor per task, in waves (tasks marked `[P]` with no shared file run side by side, up to `Max parallel tasks`). The engine commands are `harness.py tasks waves <slug>`, `tasks set <slug> <Tn> <status>` and `tasks mirror <slug>`; the status of each task is in `.specs/project/loop/internal/tasks/<slug>.json` and the pipeline shows it for the features being built.
A missing line takes the default shown; an invalid value takes the default and shows a warning in the planning status and in the Start dialog. The loop reads them when it starts, so after a start a change needs **Back to planning** (and a start again).

### Orchestrator model
The orchestrator turn of the loop runs on **Opus** (`claude --model opus`, the alias of the latest Opus), the subagents on **Sonnet**. To change the orchestrator model put `"orchestrator_model": "<alias or model id>"` in `~/.loop-harness/config.json` (`""` = pass no `--model`, the account default applies); the Start dialog shows what will be used. Panel jobs (chat, planning, follow-up, maps) never go above Sonnet.

### Settings (the Settings button)
The **Settings** button (gear icon) in the hub header and in the project header opens one dialog with the app settings of `~/.loop-harness/config.json`: isolation, caveman mode, extra skills, orchestrator model, Claude command and fallbacks, parallel subproject maps, idle shutdown, the giant-file limits and the output folder, plus the read-only data folder, plugin folder, guard path and port range. A switch saves when you flip it; the other fields show **Save** once you change them. **Reset to default** removes the key from the file, and a setting you never touch stays out of it. A **restart** tag means the app reads it only at start (restart the app); the others apply to the next `claude -p`.

**Caveman mode** (off by default): with isolation on, it carries your `SessionStart` hook whose command mentions `caveman` (from `~/.claude/settings.json`) into the isolated runs. Without such a hook the row says "Not installed" and cannot be turned on. Each loop turn records whether caveman was on (`caveman` in `turns/NNN.json`), and the dialog compares the average cost per turn with and without it over every project's recorded turns ("Not enough data yet" below 3 turns on either side).

### Demo mode (see the panel before wiring a real repo)
```
python <app>/harness.py demo [--port N] [--no-browser] [--speed S] [--data-dir D] [--loop] [--keep]
```
- Creates a fake project, `acme-checkout` (a real git repo with a few commits, a full `.specs/` with an approved 3-stage
  ROADMAP of 9 features and `deps=` edges, turns, snapshots and Claude Code style transcripts), starts the app on an
  **isolated** data folder and opens that project's panel. It never calls `claude` and never touches `~/.loop-harness`:
  everything lives in a temporary folder (or `<data-dir>/demo/`) that is removed on Ctrl+C unless `--keep`.
- An animator moves the story every few seconds: the loop shows `running`, features go pending -> speccing -> specced ->
  approved -> building -> verified in waves that respect the dependencies, agent logs fill up, the guard blocks an executor
  that edits `infra/prod/terraform.tfvars`, the loop stops on a question for about 10 s (click an option to answer it
  sooner) and the story ends `done`. One cycle takes about 2 minutes at `--speed 1` (`DEMO_CYCLE_SECONDS` is printed);
  `--speed 2` is twice as fast; `--loop` restarts the story forever (for screen recorders).
- The panel buttons stay safe: chat, Check, Start, Act and answer reach stubs that only print a demo notice.

## Part 3 - the loop from the terminal
Run it in a terminal **separate** from the conversation, outside Claude.

**Step 1 - check (creates nothing, spends nothing):**
```
python <app>/harness.py check <project-folder>
```
It says `[check] READY. Command:` or lists `[check] PROBLEM:` lines (no roadmap, roadmap not approved, no Conformance, in a multi-repo project an extra repo without a Conformance line, the skill guard not found or not blocking `git push`, push without a remote with an explicit `--push`, etc.) and `[check] WARNING:` lines (uncommitted changes in the repo, a Node project without `--setup`). When it says READY, it prints the full command (`python "<app>/harness.py" loop "<repo>" ...`).

**Step 2 - start:**
```
python <app>/harness.py loop <project-folder> --wave 1 --setup "npm ci"
```
`harness.py loop` forwards everything to `backend/harness_backend/engine/run_loop.py`.

| Flag | What for | Default |
|---|---|---|
| `--wave N` | features (lanes) in parallel per turn, each with its own agents at every step. Executions only with disjoint PATHS: the orchestrator designs the roadmap with disjoint `paths` and suggests N (2 or 3). A collision becomes a queue | 3 |
| `--e2e` | final flow-validation phase (headless Playwright) | off |
| `--no-push` | turns off the final push of the feature branch + PR to the integration branch (`--push` only requires it to work in the preflight) | **on** |
| `--pause-on-blocked` | quarantine always pauses the loop (default: only pauses if another feature depends on the blocked one) | off |
| `--continue-on-blocked` | a quarantined feature (2 failures) does NOT pause the loop; it carries on with the others | off |
| `--setup "cmd"` | one-time command in the new worktree (e.g. `npm ci`) | none |
| `--budget N` | max US$ per turn | 5 |
| `--max-cost N` | cap of US$ summed over all turns (`0` turns it off); reaching it PAUSES the loop (resumable); a cap raised in the panel (`meta.json` `max_cost_usd`) wins | 50 |
| `--timeout N` | max minutes per turn | 60 |
| `--orchestrator-model M` | model of the orchestrator turns (`""` = no `--model` flag); the app passes `orchestrator_model` of `config.json` | opus |
| `--max-stall N` | consecutive turns without progress before stopping | 10 |
| `--branch name` | name of the loop branch | `loop/<date-time>` |
| `--answer "..."` / `--option N` | answers the question that stopped the loop and continues | - |
| `--rewind N [--yes]` | goes back to the state right after turn N (without `--yes` it only shows the plan) | - |
| `--summary-dir D` | folder of the final summary | the app's output folder (`output_dir` in `config.json`, default `~/Downloads`); never the repo |
| `--no-monitor` | does not ensure the panel | panel on |
| `--no-auto-answer` | every question stops and waits for you | automatic answer on |

You can leave it running and walk away. There is nothing to answer: if it needs you, it **stops and notifies**.

## Other commands (forwarded to the engine scripts)
| Command | Script | What for |
|---|---|---|
| `harness.py state ...` | `loop_state.py` | typed state: `validate`, `disjoint`, `graph <worktree>` (Mermaid), `next`, `loop`, `migrate` |
| `harness.py notify --test [--channels email]` | `notify.py` | test notice through the configured channels (or in the hub: Test notification) |
| `harness.py summary <folder>` | `loop_summary.py` | loop summary at any time, without Claude (only files and `git log`) |
| `harness.py deliver <repo>` | `delivery.py` | push of the feature branch + PR to the integration branch (the panel does it in Act) |
| `harness.py rules ...` | `rules.py` | project block rule tested in the skill's real guard (`propose`, `write`, `test`, `list`) |
| `harness.py audit <root>` | `audit_deps.py` | forgotten dependency between features (`--write`, `--apply`, `--json`) |
| `harness.py graph <root>` | `graph_view.py` | progress drawing (`--ascii`, `--short`, `--mermaid`, `--write`) |
| `harness.py classify <root> [--write STANDARD\|LEGACY]` | `classify_structure.py` | the project's Conformance |
| `harness.py scale <root> [--json]` | `scale.py` | how many codebase mappers to dispatch |
| `harness.py inventory <folder> [--json] [--changed a,b]` | `code_inventory.py` | free inventory of a folder: sizes, entry points, giant files |
| `harness.py tasks waves <slug> [--max N] \| set <slug> <Tn> <status> [--note TEXT] \| mirror <slug>` | `task_waves.py` | task waves and task status of one feature (`--root DIR`) |
| `harness.py check <repo> [--json]` | `run_loop.py` | readiness check; `--json` adds one `[check] JSON:` line with the readiness items |
| `harness.py doctor [--json]` | - | diagnostics (the same as `GET /api/app/info`) |
| `harness.py doctor --access [--project <repo>] [--json] [--no-probe]` | - | what a `claude -p` of the app can reach: MCP, skills, bundle, probe of the isolated `claude`, `guard.log` summary (see "Isolation and access") |

## What happens underneath
- The script creates **once** the worktree `../<project>-worktree-loop` (its own) on a `loop/<date>` branch and copies `.specs/` and `CLAUDE.md` there. Your folder and your branch are not touched. A multi-repo project gets one worktree per repo (see below).
- Each turn is a new `claude -p` (clean context) that reads `.specs/`, advances one wave, commits and writes `.specs/project/loop/json/LOOP.json`. Pushing is impossible inside the turn (the skill guard + `pushurl` disabled only for that process); the final delivery is made by the script.
- The guard is the skill's (`<skill>/hooks/guard.py`). The check verifies it exists and blocks `git push`, and that the plugin's `hooks/hooks.json` points (through `${CLAUDE_PLUGIN_ROOT}`) at a guard that exists on this machine.
- Everything the loop owns lives in `.specs/project/loop/`: the files a person reads at its root (`ANSWERS.md`, `VALIDATE.md`, `DEPENDENCIES.md`, `GRAPH.md`, `NOTICE.txt`), machine state in `json/` and the rest (lock, logs, turn logs, snapshots) in `internal/`. A project written by an older layout is moved there automatically (`harness.py state migrate <root>` does it by hand; it never overwrites).
- When it stops: console, Windows toast, `.specs/project/loop/NOTICE.txt`, the panel and the **summary `loop-summary-<project>-<date>.md`**.

## Projects with several repositories
One project can change more than one git repository (for example a back end and a front end). It is off by default: a project with one repo
looks and works as before.

**Define them in the planning chat.** There is no checkbox and no confirm step: the project is multi-repo as soon as more than one repository is listed, and a project with one repository is `edit` by default. Tell the
planning chat which repositories to change and which to only read; it fills the list and writes the **reason** of each one (one sentence). By hand, **Workspace > Add repository...** opens the native folder window, adds the
folder as `edits` and opens its **Set up** dialog; removing and the role switch (same words as the tags, with the same hovers) are in the **Repositories** dialog (also in the hub), which has no AI box of its own. A folder
that is not a git repo, has a detached HEAD or is already listed is refused with the reason. Removing a repo only takes it off the list: no
folder or worktree is deleted. The list is locked while a loop runs. It is saved in `.specs/project/REPOS.json` (`role`, `reason`), which Reset specs keeps. Readiness needs at least one repository with role `edit`.

**Edits or reference.** An **edits** repo is one the loop changes: it gets its own worktree on a new `loop/*` branch and ends in a PR, but the worktree is created only when a ROADMAP feature has `paths=<repo>:...`.
A **reference** repo is read only: no worktree, the agents just read its folder to understand contracts or examples, and the loop never edits it.

**The Workspace dialog.** The **Workspace** button of the planning navbar opens it (the old WORKSPACE band is gone; the Start dialog still shows a read-only list of rows). It shows the integration branch ("PR to developer"), the plan folder and, per repository:
name, folder, role tag, the reason the chat wrote, the **current branch** (and its upstream), **starts from `<branch>` (`<sha>`)**, what the loop will do ("create the worktree `<repo>-worktree-loop` on a new `loop/*` branch from `<base>`, then open a PR to
`<integration>`" or "only read it"), the setup state (`not set up`, `set up`, `map missing`, `mapping`) with **Set up**, **Map** or **Show progress**, the uncommitted **setup files** with **Commit setup files and push**, and the **other changed files that will NOT be carried**.
The loop never edits your folder, and nothing is cloned or pushed. For each edit repo it creates its own git worktree, a sibling folder
`../<repo>-worktree-loop`, on a `loop/<date>` branch, the same name in every repo. It is created automatically **when the loop starts**, with
the reason and every step shown (nothing is silent), or earlier with **Create workspace** (a confirmation, then the steps live; the loop does
the same if you skip it). `.specs/` lives in the primary repo's worktree.

**Where the worktree starts, and what it carries.** The worktree starts from the **integration branch** (the ROADMAP `**Branch base:**` decided in planning, else `LOOP_PR_BASE` / `LOOP_INTEGRATION_BRANCH`, else `developer`) when that branch exists in the repository as a local branch,
whatever branch your folder is on; if it does not exist, from the current branch, with a warning that names it. The Start dialog and the Workspace dialog show "starts from `<branch>` (last commit `<sha>`)" and the current branch of the folder. The files the app generated while
planning and that are not in that branch (the maps in `docs/codebase`, the `CLAUDE.md` files with an app block, the `.specs/project` and `.specs/features` plan files, and in a monorepo the root `CLAUDE.md` and `SYSTEM.md`) are **copied into the new
worktree** (never over a file the branch already has), so the agents see them; the first loop commit commits them on the loop branch and they reach `developer` through the PR. Any OTHER uncommitted change of your folder is never carried: readiness says so.
Readiness shows ONE block per edit repository: "N setup files not committed: they will be carried into the worktree (or commit them from Workspace)" and, separately, "M other changed files will NOT be carried", each with the file names.

**The worktree is prepared by itself.** In a multi-repo project each repo's worktree is prepared automatically, so you normally set nothing:
the **setup command** is detected from the repo's root files (`pnpm-lock.yaml`, `yarn.lock`, `package-lock.json`, `package.json`, a `.sln` or
`.csproj`, `requirements.txt`, `poetry.lock`, `go.mod`) unless you stored your own, and the **git-ignored local config files** (`.env`,
`.env.*`, `appsettings.*.json`, `*.local.*`; never `node_modules`, `bin`, `obj`, `dist`, and never `.example`, `.sample` or `.template`
files) are copied unless you configured a list. To override, open **Advanced: files and setup** on the repo row: it says "Automatic: will run
..." or "Custom: ..." for the setup and "Automatic" or "Custom list" for the files. **Save setup** stores a command (saving it empty goes back
to automatic); tick files and **Save files to copy** stores a list (an empty list copies nothing); **Use automatic** goes back to the
automatic copy. A project with a single repo and no `REPOS.json` is unchanged: nothing is copied or installed unless you pass `--setup`.
Secrets rule: the guard blocks agents from reading copied files in the worktree (the rows tagged "hidden from agents"); the build and the app
still load them.

**Every edit repo needs Conformance and a map; a reference repo does not.** The loop works in every listed edit repo, so each one needs what the primary one needs: a
`Conformance: STANDARD|LEGACY` line in its `CLAUDE.md` and, when it has code, a codebase map (`docs/codebase`). Without them the agents do not
know that repo's conventions. The loop `--check` reports a PROBLEM for every extra edit repo without a Conformance line. A repository added to the project (by the chat or by hand) opens the
**Set up `<repo>`** dialog: the same installer as the main repository (it detects a monorepo or a single project, then Conformance, then maps and the system map, then Done). For a **reference** repository the Conformance step is skipped and the map is
optional ("Skip, read the raw code", preselected); its missing map is only a WARNING and never blocks. In the Workspace dialog each repository's **Set up** / **Map** button opens the same dialog. In planning a **repos** gate appears
while an extra edit repo misses one of them; **Approve roadmap** stays blocked. Both steps are refused while a loop runs, and mapping is refused when the repo's worktree already exists.

**Commit setup files and push.** Setting up a repository writes files in YOUR folder, so it shows as changed. Per repository the Workspace dialog lists exactly the setup files that are uncommitted (the same ones the worktree carries), names the current branch and
its upstream and, behind a confirmation that names the branch, **commits only those files** (message `chore(setup): codebase maps and agent context`) and pushes the current branch to its upstream, never forced. It is refused (and says why) on `main`/`master`, on a detached HEAD,
while a loop or a job runs, when the branch has no upstream (run `git push -u origin <branch>` once) or when there is nothing to commit. Other changes in the folder are never staged. This is the only push the app ever makes to your branch. The old **Commit and push** / **Commit only** buttons
are gone: the legacy `commit-push` route now acts only on a loop worktree.

**Roadmap.** In a multi-repo project write `paths=<repo>:<path>` (no prefix = the primary repo) and, when one repo must come before another,
`repo-order=<repo>,<repo>`. The repo is the short name from the list; an unknown name is an error. In planning, the chat looks at the repo
catalog and **suggests** a repo for each feature; you confirm, and only then is the prefix written.

**Repo catalog.** A sheet per repo (what it does, with a file that proves each capability) kept in `~/.loop-harness/repo-catalog.json`. A scan
runs as a job (`POST /api/app/catalog/scan`). A capability whose evidence file does not exist is dropped. A repo with no `docs/codebase` is
**weak**: map it first. A repo whose HEAD moved since the scan is **stale**.

**Commits, push and PR.** The orchestrator makes one commit per repo for each verified feature (same slug). It never pushes. On your action
(Act, Deliver, or `harness.py deliver`) each repo gets its own push of the `loop/*` branch and one PR against the integration branch
(`developer` by default). Each PR lists the other PRs in a **Related PRs** block. If one repo fails, the others stay; fix it and deliver again.

**Close loop.** After delivery, the close cleans up: **Finish** in the navbar runs it (the same check and the same steps as the **Close loop** button in the workspace details of the panel). It stays disabled and says which repo blocks it: push not confirmed,
no PR (a compare link alone can be confirmed), uncommitted changes in the worktree, or a copied file changed. In order it (1) backs up each
worktree, (2) copies the durable plan back to the primary repo's `.specs/` (ROADMAP with statuses, FLOWS, STATE, feature specs, `phases/`,
`VALIDATE.md`, the summary), (3) writes `.specs/project/loop/json/last-run.json`, (4) removes the worktrees without `--force`. Local branches
and commits stay. A repo that cannot close is kept and the others still close. To continue later, start the loop again: if `last-run.json`
names a branch that still exists and the run was not **finished**, the worktree is **re-attached** to it instead of making a new branch.
A wrap-up **Finish** marks the run `finished: true` in `last-run.json`, so the next Start begins on a new `loop/<timestamp>` branch from
the current base (the Start dialog says which one will happen). What the safe close treats as engine state (never blocking): the loop's run
folder `.specs/project/loop/`, the git-ignored `.specs/project/.env.test` the engine rewrites on every start, committed clean `.specs` files,
the plan files already copied back and verified, and an ignored file identical to the repository's. Anything else blocks.

**Several loops.** The hub shows every running loop with stage, feature, turn, cost and any pending question. A loop cannot start on a repo
another running project already uses; the message names that project.

**What keeps it safe.** The orchestrator may write and commit only inside this project's listed worktrees (and the primary `.specs/`). Each
extra edit repo is given to `claude -p` with `--add-dir=<worktree>` and each reference repo with `--add-dir=<its folder>`. A turn runs only with the read tools of the MCP servers you configured (see Access below) and with `WebFetch`, `WebSearch`,
`NotebookEdit`, `ssh`, `scp` and `sftp` denied. `git push`, PR creation and release are blocked for every profile.

**Not covered.** Two loops on the same repo (beyond the cross-project check above). The secrets protection does not catch a broad `Grep` or
`Glob` over a worktree, only a call that names a copied file. Shell redirects are not confined to the worktrees.

## Monorepos (several projects in one repo)
When the repo holds several projects (for example `backend/` and `frontend/`), each subproject gets its own Conformance and map, and a
`docs/codebase/SYSTEM.md` at the root describes how they talk. A project with one stack changes nothing (it gets the two-step installer: **1 Conformance**, **2 Map**). In the planning screen (and in the **Set up `<repo>`** dialog of any repository of the project) the setup is an
**installer**: one card with the steps **1 Subprojects**, **2 Conformance**, **3 Maps**, a step indicator on top (a finished step has a ✓) and a footer with **Back** and **Next**; the footer says in one sentence what the step writes (step 1: only `SUBPROJECTS.json`; step 2: one Conformance block in each subproject's `CLAUDE.md`;
step 3: only `docs/codebase` of each subproject and `docs/codebase/SYSTEM.md`; never the code).
1. **Subprojects.** The panel looks at the folders for free (solution/project files, `package.json` workspaces, `apps/`, `packages/`, `services/`) and shows a simple list: name, folder, stack and a **Map / Skip** toggle (Map by default when the folder has code), with **Remove**, **Add a folder**, **Detect again** (asks first), **Validate with AI** (optional; one Claude session compares the list with the real layout and shows what it added, skipped, renamed or rejected; no spend cap) and **Not a monorepo** (the project goes back to the normal two-step installer). **Next** confirms the list (needs at least one project that is not skipped). Confirming also removes, by itself, the Conformance block an earlier version of the app wrote in the root `CLAUDE.md` (text outside its markers is never touched, you are never asked).
2. **Conformance.** One line per non-skipped subproject with STANDARD or LEGACY preselected from the suggestion (tagged "suggested"). **Next** saves every choice at once, each in that subproject's `CLAUDE.md`; the root has no Conformance. (For a reference repository this step is skipped.)
3. **Maps.** It lists what will be mapped (every non-skipped subproject with code and no map, plus the system map). One button, **Map N subprojects and the system**, asks one sentence ("Runs claude (Sonnet) on N subprojects in parallel, then the system map. It writes only docs/codebase; it never touches the code. No spend cap.") and then runs **one Claude job per subproject at the same time**, each in its own folder and writing only its own `docs/codebase/` and `CLAUDE.md`. Only map jobs on different subproject folders run together: any other job (the planning chat, a validation, the system map, the loop) waits, and a map waits for them. At most `max_parallel_maps` run at once (`~/.loop-harness/config.json`, default 6; the rest queue). When every map succeeded, the **system map starts by itself** and writes `SYSTEM.md`. Each line shows its own progress (running with elapsed time, documents written n/7, done, or failed with the reason and **Retry**; a failed map never stops the others, and after an app restart a map that was running shows as interrupted with **Retry**). The last button becomes **Continue to planning**.

**No spend cap on setup jobs.** The subproject maps, the system map, Validate with AI and the single-repository map run without `--max-budget-usd`: there is no budget field or dialog (an old client that still sends a budget is simply ignored). The loop's own limits (per turn, per loop) are unchanged.

**SYSTEM.md: Who talks to whom.** After the subproject list, `SYSTEM.md` has a section with one line per relation, `<consumer> -> <provider>: <how> (<env or base URL name>; <main routes or topics>)`, for example `frontend -> backend: HTTP REST (VITE_API_URL; /api/orders, /api/auth/login)`, followed by the shared resources (database, queue, cache) and which subprojects use each. The system map reads each subproject's map and code (API clients, env example files, route tables) and marks a guess `(inferred)`. It is marked older when the list changed after it. During the loop, features add their route/DTO/auth/env changes to `SYSTEM.md` between turns.

The map documents, each `CLAUDE.md` (the generated `<!-- maestro:context -->` block holds the Conformance, where the map is (listed once: the older `CODEBASE` block is only written in a `CLAUDE.md` that has no context block), build/run/test and the giant-file rule; the rest of the file is yours) and `SYSTEM.md`
are setup files: when the loop worktree is created, the ones your folder has uncommitted are **copied into it**, and you can also commit them from **Workspace > Commit setup files and push**. Readiness and `check` show them in the single "uncommitted changes" block per repository.

**Giant files.** The inventory marks a code file above `inventory_giant_lines` (1500) lines or `inventory_giant_kb` (200) KB (set both in `~/.loop-harness/config.json`). If a ROADMAP feature
touches one without a `refactor-*` feature in its `deps=` (or `note=giant-accepted`), the check warns, and the planning chat adds an open question proposing a `refactor-<slug>` feature (never silently; answer "not now" and it writes `note=giant-accepted`). Check one folder yourself:
`python <app>/harness.py inventory <folder> [--json]` (writes `.specs/project/loop/internal/inventory/<name>.json`; `--changed a,b` measures only those paths and writes nothing). Other CLIs of the same feature:
`python <app>/backend/harness_backend/engine/subprojects.py detect|show|for-paths <root>` and `.../system_map.py merge|pending <root> --slugs a,b`.

## Test environment tab: credentials and access
The panel's **Test environment** tab lists every `$NAME` of `TEST-ENVIRONMENT.md` with where it is defined (`env`, project `.env.test`,
machine `~/.loop-harness/.env`, `shared` or `missing`). **Values are never shown**: secret names appear as set or missing.
- **Save a value:** type it in the row and choose where it goes: the project's `.specs/project/.env.test` (git-ignored) or your user-level
  shared `~/.claude/skills/_shared/.env`. Other lines and comments in that file stay untouched.
- **Access card:** there are no checkboxes. The card explains what the agents can use. Credentials are read in 5 layers, first hit wins: (1) the
  environment variable, (2) the project `.specs/project/.env.test`, (3) machine `~/.loop-harness/.env`, (4) the plugin `shared/.env`, (5) your
  user-level `~/.claude/skills/_shared/.env` (your generic DEV account); layers 4 and 5 are **always read**. The card shows the layer each name
  comes from. The guard blocks the agents from reading or writing every one of these `.env` files. MCP read access: every one of `grafana`, `mcp-mysql-dev` and `mcp-mysql-prod` that is configured in your `~/.claude.json` is
  available to the planning chat and the loop agents, **read only**; one that is not configured is just not offered. An old `ACCESS.json` is
  ignored.
- **SQL and migrations.** MySQL through MCP is read only: a write is blocked by the guard. The agent writes the change as a `.sql` script in the
  feature folder and stops that feature with `NEEDS_CONFIRM` ("apply <script> on DEV, then answer"); you apply it and answer. **Migrations on DEV
  are allowed** (`dotnet ef migrations add`, `dotnet ef database update` against DEV...); the same command pointing at production is blocked.
- **Production database script.** When the loop delivers or you close it, and a feature added a migration or a `.sql` script, the app writes
  `~/Downloads/<project>-prod-database-<date>.sql` (idempotent migration script per repo, then the manual scripts). Review it and run it on
  production yourself before deploying; the loop never touches production.
- **PROJECT.md** gets a generated section (between `<!-- maestro:capabilities -->` markers) telling the agents the repositories and roles, what they
  may read and edit, and that commit, push and PR are reserved; it is rewritten when repositories or the roadmap change, so do not edit inside it.
- **What the AI sees:** the names and whether each is set (`credential-names.json`), never a value. It will not ask you for one in the chat:
  fill the tab instead.

## Email notification and variables (.env)
`.env` lookup order (model: `<app>/.env.example`; the `.env` never goes to the repository):
1. `LOOP_ENV_FILE` (explicit file: only that one is read);
2. `~/.loop-harness/.env` (or `LOOP_DATA_DIR/.env`);
3. `<plugin>/shared/.env` (the plugin that holds the orchestrator skill).

Without an explicit file, 2 and 3 are merged (the first one that defines a key wins). A real environment variable beats the `.env`. Main ones: `LOOP_NOTIFY_CHANNELS` (`console,toast,file,email,webhook`), `LOOP_NOTIFY_TO` (who receives the email), `LOOP_SMTP_*` (email server), `LOOP_WEBHOOK_URL`, `LOOP_GIT_USER`, `LOOP_INTEGRATION_BRANCH`.
- Channels: `email` (generic SMTP, full HTML format) and `webhook` (POST JSON with subject, body, event and project). `console`, `toast` (short text) and `file` are local. A new service = one function in `engine/notify.py` + one line in the `CHANNELS` table.
- **Every standard message carries the ways to continue:** (1) *loop via the script*: the `... --answer "..."` command highlighted (dark block in the HTML email, `>>` line in plain text); (2) *normal conversation in the chat* (Claude with `/orchestrator`): do not run the script, answer straight in the chat; (3) *panel*: the project URL, when the loop ensured the panel.
- **Every end of the loop notifies, without exception** (success, error, question, block, an exception of the script itself, a preflight that prevents the start, `--answer` without a question, lock, worktree). Only `--check`, `--dry-run` and `--rewind` do not notify. No channel is mandatory by default; list the ones that must be delivered in `LOOP_NOTIFY_REQUIRED` and, without them in `LOOP_NOTIFY_CHANNELS` and configured, the preflight **refuses to start** (`--allow-silent` for testing only). A mandatory channel that fails is retried once; if it keeps failing, the console shouts `mandatory notice NOT delivered`. A limit no one can cover: a script killed by force (terminal closed, power outage) cannot notify.
- Test the configuration: `python <app>/harness.py notify --test` (or **Test notification** in the hub).
- When the loop stops waiting for you, the email brings what was done, the question, what to check and **the ready resume command**: one per option and one with `--answer "WRITE YOUR MESSAGE HERE"`.

## Fallback to another account (`claude` -> `cc`)
When the account that runs the turns hits its usage limit, the loop can carry on with another command, for example `cc`, a wrapper signed in with a different account, instead of stopping.
- **Config:** `claude_fallback_binaries` in `config.json` (a list such as `["cc"]`, or the flag `--claude-fallback cc`). The main command is `claude_binary` / `--claude` (default `claude`). A fallback that is not on this machine's PATH is skipped (`--check` only warns).
- **Order of attempts:** the main command first, then each fallback in the listed order. The switch is sticky: after a limit the next turns stay on the new command, no turn is run twice on a limited one.
- **What counts as a usage limit:** a failed turn whose result text or stderr says 429, `usage limit`, `rate limit`, `limit reached`, `too many requests` or `quota`; or whose own rate limit data says so (a `rejected` status, a window at 100 %, an `api_error_status` of 429). `overloaded` (529) is a server problem, never a reason to switch.
- **Where the usage comes from.** Turns and the panel's jobs run `claude -p ... --output-format stream-json --verbose`. Claude Code prints a `rate_limit_event` in that stream for every account (browser login or token) with the 5-hour and the weekly window: how much is used and when each one resets. The Maestro reads it from the output of the turn that just ran: no extra call, no token read, no usage spent, and nothing from `claude-shared` or the `cc` wrapper is needed (the `[cc] conta:` and `[cc] reinicia:` stderr lines of `cc` stay optional extras). Each turn log (`turns/NNN.json`) keeps `usage_windows` (`five_hour` / `seven_day`, each `{pct, reset_at, reset_local}`) and `limit_status` (`allowed`, `allowed_warning`, `rejected`); `meta.json` `claude_usage` keeps the last reading per command (`claude`, or `cc:<account>` when the picker named one) as `{windows, status, read_at}`; the panel call log (`claude_calls.jsonl`) has the same two fields. A turn whose output had no such event leaves the earlier reading as it was.
- **What counts as an unusable account:** a failed turn whose output carries a specific sign-in or permission failure: `oauth_org_not_allowed`, `authentication_error`, `permission_error` next to `API Error`, `API Error: 401` / `API Error: 403`, `Invalid API key`, `OAuth token has expired` / `revoked`, `Please run /login`, `not logged in`, or `disabled Claude subscription access` (the organization blocks subscription access). A bare "401" or "403" never counts: a turn may be talking about the HTTP codes of the app it is working on, and a turn that succeeded never switches.
- **What the loop does with an unusable account:** it does not wait (waiting does not fix a login). It marks that command unusable for the run, runs the same turn again on the next command, counts the switch in `meta.json` (`claude_switches`, and `claude_unusable`: a list of `{command, account, reason, turn}`), and never picks that command again in this run, not even when the main account's limit reset passes. The `cc` picker cannot yet leave one account out, so an unusable account on `cc` makes the whole `cc` command unusable for the run (excluding just that account is planned). When no command is left, the run stops with `error` naming each command, its account and the reason, and what to do: sign in again (`claude` then /login, or `claude setup-token`), ask the admin to enable Claude access, or switch to another account.
- **`cc` picker retry:** `cc` has several accounts and a picker. When the account it chose runs out during the turn, the loop runs the same turn again, once per `cc` account; each run the picker queries the fresh usage and picks the account with the least used 5-hour window (ties: the least used weekly window).
- **The blocking window.** The window at 100 % (or the one a `rejected` status names) is the one that stopped the account; when both are full, the one that frees up last. Every limit notice names it: `5-hour window, back at 17:05` or `weekly window, back on Sun 25/10 12:00`. When the output carries no window data, the notice keeps its older text and the reset comes from `[cc] reinicia:` / `limit reached|<epoch>` when present.
- **Weekly alert.** When the MAIN command's weekly window is the one that ran out, the loop sends ONE notice per run on every configured channel: `main account weekly limit reached, unusable until Sun 25/10 12:00; the loop continues on `cc``, with the last known 5-hour and weekly usage of every other command or account in the body (`usage unknown` for one that has not run yet). It is sent again only if the reset moment changes. A 5-hour limit never raises it.
- **Every command limited:** the loop waits until the first known reset plus 2 minutes (cap 8 days; when no reset time is known, `--limit-wait` minutes), up to `--limit-retries` times, then starts again from the main command. A pause in the panel interrupts the wait.
- **Back to the main command without waiting for the others:** when the main command is the one that hit the limit, the loop remembers the reset moment of its blocking window (the weekly reset when the week ran out, so it never comes back before that; unknown: now + `--limit-wait` minutes). Before each turn, if it is on a fallback and that moment has passed, it goes back to the main command. If the main command hits the limit again, the normal fallback applies.
- **`.cmd` / `.bat` wrappers** get the prompt through stdin (a multi-line prompt would break in `cmd.exe` arguments).
- **Which command and account ran:** the turn log (`turns/NNN.json`: `claude`, `claude_account`), the CLAUDE block in the panel's turn header, and `python harness.py doctor`.
- **Where the panel shows it.** The chip at the top of the loop zone ("claude · dev@x.com · subscription, browser login (iData, team)", "cc · cc account claude2 · subscription, sign-in token · 2 switches"; an API key is drawn as a warning) with a "usage" button that opens the turns, panel calls and cost per sign-in method and per command. Every turn card, the turn header, the pipeline nodes, the feature view and the agent view carry a compact badge ("claude · login", "cc · token", "API key"); an agent's badge is inherited from its turn, and its tooltip spells the sign-in out ("subscription · browser login", "subscription · sign-in token", "API key · billed per token"). The panel's own calls (chat, roadmap, map...) are listed with their command and sign-in in the call log (`GET jobs`, `calls`).
- **Who spawns the agents.** The orchestrator (Opus, one session per turn) is always the one that spawns every agent (spec-writer, spec-reviewer, executors, verifiers, mappers): no agent spawns another agent. Executors go in waves of up to `Max parallel tasks` (ROADMAP header, default 3, 1 to 5); fewer when the feature has fewer independent tasks or when the turn budget or minutes are running short. Every agent of a turn runs inside that turn's process, so it uses the turn's account and sign-in method: when the loop moves from `claude` to `cc`, the agents of the next turn move with it. The "?" next to the pipeline legend and next to the task graph of the feature view shows the same note.
- **Sign-in methods**, as `claude auth status` reports them: *browser login* (`claude.ai`) and *sign-in token* (`claude setup-token`) are both billed to the **subscription**; only an *API key* is billed per token. The notices and the panel name the method in these words.
- **Notices sent.** The subject says which account the loop is on now and how it signs in. The body names both accounts (now and before) with the e-mail or `cc` account, the method, the organization and the plan, and why it changed:

| Notice (subject) | When | Channels |
|---|---|---|
| `now on the backup account: cc account claude2 (subscription, sign-in token)` | the main command hit its limit and the loop moved to `cc`; the body names the window (`5-hour window, back at 17:05`) | every configured channel |
| `main account unusable, now on the backup account: cc account claude2 (subscription, sign-in token)` | the main account cannot sign in (logged out, revoked token, organization without access) and the loop moved to `cc`; the body carries the reason (redacted) and what to do. It needs a human, so it goes everywhere | every configured channel |
| `back on the main account: dev@x.com (subscription, browser login)` | the main account's reset moment passed and the loop went back to it | every configured channel |
| `cc switched to cc account claude3 (subscription, sign-in token)` | the `cc` picker moved to another account of the same command | local only: console, toast, file |
| `credits ran out (5-hour window, back at 17:05), back in 3h 21min` | every command is limited; names the window that frees up first and carries the reset time | every configured channel |
| `main account weekly limit reached, unusable until Sun 25/10 12:00; the loop continues on `cc`` | the main command's weekly window ran out; once per run; the body lists the other commands' last known usage | every configured channel |
| `spend at 80 % of the cap` | the cost cap is close | every configured channel |

The email provider (`LOOP_EMAIL_PROVIDER`) can be `smtp`, `http` or a local extension such as `backmail` (`engine/local_ext`, git-ignored). The `LOOP_SMTP_*` variables are ignored when another provider is chosen.

## Rewind and safety caps
- `--max-cost N` (default US$ 50; `0` turns it off): when the spend summed over all turns passes the cap (or a turn would start over it) the loop PAUSES (`paused`, exit 10, resumable) instead of failing, with one notice at 80 %. Raise the cap of the run with `POST loop/raise-limit` (the status dialog) and it continues; the spent total is kept in `meta.json` and never reset.
- On every turn the script validates the state (`loop_state.py`) and keeps a snapshot in `.specs/project/loop/internal/snapshots/NNN/`. If a turn leaves the state invalid, the loop stops and says which `--rewind` to use.
- `python <app>/harness.py loop <repo> --rewind 3` shows the plan (which commits leave the loop branch); with `--yes` it really goes back: restores ROADMAP/STATE/LOOP/ANSWERS/RULES and runs `git reset --hard` on the loop branch (the commits stay in the reflog). Then run the normal loop command to continue from turn 4. This only touches the loop worktree, never your repo.
- `python <app>/harness.py state graph <worktree>` prints the state graph (Mermaid) with the features in each node.

## Before running: what the repository needs
The check verifies almost all of this and warns about what is missing:
- **A `.gitignore` ignoring the build folders** (`bin/`, `obj/`, `node_modules/`, `dist/`). Without it the build leaves files behind, the tree looks dirty and the loop stops with "dirty tree".
- **Everything committed.** The worktree is born from the last commit; a loose change in the repo does not go along.
- **`Conformance: STANDARD|LEGACY` in `CLAUDE.md`** and **an approved `.specs/project/ROADMAP.md`** (Approve roadmap button, which also needs a flow per feature in `.specs/project/FLOWS.md`).
- **The mandatory notify channels configured** (when `LOOP_NOTIFY_REQUIRED` lists any), otherwise the preflight refuses to start.
- **The orchestrator skill reachable** (`SKILL.md` + `hooks/guard.py`, plugin `hooks/hooks.json`): `harness.py doctor` checks it.
- **.NET:** `dotnet` on PATH; the build restores on its own (needs network or the NuGet cache). **Node:** `--setup "npm ci"` if there are dependencies.

## When it stops and what to do
| Stopped on | You do |
|---|---|
| `done` | Wrap up with **Finish** in the panel (deliver, close, optional follow-up). Read the summary. The feature branch is already on the remote and the **PR link** to the integration branch comes in the notice and in the summary (the "For you to validate" section lists what was decided without stopping you). With `--no-push`, the branch stays in the worktree. |
| `awaiting_user` | Answer in the panel, or run the command from the email with `--answer "..."`. If you prefer to talk: **answer in a normal conversation opened INSIDE the worktree** (`cd ../<project>-worktree-loop` and `claude`, then `/orchestrator`); then run the same loop command again (it reuses the worktree and keeps counting turns). |
| `paused` | The spend limit was reached (`cost cap reached: US$ x of y`): raise the limit in the status dialog and it continues, or use Finish / Back to planning. A critical decision shows as `awaiting_user` instead: keep it or ask for a change. |
| `error` | The reason comes in the notice (permission denied, timeout, usage limit, stuck state, dirty tree, 10 turns without progress). Fix the cause and run again (or **Try again** in the panel). |

Golden rule: **once the worktree exists, the truth of the state is in it.** Changed the roadmap or a rule in the original repo? That does not reach the worktree. Change it there.

## Headless guard
The engine exports `LOOP_HEADLESS=1` to the loop session. In it every `git push` and every `gh` command is blocked by the guard for every caller (rules `headless-push` and `headless-gh`, hard blocks): the session only commits, and delivery belongs to the engine (`harness.py deliver` / Finish). The main session, which has no `agent_type`, gets the `orchestrator` profile instead of the permissive one. Executors dispatched per task (`SCOPE_TASK: <slug>/<Tn>`) may edit only the files of that task (`SCOPE.json` `tasks`), falling back to the feature's files.

**Turn environment.** The engine reads the app `.env` into its own process, but the loop turn does not get the secrets from it: a variable whose value came from the app `.env` and whose name contains `PASSWORD`, `PASSWD`, `SECRET`, `TOKEN`, `API_KEY`, `APIKEY`, `PRIVATE_KEY`, `WEBHOOK` or `CREDENTIAL` is left out (notices are sent by the engine, not by the turn). A real variable you had with a different value, `TEST_USER_*` and any `$NAME` / `${NAME}` written in the project's `TEST-ENVIRONMENT.md` stay. On the first turn of a run the log shows `[loop] turn environment: N secret-looking variable(s) from the app .env left out (NAMES)`: names only, never a value. If the filter fails, the turn gets the full environment and one warning is printed.

**Your own secret paths.** `GUARD_SECRET_PATHS` (a variable or a line in the app `.env`, paths separated by commas) adds files and folders the guard treats like the built-in credential files: reading or changing them from the loop is blocked by the `secrets` rule. Use it for private keys or credential files specific to your machine.

## Isolation and access (what a `claude -p` of the app can reach)
Every `claude -p` the app starts (loop turns, correction and resume turns, planning, import, chat, follow-up, quarantine summary, roadmap, map jobs) runs **isolated from your Claude profile**: `--setting-sources project,local` plus a bundle with only the 8 plugin agents and the allowlisted skills. Your user plugins, your other skills, your hooks and plugin MCP servers do not load; the project's own `CLAUDE.md` still does, and `~/.claude/CLAUDE.md` does not. The guard hook rides along through `--settings`, and in this mode it also allows only the MCP tools and skills of each profile. How it is built: `ARCHITECTURE.md`, "Runtime isolation". The rules: `MONITOR.md`, "Guard and access".

**See it.** Read only, CLI only (nothing in the panel):
```
python <app>/harness.py doctor --access [--project <repo>] [--json] [--no-probe]
```
- *static part:* the MCP servers found in your `~/.claude.json` (read tools only), the MCP tools each guard profile may call, the skills allowlist per profile, the bundle (path, agents, skills, and the **skipped** skills that exist neither in `~/.claude/skills` nor in the plugin) and whether isolation is on;
- *probe:* the real isolated argv with the prompt `reply only: ok`, `--model haiku` and `--max-budget-usd 0.05`; the report reads only the `system/init` event (tools, MCP servers, agents, skills, plugins the process loaded) and kills the process. An account that is refused still prints `init` first, so the probe then costs nothing. Anything outside the allowlists prints as `FLAG: ...`; no flags means everything loaded is inside them. `--no-probe` skips the call (no `claude` is started);
- *guard.log:* the last 200 lines of the project's `guard.log` counted by decision, rule and agent, and the last 10 denials.

It always exits 0: it is a report, not a gate.

**Skills the loop may load.** The defaults per profile live in `engine/runtime_isolation.py` (`DEFAULT_ALLOWLIST`). To let every profile load an extra skill (for example your company style guide), list it in `~/.loop-harness/config.json`:
```json
{"loop_extra_skills": ["idata-guia-estilo"]}
```
The skill must exist in `~/.claude/skills/<name>` or in `plugin/skills/<name>`; the next `claude -p` rebuilds the bundle by itself (it rebuilds only when a source changed). A skill named in `.specs/project/SUBPROJECTS.json` (`skill` of a subproject) is added to the `spec` and `executor` profiles of that project without any config. `doctor --access` lists what ended up in the bundle and what was skipped. A skill that is in the bundle but not on a profile's list is still denied for that profile by the guard (`[skill-allowlist]`).

**Turn it off.** `"loop_isolation": false` in `~/.loop-harness/config.json` gives back the old argv exactly: your whole profile loads again, and the skill allowlist is inactive (no `LOOP_SKILLS_FILE`); the MCP allowlist still applies inside the headless loop turns (`LOOP_HEADLESS=1`), and the rest of the guard keeps working. Use it only to compare or to debug a skill that does not load; `doctor --access` prints `isolation OFF` then. Default is on.

**A chat send is instant.** The panel clears the input at submit time and the server answers before the heavy work: the safety snapshot, the prerequisites check and the prompt are built in the job thread. A prerequisite that fails there ends the job as an error with the code `planning_prerequisites` and the panel shows the same message the old refusal gave. While it runs the job record says `progress: "preparing the safety snapshot"`.

## Push identity
The push uses the git identity configured on the machine. To require a specific account: `--git-user <account>` or `LOOP_GIT_USER=<account>` in the `.env`. The check verifies it and also reports a plugin hook that points the guard at a path that does not exist on the machine.

## App tests
```
python -m unittest discover -s backend/tests/engine -t backend   # loop engine (fake claude, fake plugin with the guard copied)
python -m unittest discover -s backend/tests/infra -t backend    # processes, claude, git, AppController, harness.py
python -m unittest discover -s backend/tests -t backend          # everything
```
The tests never use the real `claude` nor touch `~/.claude` or `~/.loop-harness`: they use a temporary `LOOP_DATA_DIR` and `LOOP_SKILL_DIR` pointing at a fake plugin with the public plugin's `guard.py` copied (without the plugin guard in this clone, the tests that depend on it fail with a warning).

## Do not use it when
A single task, a small adjustment, or when you want to follow and decide at each step: in those cases use the normal `/orchestrator` (`step` or `feature`).
