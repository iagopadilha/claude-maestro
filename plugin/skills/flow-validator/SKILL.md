---
name: flow-validator
description: Validates a FLOW end to end against the application actually running. Two layers - API routes via curl (contract - status, payload, auth) and UI E2E via Playwright (playwright-cli skill). Starts the needed applications (tries HTTPS first and falls back to HTTP if the port does not answer, one per port, closing the previous one if the port collides), runs the script and returns PASS, FAIL or UNVERIFIED per step, with evidence. The flow comes from what was implemented (spec and diff) OR from what the user describes. Use when the user asks to validate a flow, test E2E, test the screen, test the routes, "check this really works", or when the orchestrator asks for the flow validation phase. Do NOT activate for unit tests, build, lint or typecheck (that is the verifier), nor for writing a permanent test suite in the repo. Use ONLY when the user asks (or in the testing phase they call). Never on its own initiative after implementing or fixing something, nor for automatic retesting.
---

# Validate flow

Runs what was built and checks that it works. It does not fix code: it reports.

Two layers, which are not mixed:

| Layer | Tool | Proves |
|---|---|---|
| **Route** | `curl` | API contract: status, body, auth, error. Does not prove the screen works. |
| **E2E** | `playwright-cli` (`playwright-cli` skill) | User flow in the interface, from click to visible result. |

Curl alone **is not E2E**. A report with only curl says "routes validated", never "E2E validated".

## Steps

1. **Flow.** Build or read the script (see Input). Save it in `flows.md`.
2. **Environment.** Find out what must be up, start it, wait for it to become healthy. See `references/ENVIRONMENT.md`.
3. **Routes.** If the flow touches an API, validate the routes with curl before the UI. See `references/CURL_ROUTES.md`. A broken route makes the corresponding UI step `UNVERIFIED` by dependency, not a UI `FAIL`.
4. **E2E.** If the flow has a screen, run it with Playwright. Load the `playwright-cli` skill first. See `references/E2E_FRONT.md`.
5. **Cleanup.** Close the browser, shut down what you started, remove the test data you created.
6. **Report.** In the format below.

## Input

Two sources, and they can be combined:

- **From what was implemented.** If the project has `.specs/project/FLOWS.md` with a `## [<slug>]` section for the feature, start from it: its numbered steps are the flow and its `Expected:` lines the expected results. Then read the spec (acceptance criteria, requirement IDs) and the feature's diff, and derive one flow per observable criterion the section does not cover.
- **From what the user asks.** Free text ("log in, open flow X, filter by Y, check column Z"). Turn it into a script.

The script follows `references/FLOW_FORMAT.md`. Save it in `.specs/features/<slug>/flows.md` when there is a feature; without a feature, in `.playwright-cli/flows/<name>.md` or at the path the user gives. If the flow came from the user and is ambiguous (no login data, no success criterion), ask before running. If it came from the spec, proceed without asking.

## Hard rules

- **HTTPS first, HTTP as fallback.** For every URL, curl, browser and app, front or back, try `https://` first. If the host or port does not answer over HTTPS, fall back to `http://`: the fallback is allowed and is the default, without stopping or asking. Record in the report which scheme was used and say when there was a fallback. If the user gave a specific URL or scheme, follow theirs. `curl -k` and `ignoreHTTPSErrors` only for `localhost` and `127.0.0.1`.
- **One application per port.** Before starting any app, check whether the port is occupied. If it is, identify the process. **Standing authorization from the user (flow validation/E2E):** needed port occupied, including by an app the user left running (Visual Studio, dotnet, Vite, node), = kill the process that owns the port and start again with the current code, **without asking**; rebuild is also allowed. Record in the report (`CLEANUP`/`NOTES`) which PID and process was terminated. It applies **only to local processes on this machine**: never take down anything on a remote server or production. Never two instances on the same port: it causes CORS issues and answers from the wrong app.
- **Never production.** Run only on local or DEV. Production URL in the script: stop. Production reads only through a read-only database tool when you need to check data, never writes.
- **No PASS without running.** Could not start the app, could not authenticate, browser did not open: `UNVERIFIED`, with the reason.
- **Marked test data.** What you create carries the prefix `zz_validate_` (or the domain's equivalent) and is removed at the end. Do not delete data you did not create.
- **Credentials never in the script or the report.** Use what the project already has in local config/`.env`, or ask. Do not print tokens.
- **Do not edit project code.** Only create flow files and artifacts in `.playwright-cli/`.
- **Do not run write git commands.** Reading `git diff`/`git status` is fine.

## When to open the browser with a window (headed)

The default is **headless**. Use headed only when:

- the user asked to watch;
- manual login, captcha or MFA is needed;
- a step failed in headless and you need to debug what the page shows.

When using headed, warn in one line before opening the window.

## Report

```
FLOW: <name or slug>
VERDICT: PASS|FAIL|UNVERIFIED
LAYERS: route | e2e | route+e2e
ENVIRONMENT: <app>:<port> <https|http>[, fallback from https] ..., or "-"
STEPS:
1. <step> — PASS
2. <step> — FAIL: <expected> x <got> | evidence: <path>
3. <step> — UNVERIFIED: <reason>
EVIDENCE: <paths in .playwright-cli/>
CLEANUP: <what was started and shut down; data removed>
NOTES: up to 3 lines, or "-"
```

Overall `VERDICT`: any `FAIL` → `FAIL`; all `PASS` → `PASS`; some `UNVERIFIED` without `FAIL` → `UNVERIFIED`. Do not paste logs or whole response bodies: only the line that identifies the problem and the path to the evidence.

## References

- `references/FLOW_FORMAT.md` — how to write the script.
- `references/ENVIRONMENT.md` — find, start, wait for and shut down the applications; HTTPS with fallback to HTTP; ports.
- `references/CURL_ROUTES.md` — validating API routes.
- `references/E2E_FRONT.md` — Playwright: session, headless/headed, HTTPS with fallback to HTTP, local dev cert, evidence.
