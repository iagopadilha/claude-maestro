#!/usr/bin/env python3
"""Simulated `claude -p` of an account the organization does not allow: every call fails like the real CLI does with the 403
`oauth_org_not_allowed` error (tests of the switch to the next command on an unusable account). Appends its argv to
FAKE_UNAUTHORIZED_LOG when that variable is set."""
import json
import os
import sys

if os.environ.get("FAKE_UNAUTHORIZED_LOG"):
    with open(os.environ["FAKE_UNAUTHORIZED_LOG"], "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sys.argv[1:]) + "\n")
MESSAGE = ("API Error: 403 Your organization has disabled Claude subscription access for Claude Code · Use an Anthropic API key instead, "
           "or ask your admin to enable access (error type oauth_org_not_allowed, HTTP 403)")
print(json.dumps({"type": "result", "subtype": "error", "is_error": True, "result": MESSAGE, "total_cost_usd": 0, "session_id": "unauthorized-1"}))
sys.exit(1)
