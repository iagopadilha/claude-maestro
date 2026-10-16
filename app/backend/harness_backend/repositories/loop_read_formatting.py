"""Formatting rules shared by the loop read side: credential redaction, text clipping, numbers and clocks.

The front end and the tests depend on the exact text (e.g. `[redacted]`).
"""
import datetime
import re

CREDENTIAL_TEXT_PATTERN = re.compile(r"(?i)\b(token|secret|passwd|password|apikey|api_key|bearer|authorization)\b([^\n]{0,3})[:=]\s*\S+")
LONG_CREDENTIAL_PATTERN = re.compile(r"[A-Za-z0-9_=-]{40,}")
REDACTED_MARK = "[redacted]"
SECONDS_PER_MINUTE = 60
SECONDS_PER_HOUR = 3600


def now_iso():
    return datetime.datetime.now().isoformat(timespec="seconds")


def redact_credentials(text, limit=None):
    """Removes from the text whatever looks like a credential before it leaves through the API (turn results and transcripts
    may quote commands)."""
    text = CREDENTIAL_TEXT_PATTERN.sub(lambda found: found.group(1) + "=" + REDACTED_MARK, str(text or ""))
    text = LONG_CREDENTIAL_PATTERN.sub(REDACTED_MARK, text)
    return text[:limit] if limit else text


def clip_redacted_text(text, limit):
    """Redacts, joins the whitespace into a single line and clips at `limit`."""
    text = " ".join(redact_credentials(text).split())
    return text[:limit] if len(text) > limit else text


def to_text_list(value):
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str):
        return [part for part in re.split(r"[,\s]+", value) if part]
    return []


def to_number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def ms_to_seconds(milliseconds):
    number = to_number(milliseconds)
    return round(number / 1000) if number is not None else None


def format_minutes_clock(seconds):
    seconds = int(seconds or 0)
    return "%02d:%02d" % (seconds // SECONDS_PER_MINUTE, seconds % SECONDS_PER_MINUTE)


def format_elapsed_clock(start, moment):
    """HH:MM:SS between two datetimes; either side missing or with an incompatible time zone = ''."""
    if start is None or moment is None:
        return ""
    try:
        seconds = max(0, int((moment - start).total_seconds()))
    except TypeError:
        return ""
    return "%02d:%02d:%02d" % (seconds // SECONDS_PER_HOUR, seconds % SECONDS_PER_HOUR // SECONDS_PER_MINUTE, seconds % SECONDS_PER_MINUTE)


def parse_iso_date(value):
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def compute_seconds_between(start, end):
    first = parse_iso_date(start) if start else None
    last = parse_iso_date(end) if end else None
    if first is None or last is None:
        return None
    try:
        return max(0, round((last - first).total_seconds()))
    except TypeError:  # one side with a time zone and the other without
        return None


def sum_usage_tokens(usage):
    """Sum of the `*_tokens` fields of a `usage`; none of them = None (never invents a zero)."""
    if not isinstance(usage, dict):
        return None
    values = [value for key, value in usage.items() if key.endswith("_tokens") and to_number(value) is not None]
    return int(sum(values)) if values else None
