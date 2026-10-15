#!/usr/bin/env python3
"""Creates a project block rule and PROVES it works in the real guard (skill orchestrator-create-block-rule).

Usage:
  python rules.py propose <root> --id R-04 --phrase "never edit Taxes" --path "Taxes" --block path:src/Taxes/x.cs --pass path:src/Freight/x.cs
  python rules.py write <root> (same arguments) --rule "Never edit the Taxes folder" --reason "..." --how "..."
  python rules.py test <root>        # runs ALL stored cases against the current PERMISSIONS.json (exit 1 if any fails)
  python rules.py list <root>

  --bash REGEX   blocks a shell command (Bash/PowerShell) that matches (deny_bash_regex); repeatable
  --path REGEX   blocks writes to a path that matches (deny_path_regex); repeatable
  --block bash:<command> | path:<path>   example that MUST be blocked; repeatable, at least 1
  --pass  bash:<command> | path:<path>   example that MUST pass; repeatable, at least 1

`propose` only tests. `write` tests and, ONLY if everything passes (and the rule does not break any older rule), writes:
  .specs/project/PERMISSIONS.json (deny_*_regex), .specs/project/rules.tests.json (the examples, so `test` always runs them)
  and a line in .specs/project/RULES.md. `--phrase` is the user's EXPLICIT phrase (origin); without it nothing is written.
Each example runs through the real hooks/guard.py (executor profile) in a temp folder: no regex guesswork.
Who uses it: the orchestrator (the guard blocks subagents from running `rules.py write`). Use [\\\\/] in regexes to accept / and \\ in paths.
"""
import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(os.path.dirname(HERE), "hooks", "guard.py")
NEUTRAL = [("bash", "ls"), ("bash", "git status"), ("bash", "dotnet build"), ("path", "src/app.ts"), ("path", "README.md"), ("path", "docs/guide.md")]
ID_RE = re.compile(r"^R-\d+$")


def project_dir(root):
    return os.path.join(os.path.abspath(root), ".specs", "project")


def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, type(default)) else default
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def parse_example(text):
    kind, _, value = text.partition(":")
    if kind not in ("bash", "path") or not value.strip():
        raise ValueError("invalid example %r: use bash:<command> or path:<path>" % text)
    return kind, value.strip()


def evaluate(permissions, must_block, must_pass):
    """Runs each example in the real guard. Returns a list of dicts {example, expected, ok, detail}."""
    tmp = tempfile.mkdtemp(prefix="p")
    results = []
    try:
        os.makedirs(os.path.join(tmp, ".specs", "project"))
        save_json(os.path.join(tmp, ".specs", "project", "PERMISSIONS.json"), permissions)
        for expected, examples in (("block", must_block), ("pass", must_pass)):
            for example in examples:
                kind, value = parse_example(example)
                if kind == "bash":
                    payload = {"tool_name": "Bash", "tool_input": {"command": value}, "cwd": tmp}
                else:
                    payload = {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(tmp, value)}, "cwd": tmp}
                done = subprocess.run([sys.executable, GUARD, "--profile", "executor"], input=json.dumps(payload), capture_output=True, text=True, timeout=60)
                if expected == "block":
                    ok = done.returncode == 2 and "project-deny" in done.stderr
                    detail = "blocked by the rule" if ok else ("blocked by ANOTHER rule, not this one" if done.returncode == 2 else "was NOT blocked")
                else:
                    ok = done.returncode == 0
                    detail = "passed" if ok else "was blocked (rule too wide or another rule): " + done.stderr.strip()[:120]
                results.append({"example": example, "expected": expected, "ok": ok, "detail": detail})
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return results


def merged_permissions(root, bash, path):
    perms = load_json(os.path.join(project_dir(root), "PERMISSIONS.json"), {})
    perms.setdefault("allow", [])
    for key, new in (("deny_bash_regex", bash), ("deny_path_regex", path)):
        perms.setdefault(key, [])
        perms[key] += [item for item in new if item not in perms[key]]
    return perms


def regex_problems(bash, path):
    problems = []
    for label, patterns in (("--bash", bash), ("--path", path)):
        for pattern in patterns:
            try:
                compiled = re.compile(pattern, re.I)
            except re.error as error:
                problems.append("%s %r is not a valid regex: %s" % (label, pattern, error))
                continue
            if compiled.search(""):
                problems.append("%s %r matches empty text: it would block everything" % (label, pattern))
            wide = [value for kind, value in NEUTRAL if kind == ("bash" if label == "--bash" else "path") and compiled.search(value)]
            if wide:
                problems.append("%s %r is too wide: it matches %s" % (label, pattern, ", ".join(repr(w) for w in wide)))
    return problems


def show(results):
    for item in results:
        print("  %-4s %-8s %-48s %s" % ("ok" if item["ok"] else "FAIL", item["expected"], item["example"][:48], item["detail"]))


def append_rule_row(path, row):
    template = ("# Limiting rules\n\n> Always read. Beats spec/design/skill. Conflict = ask. Owner: orchestrator (or the main agent without a loop).\n\n"
                "| ID | Rule | Why | How to enforce | Origin |\n|----|------|-----|----------------|--------|\n")
    try:
        with open(path, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError:
        lines = template.splitlines()
    separator = next((i for i, line in enumerate(lines) if re.match(r"^\|[-\s|]+\|$", line.strip()) and "-" in line), None)
    if separator is None:
        lines += [""] + template.splitlines()[4:]
        separator = len(lines) - 1
    end = separator
    while end + 1 < len(lines) and lines[end + 1].strip().startswith("|"):
        end += 1
    lines.insert(end + 1, row)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")


def cmd_check(args, write):
    root = os.path.abspath(args.root)
    bash, path = args.bash or [], args.path or []
    if not (bash or path):
        print("PROBLEM: give at least one --bash or --path")
        return 2
    if not (args.block and args.pass_):
        print("PROBLEM: without examples there is no proof. Give at least one --block and one --pass")
        return 2
    problems = regex_problems(bash, path)
    if not ID_RE.match(args.id or ""):
        problems.append("--id must be R-<number> (e.g. R-04)")
    if write and not (args.phrase or "").strip():
        problems.append("--phrase is empty: only write a rule with the user's EXPLICIT phrase")
    tests_path = os.path.join(project_dir(root), "rules.tests.json")
    tests = load_json(tests_path, {})
    rules_md = os.path.join(project_dir(root), "RULES.md")
    if args.id in tests or re.search(r"\|\s*%s\s*\|" % re.escape(args.id or "?"), open(rules_md, encoding="utf-8").read() if os.path.isfile(rules_md) else ""):
        problems.append("%s already exists in RULES.md/rules.tests.json: use another ID" % args.id)
    try:
        for example in args.block + args.pass_:
            parse_example(example)
    except ValueError as error:
        problems.append(str(error))
    if problems:
        for item in problems:
            print("PROBLEM:", item)
        return 2
    perms = merged_permissions(root, bash, path)
    print("%s: testing in the real guard" % args.id)
    results = evaluate(perms, args.block, args.pass_)
    show(results)
    old = []
    for rule_id, data in tests.items():
        old += [dict(r, example="[%s] %s" % (rule_id, r["example"])) for r in evaluate(perms, data.get("block", []), data.get("pass", []))]
    broken = [r for r in old if not r["ok"]]
    if broken:
        print("The new rule BREAKS older rules:")
        show(broken)
    if not all(r["ok"] for r in results) or broken:
        print("REJECTED: nothing written. Adjust the regex or the examples and run again.")
        return 1
    if not write:
        print("PASSED the test. Run `write` (with the user's --phrase) to make it count.")
        return 0
    today = datetime.date.today().isoformat()
    save_json(os.path.join(project_dir(root), "PERMISSIONS.json"), perms)
    tests[args.id] = {"phrase": args.phrase.strip(), "bash": bash, "path": path, "block": args.block, "pass": args.pass_, "created": today}
    save_json(tests_path, tests)
    how = args.how or "Blocked by the hook (project-deny); the subagent returns NEEDS_CONFIRM"
    row = "| %s | %s | %s | %s | user, %s: \"%s\" |" % (args.id, (args.rule or args.phrase).strip(), (args.reason or "-").strip(), how.strip(), today, args.phrase.strip())
    append_rule_row(rules_md, row.replace("\n", " "))
    print("WRITTEN: %s in PERMISSIONS.json, rules.tests.json and RULES.md" % args.id)
    return 0


def cmd_test(args):
    root = os.path.abspath(args.root)
    tests = load_json(os.path.join(project_dir(root), "rules.tests.json"), {})
    perms = load_json(os.path.join(project_dir(root), "PERMISSIONS.json"), {})
    if not tests:
        print("no rules with a stored test (rules.tests.json empty or missing)")
        return 0
    failed = 0
    for rule_id, data in tests.items():
        results = evaluate(perms, data.get("block", []), data.get("pass", []))
        bad = [r for r in results if not r["ok"]]
        failed += len(bad)
        print("%s: %d/%d ok" % (rule_id, len(results) - len(bad), len(results)))
        if bad and not args.quiet:
            show(bad)
        elif bad:
            for item in bad:
                print("  FAIL %s %s" % (item["expected"], item["example"]))
    print("ALL rules pass" if not failed else "%d case(s) failed: PERMISSIONS.json changed or the guard changed" % failed)
    return 1 if failed else 0


def cmd_list(args):
    tests = load_json(os.path.join(project_dir(args.root), "rules.tests.json"), {})
    for rule_id, data in tests.items():
        print("%s  %s  (bash:%d path:%d, %d block / %d pass)" % (rule_id, data.get("phrase", ""), len(data.get("bash", [])), len(data.get("path", [])), len(data.get("block", [])), len(data.get("pass", []))))
    if not tests:
        print("no rule with a stored test")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Creates a project block rule with a test in the real guard")
    parser.add_argument("command", choices=["propose", "write", "test", "list"])
    parser.add_argument("root")
    parser.add_argument("--id", default="")
    parser.add_argument("--phrase", default="")
    parser.add_argument("--rule", default="")
    parser.add_argument("--reason", default="")
    parser.add_argument("--how", default="")
    parser.add_argument("--bash", action="append")
    parser.add_argument("--path", action="append")
    parser.add_argument("--block", action="append")
    parser.add_argument("--pass", dest="pass_", action="append")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    if args.command == "test":
        return cmd_test(args)
    if args.command == "list":
        return cmd_list(args)
    return cmd_check(args, write=args.command == "write")


if __name__ == "__main__":
    sys.exit(main())
