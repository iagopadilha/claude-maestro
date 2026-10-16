# Roadmap

<!-- Loop Harness template. The orchestrator writes this file (`/orchestrator roadmap`, or the app's
     "Generate roadmap" button). Only the user's click on "Approve roadmap" approves it: the loop does not run without that.

Grammar of each feature line (checked by engine/loop_state.py). One line per feature, under the
"## Stage N — Name" heading of the stage it belongs to, starting with "- [slug]" (without the ">" of the examples below):

  > - [slug] status=<pending|speccing|specced|approved|building|verified|blocked> scope=<Small|Medium|Large|Complex>
  >   kind=<back|front|both|generic> paths=<p1,p2> [deps=<slug|dir:folder,...>] [isolation=<same-tree|worktree>]
  >   [tries=<n>] [origin=adopted] [e2e=<ok|failed|not-covered>] [regression=<yes|baseline|ok|failed>] [note=...]

A new feature is born status=pending (scope, kind and paths may start empty: the spec-writer fills them in). The
regression field only applies to a refactor-* feature and only when the user asks for it (it requires a filled-in
.specs/project/TEST-ENVIRONMENT.md).

Example (the ">" keeps these lines from being read as a stage/feature):

  > ## Stage 1 — Signup
  > **Goal:** customer signup with tax-id validation.
  > - [customer-api] status=pending scope=Medium kind=back paths=src/api/customers
  > - [customer-screen] status=pending scope=Small kind=front paths=src/web/customers deps=customer-api
-->

**Goal:** FILL_IN

## Open questions

- none
