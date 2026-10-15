# How to use the unsupervised loop (step by step)

Core idea: **you decide, the loop executes.** What needs judgment (context, roadmap, rules, STANDARD or LEGACY, what is in and what is out) happens in a normal conversation with `/orchestrator`. Only after your approval does the script take over and repeat rounds until it finishes or gets stuck. You do not type loop commands or follow it step by step.

## Part 1 - normal conversation (you take part)

Open Claude in the project folder and use `/orchestrator` as usual:

1. Context and mapping (`/orchestrator map` in an existing repo).
2. Define `Conformance: STANDARD` or `LEGACY` (the orchestrator asks, explains why and saves it in `CLAUDE.md`).
3. Roadmap: the orchestrator proposes, you adjust and **approve**. Limiting rules (`RULES.md`) enter here, with your phrase.
4. Answer the specs' open questions.
5. When all of that is approved, the orchestrator **stops and hands you the ready command** (see "Handoff" in `SKILL.md`). If you prefer, you can run the `--check` from step 2 yourself.

Tip: when approving, say `autopilot` so it is recorded in `STATE.md`.

## Part 2 - the script (you only trigger it)

The script runs **from inside the plugin**: `${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/run_loop.py`. Run it in a **separate** terminal from the conversation, outside Claude.

**Step 1 - check (creates nothing, spends nothing):**
```
python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/run_loop.py" <project-folder> --check
```
It says `READY` (ready) or lists `PROBLEM`s (no roadmap, no Conformance, guard down, push without remote (explicit `--push`), etc.) and `WARNING`s (uncommitted change in the repo, Node project without `--setup`). When it says `READY`, it prints the full command.

**Step 2 - trigger (the command `--check` printed):**
```
python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/run_loop.py" <project-folder> --wave 1 --setup "npm ci"
```
| Flag | Purpose | Default |
|---|---|---|
| `--wave N` | features per round (only with disjoint PATHS). It is the way to get parallelism: the orchestrator designs the roadmap with disjoint `paths` and suggests N (2 or 3). A collision becomes a queue | 1 |
| `--e2e` | final flow-validation phase (headless Playwright) | off |
| `--no-push` | turns off the end-of-flow push of the feature branch + PR to `developer` (`--push` only requires that this works in the preflight) | **on** |
| `--pause-on-blocked` | quarantine always pauses the loop (default: only pauses if another feature depends on the blocked one) | off |
| `--setup "cmd"` | a single command in the new worktree (e.g. `npm ci`) | none |
| `--budget N` | max US$ per round | 5 |
| `--timeout N` | max minutes per round | 60 |
| `--max-stall N` | consecutive rounds without progress before stopping | 10 |
| `--branch name` | name of the loop branch | `loop/<date-time>` |
| `--continue-on-blocked` | a feature in quarantine (2 failures) does NOT pause the loop; continues with the others | off (by default it pauses and notifies) |

You can leave it running and walk away. There is nothing to answer: if it needs you, it **stops and notifies**.

## What happens underneath
- The script creates **once** the worktree `../<project>-worktree-loop` (its own) on a branch `loop/<date>` and copies `.specs/` and `CLAUDE.md` into it. Your folder and your branch are not touched.
- Each round is a new `claude -p` (clean context) that reads `.specs/`, advances one wave, commits and writes `.specs/project/LOOP.json`.
- When it stops: console, OS toast, `.specs/loop-logs/NOTICE.txt` and the **summary file `loop-summary-<project>-<date>.md`** (written to the output folder of `loop_summary.py`, `--out`).

## Notifications and variables (.env)
Variables live in **`${CLAUDE_PLUGIN_ROOT}/shared/.env`** (template: `.env.example`; the `.env` never goes to the repo). Main ones: `LOOP_NOTIFY_TO` (who receives), `LOOP_NOTIFY_CHANNELS` (`console,toast,file,email,webhook`), plus the settings of each channel you enable (e.g. mail server or webhook URL and token), `LOOP_GIT_USER`. A real environment variable beats the `.env`.
- Channels: `console`, `toast` and `file` are local; `email` (full format) and `webhook` (short message) are remote. The notifier is pluggable: a new channel = one function in `notify.py` + one line in the `CHANNELS` table.
- **Every default message carries both ways to continue:** (1) *loop through the script*: the `run_loop.py ... --answer "..."` command highlighted (dark block in the HTML e-mail, monospaced block in short messages, a `>>` line in plain text); (2) *normal chat conversation* (Claude with `/orchestrator`): do not run the script, answer directly in the chat and the orchestrator operates normally.
- **Every end of the loop notifies, without exception** (success, error, question, block, exception of the script itself, a preflight that prevents starting, `--answer` without a question, lock, worktree): `finish()` covers the normal stops and `entry()` covers the others. Only `--check`, `--dry-run` and `--rewind` do not notify. Required channels by default: **toast, e-mail and webhook** (change with `LOOP_NOTIFY_REQUIRED` in `shared/.env`; empty = none). Without them in `LOOP_NOTIFY_CHANNELS` and configured, the preflight **refuses to start** (`--allow-silent` only for testing). A required channel that fails is retried once; if it keeps failing, the console shouts `required notification NOT delivered`. A limit that cannot be covered: a script killed by force (terminal closed, power loss) cannot notify.
- Test the configuration: `python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/notify.py --test` (sends a test notification through all configured channels; `--channels webhook` limits it to one).
- When the loop stops waiting for you, the e-mail carries what was done, the question, what to check and **the ready resume command**: one per option (e.g. `--answer "yes"`, `--answer "no"`) and one with `--answer "WRITE YOUR MESSAGE HERE"`. Replace the text with your message, run it, and the loop continues from where it stopped.

## Going back in time and safety ceilings
- `--max-cost N` (default US$ 30; `0` turns it off): stops with an error if the combined spend of all rounds passes the ceiling.
- Each round the script validates the state (`loop_state.py`) and keeps a snapshot in `.specs/loop-logs/snapshots/NNN/`. If a round leaves the state invalid, the loop stops and says which `--rewind` to use.
- `python .../run_loop.py <repo> --rewind 3` shows the plan (which commits leave the loop branch); with `--yes` it really goes back: restores ROADMAP/STATE/LOOP/ANSWERS/RULES and does `git reset --hard` of the loop branch (the commits stay in the reflog). Then run the normal loop command to continue from round 4. This only touches the loop worktree, never your repo.
- `python .../loop_state.py graph <worktree>` prints the state graph (Mermaid) with the features at each node.

## Before running: what the repository needs to have
`--check` verifies almost all of this and warns about what is missing:
- **`.gitignore` ignoring the build folders** (`bin/`, `obj/`, `node_modules/`, `dist/`). The loop builds in the worktree and, each round, checks that the tree is clean; without the `.gitignore` the build leaves garbage, the tree looks dirty and the loop stops with "dirty tree". A repo created by Visual Studio or Vite usually already has it.
- **Everything committed.** The worktree is born from the last commit; a loose change in the repo does not go along.
- **`Conformance: STANDARD|LEGACY` in `CLAUDE.md`** and **an approved `.specs/project/ROADMAP.md`** (done in the normal conversation).
- **Notification channels configured** (toast, e-mail and webhook in `shared/.env`), otherwise the preflight refuses to start.
- **.NET:** `dotnet` on the PATH; the build restores by itself (needs network or the NuGet cache). **Node:** `--setup "npm ci"` if there are dependencies.

## When it stops and what to do
| Stopped by | You do |
|---|---|
| `done` | Read the summary. The feature branch is already on the remote and the **PR link** to `developer` comes in the notification and the summary (the "For you to validate" section lists what was decided without stopping you). With `--no-push`, the branch stays in the worktree. |
| `awaiting_user` | Run the e-mail's command with `--answer "..."` (the simplest way). If you prefer to talk: **answer in a normal conversation opened INSIDE the worktree** (`cd ..\<project>-worktree-loop` and `claude`, then `/orchestrator`); then run the same script command again (it reuses the worktree and continues the round count). |
| `error` | The reason comes in the notification (permission denied, timeout, usage limit, stuck state, dirty tree, 10 rounds without progress). Fix the cause and run again. |

Golden rule: **once the worktree exists, the truth about the state is in it.** Changed the roadmap or a rule in the original repo? That does not reach the worktree. Change it there.

## Running from elsewhere
The default is to run from inside the plugin. A loose copy (e.g. in a downloads folder) works if `run_loop.py` and `loop_summary.py` stay together and the plugin has the skill, the agents and the guard; the source is always `skills/orchestrator/loop/`.

## Push identity
The push uses the git identity configured on the machine. To require a specific account: `--git-user <name>` or `LOOP_GIT_USER=<name>` in `shared/.env`. `--check` verifies this and also flags an agent hook pointing the guard to a path that does not exist on the machine.

## Without Claude
`python loop_summary.py <folder>` generates the summary at any time, only reading files and the `git log`.

## Do not use when
A single task, a small tweak, or when you want to follow and decide at every step: in those cases use the normal `/orchestrator` (`step` or `feature`).
