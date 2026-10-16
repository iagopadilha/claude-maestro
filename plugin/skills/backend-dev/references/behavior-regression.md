# Regression check: the refactored code must behave the same

Use when you split, abstract, move or optimise code that already works (factory extraction, new
modules, concurrency, scraping or query changes). A green build only proves it compiles. It does not
prove the result is the same. Real-world pattern: a "faster" change cut external calls from 163 to 55 and
silently dropped rows, and the screen looked identical because the database still held the old rows.

## Rule

- **Every behavior-preserving change gets a baseline comparison**: same input, old code x new code,
  same output. Say in the delivery whether it was run or not. "Not validated" is a valid and honest
  answer; "should be the same" is not.
- **Offer it every time.** The user decides when to run the heavy part (E2E, full flow). Do not run a
  long flow on your own, but always list the check under "Points of attention" with what would be compared.
- Never commit, never production. Baseline and new code both run local/DEV.

## Make it deterministic

1. **Freeze the baseline.** Old code from git (`git worktree add <dir> <commit>` or `git stash`), in a
   folder outside the repo. Build it. Delete the folder when done (it is a temporary copy).
2. **Same input.** Same entity (order, id), same credentials, same DEV database state.
3. **Capture the same facts from both runs**, then diff:
   - **Written data**: rows per table by natural key (not by generated id), counts per table, and the
     columns that matter. Normalise volatile fields (ids, `CreatedAt`, `UpdatedAt`, GUIDs). Sort before comparing
   - **Return and notifications**: HTTP status, response body, the notification messages
   - **External calls**: count and order-independent set (URL + body hash). A count that drops a lot is a
     red flag, not a win, until each missing call is explained
   - **Logs**: no new warning or error
4. **Run it twice.** The second run must change nothing (idempotent). It also exercises the update path
   (attachments, upserts) and not only the create path.
5. **Prove the check can fail.** When the comparison is by screen or by count, make sure a lost row would show.
   If the old rows stay in the database, a screen diff cannot see a missing write: compare the rows written
   in this run (timestamps after the run start) or start from an emptied copy of the data.
6. **Edge shapes.** Cover at least: one page and many pages, empty result, an item past the first page
   (index >= page size), the update path, and a failure in one step (aggregated, not lost).

## Common silent-loss traps

- A call that "does not return rows" on the first response (state is built by earlier requests); reusing it drops rows
- Parallel work that shares a `DbContext`, or a result list filled by several tasks without a safe merge
- Per-item `try/catch` that swallows the error and returns an empty result
- `Where`/`Distinct` added during extraction that changes which rows survive
- Order that mattered (last write wins, position used as an index) and is now concurrent

## Delivery line

```
Regression: validated (old x new, order X, rows per table equal, 163 calls in both, 2nd run no change)
Regression: NOT validated. Suggest: baseline at <commit>, compare rows per table + call count. Want me to run it?
```


## Reusable script and future unit tests
Write the plan as `.regression/<slug>.md` using `${CLAUDE_PLUGIN_ROOT}/shared/REGRESSION_SCRIPT.md` (curl calls, expected objects, internal invariants such as rows per natural key and external call count, stable ids `RG-xx`). Capture baseline and new as JSON and compare with `python ${CLAUDE_PLUGIN_ROOT}/shared/regression_diff.py baseline.json new.json`. There is no unit test suite yet: each `RG-xx` is written so it can become a unit test later. Offer the check on every behavior-preserving change.
