#!/usr/bin/env python3
"""Simulated `cc` account picker in headless mode (tests of the loop's account handling).

Each call takes the next account of FAKE_PICKER_ACCOUNTS (comma-separated, e.g. "a,b"), counted in the FAKE_PICKER_STATE file,
names it on stderr like the real `cc` (`[cc] conta: <name>`) and:
  - when the account is in FAKE_PICKER_EXHAUSTED (comma-separated): fails with the usage limit, as the real CLI does;
  - when every account is used up: prints `[cc] reinicia: <FAKE_PICKER_RESET>` (when set) and fails with the usage limit;
  - otherwise: runs fake_claude.py with the same arguments and stdin (the turn really happens).
"""
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
LIMIT = {"type": "result", "subtype": "error", "is_error": True, "result": "Claude AI usage limit reached", "total_cost_usd": 0}


def main():
    accounts = [name for name in os.environ.get("FAKE_PICKER_ACCOUNTS", "a").split(",") if name]
    exhausted = set(name for name in os.environ.get("FAKE_PICKER_EXHAUSTED", "").split(",") if name)
    state = os.environ["FAKE_PICKER_STATE"]
    calls = int(open(state).read() or 0) if os.path.isfile(state) else 0
    with open(state, "w") as handle:
        handle.write(str(calls + 1))
    if calls >= len(accounts):
        if os.environ.get("FAKE_PICKER_RESET"):
            print("[cc] reinicia: %s" % os.environ["FAKE_PICKER_RESET"], file=sys.stderr)
        print(json.dumps(LIMIT))
        return 1
    account = accounts[calls]
    print("[cc] conta: %s" % account, file=sys.stderr)
    if account in exhausted:
        print(json.dumps(LIMIT))
        return 1
    done = subprocess.run([sys.executable, os.path.join(HERE, "fake_claude.py")] + sys.argv[1:], stdin=sys.stdin)
    return done.returncode


if __name__ == "__main__":
    sys.exit(main())
