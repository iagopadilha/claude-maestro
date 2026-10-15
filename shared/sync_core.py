#!/usr/bin/env python3
"""Injects shared/CORE_DEV.md into skills/backend-dev/SKILL.md and skills/frontend-dev/SKILL.md.

Idempotent: the block sits between <!-- CORE_DEV:BEGIN --> and <!-- CORE_DEV:END -->. If the markers already
exist, the content between them is replaced; otherwise the block goes in before the skill's calibration heading.
Usage: python sync_core.py [--check]   (--check only reports whether it is out of date; exit 1 if so)
"""
import os
import re
import sys

ROOT = os.path.abspath(os.environ.get("CLAUDE_PLUGIN_ROOT") or os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE = os.path.join(ROOT, "shared", "CORE_DEV.md")
TARGETS = {
    "backend-dev": r"^## 7\. Continuous Calibration",
    "frontend-dev": r"^## 13\. (?:Continuous Calibration|Calibra)",
}
BEGIN = "<!-- CORE_DEV:BEGIN -->"
END = "<!-- CORE_DEV:END -->"


def read(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def main():
    check_only = "--check" in sys.argv
    core = read(SOURCE).replace("\r\n", "\n").strip() + "\n"
    block = BEGIN + "\n" + core + END + "\n"
    stale = False
    for skill, anchor in TARGETS.items():
        path = os.path.join(ROOT, "skills", skill, "SKILL.md")
        text = read(path)
        newline = "\r\n" if "\r\n" in text else "\n"
        text = text.replace("\r\n", "\n")
        if BEGIN in text:
            pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n", re.S)
            updated = pattern.sub(lambda _: block, text, count=1)
        else:
            match = re.search(anchor, text, re.M)
            if not match:
                print("ERROR: anchor not found in", skill)
                return 2
            updated = text[: match.start()] + block + "\n---\n\n" + text[match.start():]
        if updated != text:
            stale = True
            if not check_only:
                with open(path, "w", encoding="utf-8", newline="") as handle:
                    handle.write(updated.replace("\n", newline))
            print(("out of date:" if check_only else "updated:"), skill)
        else:
            print("ok:", skill)
    return 1 if (stale and check_only) else 0


if __name__ == "__main__":
    sys.exit(main())
