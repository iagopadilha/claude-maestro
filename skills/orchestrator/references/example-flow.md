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
| `run_loop.py` | repeats turns unattended, validates state, notifies you | decide anything |

---

## Part 1: the conversation (you take part)

### 1. Map the repo, once
```
/orchestrator map
```
Phase 1 only. `brownfield-mapper` agents (how many depends on repo size) write `.specs/codebase/`: STACK, ARCHITECTURE, CONVENTIONS, STRUCTURE, TESTING, INTEGRATIONS, CONCERNS. No code is touched, nothing is committed. You can stop here and keep the docs.

### 2. Answer the conformance question
The orchestrator will not plan before the project's `CLAUDE.md` has a `Conformance:` line. It asks and explains why:

- `STANDARD`: new code follows the standard, and a file you touch may be refactored to it.
- `LEGACY`: new code follows the pattern the project already has; old files get minimal edits only (no renaming, splitting or reformatting).

The answer is saved in `CLAUDE.md`. Subagents read it before they edit anything.

### 3. Approve the roadmap
```
/orchestrator roadmap
```
The `Plan` agent proposes stages and features. After you adjust and approve, the orchestrator writes `.specs/project/ROADMAP.md`. One line per feature, a typed grammar checked by `loop/loop_state.py`:

```
## Stage 1: Coupon backend

- [coupon-model] status=pending
- [coupon-api] status=pending deps=coupon-model

## Stage 2: Checkout UI

- [coupon-input] status=pending deps=coupon-api
- [order-summary-discount] status=pending deps=coupon-api
```

Only `status` is needed now. `scope`, `kind` and `paths` are filled when each `spec-writer` answers. `deps=<slug>` means "that feature must be `verified` first". Two features that edit the same folder cannot run in the same wave, so give each its own `paths`.

### 4. Add limiting rules (optional)
Say it in plain words: "never edit anything under `infra/`". The satellite skill `orchestrator-create-block-rule` turns it into a guard rule and **proves it** with examples that must block and examples that must pass, run against the real guard. It is saved in `.specs/project/RULES.md`, `PERMISSIONS.json` and `rules.tests.json`. The only way to lift a rule is your explicit phrase.

### 5. Answer the open questions of the specs
Blocking questions (the ones that change the answer) are asked together, in a batch. Non-blocking doubts go to `.specs/project/VALIDATE.md` with the choice the loop made and how to revert it, and the work continues.

### 6. Hand-off
When everything above is approved, **the orchestrator stops and gives you the command**. It never runs the loop for you inside the chat. It also writes `autopilot` and "next step is the loop" to `STATE.md`.

Run the check in a **separate terminal**. It creates nothing and spends nothing:
```
python <plugin>/skills/orchestrator/loop/run_loop.py <project> --check
```
Roughly:
```
[check] ROADMAP: 4 features, 0 closed, 4 to do
READY
python <plugin>/skills/orchestrator/loop/run_loop.py <project> --wave 1 --setup "npm ci"
```
or a list of `PROBLEM`s (no roadmap, no `Conformance:` line, guard hook not reachable, push requested but no remote) and `WARNING`s (uncommitted changes, a Node project without `--setup`). `<plugin>` is the folder where Claude installed the plugin on your machine. The command the orchestrator hands you already has it filled in.

---

## Part 2: the loop (you only trigger it)

Run the printed command and walk away. The script:

1. creates **once** a worktree of its own, `../<project>-worktree-loop`, on branch `loop/<date-time>`, and copies `.specs/` and `CLAUDE.md` into it. Your folder and your branch are never touched;
2. starts **turn 1** as a fresh `claude -p` (clean context) running `/orchestrator continue --headless --turn 1 ...`;
3. after each turn validates the state with `loop_state.py`, snapshots it in `.specs/loop-logs/snapshots/NNN/`, and decides what to do next.

### What one turn does

| Phase | Action |
|---|---|
| 0 Orient | read `RULES.md` first, then `ROADMAP.md`, `STATE.md`; apply any pending answer in `ANSWERS.md` |
| 3 Specs | for each `pending` feature with no `spec.md`, dispatch `spec-writer` (up to 3 in parallel) |
| Dependency audit | skill `orchestrator-audit-dependencies` reads real imports and specs and flags a forgotten `deps=` before the wave starts |
| 4 Gate | `spec-reviewer` on every `Medium`, `Large` or `Complex` spec. Result `APPROVED` or `BLOCKED` with the requirement IDs checked. A `Small` spec written by this loop skips the gate; an adopted spec never does |
| 5 Execute | pick the executor by `KIND` and `PATHS`. Only features whose `deps=` are already `verified` run. A wave runs up to `--wave N` features with disjoint paths |
| 6 Verify | `verifier` runs build, typecheck and lint and returns `PASS`, `FAIL` or `UNVERIFIED`. This phase always runs |
| 6a, 6b | optional: behaviour regression for refactors (`regression=baseline`) and, only with `--e2e`, flow validation with curl and a browser |
| 7 Commit | only the orchestrator commits: one atomic Conventional Commit per verified feature |
| 8 Checkpoint | when a stage is done: `phases/stage-NN.md` is written, `STATE.md` is emptied, a short report with the progress graph is produced |

A feature moves through `pending → speccing → specced → approved → building → verified`. Every transition is checked against the legal graph, and `verified` is only accepted if there is a new commit.

The last thing a turn writes is `.specs/project/LOOP.json`:
```json
{"status": "continue", "turn": 3, "feature": "coupon-api", "reason": "", "next": "spec and gate for stage 2", "at": "2026-10-14T02:49:00"}
```
`status` is `continue`, `awaiting_user`, `done` or `error`. The script requires `turn` to match the turn it launched; a stale file is an error.

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
The script stops and **notifies you** (see below). The notice carries what was done, the question, what to check, and one ready command per option:
```
python <plugin>/skills/orchestrator/loop/run_loop.py <project> --option 2
python <plugin>/skills/orchestrator/loop/run_loop.py <project> --answer "no, one coupon per order"
```
Your answer is saved in `.specs/project/ANSWERS.md` (`## Answer <date> [pending]`, `Question:`, `User answer:`). Phase 0 of the next turn reads it before anything else, applies it (a limiting answer becomes a rule in `RULES.md`, citing your phrase) and marks it `[applied]`. You can also answer in a normal conversation instead.

### Notifications and providers
**Every end of the loop notifies**: success, error, a question, a quarantine pause, a pre-flight failure. Channels:

| Channel | Where it goes | You must provide |
|---|---|---|
| `console` | the terminal | nothing |
| `toast` | an OS notification | nothing |
| `file` | `.specs/loop-logs/NOTICE.txt` | nothing |
| `email` | SMTP | your own SMTP server and credentials |
| `webhook` | a POST of JSON `{subject, body, event, project}` | a URL from your chat or automation tool |

The plugin ships **no provider account**. E-mail and webhook are on your side: get SMTP credentials from your mail provider or relay, or a webhook URL from the tool you use, and put them in a `.env` file **outside the plugin folder** (plugin updates replace that folder):

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
Point `LOOP_ENV_FILE` at that file. `LOOP_NOTIFY_REQUIRED` lists channels that **must** work (default none). `--check` reports a required channel that is missing or not configured as a `PROBLEM`; during a run a failed delivery on it is retried once and, if it still fails, a warning is printed. Test the setup before a long run:
```
python <plugin>/skills/orchestrator/loop/notify.py --test
```
Another provider is one function in `loop/notify.py` plus one line in its channel table.

### Cost and safety ceilings
`--budget` caps one turn (default US$ 5), `--max-cost` caps all turns together (default US$ 30, `0` disables), `--timeout` caps minutes per turn, `--max-stall` stops after N turns without progress (default 10). `--rewind N --yes` restores the state and `git reset --hard` of the loop branch to just after turn N.

---

## Part 3: the end

When no feature is left open (all `verified`, or `blocked` and resolved by you), the orchestrator runs `loop/delivery.py`. It pushes **only the feature branch**, then opens a PR with `gh` against the integration branch (default `developer`, set it with `LOOP_INTEGRATION_BRANCH`). Without `gh`, or with `LOOP_PR_MODE=link`, you get the compare link instead. Pushing to the integration branch, to `main`, or force pushing is blocked by the guard. `--no-push` skips this step.

You get a summary file `loop-summary/loop-summary-<project>-<date>.md` and a last notification. Review the PR like any other.

### Where everything lands
```
<project>/
├─ CLAUDE.md                       Conformance: STANDARD
└─ .specs/
   ├─ codebase/                    the 7 mapping documents
   ├─ features/<slug>/             spec.md (and design.md, tasks.md when needed)
   ├─ loop-logs/                   snapshots, NOTICE.txt
   └─ project/
      ├─ ANSWERS.md                your answers, applied on the next turn
      ├─ ROADMAP.md                the index, one line per feature
      ├─ STATE.md                  only the present
      ├─ LOOP.json                 what the last turn reported
      ├─ RULES.md  PERMISSIONS.json  rules.tests.json   limits and your exceptions
      ├─ VALIDATE.md               non-blocking choices to confirm
      ├─ DEPENDENCIES.md  GRAPH.md  audit suggestions and the progress graph
      └─ phases/stage-NN.md        one note per closed stage
```

### Shortcuts
- Only one thing? `/orchestrator <slug>` runs a single feature from spec to commit. An existing `spec.md` is adopted, not rewritten.
- Prefer to stay in the loop? The default checkpoint mode `step` stops after every stage; `feature` stops after every feature; `autopilot` follows the whole roadmap.
- More detail on every flag: `../loop/HOW_TO_USE.md`. State files: `state.md`. Agent return formats: `contracts.md`.
