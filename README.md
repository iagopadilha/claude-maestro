# loop-engineering-harness

A Claude Code plugin that turns one chat into an **orchestrator**: roadmap → spec → review gate → implement → verify, executed by Sonnet-capped subagents, with all state kept on disk. A deterministic Python script can then run the same loop unattended, one fresh `claude -p` per turn.

## What you get

- **`orchestrator` skill**: the loop itself. It decides, dispatches subagents, writes state to `.specs/`, and stops at a checkpoint after each stage with a short report and batched questions. Partial use is supported: map only, roadmap only, one stage, one feature.
- **Satellite skills**: `orchestrator-create-block-rule` (turn a "never do X" into a tested guard rule), `orchestrator-audit-dependencies` (find forgotten `deps=` from real imports and specs), `orchestrator-show-progress-graph` (progress as text or Mermaid).
- **`tlc-spec-driven`**: the specify → design → tasks → execute engine the orchestrator drives.
- **Subagents**: `spec-writer`, `spec-reviewer`, `executor-back`, `executor-front`, `executor-generic`, `verifier`, `verifier-flow`, `brownfield-mapper`.
- **`flow-validator`**: validates a flow end to end against the running app (curl routes and Playwright E2E).
- **`backend-dev` and `frontend-dev`**: generic best-practice skills (layers, naming, SOLID, clean code, regression proof, delivery format).
- **Guard hook**: a `PreToolUse` hook that blocks pushes, writes to protected state, credentials and production writes for the subagents.
- **Unattended loop script** with a worktree of its own, cost ceilings, rewind, quarantine and notifications.

## Guarantees

- **Model ceiling: Sonnet.** No subagent is ever promoted to a larger model, not even after a failure. A failed feature goes up to the orchestrator and to you.
- **Git belongs to the orchestrator.** Subagents never run git. The loop pushes only the feature branch, only at the end, and opens a PR against the integration branch (default `developer`). Pushing to the integration branch or `main`, and force pushes, are blocked by the guard.
- **State on disk.** `.specs/project/` holds `ROADMAP.md` (index), `STATE.md` (only the present), `phases/stage-NN.md` (closing notes) and `archive/`. Any turn can resume from disk.
- **Guard is scoped.** The plugin guard acts only on this plugin's subagents (it reads `agent_type`) and the orchestrator skill. Your other Claude Code work is not touched.

## Install

```
/plugin marketplace add iagopadilha/loop-engineering-harness
/plugin install loop-engineering-harness@loop-engineering-harness
```

Requires Python 3 on `PATH` (the guard and the loop script are Python) and Git. The `gh` CLI is needed for the final PR.

## Quick start

1. In your project folder run `/orchestrator map` to document an unfamiliar codebase (writes `.specs/codebase/`, touches no code).
2. Answer the conformance question once: `Conformance: STANDARD` (new code follows the standard and touched files may be refactored) or `LEGACY` (minimal edits in the project's own style). It is saved in the project's `CLAUDE.md`.
3. `/orchestrator roadmap` proposes the roadmap. Adjust and approve it.
4. `/orchestrator stage <n>` runs one stage, or `/orchestrator <slug>` runs one feature. Say `autopilot` to follow the whole roadmap without stopping at normal checkpoints.

Checkpoint modes: `step` (default, stops after each stage), `feature` (after each feature), `autopilot`.

## Unattended loop

You decide in a normal conversation; the script executes. When the roadmap is approved the orchestrator hands you the command. Run it in a **separate terminal**, outside Claude.

Check first. This creates nothing and spends nothing:

```
python "<plugin>/skills/orchestrator/loop/run_loop.py" <project-folder> --check
```

It prints `READY` with the exact command to run, or lists `PROBLEM`s (no roadmap, no conformance line, guard not reachable, push requested without a remote) and `WARNING`s. Then run the printed command, for example:

```
python "<plugin>/skills/orchestrator/loop/run_loop.py" <project-folder> --wave 2 --setup "npm ci"
```

| Flag | Purpose | Default |
|---|---|---|
| `--wave N` | features per turn, only with disjoint `PATHS` | 1 |
| `--e2e` | final flow-validation phase (headless Playwright) | off |
| `--no-push` | do not push the feature branch or open the PR at the end | push on |
| `--setup "cmd"` | one command in the new worktree, e.g. `npm ci` | none |
| `--budget N` | max US$ per turn | 5 |
| `--max-cost N` | max US$ over all turns, `0` disables | 30 |
| `--timeout N` | max minutes per turn | 60 |
| `--max-stall N` | turns without progress before stopping | 10 |
| `--continue-on-blocked` / `--pause-on-blocked` | what a quarantined feature does to the loop | pauses only if another feature depends on it |
| `--answer "..."` / `--option N` | answer the question that stopped the loop and continue | n/a |
| `--rewind N --yes` | go back to the state after turn N (state files plus `git reset --hard` of the loop branch) | n/a |

The script creates its own worktree `../<project>-worktree-loop` on a `loop/<date>` branch and copies `.specs/` and `CLAUDE.md` into it. Your folder and your branch are never touched. Each turn is a fresh `claude -p` that reads `.specs/`, advances one wave, commits and writes `.specs/project/LOOP.json`. Full walkthrough: `skills/orchestrator/loop/HOW_TO_USE.md`.

### Notifications

Every end of the loop notifies: success, error, question, block. Channels are `console`, `toast`, `file`, `email` (SMTP) and `webhook` (POST JSON). Configure them in `shared/.env`, copied from `shared/.env.example`. The `.env` file is git-ignored; only the example with placeholder values is in the repo. Test the setup with:

```
python "<plugin>/skills/orchestrator/loop/notify.py" --test
```

When the loop stops for a question, the message carries the numbered options and the ready resume command (`--answer "..."` or `--option N`).

## Configuration

| Variable | Meaning |
|---|---|
| `LOOP_NOTIFY_CHANNELS`, `LOOP_NOTIFY_TO`, `LOOP_NOTIFY_REQUIRED` | channels, recipients, channels that must succeed |
| `LOOP_SMTP_*`, `LOOP_WEBHOOK_URL` | settings of the email and webhook channels |
| `LOOP_INTEGRATION_BRANCH` / `LOOP_PR_BASE` | branch the PR targets, default `developer` |
| `LOOP_PR_MODE`, `LOOP_PR_DRAFT` | `auto`, `link` or `off`, and draft PRs |
| `LOOP_GIT_USER` | require this git `user.name` on push |
| `GUARD_PROD_HINTS` | words that mark a target as production for the guard, default `prod,production` |

## Layout

```
.claude-plugin/        plugin.json, marketplace.json
hooks/hooks.json       registers the guard (PreToolUse)
skills/                orchestrator (+ loop/, hooks/, references/), satellites, tlc-spec-driven,
                       flow-validator, frontend-dev, backend-dev
agents/                subagent definitions
shared/                core dev text, verify, regression diff, reuse map, .env.example
```

## License

MIT. See `LICENSE`.

### Third-party

`skills/tlc-spec-driven` is based on the TLC Spec-Driven skill by [Felipe Rodrigues](https://github.com/felipfr) of the [Tech Lead's Club](https://github.com/tech-leads-club), licensed under [CC-BY-4.0](https://creativecommons.org/licenses/by/4.0/). It was modified here: translated, and adapted to be driven by the orchestrator (see the notice at the top of its `SKILL.md`). The original author and license are kept in that skill's `README.md` and frontmatter.
