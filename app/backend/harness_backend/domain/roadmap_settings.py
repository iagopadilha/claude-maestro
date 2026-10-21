"""Project-wide settings written in the ROADMAP header (not per feature).

`**E2E:** yes|no` = the loop ends with the flow-validation phase (verifier-flow runs each FLOWS.md flow against the real app,
needs .specs/project/TEST-ENVIRONMENT.md). Absent or unreadable = no. Decided during planning, never in the Start-a-loop card.
"""
import re

HTML_COMMENT_PATTERN = re.compile(r"<!--.*?-->", re.S)
E2E_LINE_PATTERN = re.compile(r"^\s*(?:[-*]\s+)?\*{0,2}E2E\*{0,2}\s*:\s*\*{0,2}\s*(yes|no|true|false)\b", re.I | re.M)
YES_VALUES = ("yes", "true")


def parse_roadmap_e2e(text):
    """True/False from the first `E2E: yes|no` line outside HTML comments (the template comment documents it); None when absent."""
    found = E2E_LINE_PATTERN.search(HTML_COMMENT_PATTERN.sub("", text or ""))
    return None if not found else found.group(1).lower() in YES_VALUES


# ---------------------------------------------------------------- loop settings (the lines of the ROADMAP header)
DEFAULT_DISPATCH = "auto"
MIN_DISPATCH, MAX_DISPATCH = 1, 8
MIN_TURN_MINUTES, MAX_TURN_MINUTES = 1, 600
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,80}")
SETUP_LIMIT = 200
AUTOMATIC_BRANCH_WORD = "automatic"
DEFAULT_BASE_BRANCH = "developer"  # where new worktrees start and the PR target, when the ROADMAP says nothing (`Branch base`)
WHEN_STUCK_VALUES = ("pause", "continue")
TRAILING_NOTE_PATTERN = re.compile(r"\s*\([^)]*\)\s*$")
MONEY_PATTERN = re.compile(r"(?:US\$|\$)?\s*(\d+(?:[.,]\d+)?)")
WHOLE_NUMBER_PATTERN = re.compile(r"\d+")

# key -> header name exactly as written in the ROADMAP (`**Name:** value`)
LOOP_SETTING_HEADER_NAMES = {
    "dispatch": "Dispatch",
    "e2e": "E2E",
    "budget_turn": "Max spend per turn",
    "budget_loop": "Max spend for the loop",
    "minutes_turn": "Max minutes per turn",
    "branch": "Branch",
    "base_branch": "Branch base",
    "setup": "Setup",
    "when_stuck": "When a feature gets stuck",
    "push_pr": "Push and PR",
}
LOOP_SETTING_DEFAULTS = {
    "dispatch": DEFAULT_DISPATCH, "e2e": False, "budget_turn": 5.0, "budget_loop": 50.0, "minutes_turn": 60,
    "branch": "", "base_branch": DEFAULT_BASE_BRANCH, "setup": "", "when_stuck": "pause", "push_pr": True,
}


def build_header_line_pattern(header_name):
    """`**Name:** value`, `Name: value`, `- Name: value` (value = the rest of the line, may be empty)."""
    return re.compile(r"^[ \t]*(?:[-*][ \t]+)?\*{0,2}%s\*{0,2}[ \t]*:[ \t]*\*{0,2}[ \t]*(.*?)[ \t]*$" % re.escape(header_name), re.I | re.M)


def describe_default(key):
    value = LOOP_SETTING_DEFAULTS[key]
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value) if value != "" else ("automatic" if key == "branch" else "empty")


LOOP_SETTING_PATTERNS = {key: build_header_line_pattern(name) for key, name in LOOP_SETTING_HEADER_NAMES.items()}


def read_header_value(text_without_comments, key):
    """Raw value of the first header line of that setting outside HTML comments; None when the line is absent."""
    found = LOOP_SETTING_PATTERNS[key].search(text_without_comments)
    return None if not found else found.group(1).strip().strip("*").strip()


def convert_yes_no(raw):
    first_word = (raw.split() or [""])[0].lower()
    if first_word in ("yes", "true"):
        return True
    if first_word in ("no", "false"):
        return False
    raise ValueError("write yes or no")


def convert_money(raw):
    found = MONEY_PATTERN.fullmatch(TRAILING_NOTE_PATTERN.sub("", raw))
    if not found:
        raise ValueError("write an amount in US$, for example 5")
    return float(found.group(1).replace(",", "."))


def convert_whole_number(raw, minimum, maximum):
    cleaned = TRAILING_NOTE_PATTERN.sub("", raw)
    if not WHOLE_NUMBER_PATTERN.fullmatch(cleaned) or not minimum <= int(cleaned) <= maximum:
        raise ValueError("write a whole number from %d to %d" % (minimum, maximum))
    return int(cleaned)


def convert_dispatch(raw):
    cleaned = TRAILING_NOTE_PATTERN.sub("", raw).strip()
    if cleaned.lower() == DEFAULT_DISPATCH:
        return DEFAULT_DISPATCH
    return convert_whole_number(cleaned, MIN_DISPATCH, MAX_DISPATCH)


def convert_branch(raw):
    cleaned = TRAILING_NOTE_PATTERN.sub("", raw).strip()
    if not cleaned or cleaned.lower() == AUTOMATIC_BRANCH_WORD:
        return ""
    if not BRANCH_PATTERN.fullmatch(cleaned) or ".." in cleaned or cleaned.startswith("-"):
        raise ValueError("use only letters, digits, . _ / - (up to 80), without '..' and not starting with '-'")
    return cleaned


def convert_base_branch(raw):
    cleaned = TRAILING_NOTE_PATTERN.sub("", raw).strip()
    if not BRANCH_PATTERN.fullmatch(cleaned) or ".." in cleaned or cleaned.startswith("-"):
        raise ValueError("use only letters, digits, . _ / - (up to 80), without '..' and not starting with '-'")
    return cleaned


def convert_setup(raw):
    if len(raw) > SETUP_LIMIT:
        raise ValueError("longer than %d characters" % SETUP_LIMIT)
    return raw


def convert_when_stuck(raw):
    first_word = (raw.split() or [""])[0].lower()
    if first_word not in WHEN_STUCK_VALUES:
        raise ValueError("write pause or continue")
    return first_word


LOOP_SETTING_CONVERTERS = {
    "dispatch": convert_dispatch,
    "e2e": convert_yes_no,
    "budget_turn": convert_money,
    "budget_loop": convert_money,
    "minutes_turn": lambda raw: convert_whole_number(raw, MIN_TURN_MINUTES, MAX_TURN_MINUTES),
    "branch": convert_branch,
    "base_branch": convert_base_branch,
    "setup": convert_setup,
    "when_stuck": convert_when_stuck,
    "push_pr": convert_yes_no,
}


def parse_loop_settings(text):
    """The loop settings written in the ROADMAP header (decided in planning, never in the Start dialog).

    -> {"settings": {...every key...}, "problems": [{"name", "value", "message"}], "missing": [keys without a header line], "defaults": {...}}. A missing line = the default
    (no problem); an invalid value = the default + a problem. Lines inside an HTML comment (the template documents them) are ignored."""
    cleaned = HTML_COMMENT_PATTERN.sub("", text or "")
    settings, problems, missing = dict(LOOP_SETTING_DEFAULTS), [], []
    for key, header_name in LOOP_SETTING_HEADER_NAMES.items():
        raw = read_header_value(cleaned, key)
        if raw == "" and key == "base_branch":  # an empty `Branch base:` is a mistake, not "no line": default + a problem
            problems.append({"name": header_name, "value": raw, "message": "write a branch name: using the default (%s)" % describe_default(key)})
            continue
        if raw is None or (raw == "" and key != "setup"):
            missing.append(key)  # an empty value on a line that needs one is the same as no line
            continue
        try:
            settings[key] = LOOP_SETTING_CONVERTERS[key](raw)
        except ValueError as failure:
            problems.append({"name": header_name, "value": raw, "message": "%s: using the default (%s)" % (failure, describe_default(key))})
    return {"settings": settings, "problems": problems, "missing": missing,
            "defaults": dict(LOOP_SETTING_DEFAULTS)}


def read_roadmap_base_branch(text):
    """The branch written in `**Branch base:**` when the line is there and valid; "" when absent, empty or invalid (callers fall back to the
    environment, then `developer`)."""
    parsed = parse_loop_settings(text)
    return "" if "base_branch" in parsed["missing"] or any(item["name"] == LOOP_SETTING_HEADER_NAMES["base_branch"] for item in parsed["problems"])         else parsed["settings"]["base_branch"]
