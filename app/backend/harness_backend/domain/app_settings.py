"""The app settings of `~/.loop-harness/config.json` that the Settings screen edits: the catalog (key, text, type, default) and the
validation of a new value. Pure: no file, no HTTP. A key the user never saved stays absent from config.json, so every default here
equals the one the code already uses (`config/app_config.py`, `engine/runtime_isolation.py`).
"""
import math
import os
import re
from dataclasses import dataclass, field

BOOL, INT, NUMBER, TEXT, LIST, CHOICE = "bool", "int", "number", "text", "list", "choice"
TEXT_MAX_LENGTH = 500
LIST_MAX_ITEMS = 50
SKILL_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")  # a skill is a folder name: never a path
CAVEMAN_NOT_INSTALLED_NOTE = ("Not installed: no SessionStart hook that mentions caveman was found in your Claude settings "
                              "(~/.claude/settings.json), so there is nothing to carry.")
CAVEMAN_RECOMMENDATION = ("Off by default: loop output is mostly machine-read lines and files, so terse replies save little. "
                          "Turn it on to measure.")


class InvalidSettingError(ValueError):
    """The key is unknown or the value does not fit it (HTTP 400)."""


class SettingUnavailableError(InvalidSettingError):
    """The setting cannot be turned on in this installation (caveman without the hook)."""


class ConfigFileUnreadableError(RuntimeError):
    """config.json exists but is not a JSON object: saving would erase it, so nothing is written (HTTP 409)."""


@dataclass(frozen=True)
class SettingDefinition:
    key: str
    label: str
    description: str
    type: str
    default: object
    restart_required: bool = False
    minimum: float = None
    choices: tuple = field(default_factory=tuple)  # (value, label) pairs of a `choice`


def default_output_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


SETTING_CATALOG = (
    SettingDefinition("loop_isolation", "Isolate every Claude run",
                      "Runs each claude -p with only the skills, agents and hooks the app allows, instead of your whole profile. "
                      "Turn it off only to get the old behavior back, for example to debug a missing skill.", BOOL, True),
    SettingDefinition("loop_caveman", "Caveman mode",
                      "Carries your caveman SessionStart hook into the isolated runs, so the replies come out terse. "
                      "Needs isolation on and the hook installed in your Claude settings.", BOOL, False),
    SettingDefinition("loop_extra_skills", "Extra skills",
                      "Skill names (one per line) added to the allowlist of every run, on top of the defaults. "
                      "Add one when a run needs a skill from your profile that the app does not include.", LIST, []),
    SettingDefinition("orchestrator_model", "Orchestrator model",
                      "The model of the loop's orchestrator turns. Subagents and app jobs keep their own ceiling (Sonnet). "
                      "Pick the account default to send no model flag.", CHOICE, "opus", restart_required=True,
                      choices=(("opus", "Opus (latest)"), ("sonnet", "Sonnet (latest)"), ("haiku", "Haiku (latest)"),
                               ("", "Account default (no flag)"))),
    SettingDefinition("claude_binary", "Claude command",
                      "The command that starts Claude Code. Empty uses claude from your PATH. "
                      "Change it to run a wrapper (for example cc) signed in with another account.", TEXT, "", restart_required=True),
    SettingDefinition("claude_fallback_binaries", "Fallback commands",
                      "Commands tried in order, one per line, when the running one hits the account usage limit. "
                      "A command missing on this machine is skipped.", LIST, [], restart_required=True),
    SettingDefinition("max_parallel_maps", "Parallel subproject maps",
                      "How many subproject map jobs of a monorepo run at the same time; the rest wait in a queue. "
                      "Lower it if your machine or account limit struggles.", INT, 6, restart_required=True, minimum=1),
    SettingDefinition("idle_hours", "Idle shutdown (hours)",
                      "The app shuts itself down after this many hours without activity or a running loop. 0 means it never shuts down.",
                      NUMBER, 2, restart_required=True, minimum=0),
    SettingDefinition("inventory_giant_lines", "Giant file (lines)",
                      "A code file with more lines than this counts as giant: the readiness check warns when a feature touches one "
                      "without a refactor feature before it.", INT, 1500, restart_required=True, minimum=1),
    SettingDefinition("inventory_giant_kb", "Giant file (KB)",
                      "A code file larger than this many KB also counts as giant. Works together with the line limit.",
                      INT, 200, restart_required=True, minimum=1),
    SettingDefinition("output_dir", "Output folder",
                      "The folder where exported files (reports, summaries) are written. Unset uses your Downloads folder.",
                      TEXT, "", restart_required=True),
)
SETTING_BY_KEY = {definition.key: definition for definition in SETTING_CATALOG}


def find_definition(key):
    definition = SETTING_BY_KEY.get(key) if isinstance(key, str) else None
    if definition is None:
        raise InvalidSettingError("unknown setting: %s" % (key if isinstance(key, str) else type(key).__name__))
    return definition


def get_default_value(definition):
    """The value shown as default (the output folder shows the real path of Downloads)."""
    if definition.key == "output_dir":
        return default_output_dir()
    return list(definition.default) if isinstance(definition.default, list) else definition.default


def is_whole_number(value):
    return isinstance(value, int) and not isinstance(value, bool)


def is_finite_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_text_value(definition, value):
    if not isinstance(value, str):
        raise InvalidSettingError("%s must be text" % definition.key)
    text = value.strip()
    if len(text) > TEXT_MAX_LENGTH or any(ord(character) < 32 for character in text):
        raise InvalidSettingError("%s must be one line of at most %d characters" % (definition.key, TEXT_MAX_LENGTH))
    return text


def read_list_value(definition, value):
    if not isinstance(value, list):
        raise InvalidSettingError("%s must be a list" % definition.key)
    items = []
    for item in value:
        if not isinstance(item, str):
            raise InvalidSettingError("%s must be a list of text" % definition.key)
        text = read_text_value(definition, item)
        if text and text not in items:
            items.append(text)
    if len(items) > LIST_MAX_ITEMS:
        raise InvalidSettingError("%s accepts at most %d items" % (definition.key, LIST_MAX_ITEMS))
    if definition.key == "loop_extra_skills":
        for name in items:
            if not SKILL_NAME_PATTERN.match(name):
                raise InvalidSettingError("%r is not a valid skill name (letters, digits, dot, dash and underscore only)" % name)
    return items


def validate_setting_value(definition, value):
    """The value to store, or InvalidSettingError. Bool must be a JSON boolean, int a whole number (no 6.5, no true), number finite."""
    if definition.type == BOOL:
        if not isinstance(value, bool):
            raise InvalidSettingError("%s must be true or false" % definition.key)
        return value
    if definition.type in (INT, NUMBER):
        usable = is_whole_number(value) if definition.type == INT else is_finite_number(value)
        if not usable:
            raise InvalidSettingError("%s must be a %s" % (definition.key, "whole number" if definition.type == INT else "number"))
        if definition.minimum is not None and value < definition.minimum:
            raise InvalidSettingError("%s must be at least %s" % (definition.key, definition.minimum))
        return value
    if definition.type == CHOICE:
        allowed = [choice for choice, _ in definition.choices]
        if not isinstance(value, str) or value.strip() not in allowed:
            raise InvalidSettingError("%s must be one of: %s" % (definition.key, ", ".join(repr(choice) for choice in allowed)))
        return value.strip()
    if definition.type == LIST:
        return read_list_value(definition, value)
    text = read_text_value(definition, value)
    if definition.key == "output_dir" and not text:
        raise InvalidSettingError("output_dir cannot be empty: use Reset to default instead")
    return text
