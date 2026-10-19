"""The demo story (data only): project "acme-checkout", its 9 features in 3 stages and the turn plan the player applies.

A turn plan is a list of steps; each step is a list of actions (dicts with a `kind`). One step is what the animator applies
per tick. Action kinds:
  orchestrator        model text of the orchestrator                       {"text"}
  orchestrator_tool   tool call of the orchestrator                        {"tool", "input", "result"}
  status              feature status change                                {"slug", "status", "note"?}
  agent_tool          tool call of the agent working on a feature           {"slug", "tool", "input", "result"}
  agent_text          model text of that agent                              {"slug", "text"}
  agent_return        final return (SubagentHandback) of that agent         {"slug", "text"}
  guard_block         tool call blocked by the PreToolUse guard (exit 2)    {"slug", "tool", "input", "rule", "reason", "profile"}
  spec                writes `.specs/features/<slug>/spec.md`               {"slug"}
  commit              commits the feature's work and marks it verified     {"slug"}
  stage_closing       writes `.specs/project/phases/stage-NN.md`            {"stage"}
  end_turn            closes the turn (turn log, LOOP.json, snapshot...)    {"status", "result", "reason"?, "next"?, "options"?}
  wait_for_answer     (animator only) holds the question, then answers it  {"answer", "hold_seconds"}
The agent of a feature action is the one the panel infers from the feature status (spec-writer, spec-reviewer, executor-<kind>,
verifier), so the live log lines land on the agent card the panel shows.
"""

PROJECT_NAME = "acme-checkout"
LOOP_BRANCH = "loop/acme-checkout"
INTEGRATION_BRANCH = "developer"
SCAFFOLD_BRANCH = "main"
WAVE = 2
MAX_COST_USD = 15
TURN_TIMEOUT_MINUTES = 45
SEEDED_TURNS = 2  # turns that already happened when the demo opens; the animator plays the rest
GOAL = "Ship a card checkout for the Acme store: payment core, cart and coupons, then invoices and receipts."

STAGES = [
    {"n": 1, "title": "Foundations", "description": "Payment core, audit trail and rate limiting that every later stage builds on."},
    {"n": 2, "title": "Checkout flow", "description": "Cart, coupons and the checkout screens the customer actually sees."},
    {"n": 3, "title": "After the purchase", "description": "Invoices, receipts and the back-office view of every order."},
]

FEATURES = [
    {"slug": "payments-api", "stage": 1, "title": "Payments API", "kind": "back", "scope": "Medium", "paths": "services/payments-api",
     "deps": "", "prefix": "PAY", "file": "services/payments-api/handlers.py",
     "goal": "Authorize, capture and refund card payments behind one idempotent REST API, with provider webhooks verified and "
             "replayed safely.",
     "requirements": ["authorize and capture with an idempotency key", "partial and full refunds", "verify webhook signatures",
                      "replay failed webhooks with backoff"]},
    {"slug": "audit-log", "stage": 1, "title": "Audit log", "kind": "back", "scope": "Small", "paths": "services/audit-log", "deps": "",
     "prefix": "AUD", "file": "services/audit-log/writer.py",
     "goal": "Append-only record of every money movement and admin action, queryable by order and by actor.",
     "requirements": ["append-only writes", "query by order id", "query by actor"]},
    {"slug": "rate-limiter", "stage": 1, "title": "Rate limiter", "kind": "generic", "scope": "Small", "paths": "infra/rate-limiter",
     "deps": "", "prefix": "RTL", "file": "infra/rate-limiter/limits.yaml",
     "goal": "Token-bucket limits per API key at the edge, so a single client cannot starve checkout.",
     "requirements": ["token bucket per API key", "burst allowance", "429 with Retry-After"]},
    {"slug": "cart-service", "stage": 2, "title": "Cart service", "kind": "back", "scope": "Medium", "paths": "services/cart-service",
     "deps": "payments-api", "prefix": "CRT", "file": "services/cart-service/cart.py",
     "goal": "Server-side cart with price snapshots, stock reservation for 15 minutes and a single total the payment step trusts.",
     "requirements": ["add, update and remove items", "price snapshot per line", "reserve stock for 15 minutes", "one signed total"]},
    {"slug": "coupon-rules", "stage": 2, "title": "Coupon rules", "kind": "back", "scope": "Small", "paths": "services/coupon-rules",
     "deps": "cart-service", "prefix": "CPN", "file": "services/coupon-rules/rules.py",
     "goal": "Percentage and fixed coupons with expiry, minimum order and one-use-per-customer rules, applied to the cart total.",
     "requirements": ["percentage and fixed coupons", "expiry and minimum order", "one use per customer"]},
    {"slug": "checkout-ui", "stage": 2, "title": "Checkout UI", "kind": "front", "scope": "Large", "paths": "web/checkout-ui",
     "deps": "cart-service,dir:web/shared", "prefix": "CUI", "file": "web/checkout-ui/CheckoutPage.tsx",
     "goal": "Three-step checkout (cart, address, payment) with inline validation, a coupon field and a clear error on declined cards.",
     "requirements": ["cart, address and payment steps", "inline validation", "coupon field", "declined-card message", "keyboard only flow"]},
    {"slug": "invoice-pdf", "stage": 3, "title": "Invoice PDF", "kind": "back", "scope": "Medium", "paths": "services/invoice-pdf",
     "deps": "payments-api,audit-log", "prefix": "INV", "file": "services/invoice-pdf/render.py",
     "goal": "Numbered PDF invoice per captured payment, rendered from a template and stored next to the order.",
     "requirements": ["sequential invoice numbers", "PDF from a template", "store with the order"]},
    {"slug": "email-receipts", "stage": 3, "title": "Email receipts", "kind": "back", "scope": "Small", "paths": "services/email-receipts",
     "deps": "invoice-pdf", "prefix": "EML", "file": "services/email-receipts/send.py",
     "goal": "Receipt email with the invoice attached, sent once per order and retried when the mail provider is down.",
     "requirements": ["one receipt per order", "invoice attached", "retry on provider failure"]},
    {"slug": "admin-dashboard", "stage": 3, "title": "Admin dashboard", "kind": "front", "scope": "Medium", "paths": "web/admin-dashboard",
     "deps": "audit-log,dir:web/shared", "prefix": "ADM", "file": "web/admin-dashboard/OrdersPage.tsx",
     "goal": "Back-office list of orders with payment status, refunds and the audit trail of each order.",
     "requirements": ["orders list with filters", "refund button with confirmation", "audit trail per order"]},
]

SCAFFOLD_FOLDERS = ["services/payments-api", "services/audit-log", "services/cart-service", "services/coupon-rules", "services/invoice-pdf",
                    "services/email-receipts", "infra/rate-limiter", "infra/prod", "web/shared", "web/checkout-ui", "web/admin-dashboard",
                    "migrations"]

PROJECT_TEXT = """# acme-checkout

<!-- Context for the loop's subagents (spec-writer, executors, verifier), like a CLAUDE.md for the loop. Keep it short
     (~300 words). It does not repeat what other files already say. -->

**Vision:** Card checkout for the Acme store, built as small services behind one gateway: payments, cart, coupons,
invoices and receipts, plus a checkout screen for customers and a back-office dashboard.

## Where to look

- Stack, architecture, conventions, structure, tests, integrations, concerns: `docs/codebase/` (read only the document the task needs; it is a guide, the code wins).
- Limits and "never X": `RULES.md`.
- Now/next, stage, open questions: `STATE.md`.
- Roadmap: `ROADMAP.md`.
- Flows to validate: `FLOWS.md`.
- Test credentials (names only): `TEST-ENVIRONMENT.md`.
"""

RULES_TEXT = """# Project rules

- Never edit `infra/prod/`: production infrastructure is applied by the platform team.
- Never write migrations from a feature: `migrations/` is reviewed by hand (blocked by PERMISSIONS.json).
- Prices and totals in integer cents.
"""

PERMISSIONS = {"deny_path_regex": ["(^|/)migrations/"]}

STATE_HEADER = """# State

Loop on branch `loop/acme-checkout`, wave 2, cost cap US$ 15.

## Turn log
"""

VALIDATE_LINES = [
    "- [payments-api] webhook retries: the provider allows 3 or 5 | chose: 5 with exponential backoff | revert: set WEBHOOK_MAX_RETRIES=3",
    "- [audit-log] retention period not stated in the spec | chose: 400 days (one fiscal year plus margin) | revert: change AUDIT_RETENTION_DAYS",
]

SEEDED_ANSWER = {"question": "payments-api: which payment provider mode should the tests use?",
                 "answer": "the provider's public test mode, with fake cards only"}

QUESTION = {"slug": "checkout-ui",
            "reason": "checkout-ui: where should the coupon code field live? The spec allows both and it changes which service "
                      "validates the code first.",
            "options": ["On the cart step (recommended)", "On the payment step"],
            "to_check": ["the coupon field position in web/checkout-ui after the next turn"],
            "answer": "On the cart step (recommended)"}

QUESTION_HOLD_SECONDS = 10

STAGE_CLOSINGS = {
    1: {"Delivered": "payments-api, audit-log and rate-limiter verified, one commit each.",
        "User decisions": "Tests use the provider's public test mode.",
        "Lessons": "The guard stopped an edit to infra/prod/terraform.tfvars: edge limits stay in infra/rate-limiter.",
        "Unverified": "Webhook replay under real provider latency.", "Blocked": "None.",
        "For the next stage": "cart-service can start: payments-api is verified."},
    2: {"Delivered": "cart-service, coupon-rules and checkout-ui verified.",
        "User decisions": "Coupon field on the cart step.",
        "Lessons": "Ask early when the spec allows two layouts: one question saved a rework turn.",
        "Unverified": "Checkout on very small screens.", "Blocked": "None.",
        "For the next stage": "invoice-pdf and admin-dashboard can run together (disjoint paths)."},
    3: {"Delivered": "invoice-pdf, email-receipts and admin-dashboard verified.",
        "User decisions": "None in this stage.",
        "Lessons": "The guard blocked a `git commit -a` from an executor: only the orchestrator commits.",
        "Unverified": "Receipt rendering in older mail clients.", "Blocked": "None.",
        "For the next stage": "Deliver the branch from the panel (Act: deliver)."},
}

TURN_COSTS_USD = [0.38, 1.12, 0.74, 0.52, 0.88, 0.95, 0.67, 0.49, 1.21, 0.58]

GUARD_HOOK_COMMAND = 'python "${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/hooks/guard.py" --profile auto'
GUARD_MARKER = "BLOCKED by the orchestrator guard"
GUARD_ADVICE = ("Do not try to work around it. Return STATUS: NEEDS_CONFIRM with the reason; the orchestrator takes it to the user.")

FEATURES_BY_SLUG = {feature["slug"]: feature for feature in FEATURES}


def stage_slugs(stage_number):
    return [feature["slug"] for feature in FEATURES if feature["stage"] == stage_number]


# ---------------------------------------------------------------- action builders
def orchestrator(text):
    return {"kind": "orchestrator", "text": text}


def orchestrator_tool(tool, tool_input, result):
    return {"kind": "orchestrator_tool", "tool": tool, "input": tool_input, "result": result}


def status(slug, new_status, note=None):
    return {"kind": "status", "slug": slug, "status": new_status, "note": note}


def agent_tool(slug, tool, tool_input, result):
    return {"kind": "agent_tool", "slug": slug, "tool": tool, "input": tool_input, "result": result}


def agent_text(slug, text):
    return {"kind": "agent_text", "slug": slug, "text": text}


def agent_return(slug, text):
    return {"kind": "agent_return", "slug": slug, "text": text}


def guard_block(slug, tool, tool_input, rule, reason, profile="executor"):
    return {"kind": "guard_block", "slug": slug, "tool": tool, "input": tool_input, "rule": rule, "reason": reason, "profile": profile}


def end_turn(turn_status, result, reason="", next_step="", options=None, to_check=None):
    return {"kind": "end_turn", "status": turn_status, "result": result, "reason": reason, "next": next_step,
            "options": list(options or []), "to_check": list(to_check or [])}


def spec_file_path(slug):
    return ".specs/features/%s/spec.md" % slug


# ---------------------------------------------------------------- step builders
def build_spec_steps(slugs):
    """3 steps: spec-writers start, read the code, write the specs (pending -> speccing -> specced)."""
    first = [orchestrator("Dispatching %d spec-writer(s) in parallel: %s." % (len(slugs), ", ".join(slugs)))]
    second, third = [], []
    for slug in slugs:
        feature = FEATURES_BY_SLUG[slug]
        first += [orchestrator_tool("Agent", {"description": "spec %s" % slug, "subagent_type": "spec-writer"}, "agent started"),
                  status(slug, "speccing"),
                  agent_tool(slug, "Read", {"file_path": ".specs/project/PROJECT.md"}, "# acme-checkout ...")]
        second += [agent_tool(slug, "Grep", {"pattern": "def |class ", "path": feature["paths"]}, "3 files"),
                   agent_text(slug, "The folder only has a README; the spec defines the module from scratch.")]
        third += [{"kind": "spec", "slug": slug},
                  agent_tool(slug, "Write", {"file_path": spec_file_path(slug)}, "File created successfully"),
                  agent_return(slug, "STATUS: DONE\nSUMMARY: spec with %d requirements, scope %s." % (len(feature["requirements"]),
                                                                                                   feature["scope"])),
                  status(slug, "specced")]
    return [first, second, third]


def build_gate_step(slugs):
    """1 step: the spec-reviewer approves each spec (specced -> approved)."""
    step = [orchestrator("Spec gate for %s." % ", ".join(slugs))]
    for slug in slugs:
        feature = FEATURES_BY_SLUG[slug]
        step += [agent_tool(slug, "Read", {"file_path": spec_file_path(slug)}, "# %s ..." % feature["title"]),
                 agent_return(slug, "VERDICT: APPROVED\nSUMMARY: %d requirements are testable and inside %s." % (len(feature["requirements"]),
                                                                                                            feature["paths"])),
                 status(slug, "approved")]
    return [step]


def build_execution_steps(slugs, blocks=None):
    """4 steps (+1 per guard block): executors implement, a verifier checks, the orchestrator commits (approved -> verified).
    `blocks` = {slug: guard_block action} inserted after the executor's first edit."""
    blocks = blocks or {}
    together = "together (disjoint paths, no dependency between them)" if len(slugs) > 1 else "alone"
    start = [orchestrator("Execution wave: %s %s." % (", ".join(slugs), together))]
    write, verify, close = [], [], []
    for slug in slugs:
        feature = FEATURES_BY_SLUG[slug]
        start += [orchestrator_tool("Agent", {"description": "implement %s" % slug, "subagent_type": "executor-" + feature["kind"]},
                                    "agent started"),
                  status(slug, "building"),
                  agent_tool(slug, "Read", {"file_path": spec_file_path(slug)}, "# %s ..." % feature["title"])]
        write += [agent_tool(slug, "Write", {"file_path": feature["file"]}, "File created successfully"),
                  agent_tool(slug, "Bash", {"command": test_command(feature)}, "%d passed" % (len(feature["requirements"]) * 3))]
        verify += [agent_return(slug, "STATUS: DONE\nSUMMARY: %s implemented inside %s; tests added." % (feature["title"], feature["paths"])),
                   status(slug, "building", note="verifying"),
                   agent_tool(slug, "Bash", {"command": test_command(feature)}, "%d passed" % (len(feature["requirements"]) * 3)),
                   agent_tool(slug, "Bash", {"command": lint_command(feature)}, "no issues")]
        close += [agent_return(slug, "VERDICT: PASS\nSUMMARY: tests, lint and typecheck green for %s." % slug),
                  {"kind": "commit", "slug": slug},
                  orchestrator_tool("Bash", {"command": 'git commit -m "feat(%s): %s"' % (slug, feature["title"].lower())},
                                    "1 file changed")]
    steps = [start, write]
    guard_steps = [[blocks[slug], agent_text(slug, "The guard blocked that edit (outside my scope). Staying inside %s."
                                             % FEATURES_BY_SLUG[slug]["paths"])] for slug in slugs if slug in blocks]
    return steps + guard_steps + [verify, close]


def test_command(feature):
    if feature["kind"] == "front":
        return "npm test --prefix %s -- --run" % feature["paths"]
    if feature["kind"] == "generic":
        return "python -m pytest infra/rate-limiter -q"
    return "python -m pytest %s -q" % feature["paths"]


def lint_command(feature):
    if feature["kind"] == "front":
        return "npm run lint --prefix %s" % feature["paths"]
    return "ruff check %s" % feature["paths"]


def path_scope_reason(paths):
    return "outside the feature's PATHS (%s). Need a file outside the scope? Return NEEDS_CONFIRM" % paths


# ---------------------------------------------------------------- the story
def build_turn_plans():
    """[(turn number, [steps])]: turns 1-2 are seeded instantly, the rest are animated."""
    stage_1, stage_2, stage_3 = stage_slugs(1), stage_slugs(2), stage_slugs(3)
    migration_block = guard_block("audit-log", "Write", {"file_path": "migrations/0007_drop_users.sql"}, "project-deny",
                                  "path forbidden by the project: (^|/)migrations/")
    terraform_block = guard_block("rate-limiter", "Edit", {"file_path": "infra/prod/terraform.tfvars"}, "path-scope",
                                  path_scope_reason("infra/rate-limiter"))
    commit_block = guard_block("admin-dashboard", "Bash", {"command": 'git commit -am "wip dashboard"'}, "git-write",
                               "git commit: write git is the orchestrator's only")
    plans = []

    turn_1 = build_spec_steps(stage_1)
    turn_1[-1].append(end_turn("continue", "Stage 1: specs written for payments-api, audit-log and rate-limiter.", next_step="spec gate"))
    plans.append((1, turn_1))

    turn_2 = build_gate_step(stage_1) + build_execution_steps(["payments-api", "audit-log"], {"audit-log": migration_block})
    turn_2[-1].append(end_turn("continue", "payments-api and audit-log verified (2 commits); the guard blocked a migration write.",
                               next_step="rate-limiter"))
    plans.append((2, turn_2))

    turn_3 = build_execution_steps(["rate-limiter"], {"rate-limiter": terraform_block})
    turn_3[-1] += [{"kind": "stage_closing", "stage": 1},
                   end_turn("continue", "Stage 1 closed: rate-limiter verified; the guard kept the executor out of infra/prod.",
                            next_step="stage 2 specs")]
    plans.append((3, turn_3))

    turn_4 = build_spec_steps(stage_2)
    turn_4[-1].append(end_turn("continue", "Stage 2: specs written for cart-service, coupon-rules and checkout-ui.", next_step="spec gate"))
    plans.append((4, turn_4))

    turn_5 = build_gate_step(stage_2) + build_execution_steps(["cart-service"])
    turn_5[-1].append(end_turn("continue", "cart-service verified; coupon-rules and checkout-ui were waiting on it.",
                               next_step="coupon-rules + checkout-ui"))
    plans.append((5, turn_5))

    turn_6 = build_execution_steps(["coupon-rules"])
    turn_6[1] += [orchestrator_tool("Agent", {"description": "implement checkout-ui", "subagent_type": "executor-front"}, "agent started"),
                  status("checkout-ui", "building"),
                  agent_tool("checkout-ui", "Read", {"file_path": spec_file_path("checkout-ui")}, "# Checkout UI ...")]
    turn_6[2] += [agent_text("checkout-ui", "The spec allows the coupon field on the cart step or on the payment step; asking."),
                  agent_return("checkout-ui", "STATUS: NEEDS_CONFIRM\nSUMMARY: coupon field position is a product decision."),
                  status("checkout-ui", "approved")]
    turn_6[-1].append(end_turn("awaiting_user", "coupon-rules verified; checkout-ui needs a decision from you.", reason=QUESTION["reason"],
                               options=QUESTION["options"], to_check=QUESTION["to_check"]))
    turn_6.append([{"kind": "wait_for_answer", "question": QUESTION["reason"], "answer": QUESTION["answer"],
                    "hold_seconds": QUESTION_HOLD_SECONDS}])
    plans.append((6, turn_6))

    turn_7 = build_execution_steps(["checkout-ui"])
    turn_7[-1] += [{"kind": "stage_closing", "stage": 2},
                   end_turn("continue", "Stage 2 closed: checkout-ui verified with the coupon field on the cart step.",
                            next_step="stage 3 specs")]
    plans.append((7, turn_7))

    turn_8 = build_spec_steps(stage_3)
    turn_8[-1].append(end_turn("continue", "Stage 3: specs written for invoice-pdf, email-receipts and admin-dashboard.",
                               next_step="spec gate"))
    plans.append((8, turn_8))

    turn_9 = build_gate_step(stage_3) + build_execution_steps(["invoice-pdf", "admin-dashboard"], {"admin-dashboard": commit_block})
    turn_9[-1].append(end_turn("continue", "invoice-pdf and admin-dashboard verified; the guard blocked a git commit from an executor.",
                               next_step="email-receipts"))
    plans.append((9, turn_9))

    turn_10 = build_execution_steps(["email-receipts"])
    turn_10[-1] += [{"kind": "stage_closing", "stage": 3},
                    end_turn("done", "All 9 features verified. Deliver the branch from the panel (Act: deliver).",
                             reason="all features verified (9/9)")]
    plans.append((10, turn_10))
    return plans
