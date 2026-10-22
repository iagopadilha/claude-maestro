# Monorepo: subprojects, per-subproject map, system bridge

A **monorepo**, for the loop, is one git repository that holds several projects with their own stack (for example `backend/` .NET and `frontend/` React). The app detects the candidates, an AI validates them, and **the user confirms the list** in the panel; until then nothing below applies. A single project, or a repo the user marked "not a monorepo", changes nothing in this file: no `SUBPROJECT:` line, no per-subproject map.

## `SUBPROJECTS.json`

`.specs/project/SUBPROJECTS.json`, written by the app, never by an agent: `{"confirmed": bool, "subprojects": [{"path", "stack", "skill", "skip"?}]}`. `path` is relative to the repo root, `skill` is `backend-dev`, `frontend-dev` or `-`, and a `skip`ped entry is not a project (tools, seeds). Read it only through `python "<engine>/subprojects.py" show <root> [--path <p>]`. The AI's proposal lives apart in `SUBPROJECTS-SUGGESTED.json` (the app validates and merges it; only the user's confirmation makes it real).

## Lines the engine prints

- `SUBPROJECT: <path> | stack | skill | conformance | map folder` for each subproject a feature's paths touch (`subprojects.py for-paths`). It only says WHICH subproject's `CLAUDE.md` and `docs/codebase/` apply.
- `SYSTEM: docs/codebase/SYSTEM.md` when the file exists (the system bridge, below).
- `CONTRACT-PENDING: .specs/features/<slug>/contract.md` for each listed feature whose contract delta is not merged yet (`system_map.py pending`).

A single project prints nothing: **no `SUBPROJECT:` line = nothing changes**.

## `map --subproject <path>`

`/orchestrator map --subproject <path> [--inventory <relpath>]` runs Phase 1 on that folder only: `scale.py` on `<root>/<path>`, the `SKILL` read with `subprojects.py show <root> --path <path>`. Each `brownfield-mapper` prompt carries `ROOT: <path>`, `SKILL: <skill>` and, when given, `INVENTORY: <relpath>` (relative to the repository root, the folder holding `.specs/`, not to `<path>`; the pre-computed large-file list: the mapper never reads a giant file whole). **Every mapper `Agent` call has `model: sonnet`.** It writes only `<path>/docs/codebase/`, no root map, no other file. The app runs these per-subproject jobs in parallel (no budget cap on setup jobs); the system map runs only after all of them finish.

`/orchestrator map --system` dispatches ONE mapper that writes only the root `docs/codebase/SYSTEM.md` (about 150 lines, keep it short): the subprojects with pointers, then a section `## Who talks to whom` (right after the subproject list, before the CONTRACT block): one line per relation `- <consumer> -> <provider>: <how> (<env or base URL NAME, never values>; <main routes, topics or tables>)`, e.g. `- frontend -> backend: HTTP REST (VITE_API_URL; /api/orders, /api/auth/login)`, `- backend -> postgres: SQL (DATABASE_URL; orders, users)`, `- worker -> backend: queue orders.created (RABBIT_URL)`; then `Shared resources` (database, queue, cache, storage: which subprojects use each). Find them in each subproject's `docs/codebase/INTEGRATIONS.md` and `ARCHITECTURE.md`, API client code (fetch/axios/HttpClient base URLs), env example files, route tables, docker-compose and proxy configs; a relation that is a guess is marked `(inferred)`. Then a section between `<!-- CONTRACT:BEGIN -->` and `<!-- CONTRACT:END -->` (base URL and env NAMES, never values; routes by area; auth scheme; shared env names and types), and how to run both sides together. Root `/orchestrator map` also passes `--inventory` through.

## CLAUDE.md of a monorepo

**Never write a `Conformance:` line, or any other block, in the root `CLAUDE.md` of a monorepo.** Each subproject's own `CLAUDE.md` carries the `CONFORMANCE` block, the `CODEBASE` block (where its map is) and the `maestro:context` block (stack, conformance in one sentence, map location, build/run/test commands). The app writes them; agents rely on the `CLAUDE.md` they already have loaded.

## Contract deltas

A back-end feature that adds, changes or removes a route, DTO, auth or env name writes `.specs/features/<slug>/contract.md` in its own folder; the engine merges it into the CONTRACT section after the feature is VERIFIED (see `references/dispatch.md`). Nobody else edits `SYSTEM.md`. The dispatch prompt of that executor MUST carry the `for-paths` lines (`SUBPROJECT:`, `SYSTEM:`) and the instruction "write .specs/features/<slug>/contract.md (routes, env, events it adds/changes) - required for back-end features in a monorepo"; the orchestrator checks the file exists before marking the feature `verified`, and the engine writes a `WARNING contract` line in `VALIDATE.md` when it does not. The file holds exactly the grammar lines `- [add|change|remove] route|dto|auth|env <identity> — <purpose> (<file>)`, no table (example: `- [add] route GET /health — liveness probe (Api/HealthController.cs)`); a file without such a line merges nothing and the engine warns. The feature folder is the slug with ':' replaced by '-' (slug `backend:version-endpoint` -> `.specs/features/backend-version-endpoint/`); every path of this document and every `.specs/features/<slug>/` in the skill means that folder name.
