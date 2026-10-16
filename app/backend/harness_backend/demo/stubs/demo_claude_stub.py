#!/usr/bin/env python3
"""Demo stand-in for the `claude` CLI (the app's `claude_binary` in demo mode): the panel's chat, action router and roadmap
jobs get a fixed `--output-format json` reply saying nothing ran. Never calls the network or the real CLI. Standard library only.
"""
import json
import sys

DEMO_REPLY = ("Demo mode: no Claude call is made here. The data in this panel is mocked by `harness.py demo`; open a real "
              "repository with `harness.py serve` to talk to the loop.")


def main():
    if "--version" in sys.argv[1:]:
        print("0.0.0 (Loop Harness demo stub)")
        return 0
    try:
        sys.stdin.read()
    except (OSError, ValueError):
        pass
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": DEMO_REPLY, "total_cost_usd": 0,
                      "session_id": "demo-session", "num_turns": 1}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
