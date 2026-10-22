# Example: a complete flow, from first chat to merged PR

A walkthrough of one real use of the orchestrator on a made-up project, so you can see who does what, what lands on disk and where you step in. Names, paths and numbers are illustrative; the commands, files, statuses and fields are the real ones.

**The project:** an existing web store. One API (`apps/api`) and one web app (`apps/web`). **The goal:** add discount coupons to checkout.

**The cast**

| Who | Does | Never does |
|---|---|---|
| You | decide: conformance, roadmap, rules, answers to blocking questions | run git by hand inside the loop |
| `orchestrator` (the chat) | plans, dispatches, writes state, commits, talks to you | write specs, code or tests itself |
| `spec-writer`, `spec-reviewer` | write the spec; review it (the gate) | touch code |
| `executor-back`, `executor-front`, `executor-generic` | implement one feature inside its declared `PATHS` | run git, write tests, leave `PATHS` |
| `verifier` | build, typecheck, lint of the feature | fix code |
| guard hook | blocks pushes, protected state, secrets, production writes for subagents | |
| Loop Harness app (`app/`) | the engine repeats turns unattended, validates state, notifies you; the local panel shows it all and takes your clicks | decide anything, approve for you |

---

## Part 1: the conversation (you take part)

### 0. Install the app, once
The loop engine and the panel live in the `app/` folder of the plugin's repository. Clone the repository and run:
```
python <repo>/app/harness.py install
```
It writes `~/.loop-harness/config.json` (`app_root_dir`, the orchestrator skill folder), which is how the skill finds the app. It runs locally with your own Claude subscription through the `claude` CLI; there is no API token to configure. `python <repo>/app/harness.py doctor` checks the skill, the guard, `claude` and the notification channels.

### 1. Map the repo, once
```
/orchestrator map
```
Phase 1 only. `brownfield-mapper` agents (how many depends on repo size) write `docs/codebase/`: STACK, ARCHITECTURE, CONVENTIONS, STRUCTURE, TESTING, INTEGRATIONS, CONCERNS. No code is touched, nothing is committed. You can stop here and keep the docs. At the end the orchestrator opens the panel and gives you its link.

### 2. Answer the conformance question
The orchestrator will not plan before the project's `CLAUDE.md` has a `Conformance:` line. It asks and explains why:

- `STANDARD`: new code follows the standard, and a file you touch may be refactored to it.
- `LEGACY`: new code follows the pattern the project already has; old files get minimal edits only (no renaming, splitting or reformatting).

The answer is saved in `CLAUDE.md`. Subagents read it before they edit anything.

### 3. Approve the roadmap
```
/orchestrator roadmap
```
The `Plan` agent proposes stages and features (or, in a new project, talk it through in the panel's planning screen, which runs `/orchestrator plan`). The orchestrator writes `.specs/project/ROADMAP.md` and one flow per feature in `.specs/project/FLOWS.md`, opens the panel and tells you to approve there. One line per feature, a typed grammar checked by the engine's `loop_state.py`:

```
## Stage 1: Coupon backend

- [coupon-model] status=pending
- [coupon-api] status=pending deps=coupon-model

## Stage 2: Checkout UI

- [coupon-input] status=pending deps=coupon-api
- [order-summary-discount] status=pending deps=coupon-api
```

Only `status` is needed now. `scope`, `kind` and `paths` are filled when each `spec-writer` answers. `deps=<slug>` means "that feature must be `verified` first". Two features that edit the same folder cannot run in the same wave, so give each its own `paths`.

Approval is **your click on Approve roadmap** in the panel. It writes `.specs/project/loop/json/ROADMAP-APPROVED.json` with the signature of the stages and features; the loop's preflight refuses an unapproved roadmap, and changing features or stages later asks for a new click. The orchestrator can never write that file (the guard blocks it). If a feature asks for regression or you want `--e2e`, the orchestrator also fills `.specs/project/TEST-ENVIRONMENT.md` (credentials by secret-manager key, test data, what must never be tested) before you approve; anything it cannot fill stays as `FILL_IN` and is asked together with the approval.

### 4. Add limiting rules (optional)
Say it in plain words: "never edit anything under `infra/`". The satellite skill `orchestrator-create-block-rule` turns it into a guard rule and **proves it** with examples that must block and examples that must pass, run against the real guard. It is saved in `.specs/project/RULES.md`, `.specs/project/loop/json/PERMISSIONS.json` and `rules.tests.json`. The only way to lift a rule is your explicit phrase.

### 5. Answer the open questions of the specs
Blocking questions (the ones that change the answer) are asked together, in a batch. Non-blocking doubts go to `.specs/project/loop/VALIDATE.md` with the choice the loop made and how to revert it, and the work continues.

### 6. Hand-off
When everything above is approved, **the orchestrator stops and gives you the command**, and the panel link. It never runs the loop for you inside the chat (unless you explicitly ask it to run the loop itself). It also writes `autopilot` and "next step is the loop" to `STATE.md`.

Press **Check** in the panel, or run the check in a **separate terminal**. It creates nothing and spends nothing:
```
python <app>/harness.py check <project>
```
Roughly:
```
[check] ROADMAP: 4 features, 0 closed, 4 to do
[check] READY. Command: python <app>/harness.py loop <project> --wave 1 --setup "npm ci"
```
or a list of `[check] PROBLEM:` lines (no roadmap, roadmap not approved, no `Conformance:` line, `TEST-ENVIRONMENT.md` missing or with `FILL_IN` when needed, a block rule whose test stopped passing, guard hook not reachable, push requested but no remote) and `[check] WARNING:` lines (uncommitted changes, a Node project without `--setup`). `<app>` is the `app/` folder of your clone; the command the orchestrator hands you already has it filled in.

---

## Part 2: the loop (you only trigger it)

Press **Start loop** in the panel, or run the printed command, and walk away. The engine:

1. creates **once** a worktree of its own, `../<project>-worktree-loop`, on branch `loop/<date-time>`, and copies `.specs/` and `CLAUDE.md` into it. Your folder and your branch are never touched;
2. starts **turn 1** as a fresh `claude -p` (clean context) running `/orchestrator continue --headless --turn 1 ...`;
3. after each turn validates the state with `loop_state.py`, snapshots it in `.specs/project/loop/internal/snapshots/NNN/`, and decides what to do next;
4. keeps the panel up (`[loop] monitor: <url>`): turns, agents and their logs, dependencies, the graph, `VALIDATE.md`, cost and limits, live.

### What one turn does

| Phase | Action |
|---|---|
| 0 Orient | read `RULES.md` first, then `ROADMAP.md`, `STATE.md`; apply any pending answer in `ANSWERS.md` |
| 3 Specs | for each `pending` feature with no `spec.md`, dispatch ONE `spec-writer` per feature, all in parallel (up to `--wave`); each feature is its own lane and moves to its gate as soon as its own spec is back |
| Dependency audit | skill `orchestrator-audit-dependencies` reads real imports and specs and flags a forgotten `deps=` before the wave starts |
| 4 Gate | one `spec-reviewer` per feature (never one for several specs) on every `Medium`, `Large` or `Complex` spec. Result `APPROVED` or `BLOCKED` with the requirement IDs checked. A `Small` spec written by this loop skips the gate; an adopted spec never does |
| 5 Execute | pick the executor by `KIND` and `PATHS`. Only features whose `deps=` are already `verified` run. Up to `--wave N` lanes run at once, with disjoint paths; a feature enters execution as soon as its own gate approved it |
| 6 Verify | one `verifier` per feature, as soon as its executor is done, runs build, typecheck and lint and returns `PASS`, `FAIL` or `UNVERIFIED`. This phase always runs |
| 6a, 6b | optional: behaviour regression for refactors (`regression=baseline`) and, only with `--e2e`, flow validation with curl and a browser |
| 7 Commit | only the orchestrator commits: one atomic Conventional Commit per verified feature |
| 8 Checkpoint | when a stage is done: `phases/stage-NN.md` is written, `STATE.md` is emptied, a short report with the progress graph is produced |

A feature moves through `pending → speccing → specced → approved → building → verified`. Every transition is checked against the legal graph, and `verified` is only accepted if there is a new commit.

The last thing a turn writes is `.specs/project/loop/json/LOOP.json` (through `harness.py state loop`, never by hand):
```json
{"status": "continue", "turn": 3, "feature": "coupon-api", "reason": "", "next": "spec and gate for stage 2", "at": "2026-10-14T02:49:00"}
```
`status` is `continue`, `awaiting_user`, `done` or `error`. The engine requires `turn` to match the turn it launched; a stale file is an error.

### When something fails
Each failure (gate `BLOCKED`, executor or verifier `FAIL`, E2E `FAIL`) adds `tries=` to the feature line. At 2 the feature becomes `blocked` and is **quarantined**. By default the loop pauses only if another feature depends on it:

```
- [coupon-api] status=blocked scope=Medium kind=back paths=apps/api/coupons tries=2
```
`--continue-on-blocked` never pauses for it; `--pause-on-blocked` always pauses. Nothing is promoted to a bigger model: a failed feature comes up to you.

### When it needs you
Suppose the spec for `coupon-api` raises a real product question: *can two coupons stack?* The turn ends with:
```json
{"status": "awaiting_user", "turn": 4, "reason": "Can two coupons stack on one order?", "options": ["yes", "no"], "to_check": ["pricing rules doc"], "next": "spec coupon-api"}
```
A question like this one (a business rule) is never answered automatically: the engine stops and **notifies you** (see below). A trivial one with an option marked `(recommended)` would be answered by the engine itself and logged in `VALIDATE.md`. The notice carries what was done, the question, what to check, the panel link and one ready command per option:
```
python <app>/harness.py loop <project> --option 2
python <app>/harness.py loop <project> --answer "no, one coupon per order"
```
Or answer in the panel. Your answer is saved in `.specs/project/loop/ANSWERS.md` (`## Answer <date> [pending]`, `Question:`, `User answer:`). Phase 0 of the next turn reads it before anything else, applies it (a limiting answer becomes a rule in `RULES.md`, citing your phrase) and marks it `[applied]`. You can also answer in a normal conversation instead, or use **Instruct the loop** in the panel to add a request at any time.

### Notifications and providers
**Every end of the loop notifies**: success, error, a question, a quarantine pause, a pre-flight failure. Channels:

| Channel | Where it goes | You must provide |
|---|---|---|
| `console` | the terminal | nothing |
| `toast` | an OS notification | nothing |
| `file` | `.specs/project/loop/NOTICE.txt` | nothing |
| `email` | SMTP | your own SMTP server and credentials |
| `webhook` | a POST of JSON `{subject, body, event, project}` | a URL from your chat or automation tool |

The plugin ships **no provider account**. E-mail and webhook are on your side: get SMTP credentials from your mail provider or relay, or a webhook URL from the tool you use, and put them in the app's `.env` file, `~/.loop-harness/.env` (**outside the plugin folder**: plugin updates replace that folder):

```
LOOP_NOTIFY_CHANNELS=console,toast,file,email
LOOP_NOTIFY_TO=you@example.com
LOOP_SMTP_HOST=smtp.example.com
LOOP_SMTP_PORT=587
LOOP_SMTP_USER=you@example.com
LOOP_SMTP_PASSWORD=...
LOOP_SMTP_FROM=loop@example.com
LOOP_WEBHOOK_URL=https://hooks.example.com/your-endpoint
LOOP_NOTIFY_REQUIRED=email
```
`LOOP_ENV_FILE` points at another file instead (then only that one is read). `LOOP_NOTIFY_REQUIRED` lists channels that **must** work (default none). `--check` reports a required channel that is missing or not configured as a `PROBLEM`; during a run a failed delivery on it is retried once and, if it still fails, a warning is printed. Test the setup before a long run:
```
python <app>/harness.py notify --test
```
(or the panel's Test notification button). Another provider is one function in the engine's `notify.py` (`app/backend/harness_backend/engine/`) plus one line in its channel table.

### Cost and safety ceilings
`--budget` caps one turn (default US$ 5), `--max-cost` caps all turns together (default US$ 30, `0` disables), `--timeout` caps minutes per turn, `--max-stall` stops after N turns without progress (default 10). `--rewind N --yes` restores the state and `git reset --hard` of the loop branch to just after turn N. The panel's **Pause** stops the loop between turns; **Resume** relaunches it.

---

## Part 3: the end

When no feature is left open (all `verified`, or `blocked` and resolved by you), the engine runs `harness.py deliver` (or you press **Deliver** in the panel's Act mode). It pushes **only the feature branch**, then opens a PR with `gh` against the integration branch (default `developer`, set it with `LOOP_INTEGRATION_BRANCH`). Without `gh`, or with `LOOP_PR_MODE=link`, you get the compare link instead. Pushing to the integration branch, to `main`, or force pushing is blocked by the guard. `--no-push` skips this step.

You get a summary file `loop-summary-<project>-<date>.md` in the app's output folder (`output_dir`, default `~/Downloads`) and a last notification. Review the PR like any other.

### Where everything lands
```
<project>/
├─ CLAUDE.md                       Conformance: STANDARD
├─ docs/codebase/                  the 7 mapping documents + README.md (versioned, not the source of truth)
└─ .specs/
   ├─ features/<slug>/             spec.md (and design.md, tasks.md when needed)
   └─ project/
      ├─ ROADMAP.md                the index, one line per feature
      ├─ FLOWS.md                  the flow that proves each feature
      ├─ STATE.md                  only the present
      ├─ RULES.md                  limits (imported by CLAUDE.md)
      ├─ TEST-ENVIRONMENT.md       how to test for real (credentials by key, test data)
      ├─ phases/stage-NN.md        one note per closed stage
      └─ loop/                     everything the loop owns
         ├─ ANSWERS.md             your answers, applied on the next turn
         ├─ VALIDATE.md            non-blocking choices to confirm
         ├─ DEPENDENCIES.md  GRAPH.md  NOTICE.txt
         ├─ json/                  LOOP.json  ROADMAP-APPROVED.json  PERMISSIONS.json  SCOPE.json  rules.tests.json  meta.json ...
         └─ internal/              turns/  snapshots/  guard.log  LOOP.lock ...
```

### Shortcuts
- Only one thing? `/orchestrator <slug>` runs a single feature from spec to commit. An existing `spec.md` is adopted, not rewritten.
- Prefer to stay in the loop? The default checkpoint mode `step` stops after every stage; `feature` stops after every feature; `autopilot` follows the whole roadmap.
- More detail on every flag and on the panel: `<app>/docs/HOW_TO_USE.md`. State files: `state.md`. Agent return formats: `contracts.md`.
