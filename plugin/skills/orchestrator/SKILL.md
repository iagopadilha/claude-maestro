---
name: orchestrator
description: Turns the current chat into the orchestrator of an engineering loop on top of the tlc-spec-driven engine. Builds the macro roadmap and, at each stage, dispatches subagents to write specs, review them (gate), implement and verify, keeping all state in .specs/. Stops at a checkpoint at the end of each stage with a short report and batched questions. Supports partial use: only map the codebase, only plan (a conversation that writes PROJECT, ROADMAP and FLOWS), only generate the roadmap, only one stage, only one feature. The unattended loop, its local web panel and the roadmap approval run in the Loop Harness app (app/ of this repo). Use when the user asks for /orchestrator, "orchestrate the project", "run the roadmap", "loop engineering", "drive this project end to end", "break it into features and execute", "map this codebase", "document this repo", or asks to drive a whole project instead of an isolated task. Do not use for a single task, a one-off bug or a question — for those use tlc-spec-driven in quick mode or work inline.
disable-model-invocation: true
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: "python \"${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py\" --profile orchestrator"
---

# Orchestrator

The main chat becomes the orchestrator. It does not write specs, implement or verify: it decides, dispatches subagents, keeps state on disk and talks to the user.

The specification engine is still `tlc-spec-driven`. This skill does not replace or reinvent its format — it orchestrates its execution through subagents.

## Runtime: the Loop Harness app (loop engine, back end and panel)

This skill is the **brain**: `SKILL.md`, `references/*.md`, the hook `hooks/guard.py` and the plugin's `agents/*.md`. The **runtime** is the Loop Harness app, in the `app/` folder of the same repository: the loop engine, a local HTTP server and a multi-project web panel. It runs on your machine with your own Claude subscription through the `claude` CLI (no API token). The old `skills/orchestrator/loop/` scripts no longer exist: do not look for them.

**Find the app (`<app>`), once per session.** The first one that exists wins: the variable `LOOP_APP_HOME`; else `~/.loop-harness/config.json`, field `app_root_dir` (`LOOP_DATA_DIR` replaces the `~/.loop-harness` folder). One line, PowerShell or bash:

```
python -c "import json,os;d=os.environ.get('LOOP_DATA_DIR') or os.path.join(os.path.expanduser('~'),'.loop-harness');print(os.environ.get('LOOP_APP_HOME') or json.load(open(os.path.join(d,'config.json'),encoding='utf-8'))['app_root_dir'])"
```

It failed (no variable and no `config.json`): tell the user to clone `https://github.com/iagopadilha/loop-engineering-harness` and run once `python <repo>/app/harness.py install` (it writes `config.json` with the app folder and this skill's folder), and **stop**. Do not look for the app anywhere else.

**Every loop command is `python "<app>/harness.py" <subcommand>`**, on one line, path in quotes. Subcommands: `serve --project <root>` (panel), `loop <root> [flags]`, `check <root> [flags]` (= `loop --check`), `state`, `notify`, `summary`, `deliver`, `rules`, `audit`, `graph`, `classify`, `scale` (same arguments as the engine scripts), `doctor` (diagnosis: skill, guard, `claude`, notification channels) and `install`. A script name in the text (`run_loop.py`, `loop_state.py`, `delivery.py`, `notify.py`...) is an engine script in `<app>/backend/harness_backend/engine/`: always call it through `harness.py`. `.specs` templates in `<app>/templates/`. App docs: `<app>/docs/HOW_TO_USE.md`, `API.md`, `ARCHITECTURE.md`, `SKILL_BRIDGE.md`.

**Panel by default.** The loop's monitor is **the app's panel** (`/p/<id>/` per project). Whenever this skill opens the panel (roadmap written, loop command handed over, mapping done), run `python "<app>/harness.py" serve --project <root>` **in the background** (`run_in_background: true`). It starts the back end and the front end and opens the browser; with a live app it only registers the project, returns the link and opens no other tab. Take the `MONITOR_URL=` line and give the user the link in one line. `--no-browser` only on the user's request. Never shut the app down. In `--headless` mode (called by the app or the engine) do not start the panel: the app already is the panel.

**The click is the user's.** The orchestrator never calls a trigger or approval endpoint (`/p/<id>/api/loop/*`, `/p/<id>/api/roadmap/approve`, `/p/<id>/api/action/*`, `/p/<id>/api/answer`, `/p/<id>/api/resume`, `/p/<id>/api/instruction`) and never reads the token in `~/.loop-harness/app.json` (the guard blocks both: `panel-trigger`, `secrets`). The user runs the loop: in the panel (Check / Start loop) or with `python "<app>/harness.py" loop <root> ...` in their own terminal.

**What stays in the plugin:** this skill, `references/`, `hooks/guard.py` and the agents. The app finds the guard through `orchestrator_skill_dir` in `config.json` (or `LOOP_SKILL_DIR`, or `$CLAUDE_PLUGIN_ROOT/skills/orchestrator`). Zero-cost suites: `python -m unittest discover -s "<app>/backend/tests" -t "<app>/backend"` (engine and app) and `python -m unittest discover -s "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks" -p test_guard.py` (guard).

## Testing rule (from the user — overrides the rest of this file and the references)

- **Spec, design and tasks carry no tests.** No "write/run tests" task, no "Tests pass", no test command in "Verify". "Done when" describes observable behavior. CI already covers whatever is automatic.
- **In this skill verification always runs**, but as **its own phase after execution** (Phase 6, `verifier`) — never embedded in a task or at the start of the cycle.
- **Flow validation (`verifier-flow`: curl/E2E) stays opt-in** (Phase 6b): only when the user says they need it. Do not dispatch it on your own initiative.
- **Only exception: refactoring regression**, only for a `refactor-*` feature with `regression=yes` written in the approved ROADMAP (`references/regression.md`). There, the feature's `regression.md` is a flow-validation script (baseline before editing, comparison after), not a unit test, and the "spec carries no tests" rule does not apply to it.
- **The executor writes no tests**, creates no throwaway test script and runs no suite "just in case". Typecheck and lint of the files it touched itself remain its job (cheap).
- Outside the loop (small tweak, one-off fix, visual-only change): do not dispatch a verification agent. The cost (tens to hundreds of thousands of tokens per turn) does not pay off.

## Roles

| Agent | Model | Does |
|---|---|---|
| orchestrator (this chat) | the session model | roadmap, dispatch, git, checkpoint, conversation |
| `brownfield-mapper` | sonnet | docs in `.specs/codebase/`; **how many agents depends on repo size** (Phase 1, `harness.py scale`) |
| `spec-writer` | sonnet | spec of one feature in `.specs/features/<slug>/` |
| `spec-reviewer` | sonnet | spec gate, read-only |
| `executor-back` | sonnet | implements, with `backend-dev` |
| `executor-front` | sonnet | implements, with `frontend-dev` |
| `executor-generic` | sonnet | implements, skill passed in the prompt |
| `verifier` | sonnet | test / build / typecheck / lint |
| `verifier-flow` | sonnet | validates a flow by running the app: routes (curl) and E2E (Playwright), via the `flow-validator` skill. Opt-in |

**Model ceiling: Sonnet.** No subagent goes up to Opus or any model above Sonnet, under any circumstance — not even after a failure. When calling `Agent`, do not pass a `model` other than `sonnet` (or `haiku`, if you want it cheaper). When a feature fails, it escalates to the orchestrator and the user, never to a bigger model.

Never use `subagent_type: "fork"` for executors: a fork inherits the parent's model and breaks the ceiling.

## Checkpoint modes

- `step` — **default**. Stops at the end of each roadmap stage: short report, batched questions, waits for the user.
- `feature` — stops after each completed feature.
- `autopilot` — runs the whole roadmap without stopping at normal checkpoints. Only entered when the user asks explicitly ("run the whole roadmap", "autopilot", "don't ask me").

The mode can change midway: the user says "go on with everything" at a checkpoint and the rest becomes autopilot.

## Partial use

The loop is not all-or-nothing. The user may ask for just a piece, and the skill stops there:

- `/orchestrator map` — runs **only Phase 1** (`brownfield-mapper`s sized by repo size), writes `.specs/codebase/` and stops. No roadmap, no feature, no code touched, no commit. For understanding an unfamiliar repo. Also answers "map this codebase", "document this repo", "what is in this project". At the end, open the panel (`harness.py serve --project <root>`, see Runtime) and give the link. Called by the app (Map button), `serve` only reuses the live app.
- `/orchestrator plan [--headless]` — **conversational planning**, one turn per user message: reads the message and `PROJECT.md`/`ROADMAP.md`/`FLOWS.md` (+ `.specs/codebase/`), updates the three incrementally and replies with what changed and the 1–3 most important open questions. Writes only those three files; no Bash, no subagents, no MCP; never approves, never starts a loop. Called by the app's planning screen (`--headless`, the same session resumed on every message). **Read `references/planning.md` first.**
- `/orchestrator roadmap` — assumes the mapping exists (or runs Phase 1 first if `.specs/codebase/` is empty), generates the roadmap (with `FLOWS.md`) and stops for approval. Executes nothing.
- `/orchestrator roadmap --headless --goal-file <file> [--map-first]` — **generates the roadmap without a user**, called by the app (the panel's **Generate roadmap** button; `claude -p` in the repo folder). Read the goal from the file (path relative to the root; today `.specs/project/loop/internal/roadmap-goal.md`, headed `# Roadmap goal` with a `Map first: yes|no` line). If the repo has code and `.specs/codebase/` is empty, or `--map-first` came, run Phase 1 first (same sizing by `harness.py scale`). Write `.specs/project/PROJECT.md`, `.specs/project/ROADMAP.md` and `.specs/project/FLOWS.md` in the format of `references/state.md` (stages `## Stage N — Name`, one line per feature, `status=pending`; one flow per feature). Follow Phase 2b if a feature has `regression=yes`. **Do not ask**: every doubt goes to the `## Open questions` section of the ROADMAP (one per line, with the recommended option). **Never approve** the roadmap (do not write `ROADMAP-APPROVED.json`) and never run the loop; do not start the panel (the app already is the panel). Do not commit. Finish with a 3-line summary: how many stages and features, and how many open questions. If the same session is resumed with an adjustment request ("Continue in this session's `/orchestrator roadmap --headless` mode..."), adjust the ROADMAP (and its flows in FLOWS.md) under the same rules and finish.
- `/orchestrator stage <n>` — runs only that roadmap stage and stops.
- `/orchestrator continue --headless --turn N --wave W --branch B [--e2e] [--phase F --ready a,b --stuck c]` — **one unsupervised loop turn**, called by the app's engine (`harness.py loop`; do not type it by hand). See "Headless mode" and `references/loop.md`.
- `/orchestrator <slug>` — runs only that feature, from spec to commit. If `.specs/features/<slug>/spec.md` already exists, the spec is **adopted** instead of rewritten (see Adopting ready-made material).

In any partial use the same hard rules apply: Sonnet ceiling, git only by the orchestrator, no writes to production, state saved before dispatch.

## Headless mode (unsupervised loop)

When the argument is `continue --headless`, the orchestrator runs **one turn** inside `claude -p`, called by the app's engine (`harness.py loop`, script `run_loop.py`). (`roadmap --headless` and `plan --headless` are other modes: they only write the plan, see Partial use.) Rules that change:
- **Does not ask.** Do not use `AskUserQuestion` and do not end the reply with a question. A mandatory stop that **blocks** = write `awaiting_user` with the batched questions and finish. A doubt that does not block goes to `VALIDATE.md` and the turn continues.
- **The turn is a wave:** up to `--wave` features (default 1) with disjoint `PATHS`, dispatched together; verification and commit one by one; then it ends. Details in `references/loop.md`.
- **The engine already chose the work** (`loop_state.py next`, deterministic): `--phase` (`execute|gate|specs|recover|final|none`) and `--ready` (the wave's features: dependencies `verified`, disjoint `paths`). **Dispatch exactly those**; do not pick others. If `--stuck` lists features, they depend on a `blocked` one: they do not move, take them to the user. `--phase none` with open features = write `awaiting_user` explaining what is blocking. Without `--phase` (manual turn or unreadable ROADMAP), decide as in the section The loop.
- **Turn Phase 0:** read `.specs/project/loop/ANSWERS.md`; each `[pending]` block is a user decision (coming from `--answer` or the panel): apply it to the state and mark it `[applied]` before choosing the work. **Instruction from the user in the panel.** A `[pending]` block with `Question: (user instruction from the panel)`, optionally with a `[<feature>] ` prefix and `Attached context:` lines, is a user decision or request, the same as an `--answer`. Apply it to the existing feature (reopen it, adjust the scope inside its PATHS, record a rule in `RULES.md`) and mark it `[applied]`. If the instruction implies new features or changes the approved roadmap, **do not change the ROADMAP on your own**: write `awaiting_user` with the proposed roadmap addition and `--options` (the most conservative one marked "(recommended)") for approval. When the loop was `done`, the instruction reopens the work through that same path (the panel relaunches the loop).
- **Last action, always:** write `STATE.md` and then `LOOP.json` **with the command, not by hand**: `python "<app>/harness.py" state loop <root> --status <continue|awaiting_user|done|error> --turn <N from the prompt> --feature a,b --reason "..." --next "..." [--options "a|b"] [--to-check "x|y"]`. It validates the enum and stamps the real time. `status` only accepts those four exact values (`in_progress`, `ok` and the like are rejected by the script and cost a whole turn). In `awaiting_user` the `--reason` is the question; add `--options` and `--to-check` when it makes sense. The engine sends the notification with every way to continue: the `--answer` command, replying in the panel, or replying in the normal chat. The engine decides to continue or stop only by that file and the ROADMAP.
- Check `git branch --show-current == --branch`; only commit on that branch; **never push** (the end-of-flow delivery, branch push + PR, belongs to the engine). Do not create or delete a worktree.
- `--e2e` = the user authorized the final flow-validation phase (Phase 6b), headless and serial.
- The final summary file is **not yours**: the engine generates it (`harness.py summary`, no Claude) from `phases/stage-NN.md` (fixed headings), ROADMAP, STATE and `git log`, into the app's output folder (`output_dir` in `config.json`, default `~/Downloads`).

## Handoff to the loop, adopting ready-made material, standalone feature vs roadmap
Details in `references/entry-adoption-standalone.md`. **Read before Phase 0** (scan of `.specs/features/*/`), when handing over the loop command, and when deciding standalone feature vs roadmap. Rules that always hold: an existing spec is not rewritten (it is adopted with `origin=adopted` and always goes through the gate); never run the loop (`harness.py loop`) inside this conversation; only `harness.py check` (or `loop --dry-run`).

## Refactoring

A request to refactor ("bring it to standard", `refactor-*` features in the roadmap) follows `references/refactoring.md`: one feature per page or module with disjoint `paths`, `refactor-shell` as `deps=` of the pages, a search gate on the front end and preserved behavior. The explicit request authorizes refactoring only inside the feature's `PATHS`, even under `LEGACY`. A new or changed front-end screen uses the project's style guide by default, unless explicitly asked otherwise.

## Blocking question vs non-blocking question

**User rule: a one-off doubt never stops the loop.** Classify every `OPEN_QUESTION` and every doubt that appears:
- **Blocks** (mandatory stop, goes to the user): **access** (credential, secret, permission, login, anything that needs authorizing); an **important business rule** (changes a calculation, a tax, a value, what the system decides for the customer); a **discrepancy** (the result diverges from the expected or from the baseline with no explanation, data that does not match the spec); changes architecture, scope, public contract or data; is irreversible or leaves the machine / hits PROD; there are two readings of the spec with incompatible results and the executor cannot choose; a project rule contradicts the spec. **Everything else, simple and reversible (spec wording, where a constant lives, a name, marking a case not covered, a PATHS adjustment, formatting), the agent decides alone and moves on.** The engine enforces that boundary in code: `references/loop.md`, "Automatic answer".
- **Does not block** (move on): small, reversible divergence (a different component than the design, name, text, order, visual detail). Choose the **most conservative and reversible** path, write one line in `.specs/project/loop/VALIDATE.md` (`- [<slug|general>] <doubt> | chose: <what was done> | revert: <how>`) and continue. It is not a question, it is a review item.
`VALIDATE.md` goes in full into the final report, the notification and the PR body as "For you to validate". Only the orchestrator writes it (guard).

**Headless: quarantine and small decisions do NOT ask** (user decision: the loop exists to run for hours without stopping; in a real run it stopped 5 times in 13 turns, twice for the same feature).
1. **Reopen on your own.** Gate rejected twice: the `spec-writer` fixes it with the findings and the gate runs again, without asking, up to **3 gate passes in total** per feature (`tries` goes back to 1 on the automatic reopening, and you note it in `STATE.md`). Only on the 3rd rejection does the feature become `blocked`.
2. **Choose the conservative option.** A small, reversible dilemma (who decides a value, where a constant lives, a name, which layer builds something) with an option that **creates no new file, does not widen `PATHS` and preserves behavior**: choose it, record in `VALIDATE.md` the choice, the reason and the alternative, and move on. If the spec-writer or the reviewer marked an option as recommended and it is conservative, use it.
3. **Only ask** (`awaiting_user`) if the decision changes a public contract or data, is irreversible or leaves the machine / hits PROD, **every** option widens `PATHS` or touches outside the scope, or there are two incompatible readings of the user's request.
4. **A blocked feature without dependents does not stop the loop.** Carry on with the others. If only `blocked` features are left, **finish `done`** (not `awaiting_user`): the summary and the notification list the pending feature with the gate findings and the recommended option, and the user decides later with `--option`/`--answer`. A quarantine question only pauses when another feature **depends** on the blocked one.
5. **Safety net in code.** If you still return `awaiting_user` with `options`, `run_loop.py` answers on its own (the recommended option, or the first one that does not abandon the feature) when the question is not about authorization, production, something irreversible, push/merge, a secret, a credential or billing (`references/loop.md`, "Automatic answer"). So, when asking, always write the `options` with the conservative one marked "(recommended)" and abandoning last.

## Mandatory stops (also apply in autopilot)

1. A pending `OPEN_QUESTIONS` that **blocks** (see above). A non-blocking question goes to `VALIDATE.md`, it does not stop.
2. Anything that leaves the machine **outside the end of the flow**: opening a PR other than the final one, deploying, sending a message. The only "leaves the machine" allowed is the final delivery (feature branch push + PR to the integration branch, `harness.py deliver`; see Git).
3. **Writing to production.** Always blocked, with no confirm option inside the loop — see the Production section.
4. Merge conflict.
5. Second failure of the same feature (quarantine): stops **only when another feature depends on it** or nothing else can move; otherwise the feature stays `blocked`, the loop continues and it goes into the final report.
6. Executor returned `NEEDS_CONFIRM` or `TOO_BIG`.
7. Roadmap not yet approved by the user — except for an adopted standalone feature, which has no roadmap.
8. Project `CLAUDE.md` without the `Conformance:` line (STANDARD or LEGACY). Ask the user, explaining why, before any spec; save the answer with `python "<app>/harness.py" classify <root> --write ...` (or the user uses the panel's Conformance button). It stays in `STATE.md` as decided.

## The loop

**Phase 0 — orient.** Does `.specs/` exist? If so, read **`RULES.md` first** (limits that always hold, see `references/state.md`), then `PROJECT.md`, `ROADMAP.md`, `STATE.md` and only the previous stage's `phases/stage-NN.md`, scan `.specs/features/*/` and reconcile the ready-made material (see Adopting ready-made material), read `.specs/project/loop/DEPENDENCIES.md` if it exists (`pending` suggestions: see Dependency audit), then resume from where it stopped (see `references/state.md`). In headless, a `[pending]` block with `Question: (user instruction from the panel)` is a user decision (see Headless mode). If not, proceed.

**Phase 1 — brownfield (existing repo only, once).** The seven documents are STACK, ARCHITECTURE, CONVENTIONS, STRUCTURE, TESTING, INTEGRATIONS, CONCERNS, but **the number of agents depends on the repo**: run `python "<app>/harness.py" scale <root>` and follow the tier. `small` (up to ~150 code files): **1** agent with all 7 (`DOCS: STACK,ARCHITECTURE,...`); `medium` (up to ~1200): **3** agents by related groups; `large`: 7 agents, 1 per document, at most 3 at a time. Each dispatch carries the line `DOCS: A,B,C`. The files are disjoint between agents, so they do not collide. If the user asks for another number, theirs wins. If `.specs/codebase/` already exists and is populated, skip this phase.

**Phase 2 — roadmap.** Dispatch the `Plan` agent to design the stages and each stage's features. Write the result into `.specs/project/ROADMAP.md` in the format of `references/state.md`, with a status line per feature, and one flow per feature in `.specs/project/FLOWS.md` (same reference; approval requires it). Design the features to run in a wave: disjoint `paths`, a folder that is only imported goes in `deps=dir:`, and suggest `--wave` to the user (`references/dispatch.md`, "Planning the roadmap to run in a wave"). No worktree per feature and no multiple loop runs by default. **Present it to the user and wait for approval.** This is the highest-leverage gate of the system: a wrong roadmap produces wrong work in parallel. Do not advance without explicit approval, not even in autopilot.

**Default behavior once the roadmap is written: open the panel and approve there.** As soon as `ROADMAP.md` is written, run `python "<app>/harness.py" serve --project <root>` in the background (see Runtime: it starts the app or reuses the live one) and tell the user, in one line, the link (`MONITOR_URL=`) and that approval is the **Approve roadmap** button in the project's checklist. The roadmap can also be born in the panel itself (**Generate roadmap** button, `roadmap --headless` mode in Partial use). Approval writes `.specs/project/loop/json/ROADMAP-APPROVED.json` with the signature of the features and stages; changing features or stages afterwards invalidates it and `harness.py check` refuses (`roadmap not approved`). Status, paths, deps and tries change during the loop and do not invalidate it. Only the user's click approves: the orchestrator never calls `/p/<id>/api/roadmap/approve`, never writes that file (guard `approval-write`, a hard rule) and never calls `/p/<id>/api/loop/*`. Starting the loop from the panel runs the preflight and launches the loop in a detached process, outside this chat. Approval also holds in chat ("approved"), but the loop only starts with the approval file written by the panel.

**Phase 2b — test environment (before the roadmap approval; mandatory when a feature has `regression=yes` or the loop will use `--e2e`).** While reading the routes and screens to build the roadmap you already know what it takes to test them for real. List it and close it **before** asking for approval, so the loop does not stop later for something that was already known: auth header and scheme of each route group, DEV user and password, app and identity provider URLs, webhook signature, e-mail destination or external service the regression triggers, users with and without permission, **test data that already exists** (check that the test user or account has records for the happy path of each route: read the DEV database or ask, before approval; an account without data only covers the edges, and finding that out only at the baseline stops the loop), port, and what is **forbidden to test** (e.g. creating a resource a third party bills for; that becomes a block rule). Write it all to `.specs/project/TEST-ENVIRONMENT.md` (template `<app>/templates/TEST_ENVIRONMENT.md`; the panel's Initialize structure button creates the file when the ROADMAP asks for regression) with what already exists (user memory, `CLAUDE.md`, code) and `FILL_IN` where something is missing. **Every credential names its exact key in your secret manager** (name, environment and the class that reads it): never ask for "the token" loosely. What is missing becomes a **roadmap open item**, asked in a batch together with the approval. Check that `.specs/` is in `.gitignore`. `harness.py check` refuses the preflight if the file does not exist or still has `FILL_IN`; do not hand over the loop command before that. A loop stop for a credential that could already be known is the orchestrator's failure, not the user's.

**Phase 3 — stage specs.** For each `pending` feature of the current stage **that has no `spec.md` on disk**, dispatch a `spec-writer`. Up to 3 in parallel. A feature whose spec was adopted in Phase 0 is `specced` and skips this phase entirely. Before each dispatch, mark the feature `speccing` in `STATE.md` (write order in `references/state.md`). Feature that depends on a service outside the project (another backend, e-mail/messaging, a file, a tax lookup, SQL on a customer system): tell the `spec-writer`, in its prompt, to consult an application-map skill (if installed). With no external service, do not mention it.

Feature with `regression=yes` in the ROADMAP: the `spec-writer` prompt carries the line `REGRESSION: yes` (it writes `regression.md`; see `references/regression.md`). `regression=` only enters the ROADMAP at the explicit request of the user, approved with the roadmap; never on the orchestrator's own initiative.

On receipt: write `PATHS`, `KIND` and `SCOPE` into the ROADMAP, mark `specced` and classify the `OPEN_QUESTIONS` (see "Blocking question vs non-blocking question"): the ones that **block** go in batch to the user at the checkpoint; the ones that do not become a line in `VALIDATE.md` with the conservative choice and the feature moves on.

**Dependency audit (after the specs, before each wave).** Skill `orchestrator-audit-dependencies`: run `python "<app>/harness.py" audit <root> --write`. It looks for a forgotten `deps=` (real code imports and citations in specs) and records each suggestion in `.specs/project/loop/DEPENDENCIES.md` (`[pending]`, `[applied]`, `[refused]`, `[cycle]`). **Interactive:** show the `pending` ones with their evidence; run what the user approves with `--apply`; mark what they refuse `[refused]`. **Autopilot in chat:** run with `--apply` (it only makes execution more serial; a cycle or invalid ROADMAP goes to `VALIDATE.md`). **Headless:** `run_loop.py` already applies it before each turn (it picks the wave); you only read the result in `DEPENDENCIES.md`. Run it again before each wave: once feature A exists, the `import` side sees more.

**Phase 4 — gate.** For each `specced` feature of size `Medium`, `Large` or `Complex`, dispatch a `spec-reviewer`. A `Small` feature **written by this loop** skips the gate and goes straight to execution; a `Small` feature with `origin=adopted` goes through the gate anyway.

- `APPROVED` with valid `CITED` → `approved`.
- `APPROVED` without valid `CITED` → treat as `BLOCKED`; the stamp is not valid.
- `BLOCKED` → return to the `spec-writer` with the `FINDINGS`. Second rejection of the same spec → quarantine (in headless: automatic reopening first, see above).

**Phase 5 — execution.** For each `approved` feature **whose `deps=` (slugs) are already `verified`**, choose the executor by `KIND` and `PATHS` (see `references/dispatch.md`). Decide parallelism and isolation from the same reference; for the wave use `python "<app>/harness.py" state next <root> --wave N`. **Feature with `regression=yes` (baseline before editing):** it moves alone in the wave. Before marking `building`, dispatch the `verifier-flow` with `FEATURE: <slug>`, `MODE: baseline` and `SCRIPT: .specs/features/<slug>/regression.md` (code still old), write `regression=baseline` and only then dispatch the executor. A baseline **entirely** `UNVERIFIED` (nothing ran) or a case that already fails on the old code = stop: `awaiting_user` with the `RG-xx` (do not continue without a baseline; do not fix it in the loop). **An environment gap that does not prevent the rest** (missing data or secret for only some cases) **does not stop**: the case becomes `not-covered:<reason>`, one line in `VALIDATE.md`, and the feature continues with a partial regression (`references/regression.md`).

Mark `building` before dispatching. Every executor prompt carries the line `SCOPE_FEATURE: <slug>` (the guard uses it to limit editing to the `PATHS` of that feature only).

**Dependencies in the roadmap.** Each line may have `deps=<slug|dir:folder,...>`: `slug` = a feature that must be `verified` first; `dir:<folder>` = a shared folder the feature only IMPORTS (editing goes in `paths`). Whoever edits a folder collides with whoever imports it; two that only import do not collide. The `spec-writer` fills `deps` in the spec (Dependencies section) and the orchestrator copies it to the ROADMAP; `harness.py state validate` rejects a nonexistent slug, a cycle and `building` with a dep that is not `verified`. `isolation=worktree` marks a feature that collides on a shared folder: interactively it follows the worktree protocol of `dispatch.md`; **in headless it runs alone in the wave** (I never create a worktree there).

`STATUS: DONE` → go to verification. Any other status → see `references/dispatch.md`.

**Phase 6 — verification (its own phase, always runs).** Dispatch the `verifier` on the feature, after execution; see "Testing rule".

- `PASS` → `verified`.
- `UNVERIFIED` → `verified`, with an unverified mark recorded in `STATE.md` and highlighted in the report.
- `FAIL` → return to the same executor with the error. Second failure → quarantine.

**Phase 6a — regression (only with `regression=baseline`).** After the `verifier` gives `PASS` or `UNVERIFIED` and before the commit, dispatch the `verifier-flow` with `MODE: new` (same `SCRIPT`). All `ok` (or `not-covered` explained) -> `regression=ok` and move on. Any `fail` -> `regression=failed`, `tries`+1, the feature goes back to `approved` with `note=regression-failed` and the same executor receives the failed `RG-xx` (expected x obtained); the baseline is not redone (set `regression=baseline` again when re-dispatching). The feature stays `building` until `regression=ok`. Second failure -> quarantine. `verified` only with `regression=ok`.

**Phase 6b — flow validation (opt-in).** Only runs when the user asked ("have a flow validation phase", "validate the flow", "test E2E") or when the prompt indicates a flow to validate. Without a request, skip it. It runs **after** the `verifier` gives `PASS` or `UNVERIFIED`, never in place of it. Dispatch the `verifier-flow` with `FEATURE: <slug>` (the feature's `## [slug]` flow in `.specs/project/FLOWS.md` when it exists, completed by the spec) or `FLOW: <user text>` / `SCRIPT: <path>`. It starts the applications (tries HTTPS first and falls back to HTTP if there is no answer), one per port, and stops them at the end; do not dispatch in parallel with another `verifier-flow` or with anything that uses the same ports.

The `verifier-flow` may kill and restart local apps that occupy the port it needs (including ones the user left running) and rebuild, without asking the user (standing authorization from them); it records the terminated PID/process in the report. Never on a remote server or production.

- `PASS` → go to commit.
- `UNVERIFIED` → continue, with the mark `(flow not validated)` in `STATE.md` and in the report, together with `LAYERS_NOT_COVERED`.
- `FAIL` → return to the same executor with the failed `STEPS` block and the evidence path. Second failure → quarantine.

**Phase 7 — commit.** Only the orchestrator commits. One atomic commit per verified feature, Conventional Commits message, authored with the configured git identity. Never `push`.

**Phase 8 — checkpoint and phase closing.** At the end of each stage, close the phase (`phases/stage-NN.md` + STATE emptied; see `references/state.md`, "State by phases"). Short report, with the progress drawing (skill `orchestrator-show-progress-graph`: `python "<app>/harness.py" graph <root>`, and `--write` for `.specs/project/loop/GRAPH.md`), batched questions and the continuity question. In autopilot, emit the report and move on without waiting, except when there is a mandatory stop.

**Phase 9 — delivery (only at the end of the WHOLE roadmap, only the orchestrator).** When no open feature remains (all `verified` or `blocked` resolved by the user), run `python "<app>/harness.py" deliver <root>`: it pushes **only the feature branch** (never the integration branch or `main`, never force) and opens the PR to the integration branch (`LOOP_INTEGRATION_BRANCH`, default `developer`; reuses an already open PR; without `gh`, returns the compare link). Put the line `PR: <url>` in the final report, together with `VALIDATE.md`. In headless, `run_loop.py` delivers (same code), never you.

## Quarantine

A feature that failed twice — at the gate, in execution, in verification or in E2E — does not take the loop down. **Count failures in `tries=` on the ROADMAP line** (1 on the first failure, 2 on the second) and on the second mark `blocked`: the engine checks (`tries>=2` outside `blocked` stops the loop). Mark `blocked` in `STATE.md` with the reason in one line, move on to the next feature and list the blocked ones in the checkpoint report. Do not try a third time on your own (the only exception is the headless gate reopening above). **Fixable failure vs unfixable contradiction (user decision, validated live):** a failure the executor can fix (compile error, lint, type) goes back to the same executor with the error, up to 2 attempts. An **unfixable contradiction** (the spec demands something a project rule forbids, e.g. a file the lint/build refuses) **goes to `blocked` on the first failure, with `tries=1` and `note=<reason>`, and asks for the user's decision**: trying again does not solve it and only wastes a turn. Never "resolve" the contradiction by deleting the project rule or the requested file. **In the headless loop quarantine only PAUSES when it blocks the operation:** `run_loop.py` detects the feature that just became `blocked` and, **if another feature depends on it** (stuck), rewrites `LOOP.json` as `awaiting_user` (options "try the feature again" / "skip the feature and carry on with the others") and notifies through all channels; the user decides with `--answer` or in the panel. A blocked feature without dependents **does not stop the loop**: it continues with the others and shows up in the final report and the PR. `--pause-on-blocked` goes back to always pausing; `--continue-on-blocked` never pauses. When `LOOP.json` already comes as `done`, `awaiting_user` or `error`, there is no extra pause.

## Hard rules

**The user always runs the loop.** When handing over the command, open the panel (`python "<app>/harness.py" serve --project <root>` in the background, see Runtime) and say in one line the link and that they can start it there (Check / Start loop) or with the command; the link also comes in every notification. The orchestrator never launches the loop (`harness.py loop`), not in the background, not "just to test", not after a generic go-ahead such as "ok", "go" or "do everything". Inside this conversation only `harness.py check` and `harness.py loop <root> --dry-run` are allowed. When the preparation is done, hand over the full, ready command, `python "<app>/harness.py" loop <root> [flags]` with `<app>` already resolved (the chosen flags, `--no-push` when the project's rule asks for it; `check` prints the command with the engine path: hand over the same flags in the `harness.py loop` form), for the user to paste into another terminal, and stop. The command goes on **a single line**, complete, in a code block, without line breaks or continuations (a broken paste made the user run only half of it); an environment variable goes on the same line, in PowerShell with `;` (`$env:NAME="value"; python ...`), and a flag that needs a value and is not needed (e.g. `--git-user` with `--no-push`) is left out. Launch it only if the user writes, in this conversation, that you yourself should run it ("run the loop", "you run it"). Applies to any project.

**Chat mode (only when the user asks "run the loop" or "you run it").** It is their option, not the default, and **you should actively suggest it** when handing over the loop command, in one line, as the case may be: a short chat and a short loop (few features, few turns) = "I can run it from here if you want"; a long chat (more than half of the context) or a loop of hours = "I recommend your terminal" and say why (it survives the chat and does not pay for wake-ups). Steps: (1) run `python "<app>/harness.py" check <root> [flags]` and only continue on `READY`; (2) **warn in 3 lines before launching**: the process is tied to this session (if it closes or drops, the loop dies), every time you are woken up the whole context is re-read (expensive in a long chat), and the authorization holds only for this run; (3) launch the one-line command with `run_in_background: true` and **do not follow it**: do not read the output in a loop, do not use sleep or polling; the system wakes you up when the process ends and the engine itself notifies the user through the configured channels; (4) when woken up, read only the end of the output and `.specs/project/loop/json/LOOP.json`, and summarize in a few lines with what to do next (an answer with `--answer`, or nothing). Never launch two loops on the same repo (the engine has a lock) and never use `--allow-silent`.

**Git.** Only the orchestrator runs write git; subagents are instructed not to. One commit per verified feature, always on a **feature branch** (never directly on the integration branch). **Push and PR only at the end of the whole roadmap and only through delivery** (`harness.py deliver`; in headless, the engine calls the same code): push only the feature branch, PR to the integration branch with the link in the final report. **Never**, in any flow, push to `developer`/`develop`/`main`/`master`/`release`/`production`/`dev` or the configured integration branch, no force, no PR merge: the guard blocks it with a hard rule that not even `PERMISSIONS.json` releases. A raw `git push` stays blocked even for the orchestrator; merging into another branch and tagging require an explicit user request.

**Production.** Reading production is allowed: through read-only tools (e.g. a read-only database connection). **Writing to production never happens inside the loop.** Prohibited, with no confirm option: any tool or skill that creates, changes, deletes or assigns something in a production system (permissions, roles, resources, translations, UI configuration, access rules). The guard recognizes production MCP tools by the words in `GUARD_PROD_HINTS` (default `prod,production`). If a feature depends on such a write, it goes to quarantine with the reason, and the user executes it outside the loop. **Ticket tracker / issue tracker: the loop never writes or edits** (guard `outbound-msg` in every profile, orchestrator included). Subagents inherit the MCP servers, so this rule is repeated in each agent's prompt — do not remove it from there.

**Guard (hook, not just prompt).** The rules above are enforced by `hooks/guard.py`, a `PreToolUse` hook that blocks with exit 2. It runs in the orchestrator chat (this skill's frontmatter, profile `orchestrator`) and in each subagent (the plugin's `hooks/hooks.json`, `--profile auto` by agent type: `executor`, `verifier`, `spec`). It only enforces anything while the loop's agents run; outside them nothing changes. Rules: `git-write`, `git-push`, `git-add-all`, `pr-release`, `prod-write`, `dev-write`, `outbound-msg`, `secrets`, `install`, `state-write` (the whole `.specs/project/loop/` folder plus ROADMAP/STATE/RULES/phases), `state-script` (only the orchestrator runs `harness.py rules write`, `audit --write|--apply`, `graph --write`, `state loop|migrate`), `delivery` (only the orchestrator delivers), `loop-run` (a subagent never starts the loop or the panel), `project-deny`, and the **hard** ones, which `PERMISSIONS.json` does not release: `push-protected`, `push-force`, `approval-write` (nobody writes `ROADMAP-APPROVED.json`), `panel-trigger` (nobody calls the panel's user-click endpoints). Applies to **Bash and PowerShell**; looks at the whole command text (`python -c`, `$(...)`) and at shell writes (`>`, `tee`, `sed -i`, `Set-Content`) to a state or credential file. **A fail-open warns:** an internal guard error lets the command through (to avoid locking up), but logs it and sends a warning through the configured channels (`harness.py notify`, once per hour). In the headless loop `claude -p` also runs with the `pushurl` of every remote disabled (`GIT_CONFIG_*`), so push is impossible even through a regex loophole.
- **The user outranks the hook.** If the user authorizes something explicitly ("you can push", "you can run the install"), write to `.specs/project/loop/json/PERMISSIONS.json` `{"allow":[{"rule":"git-push","quote":"<user's phrase>","at":"<date>"}]}`. A rule in `allow` never blocks (hard rules excepted), and each use is recorded in `.specs/project/loop/internal/guard.log`. Only write it with an explicit user phrase in this session; never by inference, never for yourself. A project still on the old flat layout (`.specs/project/PERMISSIONS.json`) keeps working until the engine migrates it.
- **Project-specific rule** (e.g. "never create tracking") goes in the same file, in `deny_bash_regex` and `deny_path_regex`. The guard applies it to all agents. **Create it through the `orchestrator-create-block-rule` skill** (`harness.py rules propose|write`): it proposes the regex with examples that must block and that must pass, runs everything in the real guard, only writes with the user's explicit phrase and stores the examples in `rules.tests.json`; the loop's `--check` re-runs the test. Never write a regex without that test.
- A blocked subagent returns `NEEDS_CONFIRM`. Take it to the user at the checkpoint; the block is a mandatory stop like any other.
- Before running unsupervised (long autopilot), confirm the guard responds: `echo '{"tool_name":"Bash","tool_input":{"command":"git push"}}' | python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py" --profile executor; echo $?` must give `2` (or `python "<app>/harness.py" doctor`).

**Scope per feature (hook).** Before dispatching an executor, and with the line `SCOPE_FEATURE: <slug>` in its prompt, write its `PATHS` to `.specs/project/loop/json/SCOPE.json` (`{"features":{"<slug>":["<path>", ...]}}`, paths relative to the repo root); remove the entry when the feature leaves `building`. With that file, the guard blocks in the executor any edit outside the `PATHS` and outside the repo; creating a new file **inside** the PATHS is free. An executor that needs a file outside returns `NEEDS_CONFIRM`; the orchestrator only widens the scope if the user agrees (rewriting `SCOPE.json`). Shell commands that write files (`sed -i`, redirection) stay outside the hook: the executor prompt covers that. The `STANDARD`/`LEGACY` policy (minimal edit vs refactor the touched class) is also passed in the executor prompt, read from `CLAUDE.md`.

**State.** Only the orchestrator writes `.specs/project/ROADMAP.md`, `.specs/project/FLOWS.md`, `.specs/project/STATE.md` and the loop folder `.specs/project/loop/`. A subagent writes only inside its own feature folder or, in brownfield, its own document. Write state **before** each dispatch, not after: this chat's context may be compacted, and `.specs/` alone has to be enough to rebuild the loop.

**Concurrency.** At most 3 simultaneous subagents. Dispatch them in a single message, so they really run in parallel.

**Context.** Keep pointers, not content. Never ask a subagent to paste a spec, diff or log in its return; the contracts in `references/contracts.md` already forbid it. If you need the content, read the file.

## Checkpoint report

Short, in the style of the rest of the session. One line per feature:

```
Stage 2 — Checkout | 2/3 verified

card-payment      verified   apps/api/orders     PASS
checkout-summary  verified   apps/web/checkout   UNVERIFIED (no test/build/lint)
coupon            blocked    packages/ui         gate 2x: acceptance criterion missing

Blocking questions (2):
1. Does the coupon stack with free shipping? a) yes b) no
2. How long until the payment link expires? a) 15min b) 30min c) 1h

For you to validate (did not block, I chose the conservative option): 2 items in .specs/project/loop/VALIDATE.md
PR: https://github.com/<org>/<repo>/pull/123   (final report only)

.specs/features/ has the detail.
Shall I continue to stage 3?
```

Do not repeat in the report what is already in the file. Blocked items and questions highlighted; the rest is one line.

## The loop panel (the app's web panel)

The loop's monitor is **the Loop Harness panel** (`/p/<id>/` per project, `127.0.0.1` only, a token on every POST). See Runtime. It is **the default view of the loop**: the loop (`harness.py loop`) makes sure the panel is up on its own (it reuses the panel that launched it, the live app, or starts `harness.py serve --project` detached), prints `[loop] monitor: <url>`, and the link goes in every notification (console, toast, file, e-mail, webhook) as a third way to continue ("answer in the panel"). It opens the browser only when the app starts; reusing the live app does not reopen it. `LOOP_MONITOR_OPEN=off` turns off only the browser opening; `--no-monitor` (or `LOOP_MONITOR=off`) turns off the panel for that loop. The variables live in the app's `.env` (`~/.loop-harness/.env`, then `${CLAUDE_PLUGIN_ROOT}/shared/.env`; `LOOP_ENV_FILE` points at one file only). When handing over the loop command, tell the user this in one line.

The panel is the project's control: it stays up without a loop, and through it the user generates and approves the roadmap, edits `TEST-ENVIRONMENT.md` and `RULES.md`, runs the preflight, starts, answers, resumes, pauses, instructs, chats (read-only) and delivers. It shuts down on its own after 2 h without use while no loop runs; with no live app `python "<app>/harness.py" serve --project <root>` is enough (it also brings the panel back after the automatic shutdown). **Act mode:** objective actions from a **whitelist**, done by code (not by the AI), always with the user's **explicit confirmation**: `deliver` (push **only the feature branch** + PR to the integration branch, via `harness.py deliver`), `summary` (`harness.py summary`, same output folder), `push` (push the feature branches, no PR) and `clean_worktrees` (backup first). Free text goes through a router (`claude -p` with haiku, no tools) that can only choose an action from the list; outside it becomes `none` and the front end offers to send it as an instruction. Act **does not reopen the loop**. Delivery refuses a protected branch, force and an open feature; that is why `deliver` only works with the loop stopped and no open feature. The orchestrator opens the panel (roadmap written, loop command handed over, mapping done), never shuts it down and never calls `/p/<id>/api/loop/*`, `/p/<id>/api/roadmap/approve` or `/p/<id>/api/action/*`. A click on Start loop, Answer, Resume or confirming an action is the user triggering it. Details in `references/loop.md` (section "Panel") and `<app>/docs/HOW_TO_USE.md`.

## References

- `references/contracts.md` — return format of each agent and how to interpret it.
- `references/entry-adoption-standalone.md` — handoff to the unsupervised loop, adopting ready-made material (`origin=adopted`), standalone feature vs roadmap. Read before Phase 0.
- `references/example-flow.md` — a complete run from the first chat to the PR: files, statuses, a failure, a question, notifications, the panel.
- `references/dispatch.md` — executor routing, path collision, parallelism, worktree protocol.
- `references/regression.md` — refactoring regression (`regression=yes` in the ROADMAP, `refactor-*` only): `RG-xx` cases, baseline before editing, comparison after, how to check effect (curl, Playwright, database, logs).
- `references/refactoring.md` — refactoring to the project's standard in the loop: `refactor-*` feature, shell before pages, search gate, scope in a LEGACY project.
- `references/graph.md` — the orchestrator as a graph: map of nodes and edges, legal transitions, anti-loop ceilings, fan-out, checkpoint and `--rewind`, known limits.
- `references/loop.md` — headless mode: arguments, `LOOP.json` contract, wave, final E2E phase, quarantine without dirtying the tree, tolerated permission denials, automatic answer, gate tiebreak, the panel.
- `references/planning.md` — `plan` mode: the planning conversation that writes PROJECT, ROADMAP and FLOWS; what it may touch, one turn, the reply.
- `references/state.md` — `.specs/` layout, status machine, who writes what, `FLOWS.md` format, write order, resuming after compaction.
- Engine and panel (Loop Harness app): `<app>/docs/HOW_TO_USE.md` (step-by-step of the loop for the user), `<app>/docs/API.md` (routes `/p/<id>/api/...`), `<app>/docs/ARCHITECTURE.md`, `<app>/docs/SKILL_BRIDGE.md`. Engine scripts in `<app>/backend/harness_backend/engine/`, always through `harness.py`:
  - `harness.py state` — typed state: ROADMAP grammar, legal transitions, `validate`, `disjoint`, `next`, `graph` (Mermaid), `loop` (writes `LOOP.json`), `migrate` (old layouts), snapshots and rewind.
  - `harness.py rules` — skill `orchestrator-create-block-rule`: proposes/writes a block rule and proves it in the real guard (`rules.tests.json`).
  - `harness.py audit` — skill `orchestrator-audit-dependencies`: forgotten `deps=` (imports + specs), recorded in `.specs/project/loop/DEPENDENCIES.md`.
  - `harness.py graph` — skill `orchestrator-show-progress-graph`: progress as text, short and Mermaid; writes `.specs/project/loop/GRAPH.md`.
  - `harness.py deliver` — end of the flow: feature branch push + PR to the integration branch (link in the report). Refuses a protected branch, force, an open feature and headless.
  - `harness.py scale` — sizes Phase 1: how many `brownfield-mapper`s and which documents each one gets.
  - `harness.py notify` — notification (console, toast, file, e-mail, webhook; pluggable channels); `.env` of the app (`~/.loop-harness/.env`, then `${CLAUDE_PLUGIN_ROOT}/shared/.env`, template `.env.example`).
  - `harness.py loop` / `harness.py check` and `harness.py summary` — the unsupervised loop (`run_loop.py`) and the final summary generator (`loop_summary.py`), Python with no Claude.
  - `harness.py classify` — the `Conformance:` line of the project's `CLAUDE.md`.
  - `harness.py serve` — the panel; it starts or reuses the app and writes `LOOP-CMD.json` for relaunching.
