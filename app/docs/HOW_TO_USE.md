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
- **What a turn is:** a new `claude -p "/orchestrator continue --headless --turn N ..."` (clean context) that runs one wave of features: writes the spec, passes the gate, implements, verifies, commits and writes the state in `.specs/`.
- **What `--wave N` is:** how many features move together per turn. Only features that edit different files and do not depend on each other move together. A feature with `regression=yes` moves alone.
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
   - **Approve roadmap** stays disabled (and the back end refuses it) until every gate is green: prerequisites, the PROJECT goal without `FILL_IN`, at least one feature with no grammar or field error, `## Open questions` at `- none`, and in `FLOWS.md` a flow of at least 2 steps for every feature. Until then the loop, the chat with the agents, Act and the whiteboard are not reachable.
   - `FLOWS.md` holds, per feature (`## [slug] Title`), the numbered steps and `Expected:` results that prove it works; the flow validation (`verifier-flow`, `--e2e`) uses it. `/orchestrator roadmap` writes it too.
4. **Test environment:** the orchestrator gathers headers, DEV users, URLs and the secret manager key of each secret (name, environment, class that reads it) in `.specs/project/TEST-ENVIRONMENT.md` (template in `<app>/templates/TEST_ENVIRONMENT.md`) and asks you, in one batch, only what is missing. This comes **before** the approval because it is what stops the loop most.
5. Answer the open questions of the specs.
6. When all of that is approved, the orchestrator **stops and hands you the ready command**, in a single line.

## Part 2 - the panel (the default way)
```
python <app>/harness.py serve --project <project-folder>
```
- Starts the app (only `127.0.0.1`, a free port from 47800 to 47899), prints `MONITOR_URL=<project panel url>` and `APP_URL=<hub url>` and opens the browser. If the app **is already up**, it only registers the project in it, prints the URL and exits (one app for every project).
- Flags: `--port N` (fixed port), `--idle-hours H` (hours without use until it shuts down on its own; `0` = never; default 2), `--no-browser`, `--data-dir D` (default `~/.loop-harness` or `LOOP_DATA_DIR`).
- The hub (`APP_URL`) lists the projects, opens an existing folder, creates a project from scratch (folder + `git init` + `.specs/`, landing in the planning screen) and shows the diagnostics.
- The project panel (`/p/<id>/`) shows roadmap, turns, agents, specs, questions, validations, cost and limits, and has the loop buttons.

The loop also ensures the panel on its own: when it starts, `run_loop` reuses the panel that launched it (`LOOP_MONITOR_URL`), or the work's live `MONITOR.json`, or registers the project in the live app (`app.json`), or starts `harness.py serve --project <repo>` detached. It prints `[loop] monitor: <url>` and the URL goes in every notice.

### In the panel
- **Follow:** loop status, features, agents, cost and limits.
- **Guard:** the **Guard · PreToolUse** feed under the inspector lists the last tool calls the guard blocked (time, profile, rule,
  detail, `exit 2`) with a count per rule, and the header shows a red `guard · N blocked` badge when there are any. In an
  agent's log, a blocked call shows up as a red `blocked by the guard · <rule>` item with the reason.
- **Check / Start loop:** **Check** = `harness.py check`; **Start loop** is you starting the loop (Claude never starts it through the panel).
- **Answer:** when the loop stops at `awaiting_user`, click an option or write the answer. The panel relaunches the loop with your answer (the same as `--answer`). **Resume** / **Try again** relaunch without an answer (`error`, `stopped` or pause). **Add note** leaves a note for the next turn to apply without stopping the loop. **Pause** makes the loop stop before the next turn (the turn in progress finishes first).
- **Act (deliver and summary):** pick `Deliver` (push of the feature branch only + PR to the integration branch, `LOOP_INTEGRATION_BRANCH`) or `Summary`, or describe it in free text; the panel shows what it will do and only runs it after you **confirm**. Only with the loop stopped and no open feature; a protected branch, force and the guard rules still refuse. Your click is the authorization; Claude never triggers it.
- **Chat with the AI and instruct the loop:**
  1. Click an agent (or the turn you want to understand) and open the **Chat** tab.
  2. **Ask** only clears doubts (the AI reads the files and answers, changing nothing; read-only tools, no MCP; up to US$ 1 per answer). **Instruct the loop** (default on the current version) sends a request the loop applies. On an old turn only Ask exists; use "Bring to the current version" to turn it into an instruction.
  3. The answer says what happened: **note** = the loop is running and applies it on the next turn; **answer** = the loop was waiting for you and was relaunched with the text; **note+resume** = the loop was stopped, in error or done and was relaunched to apply it.
  4. A request that adds a feature or changes the approved roadmap is not applied on its own: the loop stops and asks, with the recommended option marked.
- **Shut down:** button in the hub (`POST /api/app/shutdown`). On its own the app only shuts down after `--idle-hours` without use and with **no** loop running (nor chat/roadmap in progress). It **does not die when the loop ends**, on purpose. Without a live app, `harness.py serve --project <repo>` brings the panel back.
- **Do not want the panel:** `--no-monitor` on the loop command (or `LOOP_MONITOR=off` in the `.env` for every loop). `LOOP_MONITOR_OPEN=off` only avoids opening the browser. If the panel does not start, the loop carries on normally and says so in the console.
- Security: only `127.0.0.1`, Host checked and a token on every method that changes state (the token is in `app.json`, readable only by your user).

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
It says `[check] READY. Command:` or lists `[check] PROBLEM:` lines (no roadmap, roadmap not approved, no Conformance, the skill guard not found or not blocking `git push`, push without a remote with an explicit `--push`, etc.) and `[check] WARNING:` lines (uncommitted changes in the repo, a Node project without `--setup`). When it says READY, it prints the full command (`python "<app>/harness.py" loop "<repo>" ...`).

**Step 2 - start:**
```
python <app>/harness.py loop <project-folder> --wave 1 --setup "npm ci"
```
`harness.py loop` forwards everything to `backend/harness_backend/engine/run_loop.py`.

| Flag | What for | Default |
|---|---|---|
| `--wave N` | features per turn (only with disjoint PATHS). It is the way to get parallelism: the orchestrator designs the roadmap with disjoint `paths` and suggests N (2 or 3). A collision becomes a queue | 1 |
| `--e2e` | final flow-validation phase (headless Playwright) | off |
| `--no-push` | turns off the final push of the feature branch + PR to the integration branch (`--push` only requires it to work in the preflight) | **on** |
| `--pause-on-blocked` | quarantine always pauses the loop (default: only pauses if another feature depends on the blocked one) | off |
| `--continue-on-blocked` | a quarantined feature (2 failures) does NOT pause the loop; it carries on with the others | off |
| `--setup "cmd"` | one-time command in the new worktree (e.g. `npm ci`) | none |
| `--budget N` | max US$ per turn | 5 |
| `--max-cost N` | cap of US$ summed over all turns (`0` turns it off) | 30 |
| `--timeout N` | max minutes per turn | 60 |
| `--max-stall N` | consecutive turns without progress before stopping | 10 |
| `--branch name` | name of the loop branch | `loop/<date-time>` |
| `--answer "..."` / `--option N` | answers the question that stopped the loop and continues | - |
| `--rewind N [--yes]` | goes back to the state right after turn N (without `--yes` it only shows the plan) | - |
| `--summary-dir D` | folder of the final summary | `loop-summary/` in the current folder (the panel passes the app's output folder) |
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
| `harness.py doctor [--json]` | - | diagnostics (the same as `GET /api/app/info`) |

## What happens underneath
- The script creates **once** the worktree `../<project>-worktree-loop` (its own) on a `loop/<date>` branch and copies `.specs/` and `CLAUDE.md` there. Your folder and your branch are not touched.
- Each turn is a new `claude -p` (clean context) that reads `.specs/`, advances one wave, commits and writes `.specs/project/loop/json/LOOP.json`. Pushing is impossible inside the turn (the skill guard + `pushurl` disabled only for that process); the final delivery is made by the script.
- The guard is the skill's (`<skill>/hooks/guard.py`). The check verifies it exists and blocks `git push`, and that the plugin's `hooks/hooks.json` points (through `${CLAUDE_PLUGIN_ROOT}`) at a guard that exists on this machine.
- Everything the loop owns lives in `.specs/project/loop/`: the files a person reads at its root (`ANSWERS.md`, `VALIDATE.md`, `DEPENDENCIES.md`, `GRAPH.md`, `NOTICE.txt`), machine state in `json/` and the rest (lock, logs, turn logs, snapshots) in `internal/`. A project written by an older layout is moved there automatically (`harness.py state migrate <root>` does it by hand; it never overwrites).
- When it stops: console, Windows toast, `.specs/project/loop/NOTICE.txt`, the panel and the **summary `loop-summary-<project>-<date>.md`**.

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

## Rewind and safety caps
- `--max-cost N` (default US$ 30; `0` turns it off): stops with an error if the spend summed over all turns passes the cap.
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
| `done` | Read the summary. The feature branch is already on the remote and the **PR link** to the integration branch comes in the notice and in the summary (the "For you to validate" section lists what was decided without stopping you). With `--no-push`, the branch stays in the worktree. |
| `awaiting_user` | Answer in the panel, or run the command from the email with `--answer "..."`. If you prefer to talk: **answer in a normal conversation opened INSIDE the worktree** (`cd ../<project>-worktree-loop` and `claude`, then `/orchestrator`); then run the same loop command again (it reuses the worktree and keeps counting turns). |
| `error` | The reason comes in the notice (permission denied, timeout, usage limit, stuck state, dirty tree, 10 turns without progress). Fix the cause and run again (or **Try again** in the panel). |

Golden rule: **once the worktree exists, the truth of the state is in it.** Changed the roadmap or a rule in the original repo? That does not reach the worktree. Change it there.

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
