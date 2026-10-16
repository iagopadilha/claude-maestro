#!/usr/bin/env python3
"""Draws the loop progress in 3 formats, from the ROADMAP (skill orchestrator-show-progress-graph). Stdlib only, no AI.

Usage:
  python graph_view.py <root> [--ascii | --short | --mermaid | --write]

  --ascii    (default) plain text, one line per feature, with dependencies. For chat, email (<pre>) and checkpoints.
  --short    one line per stage, no drawing (size-limited channels).
  --mermaid  feature graph (stages as groups, deps= arrows) for GitHub (PR) and the .md summary.
  --write    writes .specs/project/loop/GRAPH.md (date + ASCII + Mermaid) and prints the path.

Legend: [x] verified  [>] building/speccing  [.] pending/specced/approved  [!] blocked.
"""
import datetime
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import loop_state  # noqa: E402

DONE, BUSY, OPEN, BLOCKED = "verified", ("building", "speccing"), ("pending", "specced", "approved"), "blocked"
SYMBOL = {"verified": "[x]", "building": "[>]", "speccing": "[>]", "pending": "[.]", "specced": "[.]", "approved": "[.]", "blocked": "[!]"}
WORD = {"verified": "done", "building": "building", "speccing": "spec", "pending": "pending", "specced": "spec ok", "approved": "approved", "blocked": "BLOCKED"}


def load(root):
    path = os.path.join(os.path.abspath(root), ".specs", "project", "ROADMAP.md")
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return loop_state.parse_roadmap(handle.read())[0]
    except OSError:
        return {}


def stages(features):
    order = []
    for slug, f in features.items():
        stage = f.get("stage", "")
        if stage not in [name for name, _ in order]:
            order.append((stage, []))
        dict(order)[stage].append(slug)
    return order


def waiting_on(features, slug):
    """Dependency text: `api(done) ui(building) dir:packages/ui`."""
    slugs, folders = loop_state.split_deps(features[slug])
    parts = ["%s(%s)" % (dep, WORD.get(features.get(dep, {}).get("status", "?"), "?")) for dep in slugs]
    return parts + ["dir:" + folder for folder in folders]


def ascii_graph(features):
    if not features:
        return "(ROADMAP has no features)"
    out, width = [], max(len(s) for s in features) + 1
    for stage, slugs in stages(features):
        done = sum(features[s]["status"] in (DONE, BLOCKED) for s in slugs)
        out.append("Stage %s  (%d/%d closed)" % (stage or "?", done, len(slugs)))
        for slug in slugs:
            f = features[slug]
            line = "  %s %-*s %s" % (SYMBOL.get(f["status"], "[?]"), width, slug, WORD.get(f["status"], f["status"]))
            deps = waiting_on(features, slug)
            if f["status"] != DONE and deps:
                line += "   <- " + " ".join(deps)
            stuck = loop_state.stuck_by_blocked(features, slug)
            if stuck and f["status"] != BLOCKED:
                line += "   STUCK on: " + ", ".join(stuck)
            if f["status"] == BLOCKED and f.get("tries"):
                line += "   (%s failures)" % f["tries"]
            out.append(line)
    out.append("")
    out.append("Legend: [x] done  [>] in progress  [.] to do  [!] blocked  (<- waits for)")
    return "\n".join(out)


def short(features, limit=300):
    if not features:
        return "ROADMAP empty"
    parts = []
    for stage, slugs in stages(features):
        items = ", ".join("%s %s" % (s, WORD.get(features[s]["status"], features[s]["status"])) for s in slugs)
        parts.append("S%s: %s" % (stage or "?", items))
    text = " | ".join(parts)
    if len(text) <= limit:
        return text
    counts = {}
    for f in features.values():
        counts[f["status"]] = counts.get(f["status"], 0) + 1
    return ("%d features: " % len(features) + ", ".join("%d %s" % (n, WORD.get(k, k)) for k, n in sorted(counts.items())))[:limit]


def node_id(slug):
    return "n_" + re.sub(r"\W", "_", slug)


def mermaid(features):
    lines = ["flowchart LR"]
    for stage, slugs in stages(features):
        lines.append('  subgraph S%s["Stage %s"]' % (re.sub(r"\W", "_", stage or "x"), stage or "?"))
        for slug in slugs:
            lines.append('    %s["%s<br/>%s"]:::%s' % (node_id(slug), slug, WORD.get(features[slug]["status"], "?"), features[slug]["status"]))
        lines.append("  end")
    for slug in features:
        for dep in loop_state.split_deps(features[slug])[0]:
            if dep in features:
                lines.append("  %s --> %s" % (node_id(dep), node_id(slug)))
    lines += ["  classDef verified fill:#d1fae5,stroke:#047857", "  classDef building fill:#dbeafe,stroke:#1d4ed8", "  classDef speccing fill:#dbeafe,stroke:#1d4ed8",
              "  classDef blocked fill:#fee2e2,stroke:#b91c1c", "  classDef pending fill:#f3f4f6,stroke:#6b7280", "  classDef specced fill:#f3f4f6,stroke:#6b7280",
              "  classDef approved fill:#fef3c7,stroke:#b45309"]
    return "\n".join(lines)


def write(root):
    features = load(root)
    target = loop_state.state_file(os.path.abspath(root), "GRAPH.md")
    os.makedirs(os.path.dirname(target), exist_ok=True)
    text = "# Progress (generated by graph_view.py on %s; do not edit)\n\n```\n%s\n```\n\n```mermaid\n%s\n```\n" % (
        datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), ascii_graph(features), mermaid(features))
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return target


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    root = args[0]
    if "--write" in args:
        print(write(root))
        return 0
    features = load(root)
    if "--mermaid" in args:
        print(mermaid(features))
    elif "--short" in args:
        print(short(features))
    else:
        print(ascii_graph(features))
    return 0


if __name__ == "__main__":
    sys.exit(main())
