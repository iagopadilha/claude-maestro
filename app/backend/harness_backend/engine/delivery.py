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
  * a configured git identity (and LOOP_GIT_USER, if set in the app .env: ~/.loop-harness/.env or <plugin>/shared/.env).
PR: `gh pr create` (or reuses the branch's already open PR). Without `gh`, or with LOOP_PR_MODE=link, returns only the compare link
(https://github.com/<org>/<repo>/compare/<base>...<branch>?expand=1). LOOP_PR_MODE=off neither creates nor computes a PR.
Output: JSON {"push": ..., "pr": <url|None>, "pr_status": ...} (also in .specs/project/loop/json/delivery.json).

Multi-repo projects (meta.json has a `repos` map): `finalize_project()` runs `finalize()` once per repo worktree, each on its own
`loop/*` branch, one PR each against the integration branch; a failure in one repo never stops the others. Each worktree keeps its
own delivery.json; the primary's one is the aggregate (its top-level fields are the primary repo's, `repos` holds every result).
Each PR body gets a "Related PRs" block with the sibling links. Single-repo behaviour is unchanged.
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
import notify  # noqa: E402  (only to read variables from the app .env via envload)
import graph_view  # noqa: E402  (Mermaid graph in the PR body)

BASE_PROTECTED = {"main", "master", "develop", "developer", "dev", "release", "production"}
OPEN = {"pending", "speccing", "specced", "approved", "building"}
DEFAULT_BASE = "developer"


def integration_branch():
    """Integration branch (PR target): LOOP_PR_BASE / LOOP_INTEGRATION_BRANCH from the environment or the app .env, default `developer`."""
    notify.envload.load()
    for name in ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH"):
        value = os.environ.get(name) or notify.envload.get(name)
        if value and value.strip():
            return value.strip()
    return DEFAULT_BASE


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
    """Items of .specs/project/loop/VALIDATE.md: what the orchestrator decided on its own and the user should check."""
    items = []
    for line in read(loop_state.state_file(root, "VALIDATE.md")).splitlines():
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


def related_prs_block(related_prs, own_name=""):
    """The "Related PRs" section: the sibling repos' PR (or compare) links; empty when there is none."""
    links = [(name, link) for name, link in (related_prs or {}).items() if link and name != own_name]
    if not links:
        return []
    return ["", "## Related PRs", ""] + ["- %s: %s" % (name, link) for name, link in links]


def pr_body(root, branch, base, roadmap_root="", related_prs=None, own_name=""):
    roadmap_root = roadmap_root or root
    out = ["Orchestrator delivery: branch `%s` to `%s`." % (branch, base), ""]
    feats = features_of(roadmap_root)
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
    items = validate_items(roadmap_root)
    if items:
        out += ["", "## For you to validate", ""] + ["- " + item for item in items[:30]]
    _, log, _ = git(root, "log", "--oneline", "--no-decorate", "%s..%s" % ("origin/" + base, "HEAD"))
    if log:
        out += ["", "## Commits", "", "```", "\n".join(log.splitlines()[:30]), "```"]
    out += related_prs_block(related_prs, own_name)
    out += ["", "Generated from .specs/ (ROADMAP, VALIDATE). Nothing was merged: the merge decision is yours."]
    return "\n".join(out)


def identity_problem(root):
    _, user, _ = git(root, "config", "user.name")
    _, mail, _ = git(root, "config", "user.email")
    if not user or not mail:
        return "git user.name/user.email not configured"
    wanted = notify.envload.get("LOOP_GIT_USER")
    if wanted and user != wanted:
        return "git user.name is %r, expected %r (LOOP_GIT_USER in the app .env)" % (user, wanted)
    return ""


def finalize(root, branch="", base="", draft=False, dry_run=False, roadmap_root="", related_prs=None, own_name=""):
    """Push of the feature branch + PR. Returns {"push", "pr", "pr_status"}; never raises; never forces or merges.

    `roadmap_root` (multi-repo): where the ROADMAP lives (the primary worktree) when `root` is another repo's worktree."""
    roadmap_root = roadmap_root or root
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
        open_features = sorted(slug for slug, f in features_of(roadmap_root).items() if f.get("status") in OPEN)
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
                        handle.write(pr_body(root, branch, base, roadmap_root, related_prs, own_name))
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
        target = loop_state.loop_path(root, "delivery.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(dict(result, branch=branch, base=base, at=datetime.datetime.now().isoformat(timespec="seconds")), handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return result


def timestamp():
    return datetime.datetime.now().isoformat(timespec="seconds")


def edit_pr_body(root, pr_url, branch, base, roadmap_root, related_prs, own_name):
    """Rewrites the body of a PR THIS run created so it lists the siblings created after it. Best effort; True when edited."""
    if not shutil.which("gh"):
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as handle:
        handle.write(pr_body(root, branch, base, roadmap_root, related_prs, own_name))
        body_file = handle.name
    try:
        return run(["gh", "pr", "edit", pr_url, "--body-file", body_file], cwd=root, timeout=90)[0] == 0
    finally:
        os.unlink(body_file)


def read_base(root):
    """The PR base `finalize()` used, as it wrote it in that worktree's delivery.json (falls back to the integration branch)."""
    try:
        with open(loop_state.loop_path(root, "delivery.json"), encoding="utf-8") as handle:
            return json.load(handle).get("base") or integration_branch()
    except (OSError, ValueError):
        return integration_branch()


def finalize_project(primary_work, meta, draft=False, dry_run=False):
    """Delivery of the whole project. `meta` = the primary worktree's meta.json (`repos`: name -> {worktree, branch, ...}).

    Without a `repos` map this IS the single-repo delivery (same call as before). With it: one `finalize()` per repo, the primary
    first, then the sibling links are written into the PR bodies this run created. Returns the aggregate (top-level fields =
    the primary repo's, plus `repos`). The PR base is always the integration branch, never the branch of the user's folder."""
    repos = meta.get("repos")
    if not isinstance(repos, dict) or not repos:
        return finalize(primary_work, meta.get("branch", ""), draft=draft, dry_run=dry_run)
    names = sorted(repos, key=lambda name: not repos[name].get("primary"))
    primary_name = names[0]
    results, roots = {}, {}
    for name in names:
        entry = repos[name]
        root = os.path.abspath(primary_work if name == primary_name else (entry.get("worktree") or ""))
        roots[name] = root
        if not os.path.isdir(root):
            results[name] = {"push": "not done: worktree missing", "pr": None, "pr_status": "not created",
                             "branch": entry.get("branch", ""), "base": integration_branch(), "at": timestamp()}
            continue
        try:
            result = finalize(root, entry.get("branch", ""), draft=draft, dry_run=dry_run, roadmap_root=primary_work)
        except Exception as error:  # one repo failing never stops the others
            result = {"push": "FAILED: delivery.py: %s" % str(error)[:200], "pr": None, "pr_status": "not created"}
        results[name] = dict(result, branch=entry.get("branch", ""), base=read_base(root), at=timestamp())
    links = {name: result["pr"] for name, result in results.items() if result.get("pr")}
    if len(links) > 1:
        for name, result in results.items():
            if result.get("pr_status") == "PR created":
                edit_pr_body(roots[name], result["pr"], result["branch"], result["base"], primary_work, links, name)
    aggregate = dict(results[primary_name], repos=results)
    try:
        target = loop_state.loop_path(primary_work, "delivery.json")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            json.dump(aggregate, handle, ensure_ascii=False, indent=2)
    except OSError:
        pass
    return aggregate


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
    root = os.path.abspath(opts.root)
    meta = {}
    try:
        with open(loop_state.loop_path(root, "meta.json"), encoding="utf-8") as handle:
            meta = json.load(handle)
    except (OSError, ValueError):
        pass
    if isinstance(meta.get("repos"), dict) and meta["repos"] and not opts.branch and not opts.base:
        result = finalize_project(root, meta, opts.draft, opts.dry_run)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if all(item["push"].startswith(("done", "dry-run")) for item in result["repos"].values()) else 1
    result = finalize(root, opts.branch, opts.base, opts.draft, opts.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["push"].startswith(("done", "dry-run")) else 1


if __name__ == "__main__":
    sys.exit(main())
