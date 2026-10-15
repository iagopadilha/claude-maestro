#!/usr/bin/env python3
"""End-of-flow delivery: push of the FEATURE BRANCH and a PR to the integration branch, with the link in the final report.

Usage (orchestrator in chat, when closing the last roadmap item; run_loop.py calls `finalize()` by itself):
  python delivery.py <repo-or-worktree-root> [--base <integration branch>] [--branch NAME] [--draft] [--dry-run]

The integration branch (PR target) is `--base`, else env/.env LOOP_INTEGRATION_BRANCH, else `developer`.

Rules (user decision; no flag turns them off):
  * feature branch only: the integration branch, develop, main, master, release, production, dev are REFUSED (push and PR from/to them);
  * only at the END: no open feature (pending/speccing/specced/approved/building) left in the ROADMAP;
  * no force, no merge, no branch deletion;
  * only the orchestrator (the guard blocks executor/verifier/spec) and never in the headless loop (LOOP_HEADLESS=1: the script delivers);
  * a configured git identity (and LOOP_GIT_USER, if set in shared/.env).
PR: `gh pr create` (or reuses the branch's already open PR). Without `gh`, or with LOOP_PR_MODE=link, returns only the compare link
(https://github.com/<org>/<repo>/compare/<base>...<branch>?expand=1). LOOP_PR_MODE=off neither creates nor computes a PR.
Output: JSON {"push": ..., "pr": <url|None>, "pr_status": ...} (also in .specs/loop-logs/delivery.json).
"""
import argparse
import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loop_state  # noqa: E402
import notify  # noqa: E402  (only to read variables from shared/.env via envload)
import graph_view  # noqa: E402  (Mermaid graph in the PR body)

BASE_PROTECTED = {"main", "master", "develop", "developer", "dev", "release", "production"}
OPEN = {"pending", "speccing", "specced", "approved", "building"}
DEFAULT_BASE = "developer"


def integration_branch():
    """Integration branch (PR target): LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH from the environment or shared/.env, default `developer`."""
    notify.envload.load()
    return (os.environ.get("LOOP_PR_BASE") or os.environ.get("LOOP_INTEGRATION_BRANCH") or DEFAULT_BASE).strip()


def protected_names():
    return BASE_PROTECTED | {integration_branch().lower()}


def run(args, cwd=None, timeout=120):
    try:
        out = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
        return out.returncode, out.stdout.strip(), out.stderr.strip()
    except (OSError, subprocess.SubprocessError) as error:
        return 1, "", str(error)


def git(cwd, *args):
    return run(["git", "-C", cwd, *args])


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def features_of(root):
    return loop_state.parse_roadmap(read(os.path.join(root, ".specs", "project", "ROADMAP.md")))[0]


def validate_items(root):
    """Items of .specs/project/VALIDATE.md: what the orchestrator decided on its own and the user should check."""
    items = []
    for line in read(os.path.join(root, ".specs", "project", "VALIDATE.md")).splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return items


def github_slug(remote_url):
    """git@github.com:org/repo.git | https://github.com/org/repo(.git) -> 'org/repo' (None if not GitHub)."""
    match = re.search(r"github\.com[:/]+([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", remote_url or "")
    return "%s/%s" % (match.group(1), match.group(2)) if match else None


def compare_url(remote_url, base, branch):
    slug = github_slug(remote_url)
    return "https://github.com/%s/compare/%s...%s?expand=1" % (slug, base, branch) if slug else None


def pr_body(root, branch, base):
    out = ["Orchestrator delivery: branch `%s` to `%s`." % (branch, base), ""]
    feats = features_of(root)
    verified = [s for s, f in feats.items() if f.get("status") == "verified"]
    blocked = [s for s, f in feats.items() if f.get("status") == "blocked"]
    out.append("**Verified features (%d):** %s" % (len(verified), ", ".join(verified) or "none"))
    if blocked:
        out += ["", "**Quarantined (NOT delivered, your call):** " + ", ".join(blocked)]
    unverified = [s for s, f in feats.items() if f.get("note") and "unverified" in f.get("note", "")]
    if unverified:
        out += ["", "**Without automatic verification:** " + ", ".join(unverified)]
    try:
        out += ["", "## Progress", "", "```mermaid", graph_view.mermaid(feats), "```"]
    except Exception:
        pass
    items = validate_items(root)
    if items:
        out += ["", "## For you to validate", ""] + ["- " + item for item in items[:30]]
    _, log, _ = git(root, "log", "--oneline", "--no-decorate", "%s..%s" % ("origin/" + base, "HEAD"))
    if log:
        out += ["", "## Commits", "", "```", "\n".join(log.splitlines()[:30]), "```"]
    out += ["", "Generated from .specs/ (ROADMAP, VALIDATE). Nothing was merged: the merge decision is yours."]
    return "\n".join(out)


def identity_problem(root):
    _, user, _ = git(root, "config", "user.name")
    _, mail, _ = git(root, "config", "user.email")
    if not user or not mail:
        return "git user.name/user.email not configured"
    wanted = notify.envload.get("LOOP_GIT_USER")
    if wanted and user != wanted:
        return "git user.name is %r, expected %r (LOOP_GIT_USER in shared/.env)" % (user, wanted)
    return ""


def finalize(root, branch="", base="", draft=False, dry_run=False):
    """Push of the feature branch + PR. Returns {"push", "pr", "pr_status"}; never raises; never forces or merges."""
    notify.envload.load()
    result = {"push": "not done", "pr": None, "pr_status": "not created"}
    branch = branch or git(root, "branch", "--show-current")[1]
    base = base or integration_branch()
    mode = (os.environ.get("LOOP_PR_MODE") or notify.envload.get("LOOP_PR_MODE") or "auto").lower()
    if not branch:
        result["push"] = "not done: detached HEAD (no branch)"
    elif branch.lower() in protected_names():
        result["push"] = "refused: %s is a protected branch; feature branch only" % branch
    elif branch == base:
        result["push"] = "refused: the feature branch cannot be the PR base (%s)" % base
    else:
        open_features = sorted(slug for slug, f in features_of(root).items() if f.get("status") in OPEN)
        problem = identity_problem(root)
        origin = git(root, "remote", "get-url", "origin")[1]
        if open_features:
            result["push"] = "not done: there are still open features (%s); push only at the end" % ", ".join(open_features[:6])
        elif problem:
            result["push"] = "refused: " + problem
        elif not origin:
            result["push"] = "not done: no origin remote"
        elif dry_run:
            result["push"] = "dry-run: git push -u origin %s" % branch
        else:
            code, out, err = git(root, "push", "-u", "origin", branch)
            result["push"] = ("done: origin/%s" % branch) if code == 0 else "FAILED: " + (err or out)[:300]
        if result["push"].startswith(("done", "dry-run")) and mode != "off":
            link = compare_url(origin, base, branch)
            result["pr"], result["pr_status"] = link, "compare link (PR not created)"
            if mode != "link" and shutil.which("gh") and not dry_run:
                code, existing, _ = run(["gh", "pr", "list", "--head", branch, "--base", base, "--state", "open", "--json", "url", "-q", ".[0].url"], cwd=root, timeout=60)
                if code == 0 and existing:
                    result["pr"], result["pr_status"] = existing, "PR already existed"
                else:
                    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
                        handle.write(pr_body(root, branch, base))
                        body_file = handle.name
                    title = "[orchestrator] %s" % branch
                    cmd = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title, "--body-file", body_file]
                    if draft or (os.environ.get("LOOP_PR_DRAFT") or notify.envload.get("LOOP_PR_DRAFT")) in ("1", "true"):
                        cmd.append("--draft")
                    code, out, err = run(cmd, cwd=root, timeout=90)
                    os.unlink(body_file)
                    url = next((line.strip() for line in out.splitlines() if line.strip().startswith("http")), "")
                    if code == 0 and url:
                        result["pr"], result["pr_status"] = url, "PR created"
                    else:
                        result["pr_status"] = "compare link; gh failed: " + (err or out)[:200]
    try:
        folder = os.path.join(root, ".specs", "loop-logs")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "delivery.json"), "w", encoding="utf-8") as handle:
            json.dump(dict(result, branch=branch, base=base, at=datetime.datetime.now().isoformat(timespec="seconds")), handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return result


def main():
    parser = argparse.ArgumentParser(description="Push of the feature branch + PR to the integration branch (end of flow)")
    parser.add_argument("root")
    parser.add_argument("--branch", default="")
    parser.add_argument("--base", default="", help="PR target branch (default: LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH, else developer)")
    parser.add_argument("--draft", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    opts = parser.parse_args()
    if os.environ.get("LOOP_HEADLESS") == "1":
        print(json.dumps({"push": "refused: in the headless loop the script delivers (run_loop.py)", "pr": None, "pr_status": "not created"}, ensure_ascii=False))
        return 2
    result = finalize(os.path.abspath(opts.root), opts.branch, opts.base, opts.draft, opts.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["push"].startswith(("done", "dry-run")) else 1


if __name__ == "__main__":
    sys.exit(main())
