# Validating routes with curl

Validates the **contract**, not the interface. Try `https://` first; if the port does not answer over HTTPS, use `http://` (fallback) and record in the report the scheme used.

## Before writing the call

Confirm field name, route, verb and type by reading the **source code** (controller/DTO `.cs`, Nest `.ts`), never from documentation or memory. An invented field produces a false FAIL.

## Auth

- Find out how the route authenticates (Bearer JWT, cookie) in the code/config.
- The token comes from a real login in the DEV/local environment or from existing local config. Do not print the token; pass it through a shell variable.
- Also check the no-token case: expect 401.

## What to check per route

| Item | How |
|---|---|
| Status | `-w "%{http_code}"` |
| Body | expected fields present and of the right type (`jq`, if available) |
| Error | invalid input → 400 with a message; no permission → 403; nonexistent → 404 |
| Effect | after POST/PUT/DELETE, a GET confirms the state |
| Idempotency | only if the spec asks |

## Form

```bash
curl -sS -k -o .playwright-cli/routes/<step>.json -w "%{http_code}" \
  -X POST "https://localhost:<port>/api/..." \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"field":"value"}'
```

`-k` only for `localhost` and `127.0.0.1`. Save the body to a file and read only what is needed; do not dump the whole response into the report.

## Limits

- Writes only on local/DEV, with `zz_validate_` data, and clean up what you created.
- A route that triggers an external effect (email, payment, real webhook): do not call it. Mark `UNVERIFIED` and give the reason.
- Load and performance are out of scope; this is functional validation.
