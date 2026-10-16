# Flows

<!-- Loop Harness template. One "## [slug] Title" section per ROADMAP feature: the user-visible (or API) flow that
     proves the feature works. Numbered steps ("1." or "1)"); the "Expected:" steps are what the verifier-flow checks.
     A feature has a flow when its section exists with at least 2 steps; "Approve roadmap" requires one for EVERY
     feature. `/orchestrator plan` (the planning chat) and `/orchestrator roadmap` write this file.

Example (the ">" keeps these lines from being read as a flow):

  > ## [customer-api] Register a customer
  > 1. POST /customers with a valid tax id
  > 2. Expected: 201 and the customer id
  > 3. POST /customers with the same tax id
  > 4. Expected: 409
  >
  > ## [customer-screen] Sign up from the web
  > 1. Open /signup, fill name and tax id, submit
  > 2. Expected: confirmation screen with the customer id
-->
