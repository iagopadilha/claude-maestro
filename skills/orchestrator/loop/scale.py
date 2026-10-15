#!/usr/bin/env python3
"""Sizes Phase 1 (brownfield): how many `brownfield-mapper` agents to dispatch and which documents each writes.

Usage:  python scale.py <repo-root> [--json]

Measures the repo (versioned and code files, size, top-level areas) and returns a tier:
  small   (up to ~150 code files)   -> 1 agent writes the 7 documents
  medium  (up to ~1200)             -> 3 agents (related groups)
  large   (above)                   -> 7 agents, 1 document each (cap of 3 at a time)
The user outranks the tier: if they ask for more or fewer agents, that is what applies.
No Claude, it only reads the repo. Writes nothing.
"""
import json
import os
import subprocess
import sys

CODE_EXT = {".cs", ".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".go", ".rb", ".php", ".sql", ".vue", ".svelte", ".kt", ".rs", ".cpp", ".c", ".h", ".razor", ".cshtml", ".scss", ".css", ".html"}
SKIP = {"node_modules", "bin", "obj", ".git", ".specs", "dist", "build", ".next", ".vs", "packages", "__pycache__", "coverage"}
DOCS = ["STACK", "ARCHITECTURE", "CONVENTIONS", "STRUCTURE", "TESTING", "INTEGRATIONS", "CONCERNS"]
GROUPS = {
    "small": [DOCS],
    "medium": [["STACK", "STRUCTURE", "CONVENTIONS"], ["ARCHITECTURE", "INTEGRATIONS"], ["TESTING", "CONCERNS"]],
    "large": [[doc] for doc in DOCS],
}


def list_files(root):
    try:
        out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True, timeout=60)
        if out.returncode == 0 and out.stdout.strip():
            return [line for line in out.stdout.splitlines() if not SKIP.intersection(line.replace("\\", "/").split("/")[:-1])]
    except (OSError, subprocess.SubprocessError):
        pass
    found = []
    for folder, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in SKIP]
        found += [os.path.relpath(os.path.join(folder, n), root) for n in names]
    return found


def measure(root):
    files = list_files(root)
    code = [f for f in files if os.path.splitext(f)[1].lower() in CODE_EXT]
    size = 0
    for name in code[:5000]:
        try:
            size += os.path.getsize(os.path.join(root, name))
        except OSError:
            pass
    tops = {}
    for name in code:
        parts = name.replace("\\", "/").split("/")
        tops[parts[0] if len(parts) > 1 else "."] = tops.get(parts[0] if len(parts) > 1 else ".", 0) + 1
    return {"files": len(files), "code": len(code), "code_kb": size // 1024, "areas": dict(sorted(tops.items(), key=lambda kv: -kv[1])[:8])}


def tier_of(measure_):
    code = measure_["code"]
    return "small" if code <= 150 else ("medium" if code <= 1200 else "large")


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    root = os.path.abspath(args[0])
    info = measure(root)
    tier = tier_of(info)
    plan = {"tier": tier, "agents": len(GROUPS[tier]), "groups": GROUPS[tier], "measured": info}
    if "--json" in args:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
    else:
        print("%s: %d code files (%d KB) -> %d agent(s)" % (tier, info["code"], info["code_kb"], plan["agents"]))
        for number, group in enumerate(plan["groups"], 1):
            print("  agent %d: DOCS: %s" % (number, ",".join(group)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
