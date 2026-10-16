# Regression script (template)

Copy to `.regression/<slug>.md` in the project. Today it serves to compare old x new code and, **in the future, to become
a unit test** (each `RG-xx` already carries input, expected output and invariant). It is not a test suite; do not run it unless asked.

```
SLUG: <flow-or-feature>
BASELINE: <commit/branch of the old code>     NEW: <commit/branch or working tree>
ENVIRONMENT: local or DEV (never production). Scheme: https first, http if it does not answer.
DATA: <fixed entity/id used in both runs>; prefix zz_regression_ on anything created

## Calls (curl)
RG-01  POST <url>        body: <file or summary>      expects: status <n>, fields <a,b,c>
RG-02  GET  <url>                                     expects: status <n>, items == <n>

## Objects / expected outputs
RG-03  response of RG-01 contains <field>=<value> (after normalizing id/date/guid)

## Invariants of the internal logic (what the code does inside)
RG-04  rows written by natural key, per table: <table>=<n> (compare baseline x new)
RG-05  external calls: <n> in baseline == <n> in new (a big drop = alert, explain each one)
RG-06  a second run changes nothing (idempotent)
RG-07  a failure in one step is aggregated, not lost: <case>

## Capture
baseline.json and new.json in the SAME format: {"responses": {...}, "tables": {...}, "externalCalls": [...]}
Compare: python "${CLAUDE_PLUGIN_ROOT}/shared/regression_diff.py" baseline.json new.json [--key naturalKey]

## Result
RG-01 PASS | RG-02 PASS | ... | RG-0x FAIL: <expected> x <got>
Regression: validated (<what was compared>) | NOT validated (<what would be compared>)
```

Rules: the `RG-xx` ID never changes and is never reused. UI (E2E) uses `playwright-cli` always **headless**; a window only on
explicit request. Never production.
