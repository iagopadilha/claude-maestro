# Environment: find, start, wait, shut down

This skill is the one that starts the applications. Do not assume they are already running: check.

## Where the repos are

Locate the repository (or repositories) of the flow's apps from the working directory and the paths named in the spec or by the user. If the project keeps an app map or a folder index (README, docs, monorepo workspace file), read it; otherwise use `ls` on the workspace root and its backend/frontend/monorepo folders. Ignore archived or deprecated projects unless asked. The disk is the source of truth: if it diverges from a map, tell the user.

A name cited without a prefix: monorepo before a standalone repo; active before archived. Ambiguous between active ones: ask.

## How to find the port and the command

Port and command are not documented in a single place. Read them from the project, in this order:

| What | Where to look |
|---|---|
| .NET backend: URL and port | `Properties/launchSettings.json` of the `*.Api` project (`https` profile) |
| NestJS backend | `package.json` scripts (`start:dev`), `main.ts` (`listen`), `.env` |
| Vite frontend | `vite.config.*` (`server.port`, `server.https`), `package.json` scripts (`dev`) |
| Next frontend | `package.json` scripts |
| Any of them | `README.md` of the repo and the folder, `docker-compose.yml` |
| Backend the frontend calls | the frontend's `.env*` (`VITE_*_URL`) |

The frontend points at the backend through an environment variable. Make sure the backend URL the frontend uses is the one of the backend you started (same port and same scheme). If it does not match, adjust through a process environment variable, **without editing a project file**; if only editing a file solves it, stop and ask.

## HTTPS

- .NET backend without a useful `launchSettings.json`, or when another port is needed: `dotnet build` first and then `dotnet run --no-build --project <csproj> --urls http://127.0.0.1:<port>` (confirmed: it starts and answers 200; shut the process down at the end and check the port was freed).
- .NET backend: use the `https` profile from `launchSettings.json` (`dotnet run --launch-profile https`). If the dev certificate is not trusted, `dotnet dev-certs https --trust` is a user action: warn, do not run it yourself.
- Default scheme, for front and back: try `https://` first. If the host or port does not answer over HTTPS, use `http://` (fallback allowed, without stopping or asking) and record in the report the scheme used and that there was a fallback.
- A frontend (Vite, React, Next) on `localhost`/`127.0.0.1` without `server.https` or a certificate falls into this fallback and starts on `http://localhost:<port>`. If the project already serves the frontend over HTTPS, follow what it does.
- Self-signed cert of the local backend: Playwright with `ignoreHTTPSErrors` (see `E2E_FRONT.md`); curl with `-k` **only** for `https://localhost` and `127.0.0.1`. Remote host with an invalid cert: do not use `-k` or `ignoreHTTPSErrors`; treat it as HTTPS not answering and fall back to HTTP.
- If the user gave a specific URL or scheme, follow theirs.

## One per port

Before starting each app:

1. Check who uses the port (PowerShell): `Get-NetTCPConnection -LocalPort <port> -State Listen -ErrorAction SilentlyContinue | Select-Object OwningProcess`
2. Port free: start.
3. Port occupied by a process **this skill started earlier** (recorded in `.playwright-cli/environment.json`): stop it and start again.
4. Port occupied by **another** local process (including an app the user left running: Visual Studio, dotnet, Vite, node): the user gave standing authorization, for flow validation/E2E. Get the PID and name (`Get-Process -Id <pid>`), kill it (`Stop-Process -Id <pid> -Force`; if it is a child of dotnet/Visual Studio, check the port was freed) and start again with the current code, **without asking**. Rebuild allowed. Record in the report which PID/process was terminated.

The exception that still holds: never take down anything on a remote server or production; the rule applies only to local processes on this machine.

Never leave two instances on the same port. The same rule applies between different apps competing for the port.

## Start

- Run in the background, with the log in a file (`.playwright-cli/logs/<app>.log`).
- Record in `.playwright-cli/environment.json`: app, command, port, PID. This lets you shut down later and recognize "I started that".
- Wait until healthy: try the `https://` URL and, if the port does not answer over HTTPS, `http://` (fallback), until it answers (limit ~60s, polling every 2s). Prefer a health route if one exists; otherwise the root.
- Did not answer within the limit: `UNVERIFIED` with the last useful log line. Do not retry in a loop.
- Database: the backend starts against the local/DEV configuration the project already has. Never switch the connection string to production.

## Shut down

At the end (and also if the flow aborts): stop only the PIDs you recorded, check the port was freed, delete the record. If the user asked to leave it running, leave it and say so in the report.
