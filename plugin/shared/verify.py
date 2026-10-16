#!/usr/bin/env python3
"""Deterministic project verification: detects and runs build, typecheck, lint and tests.

Usage:  python verify.py [--scope FOLDER] [--dry-run] [--timeout SECONDS] [--only build,lint,...]
Output (format of the `verifier` agent):
    VERDICT: PASS|FAIL|UNVERIFIED
    RAN: cmd, cmd
    FAIL:
    - <cmd>: <first useful error line>
Exit: 0 PASS, 1 FAIL, 3 UNVERIFIED (nothing to run). Does not edit files, run git or install dependencies.
"""
import glob
import json
import os
import re
import subprocess
import sys

STEP_ORDER = ["typecheck", "lint", "build", "test"]
ERROR_LINE = re.compile(r"(error|failed|fail\b|cannot find|TS\d{4}|CS\d{4}|✖|✗)", re.I)


def has_file(folder, *names):
    return any(os.path.exists(os.path.join(folder, name)) for name in names)


def node_steps(folder):
    package_path = os.path.join(folder, "package.json")
    with open(package_path, encoding="utf-8") as handle:
        scripts = json.load(handle).get("scripts", {})
    if has_file(folder, "pnpm-lock.yaml"):
        runner = "pnpm run"
    elif has_file(folder, "yarn.lock"):
        runner = "yarn"
    else:
        runner = "npm run"
    steps = {}
    if "typecheck" in scripts:
        steps["typecheck"] = "%s typecheck" % runner
    elif has_file(folder, "tsconfig.json", "tsconfig.app.json"):
        steps["typecheck"] = "npx --no-install tsc --noEmit -p %s" % (
            "tsconfig.app.json" if has_file(folder, "tsconfig.app.json") else "tsconfig.json"
        )
    if "lint" in scripts:
        steps["lint"] = "%s lint" % runner
    if "build" in scripts:
        steps["build"] = "%s build" % runner
    test_script = scripts.get("test", "")
    if test_script and "no test specified" not in test_script:
        steps["test"] = "%s test" % runner
    return steps


DOTNET_SKIP = {"bin", "obj", "node_modules", ".git", ".specs", ".vs", "packages"}


def find_dotnet_target(folder, max_depth=3):
    """Closest .NET solution or project to the root (up to `max_depth` folders down; .sln before .csproj at the same
    depth). Returns the relative path with `/`, or None. Covers repos like src/App/App.csproj, not only the root."""
    for depth in range(max_depth + 1):
        for extension in ("*.sln", "*.slnx", "*.csproj"):
            hits = sorted(
                path for path in glob.glob(os.path.join(folder, *(["*"] * depth), extension))
                if not DOTNET_SKIP.intersection(os.path.relpath(path, folder).split(os.sep)[:-1])
            )
            if hits:
                return os.path.relpath(hits[0], folder).replace(os.sep, "/")
    return None


def dotnet_steps(folder):
    steps = {}
    target = find_dotnet_target(folder)
    if not target:
        return steps
    if has_file(folder, ".editorconfig"):  # lint only when the project declares its style; without .editorconfig the formatter would fight legacy code
        steps["lint"] = "dotnet format %s --verify-no-changes -v q" % target
    steps["build"] = "dotnet build %s --nologo -v q" % target
    has_tests = any("test" in path.lower() for path in glob.glob(os.path.join(folder, "**", "*.csproj"), recursive=True))
    if has_tests:
        steps["test"] = "dotnet test %s --nologo -v q" % target
    return steps


def detect(folder):
    steps = {}
    if has_file(folder, "package.json"):
        steps.update(node_steps(folder))
    if find_dotnet_target(folder):
        for name, command in dotnet_steps(folder).items():
            steps.setdefault(name, command)
    return [(name, steps[name]) for name in STEP_ORDER if name in steps]


def first_error_line(output):
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    for line in lines:
        if ERROR_LINE.search(line):
            return line[:220]
    return lines[-1][:220] if lines else "no output"


def main():
    args = sys.argv[1:]
    folder = os.getcwd()
    timeout = 600
    only = None
    if "--scope" in args:
        folder = os.path.abspath(args[args.index("--scope") + 1])
    if "--timeout" in args:
        timeout = int(args[args.index("--timeout") + 1])
    if "--only" in args:
        only = set(args[args.index("--only") + 1].split(","))
    steps = [(name, command) for name, command in detect(folder) if not only or name in only]
    if "--dry-run" in args:
        print("FOLDER:", folder)
        for name, command in steps:
            print("%-9s %s" % (name, command))
        if not steps:
            print("(nothing detected)")
        return 0
    if not steps:
        print("VERDICT: UNVERIFIED\nRAN: -\nFAIL: none\nNOTES: no build/typecheck/lint/test detected in %s" % folder)
        return 3
    failures = []
    ran = []
    for name, command in steps:
        ran.append(command)
        try:
            result = subprocess.run(command, cwd=folder, shell=True, capture_output=True, text=True, timeout=timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            failures.append("%s: timeout of %ss" % (command, timeout))
            continue
        if result.returncode != 0:
            failures.append("%s: %s" % (command, first_error_line(result.stdout + "\n" + result.stderr)))
    print("VERDICT:", "FAIL" if failures else "PASS")
    print("RAN:", ", ".join(ran))
    print("FAIL:" + ("" if failures else " none"))
    for failure in failures:
        print("- " + failure)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
