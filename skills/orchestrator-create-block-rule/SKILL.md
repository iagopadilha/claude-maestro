---
name: orchestrator-create-block-rule
description: Turns a user prohibition ("never edit folder X", "never create tracking", "do not run command Y") into a project block rule in the orchestrator hook, and PROVES it works with examples that must block and examples that must pass, run in the real guard. Writes to .specs/project (RULES.md, PERMISSIONS.json, rules.tests.json). Use when the user says "never", "forbidden", "block", "create a rule/hook for" in a project using /orchestrator or the loop. Do NOT activate for application user permissions or for releasing an action (releasing is PERMISSIONS.json allow, only with an explicit phrase).
---

# Create a project block rule (with a test)

Depends on the `orchestrator` skill (uses `${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/rules.py` and `hooks/guard.py`). Without `.specs/` in the project there is nowhere to write: warn and stop.

## When

The user says what must **never** happen in that project. The orchestrator (the chat) writes it, **only with the user's explicit phrase** (it becomes the rule's origin). Inside the unsupervised (headless) loop no rule is created: nobody confirms the phrase.

## Step by step

1. **Classify.** Can the phrase be enforced by **command** (the shell command text) or by **path** (the file written)? If it is about meaning ("do not touch the tax logic"), tell the user it can only be approximated by folder/name and that it **may block too much**; show that in the examples that must pass. If it cannot be enforced by regex, write it only in `RULES.md` as text and warn that **the hook will not block it**.
2. **Propose**, in plain language: the regex and 2+ examples that **must block** and 2+ that **must pass** (including a similar one that must not be blocked). Use `[\\/]` to accept `/` and `\` in paths.
3. **Test without writing:**
   ```
   python ${CLAUDE_PLUGIN_ROOT}/skills/orchestrator/loop/rules.py propose <root> --id R-04 --phrase "<user's phrase>" \
     --path "Taxes" --block path:src/Taxes/Calculation.cs --block path:Taxes/x.ts \
     --pass path:src/Shipping/Calculation.cs --pass path:docs/guide.md
   ```
   (`--bash REGEX` for a command; `--block bash:"<command>"`.) Each example runs in the real guard. Failed: adjust the regex and repeat. The script rejects an invalid, empty or too broad regex.
4. **Show the result to the user and wait for the ok.** They decide whether what was blocked/released is right.
5. **Write** (same arguments, replacing `propose` with `write`, with `--rule "<short text>" --reason "<why>"`). The script only writes if the tests pass **and** the new rule does not break any old one. It writes `PERMISSIONS.json` (`deny_*_regex`), `rules.tests.json` (the examples) and the `R-xx` line in `RULES.md`.
6. Confirm the project's `CLAUDE.md` imports `@.specs/project/RULES.md` (see `${CLAUDE_PLUGIN_ROOT}/shared/PROJECT_RULES.md`).

## Afterwards

- `rules.py test <root>` re-runs all the examples; the loop's `--check` does that on its own and **fails the preflight** if a rule stopped working.
- `rules.py listar <root>` shows the rules with a test.
- A new rule applies to the orchestrator and all subagents (hook `project-deny`).

## Do not

- Do not write without the user's phrase, nor by inference.
- Do not use this to **release** something (that is `allow` in `PERMISSIONS.json`, also only with an explicit phrase).
- Do not delete an old rule to make the new one pass: if it breaks, show the user the conflict.
