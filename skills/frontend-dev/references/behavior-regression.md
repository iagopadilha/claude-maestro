# Regression: the refactored code must behave the same

Applies when you split, abstract or move code that already works (extract a hook, split a component,
move a type or constant, swap a query, change a query key). A green build and `tsc` only prove it
compiles. They do not prove the screen does the same thing.

## Rule

- **Every change that must preserve behavior gets a comparison with the previous version**: same
  input, old code x new code, same output. Say in the delivery whether it ran or not. "Not validated" is a valid and honest
  answer; "it should be the same" is not.
- **Always offer it.** The user decides when to run the heavy part (E2E). Do not run a long flow on
  your own, but always list the check under "Points of attention", saying what would be compared.
- Never production, never commit.

## How to make it deterministic

1. **Freeze the baseline.** Old code from git (`git worktree add <folder> <commit>`), in a folder outside the
   repository, running on another port. Delete the folder at the end.
2. **Same input.** Same account, same record, same DEV state, same window size.
3. **Capture the same facts from both versions** and compare:
   - **Accessibility snapshot** (`playwright-cli snapshot`) of each state: loading, empty, with
     data, with error, dialog open. Compare with `diff` (text, roles, labels, values)
   - **Network**: list of requests (method, URL, body) and count. An extra or missing call needs an explanation
   - **Console**: no new warning or error (repeated `key`, `act`, missing i18n)
   - **Texts**: every `t('key')` renders the same text; no text turned into the key itself
   - **Form**: initial values, error messages for each schema rule, submit with and without errors
   - **Table**: row count, sorting, filter, pagination (only shows with more than 15 items), selection
4. **Walk through the states, not only the happy path.** Switching items with a pending change, refreshing the
   page, going back, a different language, a small screen.
5. **Query keys.** The `useQuery` key and the `invalidateQueries` key stay equal. An error here
   shows stale data with no error at all.
6. **Prove the check can fail.** If the comparison is only visual, a row or field that disappeared
   must show up in the diff. Compare the API data and the screen data, not only the screen.

## Traps that vanish silently

- An effect that had the right dependency and lost it when extracted to a hook (the fetch runs only once, or loops)
- State that moved to another hook and no longer resets when the item changes
- `useMemo`/`useCallback` with a missing dependency, showing a stale value
- The default text of `t()` changed, or a new key without translation
- A handler that moved and lost the `showSuccess` of the `onSuccess`

## Delivery line

```
Regression: validated (old x new, order X, snapshots equal in the 5 states, same 7 requests, clean console)
Regression: NOT validated. Suggestion: baseline at commit <hash>, compare snapshots + requests. Want me to run it?
```


## Reusable script and future unit tests
Write the plan as `.regression/<slug>.md` with `${CLAUDE_PLUGIN_ROOT}/shared/REGRESSION_SCRIPT.md` (calls, expected objects, invariants such as request count and `t()` texts, stable ids `RG-xx`). Keep baseline and new as JSON and compare with `python ${CLAUDE_PLUGIN_ROOT}/shared/regression_diff.py baseline.json new.json`. Browser (`playwright-cli`) **always headless**; a visible window only on explicit request. There is no unit suite yet: each `RG-xx` is written so it can become a test later. Offer the regression on every change that must preserve behavior.
