#!/usr/bin/env python3
"""Command line of `run_loop.py` (`python run_loop.py <repo> ...`, or `python harness.py loop <repo> ...`, which forwards here).

`build_parser()` returns the argparse parser exactly as `main()` used to build it inline: same flags, defaults and help text.
"""
import argparse
import os
import sys

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
from claude_turn import DEFAULT_ORCHESTRATOR_MODEL  # noqa: E402


def build_parser():
    parser = argparse.ArgumentParser(description="Unsupervised /orchestrator loop")
    parser.add_argument("repo")
    parser.add_argument("--wave", type=int, default=3, help="features (lanes) in parallel per turn, each with its own agents at every step (execution only with disjoint PATHS and met dependencies); default 3")
    parser.add_argument("--e2e", action="store_true", help="authorizes the final flow-validation phase (verifier-flow, headless)")
    parser.add_argument("--push", action="store_true", default=None, help="requires the final push+PR (fails the preflight if there is no remote/identity). Without the flag push+PR is already the DEFAULT (feature branch only)")
    parser.add_argument("--no-push", dest="push", action="store_false", help="do not push the branch nor open a PR at the end")
    parser.add_argument("--budget", type=float, default=5.0, help="max US$ per turn")
    parser.add_argument("--timeout", type=int, default=60, help="max minutes per turn")
    parser.add_argument("--max-stall", type=int, default=10, help="consecutive turns without progress before stopping")
    parser.add_argument("--limit-wait", type=int, default=30, help="minutes to wait when the usage limit is hit")
    parser.add_argument("--limit-retries", type=int, default=8)
    parser.add_argument("--permission-mode", default="auto", help="auto | acceptEdits | dontAsk")
    parser.add_argument("--branch", default="")
    parser.add_argument("--base-branch", default="", help="branch new worktrees start from and the PR target (the ROADMAP `Branch base`); default: LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH, else developer")
    parser.add_argument("--git-user", default="", help="requires this git user.name on push (or LOOP_GIT_USER in the app .env); default: any configured account")
    parser.add_argument("--setup", default="", help='one-time command after creating the worktree (e.g. "npm ci")')
    parser.add_argument("--claude", default="", help="claude binary (or a test .py)")
    parser.add_argument("--claude-fallback", action="append", default=[], metavar="COMMAND",
                        help="command tried when the running one hits the account usage limit (repeatable, in order; e.g. `cc`, a wrapper signed in with another account)")
    parser.add_argument("--option", type=int, default=0, metavar="N", help="answers the question by the NUMBER of the option listed in the notice (same as --answer with that option's text)")
    parser.add_argument("--answer", default="", help="answers the question that stopped the loop (awaiting_user) and continues; goes to ANSWERS.md")
    parser.add_argument("--max-cost", type=float, default=50.0, help="cap of US$ summed over all turns (0 turns it off); a cap raised in the panel (meta max_cost_usd) wins; when exceeded the loop PAUSES (resumable)")
    parser.add_argument("--orchestrator-model", default=DEFAULT_ORCHESTRATOR_MODEL, help="model of the orchestrator turns (--model of claude); empty = no flag, the CLI default")
    parser.add_argument("--rewind", type=int, default=None, metavar="N", help="go back to the state right after turn N (state files + git reset --hard of the loop branch). Without --yes it only shows the plan")
    parser.add_argument("--yes", action="store_true", help="confirms --rewind")
    parser.add_argument("--leftovers", choices=["commit", "discard"], default="", help="uncommitted files outside .specs/ left in the worktree: commit them all as a checkpoint or discard them, then carry on (the panel's recovery buttons)")
    parser.add_argument("--summary-dir", default="", help="folder of the final summary (default: the app's output folder, ~/Downloads when unset)")
    parser.add_argument("--json", action="store_true", help="with --check: after the text lines, print one `[check] JSON: {...}` line with problems, warnings and `items` (kind, repo, files, text, fix)")
    parser.add_argument("--check", action="store_true", help="only checks the preflight, prints the ready command and exits (creates nothing)")
    parser.add_argument("--planning", action="store_true", help="with --check: readiness of a roadmap NOT yet approved (skips the approval, adds worktree, feature-path, MCP, credential and test-command checks)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--ensure-worktrees", action="store_true", help="only creates (or re-attaches) the worktrees as the loop start does, prints the `[loop] [<repo>] <kind>: <text>` lines and exits; no preflight, no turn")
    parser.add_argument("--repos", default="", help="with --ensure-worktrees: comma-separated repo names to create (the primary is always ensured)")
    parser.add_argument("--continue-on-blocked", action="store_true", help="a quarantined feature NEVER pauses the loop, not even when another depends on it")
    parser.add_argument("--pause-on-blocked", action="store_true", help="a quarantined feature ALWAYS pauses and notifies (this is now the default; kept for compatibility; formerly: only pauses when another feature depends on the blocked one)")
    parser.add_argument("--allow-silent", action="store_true", help="testing only: waives the mandatory notice channels (LOOP_NOTIFY_REQUIRED)")
    parser.add_argument("--no-auto-answer", dest="auto_answer", action="store_false", default=True,
                        help="turns the automatic answer off: every orchestrator question stops the loop and waits for you (default: the script answers on its own the ones that do not need you)")
    parser.add_argument("--no-monitor", action="store_true", help="does not start the loop web panel (default: starts on its own; also off with LOOP_MONITOR=off in the app .env)")
    parser.add_argument("--gate-tiebreaks", type=int, default=1, help="gate tiebreaks per feature: after several rejections the script tells the orchestrator to approve the spec and run it (0 turns it off)")
    parser.add_argument("--max-auto-answers", type=int, default=12, help="cap of automatic answers per script run")
    parser.add_argument("--auto-answers-per-feature", type=int, default=3, help="cap of automatic answers for the same feature (avoids spinning forever on a gate that rejects)")
    return parser
