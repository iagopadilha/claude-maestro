#!/usr/bin/env python3
"""Classifies a project's structure as STANDARD or LEGACY and (once confirmed) writes it to CLAUDE.md.

Usage:
  python classify_structure.py <root>                      # shows evidence and the SUGGESTION (writes nothing)
  python classify_structure.py <root> --write STANDARD|LEGACY [--note "text"]   # writes the Conformance section

The suggestion is only a suggestion: the user decides. Without a "Conformance" section in the project's CLAUDE.md, the skill
must explain why, show this evidence and ASK the user before writing.

STANDARD = the skill's structure: back end with Api/Data/Domain (.NET projects or NestJS folders under src/);
           front end with src/pages + src/components (+ services/hooks).
LEGACY   = anything else. New code imitates the project; old files only get minimal edits.
"""
import datetime
import glob
import os
import re
import sys

BEGIN = "<!-- CONFORMANCE:BEGIN -->"
END = "<!-- CONFORMANCE:END -->"
# The one shared list of folder names no scan descends into (classify_structure, scale, subprojects and code_inventory import it).
SKIP_DIR_NAMES = frozenset({"node_modules", "bin", "obj", ".git", "dist", "build", ".next", ".specs", ".vs", "coverage", "__pycache__"})
SKIP = SKIP_DIR_NAMES


def subdirs(path):
    try:
        return [name for name in os.listdir(path) if os.path.isdir(os.path.join(path, name)) and name not in SKIP]
    except OSError:
        return []


def analyze(root):
    evidence = []
    stack = "unknown"
    verdict = "LEGACY"
    top = subdirs(root)
    lower_top = {name.lower(): name for name in top}
    csproj_here = glob.glob(os.path.join(root, "*.csproj"))
    solutions = glob.glob(os.path.join(root, "*.sln"))
    package = os.path.join(root, "package.json")
    if solutions or csproj_here:
        stack = ".NET"
        layers = {}
        for layer in ("api", "data", "domain"):
            found = [name for name in top if re.search(r"(^|\.)%s$" % layer, name, re.I)]
            layers[layer] = found
            evidence.append("project/folder %s: %s" % (layer.capitalize(), ", ".join(found) if found else "NOT FOUND"))
        if all(layers.values()):
            verdict = "STANDARD"
        if csproj_here and not all(layers.values()):
            evidence.append(".csproj in the root (single project): flat structure")
        flat = [name for name in top if name in ("Controllers", "Services", "Models", "Utils", "Interfaces")]
        if flat:
            evidence.append("flat folders in the root: " + ", ".join(flat))
    elif os.path.exists(package):
        src = os.path.join(root, "src")
        src_dirs = {name.lower(): name for name in subdirs(src)}
        with open(package, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
        if "@nestjs" in text:
            stack = "NestJS"
            for layer in ("api", "data", "domain"):
                evidence.append("src/%s: %s" % (layer, "ok" if layer in src_dirs else "NOT FOUND"))
            if all(layer in src_dirs for layer in ("api", "data", "domain")):
                verdict = "STANDARD"
        else:
            stack = "React/Vite/TS" if ("react" in text and not os.path.isdir(os.path.join(root, "app"))) else "React/Next"
            for name in ("pages", "components", "services", "hooks"):
                evidence.append("src/%s: %s" % (name, "ok" if name in src_dirs else "NOT FOUND"))
            if os.path.isdir(os.path.join(root, "app")) and "pages" not in src_dirs:
                evidence.append("app/ folder in the root (Next app router structure, no src/pages)")
            if "pages" in src_dirs and "components" in src_dirs:
                verdict = "STANDARD"
    else:
        evidence.append("neither .sln/.csproj nor package.json in the root: confirm the project root")
    return stack, verdict, evidence


def section(classification, stack, suggestion, note):
    today = datetime.date.today().isoformat()
    lines = [
        BEGIN,
        "## Conformance",
        "Conformance: %s" % classification,
        "Stack: %s" % stack,
        "Defined by: user on %s (script suggestion: %s)" % (today, suggestion),
    ]
    if note:
        lines.append("Note: " + note)
    if classification == "STANDARD":
        lines += [
            "Rule: new code follows the skills' standard (back end Api/Data/Domain; front end pages/components). A file the feature touches may be edited and the touched class refactored to the standard (only it), with a regression check offered.",
        ]
    else:
        lines += [
            "Rule: LEGACY. New code follows the pattern the project already has (no new layer). Old files only get minimal edits, no renaming or splitting; refactoring becomes a Point of attention. Never touch anything outside the feature's PATHS.",
        ]
    lines += ["Scope (always): only the feature's PATHS and only repo code. A new file is fine if it stays in scope; a large deviation = stop and ask.", END, ""]
    return "\n".join(lines)


def write_claude_md(root, classification, stack, suggestion, note):
    path = os.path.join(root, "CLAUDE.md")
    block = section(classification, stack, suggestion, note)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    else:
        text = "# " + os.path.basename(root) + "\n\n"
    if BEGIN in text:
        text = re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", lambda _: block, text, count=1, flags=re.S)
    else:
        text = text.rstrip("\n") + "\n\n" + block
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    return path


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return 2
    root = os.path.abspath(args[0])
    stack, suggestion, evidence = analyze(root)
    if "--write" in args:
        classification = args[args.index("--write") + 1].upper()
        if classification not in ("STANDARD", "LEGACY"):
            print("use STANDARD or LEGACY")
            return 2
        note = args[args.index("--note") + 1] if "--note" in args else ""
        print("written to", write_claude_md(root, classification, stack, suggestion, note))
        return 0
    print("ROOT:", root)
    print("STACK:", stack)
    print("SUGGESTION:", suggestion)
    for line in evidence:
        print(" -", line)
    print("QUESTION FOR THE USER: is this project already in the skills' new standard (STANDARD) or is it legacy (LEGACY)?")
    print("WHY ASK: in LEGACY the agent only makes minimal edits to old files (without tests, refactoring there causes silent regressions); in STANDARD it may refactor the class it touches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
