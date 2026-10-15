# UI E2E with Playwright

Use the **`playwright-cli`** skill (load it before running the first command; it has the full command list). Only the rules specific to this use live here.

## Session

```bash
playwright-cli -s=validate open --config=.playwright-cli/validate-config.json   # headless (default)
playwright-cli -s=validate goto https://localhost:<port>   # try https first; if it does not answer, http://localhost:<port> (fallback)
playwright-cli -s=validate snapshot
playwright-cli -s=validate click <ref>
playwright-cli -s=validate close
```

Always use a named session (`-s=validate`) so you do not collide with another user session. Close it at the end.

## Local HTTPS with a dev certificate

Try `https://` first; if the port does not answer over HTTPS, use `http://localhost:<port>` (fallback allowed, without asking) and record the scheme used. `ignoreHTTPSErrors` is only needed when the local frontend or the local backend the screen calls uses a dev certificate. In that case, create `.playwright-cli/validate-config.json` with `ignoreHTTPSErrors` turned on in the context options. The exact format is in `--help` and in the `playwright-cli` documentation and may change between versions; check before writing. Only ignore certificate errors for `localhost` and `127.0.0.1`.

## Headless x headed

Default: headless. Headed only in the cases of `SKILL.md` (user asked, manual login/captcha, debugging). In headed use `open --headed` and warn beforehand. Headed stays open until `close`.

## How to run a step

1. `snapshot` of the page. The element `ref`s come from it; do not guess selectors.
2. Act (`click`, `fill`, `press`, `select`).
3. `snapshot` again and **check the script's expected result**: text, row, element present or absent. A step without a check does not count as PASS.
4. On FAIL: `screenshot` with `--filename=.playwright-cli/images/<flow>/<step>.png`, `console` (errors) and `network` (the call that failed). Record the path in the evidence.

## Login

Prefer reusing the session: log in once and `state-save`; load it in the following flows (see `references/storage-state.md` of the playwright-cli skill). Credentials come from local config or from the user; never write them in the script.

## Network as proof

When the UI fires an API call, check with `network` that the call went to the right backend URL (in the scheme you started) and came back with the expected status. This links the E2E layer to the route layer.

## Artifacts

Everything in `.playwright-cli/` (snapshots, console, images in `images/<flow>/`; trace and video per `tracing.md` and `video-recording.md` of the playwright-cli skill). Do not leave loose files in the repo root.

## Limits

- Flow that depends on captcha or MFA without a window: `UNVERIFIED`.
- Do not test layout pixel by pixel; test behavior.
- A page that does not load in ~30s: `UNVERIFIED` with the console error, without retrying in a loop.
