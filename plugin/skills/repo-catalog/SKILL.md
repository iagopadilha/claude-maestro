---
name: repo-catalog
description: Maps which repository does what, so "which repo already does X?" has an answer backed by files. Use when the user asks to catalog, summarize or index repositories, wants to know which existing repo already sends e-mail, charges, stores files or any other capability before building it again, or when the planning chat must suggest which repo a wish belongs to. Do not use to change code in a repo.
---

# repo-catalog

One short sheet per repository: what it is for and the capabilities it offers, each one proved by a file that exists.

## When to use
- Building a sheet for a repo (the Loop Harness app runs this as a job; a user can also ask for it in chat).
- Answering "which repo already does X": read the index first (name, purpose, capability ids), open a sheet only for the best match.

## Sheet format
The sheet is ONE JSON object that follows `schema.json` (a filled example is in `references/example-sheet.json`):
- `purpose`: one sentence, at most 200 characters, in the repo's own terms. What the repo is for, not how it is built.
- `capabilities`: a list of `{id, description, evidence}`.
  - `id`: short kebab-case verb-object name (`send-email`, `issue-invoice`).
  - `description`: one sentence about what a caller gets.
  - `evidence`: path of an EXISTING file, relative to the repo root, forward slashes (a controller, a route file, a job, an exported module).

## How to build one
1. Read the repo's `docs/codebase/*` first (STACK, ARCHITECTURE, INTEGRATIONS). When it does not exist, say so: the repo is "weak" and should be mapped before it is trusted.
2. Confirm every capability in code: open or search the file you cite. Prefer the entry point (controller, route, handler) over a helper.
3. Return only the JSON object, no prose, no fences.

## Hard rules
- Never invent a capability. No file that proves it = leave it out.
- Never cite a path you have not seen exist. A sheet with a missing evidence file is rejected capability by capability.
- Never copy secrets, connection strings, hosts or customer names into a sheet.
- Few and true beats many and vague: 3 to 12 capabilities per repo is typical.
- Read-only: this skill never edits the repo.

## What the app checks afterwards
Facts (name, remote, stack files, whether `docs/codebase` exists, HEAD sha) are collected by Python, not by you. Python rejects a sheet that breaks the schema or a purpose longer than 200 characters, drops any capability whose evidence file is missing, and keeps the result per repo until its HEAD changes.
