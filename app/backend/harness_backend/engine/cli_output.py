#!/usr/bin/env python3
"""One tolerant parser for the output of `claude -p ... --output-format json` (standard library only).

Used by the engine (`run_loop.parse_result`, imported as a script sibling) and by the app
(`infrastructure/claude_client.ClaudeClient.parse_json_output`), so both read the CLI the same way.

Accepted shapes, so a change in the CLI output does not silently break a turn:
  - one JSON object (the `--output-format json` contract);
  - a JSON array of events (what `--verbose` prints): the last event with `type == "result"` wins;
  - stream-json (one JSON object per line): the last `result` event wins;
  - any of the above with non-JSON noise before or after it (a warning line, an update notice, trailing text).

`parse(text)` always returns a dict and never raises:
  recognised:   {"ok": True, <every field of the result event>, plus the normalised fields below}
  unrecognised: {"ok": False, "reason": "unrecognized_output", "detail": <why>, "sample": <first 300 chars, redacted>}

Normalised fields (missing, extra or wrongly typed fields never crash the parser):
  result (str), is_error (bool), subtype (str), total_cost_usd (float or None, falls back to `cost_usd`),
  duration_ms (int or None), num_turns (int or None), session_id (str), usage (dict), permission_denials (list of dicts).
"""
import json
import re

# The newest `claude --version` this contract was verified against (tests/engine/test_cli_contract.py, live test included).
# An older CLI only gets a warning in `harness.py doctor`, never a failure. Bump it after re-running the live contract test.
MIN_TESTED_CLAUDE_VERSION = "2.1.301"
VERSION_PATTERN = re.compile(r"(\d+)\.(\d+)\.(\d+)")
UNRECOGNIZED = "unrecognized_output"
SAMPLE_LIMIT = 300
REDACTION_MARK = "[redacted]"
SECRET_TEXT_PATTERN = re.compile(r"(?i)\b(token|secret|password|apikey|api_key|bearer|authorization)\b([^\n]{0,3})[:=]\s*\S+")
SUSPICIOUS_LONG_TEXT_PATTERN = re.compile(r"[A-Za-z0-9_=-]{40,}")
# keys that only a result event carries: an object without `type` is accepted as the result when it has one of them
RESULT_KEYS = ("result", "is_error", "total_cost_usd", "cost_usd", "session_id", "subtype", "num_turns")
JSON_START = re.compile(r"[\[{]")
MAX_DECODE_ATTEMPTS = 2000


def redact(text, limit=None):
    """Removes what looks like a credential (`token=...`, `Authorization: ...`, long opaque strings)."""
    text = SECRET_TEXT_PATTERN.sub(lambda found: found.group(1) + "=" + REDACTION_MARK, str(text or ""))
    text = SUSPICIOUS_LONG_TEXT_PATTERN.sub(REDACTION_MARK, text)
    return text[:limit] if limit else text


def _number(value, kind):
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
        if number != number or number in (float("inf"), float("-inf")):  # NaN or infinite
            return None
        return int(number) if kind is int else number
    except (TypeError, ValueError, OverflowError):
        return None


def _boolean(value):
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return bool(value)


def _text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _denials(value):
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    denials = []
    for item in items:
        if isinstance(item, dict):
            denials.append(item)
        elif item is not None:
            denials.append({"tool_name": "", "tool_input": {"command": _text(item)}})
    return denials


def _decoded_values(text):
    """Every top-level JSON value found in the text, in order: the whole text when it is JSON, else each object/array that
    decodes from a `{` or `[` (noise between them is skipped). Lists are kept as they are."""
    try:
        return [json.loads(text)]
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    values, position, attempts = [], 0, 0
    while attempts < MAX_DECODE_ATTEMPTS:
        found = JSON_START.search(text, position)
        if not found:
            break
        attempts += 1
        try:
            value, end = decoder.raw_decode(text, found.start())
        except ValueError:
            position = found.start() + 1
            continue
        values.append(value)
        position = end
    return values


def _events(values):
    events = []
    for value in values:
        if isinstance(value, dict):
            events.append(value)
        elif isinstance(value, list):
            events.extend(item for item in value if isinstance(item, dict))
    return events


def _pick_result(events):
    for event in reversed(events):
        if str(event.get("type") or "").lower() == "result":
            return event
    for event in reversed(events):
        if "type" not in event and any(key in event for key in RESULT_KEYS):
            return event
    return None


def normalize(event):
    """Result event -> dict with every original field plus the normalised ones and `ok: True`."""
    data = dict(event)
    cost = _number(event.get("total_cost_usd"), float)
    if cost is None:
        cost = _number(event.get("cost_usd"), float)
    subtype = _text(event.get("subtype"))
    is_error = _boolean(event.get("is_error")) if "is_error" in event else subtype.lower().startswith("error")
    usage = event.get("usage")
    data.update({
        "ok": True,
        "type": "result",
        "result": _text(event.get("result")),
        "is_error": is_error,
        "subtype": subtype,
        "total_cost_usd": cost,
        "duration_ms": _number(event.get("duration_ms"), int),
        "num_turns": _number(event.get("num_turns"), int),
        "session_id": _text(event.get("session_id")),
        "usage": usage if isinstance(usage, dict) else {},
        "permission_denials": _denials(event.get("permission_denials")),
    })
    return data


def unrecognized(text, detail):
    return {"ok": False, "reason": UNRECOGNIZED, "detail": detail, "sample": redact((text or "").strip()[:SAMPLE_LIMIT], SAMPLE_LIMIT)}


def parse(text):
    """Output of `claude -p --output-format json|stream-json` -> normalised result dict (see the module docstring)."""
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    text = (text or "").strip().lstrip("\ufeff")
    if not text:
        return unrecognized(text, "empty output")
    events = _events(_decoded_values(text))
    if not events:
        return unrecognized(text, "no JSON object in the output")
    event = _pick_result(events)
    if event is None:
        return unrecognized(text, "JSON found but no result event (type=result) in it")
    return normalize(event)


def parse_version(text):
    """`2.1.301 (Claude Code)` -> (2, 1, 301); None when there is no x.y.z in the text."""
    found = VERSION_PATTERN.search(str(text or ""))
    return tuple(int(part) for part in found.groups()) if found else None


def describe_version(version_output):
    """What `harness.py doctor` shows about `claude --version`:
    {"version": "x.y.z" or "", "min_tested_version", "older_than_tested": bool, "warning": "" or text}. Never a failure."""
    version = parse_version(version_output)
    minimum = parse_version(MIN_TESTED_CLAUDE_VERSION)
    info = {"version": ".".join(str(part) for part in version) if version else "", "min_tested_version": MIN_TESTED_CLAUDE_VERSION,
            "older_than_tested": bool(version and version < minimum), "warning": ""}
    if not version:
        info["warning"] = "could not read the `claude --version` output; the JSON contract was tested with %s" % MIN_TESTED_CLAUDE_VERSION
    elif info["older_than_tested"]:
        info["warning"] = ("claude %s is older than the version this harness was tested with (%s); update Claude Code if a turn "
                           "fails with unrecognized_output" % (info["version"], MIN_TESTED_CLAUDE_VERSION))
    return info


if __name__ == "__main__":
    import sys
    print(json.dumps(parse(sys.stdin.read()), ensure_ascii=False, indent=2))
