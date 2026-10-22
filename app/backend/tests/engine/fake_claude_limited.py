#!/usr/bin/env python3
"""Simulated `claude -p` of an account that hit its usage limit: every call fails like the real CLI does (tests of the
`--claude-fallback` switch). Appends its argv to FAKE_LIMITED_LOG when that variable is set."""
import json
import os
import sys

if os.environ.get("FAKE_LIMITED_LOG"):
    with open(os.environ["FAKE_LIMITED_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")
print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "Claude AI usage limit reached|1767225600",
                  "total_cost_usd": 0, "session_id": "limited-1"}))
sys.exit(1)
