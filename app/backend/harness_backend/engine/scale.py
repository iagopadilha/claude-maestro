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

ENGINE_DIR = os.path.dirname(os.path.abspath(__file__))
if ENGINE_DIR not in sys.path:
    sys.path.insert(0, ENGINE_DIR)
from classify_structure import SKIP_DIR_NAMES  # noqa: E402

CODE_EXT = {".cs", ".ts", ".tsx", ".js", ".jsx", ".py", ".java", ".go", ".rb", ".php", ".sql", ".vue", ".svelte", ".kt", ".rs", ".cpp", ".c", ".h", ".razor", ".cshtml", ".scss", ".css", ".html"}
SKIP = SKIP_DIR_NAMES | {"packages"}
INVENTORY_ROOT_NAME = "_root"
INVENTORY_RELATIVE_DIR = os.path.join(".specs", "project", "loop", "internal", "inventory")
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


def code_files(root):
    return [f for f in list_files(root) if os.path.splitext(f)[1].lower() in CODE_EXT]


def last_commit_time(folder):
    """Unix time of the last commit touching the folder, or None (no git, no commit)."""
    try:
        out = subprocess.run(["git", "-C", folder, "log", "-1", "--format=%ct", "--", "."], capture_output=True, text=True, timeout=30)
        return int(out.stdout.strip()) if out.returncode == 0 and out.stdout.strip() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def find_inventory_file(folder):
    """The inventory file of the folder (`internal/inventory/<name>.json` of the nearest parent with `.specs`), or None."""
    folder = os.path.abspath(folder)
    current = folder
    while True:
        if os.path.isdir(os.path.join(current, ".specs")):
            relative = os.path.relpath(folder, current).replace("\\", "/")
            name = INVENTORY_ROOT_NAME if relative == "." else relative.replace("/", "-")
            path = os.path.join(current, INVENTORY_RELATIVE_DIR, name + ".json")
            return path if os.path.isfile(path) else None
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


def inventory_code_count(folder):
    """Code-file count of a fresh inventory (file newer than the folder's last commit), else None."""
    path = find_inventory_file(folder)
    commit_time = last_commit_time(folder) if path else None
    if not path or commit_time is None:
        return None
    try:
        if os.path.getmtime(path) < commit_time:
            return None
        with open(path, encoding="utf-8") as handle:
            return int(json.load(handle)["totals"]["files"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def count_code_files(folder):
    """Number of code files of the folder: from the free inventory when it is fresh, else measured here."""
    counted = inventory_code_count(folder)
    return counted if counted is not None else len(code_files(folder))


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
    counted = inventory_code_count(root)
    return {"files": len(files), "code": counted if counted is not None else len(code), "code_kb": size // 1024,
            "areas": dict(sorted(tops.items(), key=lambda kv: -kv[1])[:8])}


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
