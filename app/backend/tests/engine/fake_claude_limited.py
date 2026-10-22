#!/usr/bin/env python3
"""Simulated `claude -p` of an account that hit its usage limit: every call fails like the real CLI does (tests of the
`--claude-fallback` switch). Appends its argv to FAKE_LIMITED_LOG when that variable is set.

With FAKE_LIMITED_WINDOW=five_hour|seven_day it plays the stream-json output of claude 2.1.x instead: a `rate_limit_event` rejected on that
window (reset in FAKE_LIMITED_RESET_IN seconds, default 2 h / 3 days) and a result whose text names no limit ("You've hit your limit"), so the
loop has to read the limit from the event. FAKE_LIMITED_OTHER_PCT is the other window's utilization in percent (default 40)."""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from fake_claude import rate_limit_event  # noqa: E402

if os.environ.get("FAKE_LIMITED_LOG"):
    with open(os.environ["FAKE_LIMITED_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")
WINDOW = os.environ.get("FAKE_LIMITED_WINDOW", "")
if WINDOW:
    other = float(os.environ.get("FAKE_LIMITED_OTHER_PCT", "40")) / 100
    settings = {"status": "rejected", "limit_type": WINDOW, "five_hour": 1.0 if WINDOW == "five_hour" else other,
                "seven_day": 1.0 if WINDOW == "seven_day" else other}
    settings[WINDOW + "_reset_in"] = int(os.environ.get("FAKE_LIMITED_RESET_IN", "7200" if WINDOW == "five_hour" else "259200"))
    print(json.dumps({"type": "system", "subtype": "init", "session_id": "limited-1"}))
    print(json.dumps(rate_limit_event(settings)))
    print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "You've hit your limit",
                      "total_cost_usd": 0, "session_id": "limited-1"}))
    sys.exit(1)
print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": "Claude AI usage limit reached|1767225600",
                  "total_cost_usd": 0, "session_id": "limited-1"}))
sys.exit(1)
