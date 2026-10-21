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

Project-wide setting (one line under the Goal, outside any feature; absent = no):

  > **E2E:** yes|no   (yes = the loop ends with a flow-validation phase: verifier-flow runs every FLOWS.md flow against the real app;
  >                    it needs a filled-in .specs/project/TEST-ENVIRONMENT.md. Decided here, in planning; not in the Start-a-loop dialog.)

Loop settings (the lines below the Goal; the Start dialog only shows them and the loop reads them when it starts; a missing
line = the default shown, an invalid value = the default and a warning in the planning status): Dispatch = features per turn (auto or
1-8); Max spend per turn / for the loop in US$ (0 on the loop = no cap); Max minutes per turn; Branch = the loop branch name or
automatic; Branch base = the branch new worktrees start from and the PR target (default developer; the loop falls back to the
current branch with a warning when it does not exist locally); Setup = one command run once in the new worktree (for example npm ci); When a feature gets stuck = pause or continue;
Push and PR = push the feature branch and open the PR at the end (yes/no).
-->

**Goal:** FILL_IN

**E2E:** no
**Dispatch:** auto
**Max spend per turn:** 5
**Max spend for the loop:** 50
**Max minutes per turn:** 60
**Branch:** automatic
**Branch base:** developer
**Setup:**
**When a feature gets stuck:** pause
**Push and PR:** yes

## Open questions

- none
