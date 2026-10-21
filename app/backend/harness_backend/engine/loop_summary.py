#!/usr/bin/env python3
"""Generates the loop's final summary in Markdown, WITHOUT Claude: it only reads files and the git log.

Usage:
  python loop_summary.py <project-or-worktree-root> [--out <folder>] [--base <ref>] [--branch <name>]

Reads (everything optional, whatever is missing becomes "not reported"):
  .specs/project/ROADMAP.md           lines "- [slug] status=... scope=... kind=... paths=..."
  .specs/project/phases/stage-NN.md   fixed sections: Delivered, User decisions, Lessons, Unverified,
                                      Blocked, For the next stage
  .specs/project/STATE.md             STOP block and open questions
  .specs/project/loop/json/LOOP.json  last orchestrator status
  .specs/project/loop/json/meta.json  written by run_loop.py (branch, base, turns, stop reason)
  .specs/project/loop/internal/turns/NNN.json  output of each `claude -p` turn (cost, duration)
  git log <base>..<branch>            loop commits

Output: <out>/loop-summary-<project>-<YYYY-MM-DD>.md (default: the app's output folder, `output_dir` in config.json,
~/Downloads when unset; never the repo or the current directory).
"""
import datetime
import glob
import json
import os
import re
import subprocess
import sys

LINE = re.compile(r"^- \[([^\]]+)\]\s+(.*)$")
SECTIONS = ["Delivered", "User decisions", "Lessons", "Unverified", "Blocked", "For the next stage"]


def default_output_dir():
    """The app's output folder; ~/Downloads when the app package cannot be loaded (the script copied alone somewhere)."""
    try:
        import app_paths
        return app_paths.default_output_dir()
    except Exception:
        return os.path.join(os.path.expanduser("~"), "Downloads")


def read(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError):
        return {}


def git(root, *args):
    try:
        out = subprocess.run(["git", "-C", root, *args], capture_output=True, text=True, timeout=60)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def parse_roadmap(text):
    stages, current = [], None
    for raw in text.splitlines():
        if raw.startswith("## "):
            current = {"title": raw[3:].strip(), "features": []}
            stages.append(current)
            continue
        match = LINE.match(raw.strip())
        if match and current is not None:
            fields = dict(re.findall(r"(\w+)=(\S+)", match.group(2)))
            fields["slug"] = match.group(1)
            current["features"].append(fields)
    return stages


def sections(text):
    found, name = {}, None
    for raw in text.splitlines():
        if raw.startswith("## "):
            name = raw[3:].strip()
            found[name] = []
        elif name is not None:
            found[name].append(raw)
    return {key: "\n".join(value).strip() for key, value in found.items()}


def add_repositories_section(add, meta, primary_root):
    """Multi-repo only (meta.json has a `repos` map): one block per repo with branch, push, PR and its own commits."""
    repos = meta.get("repos")
    if not isinstance(repos, dict) or not repos:
        return
    add("")
    add("## Repositories")
    for name in sorted(repos, key=lambda key: not repos[key].get("primary")):
        entry = repos[name]
        worktree = primary_root if entry.get("primary") else (entry.get("worktree") or "")
        delivery = load_json(os.path.join(worktree, ".specs", "project", "loop", "json", "delivery.json")) if worktree else {}
        add("")
        add("### %s%s" % (name, " (primary)" if entry.get("primary") else ""))
        add("")
        add("- Branch: `%s`%s" % (entry.get("branch") or "not reported", (" (forked from `%s`)" % entry["base"]) if entry.get("base") else ""))
        add("- Work folder: `%s`" % (worktree or "not reported"))
        add("- Branch push: %s" % (delivery.get("push") or "not done"))
        if delivery.get("pr"):
            add("- **PR: %s** (%s)" % (delivery["pr"], delivery.get("pr_status", "")))
        base = delivery.get("base") or entry.get("base")
        commits = git(worktree, "log", "--oneline", "--no-decorate", (base + ".." + (entry.get("branch") or "HEAD")) if base else "-30") if worktree and os.path.isdir(worktree) else ""
        add("")
        add("```\n%s\n```" % (commits or "no commits found"))


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    root = os.path.abspath(args[0])

    def opt(flag, default=""):
        return args[args.index(flag) + 1] if flag in args else default

    project = os.path.basename(root.rstrip("/\\"))
    project = re.sub(r"-(worktree-)?loop$", "", project)
    specs = os.path.join(root, ".specs")
    meta = load_json(os.path.join(specs, "project", "loop", "json", "meta.json"))
    loop = load_json(os.path.join(specs, "project", "loop", "json", "LOOP.json"))
    branch = opt("--branch", meta.get("branch") or git(root, "branch", "--show-current"))
    base = opt("--base", meta.get("base", ""))

    cost, duration, turns = 0.0, 0.0, 0
    for path in sorted(glob.glob(os.path.join(specs, "project", "loop", "internal", "turns", "[0-9]*.json"))):
        data = load_json(path)
        turns += 1
        cost += float(data.get("total_cost_usd") or 0)
        duration += float(data.get("duration_ms") or 0)

    stages = parse_roadmap(read(os.path.join(specs, "project", "ROADMAP.md")))
    counts = {}
    for stage in stages:
        for feature in stage["features"]:
            counts[feature.get("status", "?")] = counts.get(feature.get("status", "?"), 0) + 1

    commits = git(root, "log", "--oneline", "--no-decorate", (base + ".." + branch) if base else "-30")
    out = []
    add = out.append
    add("# Loop summary: %s" % project)
    add("")
    add("- Generated: %s (by script, no Claude)" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M"))
    add("- Loop branch: `%s`%s" % (branch or "not reported", (" (base `%s`)" % base) if base else ""))
    add("- Work folder: `%s`" % root)
    add("- Turns run: %s | total cost: US$ %.2f | time in turns: %.0f min" % (turns or meta.get("turns", "?"), cost, duration / 60000))
    cost_cap = float(meta.get("max_cost_usd") or 0) or float(opt("--max-cost", 0) or 0)
    add("- Spend: US$ %.2f of the US$ %s cap | turns: %s" % (float(meta.get("cost_usd", cost) or 0), ("%.2f" % cost_cap) if cost_cap else "no", turns or meta.get("turns", "?")))
    add("- Stop reason: **%s**%s" % (meta.get("stopped") or loop.get("status") or "not reported", (" - " + (meta.get("reason") or loop.get("reason"))) if (meta.get("reason") or loop.get("reason")) else ""))
    add("- Branch push: %s" % meta.get("push", "not done"))
    if meta.get("pr"):
        add("- **PR: %s** (%s)" % (meta["pr"], meta.get("pr_status", "")))
    add("- Features: " + (", ".join("%s %s" % (v, k) for k, v in sorted(counts.items())) or "none found in the ROADMAP"))
    add("")

    add("## Features by stage")
    if not stages:
        add("ROADMAP.md not found.")
    for stage in stages:
        add("")
        add("### " + stage["title"])
        add("")
        add("| Feature | Status | Scope | Kind | Paths | Notes |")
        add("|---|---|---|---|---|---|")
        for feature in stage["features"]:
            obs = " ".join(filter(None, [feature.get("note", ""), ("origin " + feature["origin"]) if feature.get("origin") else ""]))
            add("| %s | %s | %s | %s | %s | %s |" % (feature["slug"], feature.get("status", "?"), feature.get("scope", "-"), feature.get("kind", "-"), feature.get("paths", "-"), obs or "-"))

    try:
        import graph_view
        features_now = graph_view.load(root)
        add("")
        add("## Progress by feature")
        add("")
        add("```\n%s\n```" % graph_view.ascii_graph(features_now))
        add("")
        add("```mermaid\n%s\n```" % graph_view.mermaid(features_now))
    except Exception:
        pass

    try:
        import loop_state
        add("")
        add("## State graph")
        add("")
        add("```mermaid\n%s\n```" % loop_state.graph(read(os.path.join(specs, "project", "ROADMAP.md"))))
    except Exception:
        pass

    add("")
    add("## Loop commits")
    add("")
    add("```\n%s\n```" % (commits or "no commits found"))

    add_repositories_section(add, meta, root)

    phases = sorted(glob.glob(os.path.join(specs, "project", "phases", "stage-*.md")))
    for path in phases:
        parts = sections(read(path))
        add("")
        add("## Closing: %s" % os.path.splitext(os.path.basename(path))[0])
        for name in SECTIONS:
            body = parts.get(name, "").strip()
            if body:
                add("")
                add("**%s**" % name)
                add("")
                add(body)
    if not phases:
        add("")
        add("## Stage closing")
        add("")
        add("No `phases/stage-NN.md` found (the loop stopped before closing a stage).")

    state = read(os.path.join(specs, "project", "STATE.md"))
    pending = [block for block in re.split(r"\n(?=#+ )", state) if re.match(r"#+\s*(STOP|Questions|Quarantine)", block, re.I)]
    if pending:
        add("")
        add("## For you to decide (from STATE.md)")
        for block in pending:
            add("")
            add(block.strip())

    to_validate = [line.strip()[2:] for line in read(os.path.join(specs, "project", "loop", "VALIDATE.md")).splitlines() if line.strip().startswith("- ")]
    if to_validate:
        add("")
        add("## For you to validate (decided without stopping the loop)")
        add("")
        for item in to_validate:
            add("- " + item)

    if loop.get("next"):
        add("")
        add("## Next step")
        add("")
        add(str(loop["next"]))

    dest = opt("--out", "") or default_output_dir()
    os.makedirs(dest, exist_ok=True)
    target = os.path.join(dest, "loop-summary-%s-%s.md" % (project, datetime.date.today().isoformat()))
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(out) + "\n")
    print(target)
    return 0


if __name__ == "__main__":
    sys.exit(main())
