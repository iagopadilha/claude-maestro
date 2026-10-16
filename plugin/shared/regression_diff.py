#!/usr/bin/env python3
"""Compares two JSON snapshots (baseline = old code, new = refactored code).

Usage: python regression_diff.py baseline.json new.json [--ignore regex ...] [--key field ...]
- Drops volatile keys (id, createdAt, updatedAt, timestamp, guid... and those from --ignore).
- Replaces values that look like a GUID or ISO date with <GUID> / <DATE>.
- Sorts lists of objects (by --key if given, otherwise by canonical JSON), so ordering does not cause false alarms.
- Prints each difference with its path (e.g. tables.Order[2].amount) and a count summary.
Exit: 0 same, 1 different. A large drop in counts is a warning, not a win: explain every item that disappeared.
"""
import json
import re
import sys

DEFAULT_IGNORE = r"^(id|.*Id|createdAt|updatedAt|created_at|updated_at|timestamp|guid|traceId|requestId)$"
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?$")


def normalize(value, ignore, keys):
    if isinstance(value, dict):
        return {name: normalize(inner, ignore, keys) for name, inner in value.items() if not ignore.search(name)}
    if isinstance(value, list):
        items = [normalize(item, ignore, keys) for item in value]
        if keys:
            items.sort(key=lambda item: tuple(json.dumps(item.get(key), sort_keys=True) if isinstance(item, dict) else "" for key in keys))
        else:
            items.sort(key=lambda item: json.dumps(item, sort_keys=True, ensure_ascii=False))
        return items
    if isinstance(value, str):
        if GUID.match(value):
            return "<GUID>"
        if ISO_DATE.match(value):
            return "<DATE>"
    return value


def compare(old, new, path, out):
    if type(old) != type(new):
        out.append("%s: type %s x %s" % (path, type(old).__name__, type(new).__name__))
    elif isinstance(old, dict):
        for name in sorted(set(old) | set(new)):
            if name not in new:
                out.append("%s.%s: ONLY IN BASELINE (%s)" % (path, name, json.dumps(old[name], ensure_ascii=False)[:80]))
            elif name not in old:
                out.append("%s.%s: ONLY IN NEW (%s)" % (path, name, json.dumps(new[name], ensure_ascii=False)[:80]))
            else:
                compare(old[name], new[name], "%s.%s" % (path, name), out)
    elif isinstance(old, list):
        if len(old) != len(new):
            out.append("%s: %d items in baseline x %d in new" % (path, len(old), len(new)))
        for index, (left, right) in enumerate(zip(old, new)):
            compare(left, right, "%s[%d]" % (path, index), out)
    elif old != new:
        out.append("%s: %s x %s" % (path, json.dumps(old, ensure_ascii=False)[:60], json.dumps(new, ensure_ascii=False)[:60]))


def main():
    args = sys.argv[1:]
    files = []
    ignores = [DEFAULT_IGNORE]
    keys = []
    mode = None
    for arg in args:
        if arg == "--ignore":
            mode = "ignore"
        elif arg == "--key":
            mode = "key"
        elif mode == "ignore":
            ignores.append(arg)
            mode = None
        elif mode == "key":
            keys.append(arg)
            mode = None
        else:
            files.append(arg)
    if len(files) != 2:
        print(__doc__)
        return 2
    ignore = re.compile("|".join("(?:%s)" % pattern for pattern in ignores))
    with open(files[0], encoding="utf-8") as handle:
        old = normalize(json.load(handle), ignore, keys)
    with open(files[1], encoding="utf-8") as handle:
        new = normalize(json.load(handle), ignore, keys)
    differences = []
    compare(old, new, "$", differences)
    if not differences:
        print("REGRESSION: SAME (baseline x new, after normalizing)")
        return 0
    print("REGRESSION: DIFFERENT (%d)" % len(differences))
    for line in differences[:40]:
        print("-", line)
    if len(differences) > 40:
        print("- ... %d more" % (len(differences) - 40))
    return 1


if __name__ == "__main__":
    sys.exit(main())
