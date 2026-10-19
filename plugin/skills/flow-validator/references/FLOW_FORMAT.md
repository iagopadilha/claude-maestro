# Flow script format

One file, one or more flows. Each flow is a sequence of observable steps.

```markdown
# Flow: <short name>
ORIGIN: FLOWS.md [<slug>] | spec <slug> REQ-3 | user request
LAYERS: route | e2e | route+e2e
APPS: <back>, <front>          # folder names; ENVIRONMENT.md resolves where and how to start
PRE: <required state: user, tenant, existing data>
DATA: <what will be created, with prefix zz_validate_>

## Steps
1. [route] POST /api/orders  body: {...}       -> expects 201 and field `id`
2. [e2e]   open /orders, log in, filter by Y   -> expects to see a row with `zz_validate_...`
3. [e2e]   click Delete                        -> expects the row to disappear
4. [route] GET /api/orders/{id}                -> expects 404

## Cleanup
- DELETE what step 1 created, if step 3 fails
```

## Rules

- Every step has an **observable expected result**: status, field, visible text, element present/absent. "Works" does not count.
- A UI step describes the intent ("filter by Y"), not a selector. The selector comes from the page snapshot at run time.
- A flow derived from a spec cites the requirement ID in `ORIGIN`, so the report can trace it.
- Include at least one error path when the spec describes it (validation, 403, 404).
- One flow = one goal. If it goes past ~10 steps, split it.
- Do not put passwords, tokens or secrets in the file.
