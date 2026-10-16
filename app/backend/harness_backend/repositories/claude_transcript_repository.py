"""Claude Code transcripts (`<transcripts_dir>/<work slug>/<session>.jsonl` and `.../<session>/subagents/agent-*.jsonl`).

Read only. Each file is parsed once per (mtime, size): the cache avoids re-reading large transcripts on every poll.
Credentials come out redacted; whatever cannot be extracted becomes None (a number is never invented).
"""
import json
import os
import re
import threading
from typing import Protocol

from .loop_read_formatting import compute_seconds_between, format_elapsed_clock, parse_iso_date, redact_credentials, to_number

TRANSCRIPT_TEXT_LIMIT = 2000
LOG_ITEMS_LIMIT = 400
SUMMARY_LIMIT = 200
ACTION_LIMIT = 160
TRANSCRIPT_CACHE_LIMIT = 256
SESSION_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SUBAGENT_FILE_PATTERN = re.compile(r"agent-([A-Za-z0-9_-]{1,80})\.jsonl")
PROMPT_FEATURE_PATTERN = re.compile(r"\b(?:SCOPE_)?FEATURE:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)")
PATH_FEATURE_PATTERN = re.compile(r"\.specs[/\\]features[/\\]([A-Za-z0-9][A-Za-z0-9._-]*)")
ANY_CASE_FEATURE_PATTERN = re.compile(r"(?i)\b(?:SCOPE_)?FEATURE:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)")
RETURN_KEYWORDS = ("DONE", "APPROVED", "BLOCKED", "PASS", "FAIL", "UNVERIFIED", "NEEDS_CONFIRM", "TOO_BIG")
RETURN_STATUS_LINE_PATTERN = re.compile(r"(?im)^\s*(?:STATUS|VERDICT)\s*:\s*\**\s*([A-Za-z_]+)")
RETURN_DECISION_LINE_PATTERN = re.compile(r"(?im)^\s*(?:SUMMARY|DECISION)\s*:\s*(\S.*)$")
RETURN_HEADER_LINE_PATTERN = re.compile(r"(?i)^\s*(?:FEATURE|SCOPE_FEATURE|FLOW|SCRIPT|STATUS|VERDICT|SCOPE|CITED|FILES|PATHS|DEPS|KIND|RAN|LAYERS|"
                                        r"ENVIRONMENT|CHANGED|FAIL|NOTES|STEPS)\s*:")
TOOL_SUMMARY_KEYS = ("command", "file_path", "pattern", "url", "query", "description", "prompt", "path")
SUBAGENT_RETURN_TOOL = "SubagentHandback"
END_TURN_REASON = "end_turn"
SYSTEM_REMINDER_PREFIX = "<system-reminder>"
# a PreToolUse hook that exits 2 has its stderr put in the tool_result (is_error true); the orchestrator guard starts it with this marker
GUARD_BLOCK_PATTERN = re.compile(r"BLOCKED by the orchestrator guard \[([^\]\n]{1,200})\]:\s*(.*)", re.S)
GUARD_ADVICE_PATTERN = re.compile(r"\.?\s*Do not try to work around it\b.*$", re.S)
GUARD_LOG_TYPE = "guard"
GUARD_EXIT_LABEL = "exit 2"
GUARD_REASON_LIMIT = 400


def list_project_slug_variants(work_dir):
    """Possible project folder names for this work dir: non alphanumeric becomes '-'; drive letter as is, lower and upper case."""
    path = os.path.abspath(work_dir).rstrip("\\/") if work_dir else ""
    variants = [path]
    if len(path) > 1 and path[1] == ":":
        variants += [path[0].lower() + path[1:], path[0].upper() + path[1:]]
    return list(dict.fromkeys(re.sub(r"[^A-Za-z0-9]", "-", item) for item in variants if item))


def extract_block_text(content):
    """Text of a message/tool_result content (string or list of blocks)."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(str(block.get("text")) for block in content if isinstance(block, dict) and block.get("text") is not None)
    return ""


def summarize_tool_call(name, tool_input):
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    for key in TOOL_SUMMARY_KEYS:
        if tool_input.get(key):
            extra = " in %s" % tool_input["path"] if key == "pattern" and tool_input.get("path") else ""
            return "%s %s%s" % (name, tool_input[key], extra)
    try:
        return "%s %s" % (name, json.dumps(tool_input, ensure_ascii=False))
    except (TypeError, ValueError):
        return str(name)


def parse_guard_block(result_text, is_error):
    """(rule, reason) when the tool result is a block by the orchestrator guard; None otherwise. Only an error result counts:
    a tool that merely READ the guard source or its log also contains the marker."""
    if not is_error:
        return None
    found = GUARD_BLOCK_PATTERN.search(result_text or "")
    if not found:
        return None
    reason = GUARD_ADVICE_PATTERN.sub("", found.group(2)).strip()
    return found.group(1).strip(), redact_credentials(" ".join(reason.split()), GUARD_REASON_LIMIT)


def list_message_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def extract_return_status(final_return):
    """Keyword of the final return (`STATUS:`/`VERDICT:` or first word of the handback); None when there is none."""
    if not final_return:
        return None
    found = RETURN_STATUS_LINE_PATTERN.search(final_return)
    word = found.group(1) if found else (re.match(r"\W*([A-Za-z_]+)", final_return) or [None, ""])[1]
    word = (word or "").upper()
    return word if word in RETURN_KEYWORDS else None


def extract_return_decision(final_return):
    """Short sentence of what the agent concluded: SUMMARY/DECISION line, else 1st FINDINGS item, else the 1st non-header line."""
    if not final_return:
        return None
    found = RETURN_DECISION_LINE_PATTERN.search(final_return)
    if found:
        return redact_credentials(found.group(1).strip(), SUMMARY_LIMIT)
    lines = [line.strip() for line in final_return.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        if re.match(r"(?i)^FINDINGS\s*:\s*$", line):
            item = next((following for following in lines[index + 1:] if following.startswith(("-", "*"))), None)
            if item:
                return redact_credentials(item.lstrip("-* ").strip(), SUMMARY_LIMIT)
        elif re.match(r"(?i)^FINDINGS\s*:\s*\S", line):
            return redact_credentials(line.partition(":")[2].strip(), SUMMARY_LIMIT)
    free = next((line for line in lines if not RETURN_HEADER_LINE_PATTERN.match(line)), None)
    return redact_credentials(free.lstrip("-* ").strip(), SUMMARY_LIMIT) if free else None


class TranscriptReader:
    """Accumulates a .jsonl transcript line by line (tolerates a corrupted line and a missing field)."""

    def __init__(self):
        self.first_stamp = None
        self.last_stamp = None
        self.usage_by_message = {}
        self.prompt = None
        self.final_return = None
        self.last_text = None
        self.last_stop_reason = None
        self.items = []
        self.pending_tools = {}
        self.total_items = 0
        self.last_result_was_error = False
        self.had_api_error = False
        self.last_action = None

    def add_item(self, item_type, text, moment, result=""):
        self.total_items += 1
        if len(self.items) >= LOG_ITEMS_LIMIT:
            return None
        item = {"type": item_type, "text": redact_credentials(text, TRANSCRIPT_TEXT_LIMIT),
                "result": redact_credentials(result, TRANSCRIPT_TEXT_LIMIT), "_ts": moment}
        self.items.append(item)
        return item

    def read_line(self, line):
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict):
            return
        stamp = record.get("timestamp") if isinstance(record.get("timestamp"), str) else None
        moment = parse_iso_date(stamp) if stamp else None
        if stamp:
            self.first_stamp = self.first_stamp or stamp
            self.last_stamp = stamp
        message = record.get("message") if isinstance(record.get("message"), dict) else None
        record_type = record.get("type")
        if message is None or record_type not in ("assistant", "user") or record.get("isMeta"):
            return
        blocks = list_message_blocks(message.get("content"))
        if record_type == "assistant":
            self.read_assistant_message(record, message, blocks, moment)
        else:
            self.read_user_message(blocks, moment)

    def read_assistant_message(self, record, message, blocks, moment):
        self.last_result_was_error = False
        self.last_stop_reason = message.get("stop_reason") or self.last_stop_reason
        self.had_api_error = self.had_api_error or bool(record.get("isApiErrorMessage"))
        usage = message.get("usage")
        if isinstance(usage, dict):  # repeated lines of the same message.id (streaming) count once
            slot = self.usage_by_message.setdefault(message.get("id") or "line-%d" % len(self.usage_by_message), {})
            for key, value in usage.items():
                if key.endswith("_tokens") and to_number(value) is not None:
                    slot[key] = max(slot.get(key, 0), value)
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and str(block.get("text") or "").strip():
                self.last_text = str(block["text"])
                self.add_item("ai", self.last_text, moment)
                self.last_action = {"type": "ai", "text": self.last_text, "_ts": moment}
            elif block.get("type") == "tool_use":
                name = str(block.get("name") or "tool")
                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                if name == SUBAGENT_RETURN_TOOL:  # the subagent's final return shows up as model speech
                    self.final_return = str(tool_input.get("message") or "")
                    self.add_item("ai", self.final_return, moment)
                    continue
                self.last_action = {"type": "tool", "text": summarize_tool_call(name, tool_input), "_ts": moment}
                item = self.add_item("tool", summarize_tool_call(name, tool_input), moment)
                if item is not None and block.get("id"):
                    self.pending_tools[block["id"]] = item

    def read_user_message(self, blocks, moment):
        saw_result = False
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                saw_result = True
                self.last_result_was_error = bool(block.get("is_error"))
                item = self.pending_tools.pop(block.get("tool_use_id"), None)
                if item is not None:
                    result_text = extract_block_text(block.get("content"))
                    item["result"] = redact_credentials(result_text, TRANSCRIPT_TEXT_LIMIT)
                    guard_block = parse_guard_block(result_text, block.get("is_error"))
                    if guard_block:  # blocked by the PreToolUse guard: a `guard` item with the rule and the reason
                        item["type"], item["rule"], item["reason"] = GUARD_LOG_TYPE, guard_block[0], guard_block[1]
                        item["result"] = "%s · [%s] %s" % (GUARD_EXIT_LABEL, guard_block[0], guard_block[1])
            elif (block.get("type") == "text" and str(block.get("text") or "").strip()
                  and not str(block["text"]).lstrip().startswith(SYSTEM_REMINDER_PREFIX)):
                text = str(block["text"])
                self.add_item("user" if self.prompt is not None else "sys", text, moment)
                if self.prompt is None and self.last_action is None:
                    self.last_action = {"type": "sys", "text": text, "_ts": moment}
                self.prompt = text if self.prompt is None else self.prompt
        self.last_result_was_error = self.last_result_was_error if saw_result else False

    def build_result(self):
        start = parse_iso_date(self.first_stamp) if self.first_stamp else None
        for item in self.items:
            item["t"] = format_elapsed_clock(start, item.pop("_ts"))
        totals = [sum(slot.values()) for slot in self.usage_by_message.values() if slot]
        finished = self.final_return is not None or self.last_stop_reason == END_TURN_REASON
        final_return = self.final_return if self.final_return is not None else (self.last_text if finished else None)
        last_action = None
        if self.last_action is not None:
            last_action = {"type": self.last_action["type"],
                           "text": " ".join(redact_credentials(self.last_action["text"]).split())[:ACTION_LIMIT],
                           "t": format_elapsed_clock(start, self.last_action["_ts"]) or None}
        lines = [part.strip() for part in (self.final_return or self.last_text or "").splitlines() if part.strip()]
        found = None
        if self.prompt:
            found = (PROMPT_FEATURE_PATTERN.search(self.prompt) or ANY_CASE_FEATURE_PATTERN.search(self.prompt)
                     or PATH_FEATURE_PATTERN.search(self.prompt))
        return {"started_at": self.first_stamp, "finished_at": self.last_stamp,
                "duration_s": compute_seconds_between(self.first_stamp, self.last_stamp),
                "tokens": int(sum(totals)) if totals else None, "feature": found.group(1).rstrip(".") if found else None,
                "summary": redact_credentials(lines[0], SUMMARY_LIMIT) if lines else None, "final": finished,
                "failed": (self.last_result_was_error or self.had_api_error) and self.final_return is None,
                "last_action": last_action, "return_status": extract_return_status(final_return),
                "decision": extract_return_decision(final_return),
                "initial_prompt": redact_credentials(self.prompt, TRANSCRIPT_TEXT_LIMIT) if self.prompt else None,
                "final_return": redact_credentials(final_return, TRANSCRIPT_TEXT_LIMIT) if final_return else None,
                "log": self.items, "truncated": self.total_items > LOG_ITEMS_LIMIT}


def parse_transcript_file(path):
    """Reads a .jsonl transcript. None = unreadable file."""
    try:
        handle = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return None
    reader = TranscriptReader()
    with handle:
        for line in handle:
            reader.read_line(line)
    return reader.build_result()


class IClaudeTranscriptRepository(Protocol):
    def read_transcript(self, path) -> dict | None: ...
    def locate_session_files(self, work_dir, session_id) -> tuple: ...


class ClaudeTranscriptRepository:
    def __init__(self, transcripts_dir, log, cache_limit=TRANSCRIPT_CACHE_LIMIT):
        self.transcripts_dir = os.path.abspath(transcripts_dir)
        self.log = log
        self.cache_limit = cache_limit
        self.transcript_cache = {}  # path -> ((mtime_ns, size), result)
        self.cache_lock = threading.Lock()
        self.parse_count = 0  # how many times a file was actually parsed (cache tests)

    def read_transcript(self, path):
        """Parsed transcript with a cache by (mtime, size). None = missing or unreadable."""
        try:
            info = os.stat(path)
        except OSError:
            return None
        stamp = (info.st_mtime_ns, info.st_size)
        with self.cache_lock:
            stored = self.transcript_cache.get(path)
            if stored and stored[0] == stamp:
                return stored[1]
        try:
            parsed = parse_transcript_file(path)
        except Exception as failure:  # a strange transcript never brings the API down
            self.log.warn("loop_read_transcript_unreadable", path=path, type=type(failure).__name__)
            parsed = None
        with self.cache_lock:
            self.parse_count += 1
            while self.transcript_cache and len(self.transcript_cache) >= self.cache_limit:
                self.transcript_cache.pop(next(iter(self.transcript_cache)), None)
            self.transcript_cache[path] = (stamp, parsed)
        return parsed

    def locate_session_files(self, work_dir, session_id):
        """(orchestrator transcript or None, [(agentId, jsonl, meta.json)]) of a session. session_id outside the pattern = nothing."""
        if not isinstance(session_id, str) or not SESSION_PATTERN.fullmatch(session_id):
            return None, []
        for slug in list_project_slug_variants(work_dir):
            folder = os.path.join(self.transcripts_dir, slug)
            main_path = os.path.join(folder, session_id + ".jsonl")
            subagents_dir = os.path.join(folder, session_id, "subagents")
            has_main, has_subagents = os.path.isfile(main_path), os.path.isdir(subagents_dir)
            if not (has_main or has_subagents):
                continue
            try:
                names = sorted(os.listdir(subagents_dir)) if has_subagents else []
            except OSError:
                names = []
            subagents = []
            for name in names:
                found = SUBAGENT_FILE_PATTERN.fullmatch(name)
                if found:
                    subagents.append((found.group(1), os.path.join(subagents_dir, name),
                                      os.path.join(subagents_dir, "agent-%s.meta.json" % found.group(1))))
            return (main_path if has_main else None), subagents
        return None, []

    def read_agent_type(self, meta_path):
        """`agentType` of the subagent's `.meta.json`; unreadable/missing = None."""
        try:
            with open(meta_path, encoding="utf-8") as handle:
                meta = json.load(handle)
        except (OSError, ValueError):
            return None
        agent_type = meta.get("agentType") if isinstance(meta, dict) else None
        return agent_type if isinstance(agent_type, str) and agent_type else None
