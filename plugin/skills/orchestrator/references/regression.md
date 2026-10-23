# Regression in the loop (refactoring only, only when the ROADMAP asks)

Refactoring must keep behavior. A green build does not prove that. For a large refactoring the user may ask, **in the ROADMAP**, that the loop prove it: before editing, capture how the flow behaves today (baseline); after editing, run the same cases and compare.

## Rules (user decision)

- **It only happens if written in the ROADMAP**: field `regression=yes` on the feature's line, approved together with the roadmap. No regression on the initiative of the orchestrator or the agents.
- **Only on a refactoring feature** (slug `refactor-*`). `harness.py state validate` rejects `regression=` on any other slug. A new feature or a behavior change is not covered for now.
- If the user asked for regression on a feature that is not `refactor-*`, say it is not supported yet and ask whether it becomes a refactoring feature.
- This is the **only** kind of automatic flow validation in the loop. The others stay opt-in (`--e2e`, Phase 6b). The rule "spec carries no tests" still holds for spec, design and tasks; `regression.md` is a flow-validation script, not a unit test.
- Layers: **curl** (routes) and **Playwright** (UI E2E, headless). Nothing beyond that for now.
- Environment: local or DEV. **Never production** (writes). Reading production data (read-only database access) and the observability stack (logs) are allowed to check effects.

## Field in the ROADMAP

```
- [refactor-approve-order] status=approved scope=Large kind=back paths=src/Orders regression=yes
```

Values: `yes` (requested) -> `baseline` (old code captured) -> `ok` or `failed` (comparison result). The orchestrator writes it. A feature with `regression=` **runs alone in the wave** (the scheduler handles it): the baseline needs a tree that only it changes.

## Where it lives

In `.specs/features/<slug>/`:

| File | Who writes | What it is |
|---|---|---|
| `regression.md` | `spec-writer` creates; `verifier-flow` marks the result | the `RG-xx` cases |
| `regression-baseline.json` | `verifier-flow` (baseline mode) | what the old code did |
| `regression-new.json` | `verifier-flow` (new mode) | what the new code did |

Same format as the snapshots of `<TOOLKIT>/shared/regression_diff.py`: `{"responses": {...}, "tables": {...}, "externalCalls": [...], "logs": [...]}`.

## What an `RG-xx` case is

The `spec-writer` reads the code to be refactored and surfaces the **use cases and nuances**: what the flow does that is observable. Each case is verifiable by curl, Playwright, database (read) or log. Examples of the right kind:

```
RG-01 [curl]       POST /orders/{id}/approve with a valid order -> 200, fields status=Approved
RG-02 [database]   after RG-01, Order.Status=Approved and 1 row in OrderHistory (natural key: OrderId+Event)
RG-03 [log/email]  after RG-01, 1 "confirmation" e-mail goes out in the notify step (log event EmailSent, 1 occurrence)
RG-04 [curl]       approve again -> nothing changes (idempotent), 0 new e-mails
RG-05 [curl]       order with no items -> 400 with message X, 0 writes, 0 e-mails
RG-06 [e2e]        orders screen: the Approve button changes the badge to Approved and shows a success toast
```

File format: template `<TOOLKIT>/shared/REGRESSION_SCRIPT.md`, plus one status column per case:

```
SLUG: refactor-approve-order
BASELINE: <commit before the feature>      NEW: working tree
ENVIRONMENT: local/DEV    DATA: order zz_regression_001 (created by the script itself)

| ID    | Layer | Case (what must happen)                              | How to check                 | Baseline | New |
|-------|--------|------------------------------------------------------|------------------------------|----------|------|
| RG-01 | curl   | approve valid order -> 200, status=Approved          | response                     | pending | pending |
| RG-03 | log    | 1 confirmation e-mail in the notify step             | log: EmailSent == 1          | pending | pending |
```

Status per column: `pending`, `ok`, `fail:<expected x obtained>`, `not-covered:<reason>`. An ID never changes nor is reused.

**How to check effects without touching production:** HTTP response and DTO (curl); rows in the DEV database (read) by natural key; in a real environment, reads on the production database; logs and errors per user/action from the observability stack; e-mail sent = a log event or a table record, never check an inbox. A case that cannot be observed stays `not-covered` with the reason; it never becomes `ok` by assumption.

## Order within the feature (only with `regression=yes`)

```
spec (+ regression.md) -> gate -> BASELINE -> executor -> verifier (build/lint) -> REGRESSION -> commit
```

1. **Spec.** The `spec-writer` receives `REGRESSION: yes` in the prompt and writes `regression.md` with the cases. The `spec-reviewer` checks that each case is observable and that the list covers the flow (happy path, empty, failure in a step, second execution).
2. **Baseline (before any edit).** With the feature `approved` and **before dispatching the executor**: `verifier-flow` with `FEATURE: <slug>`, `MODE: baseline`, `SCRIPT: .specs/features/<slug>/regression.md`. It starts the app on the current (still old) code, runs each case, writes `regression-baseline.json` and the Baseline column. The orchestrator writes `regression=baseline`.
   - A case that already fails on the old code: it is not the refactoring's fault. Mark `fail` in the Baseline column and **the orchestrator takes it to the user** (spec of what is correct vs what exists today) before continuing; do not fix it in the loop.
   - Baseline **entirely** `UNVERIFIED` (the app did not start, nothing authenticated, or no case ran): the feature does **not** proceed to the executor; it goes to `awaiting_user` with the reason. Refactoring without any baseline when the user asked for regression goes against the request.
   - **Principle: go as far as possible.** Do everything you can: baseline of what is covered, refactoring, build, verification and comparison of the covered cases. Only consider stopping when, to close the feature, **only** the gap is left and there is nothing more to do in it. Even then the rule below comes first: a partial gap becomes `not-covered` and the feature continues. Stopping and notifying is for what `TEST-ENVIRONMENT.md` cannot solve even in part (no comparable case at all, or a gap the user marked there as mandatory). The right way not to get here is to close the test environment **at the start** (`SKILL.md`, Phase 2b).
   - **An environment gap that does not prevent the rest** (missing test data, such as an account with no records; a secret missing for only some cases; an e-mail that must not be sent): the case becomes `not-covered:<reason>` in the Baseline and New columns, gets **one line in `VALIDATE.md`** (`- [<slug>] case RG-xx without test data | chose: continue with a partial regression | revert: provide the data and run only that case`) and the feature **continues**. **Never stop the loop for that** (user decision: the headless loop exists so it does not stop for nothing). Before declaring a gap, look for the data: `TEST-ENVIRONMENT.md`, a read of the DEV database, another user or account listed there; never create what `RULES.md` forbids. `VALIDATE.md` goes in the summary, the notification and the PR.
3. **Executor + verifier** as usual.
4. **Regression (after the `verifier` gives PASS).** `verifier-flow` with `MODE: new`: runs the same cases on the new code, writes `regression-new.json` and the New column; compares with `python "<TOOLKIT>/shared/regression_diff.py" regression-baseline.json regression-new.json` (run in the feature folder). A large drop in counts (rows, calls) is an alert until explained.
   - All `ok` -> `regression=ok`, go to commit.
   - Any `fail` -> `regression=failed`, the feature goes back to `approved` with `note=regression-failed`, the same executor receives the failed `RG-xx` (expected x obtained). `tries` +1; second failure = quarantine. **The baseline is not redone**: it belongs to the old code and remains valid.
5. **Done.** The summary, `VALIDATE.md` and the PR list per feature: `ok` cases, `not-covered` (with reason) and what failed.

## Ports, data and cleanup

- `verifier-flow` runs one at a time (it uses ports). Since a feature with `regression=` already moves alone in the wave, there is no collision with another executor.
- Test data with the prefix `zz_regression_`, created by the script and removed at the end. Never in production.
- Login and credentials: those of the project's DEV environment, listed by `$NAME` in `.specs/project/TEST-ENVIRONMENT.md` (values in the git-ignored `.env.test` / `~/.loop-harness/.env`; run commands with `python "<TOOLKIT>/shared/testenv.py" exec --project <root> '<command using $NAME>'` in single quotes, which fills the values in and redacts them from the output; never read the `.env` files); if a credential that blocks **all** cases is missing, `UNVERIFIED` with the question in `NOTES` and the exact secret-manager key of each item; if it is missing only for some cases, `not-covered` and continue (rule above).

## Bridge to unit tests (future)

Each `RG-xx` already carries input, expected output and invariant, so it can become a unit test later without rewriting. Today we do not create a suite.
