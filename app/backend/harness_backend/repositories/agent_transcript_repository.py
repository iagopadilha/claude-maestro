"""Reads Claude Code transcripts only to build the CONTEXT of an agent's chat (read-only).

Where they live: `<claude_transcripts_dir>/<slug>/<session_id>.jsonl` (the turn's orchestrator) and
`<slug>/<session_id>/subagents/agent-<id>.jsonl` + `agent-<id>.meta.json` (agent type); `<slug>` = absolute path of the
`work` with every non-alphanumeric character replaced by `-` (also tries the drive letter in lower/upper case).
Extracts only what the chat uses: initial prompt, last actions, final return and feature. Everything comes out redacted.
"""
import json
import os
import re
from typing import Protocol

from ..infrastructure.claude_client import redact_secrets
from .loop_command_file_repository import loop_state, read_json_object

SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
SUBAGENT_FILE_PATTERN = re.compile(r"agent-([A-Za-z0-9_-]{1,80})\.jsonl")
FEATURE_IN_PROMPT_PATTERN = re.compile(r"\b(?:SCOPE_)?FEATURE:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)")
FEATURE_IN_PROMPT_CASELESS_PATTERN = re.compile(r"(?i)\b(?:SCOPE_)?FEATURE:[ \t]*([A-Za-z0-9][A-Za-z0-9._-]*)")
FEATURE_IN_SPEC_PATH_PATTERN = re.compile(r"\.specs[/\\]features[/\\]([A-Za-z0-9][A-Za-z0-9._-]*)")
TRANSCRIPT_TEXT_LIMIT = 2000
MAX_LOG_ITEMS = 400
SUBAGENT_HANDBACK_TOOL_NAME = "SubagentHandback"
TOOL_SUMMARY_KEYS = ("command", "file_path", "pattern", "url", "query", "description", "prompt", "path")
END_OF_TURN_REASON = "end_turn"
SYSTEM_REMINDER_START = "<system-reminder>"


def list_project_slug_variants(work_dir):
    """Possible transcript folder names for this work (drive letter as is, lower case and upper case)."""
    path = os.path.abspath(work_dir).rstrip("\\/") if work_dir else ""
    variants = [path]
    if len(path) > 1 and path[1] == ":":
        variants += [path[0].lower() + path[1:], path[0].upper() + path[1:]]
    return list(dict.fromkeys(re.sub(r"[^A-Za-z0-9]", "-", item) for item in variants if item))


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


def list_message_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


class IAgentTranscriptRepository(Protocol):
    def read_turn_session_id(self, work_dir, turn_number): ...
    def locate_session_files(self, work_dir, session_id) -> tuple: ...
    def read_transcript_summary(self, path): ...
    def read_agent_type(self, meta_path): ...


class AgentTranscriptRepository:
    def __init__(self, claude_transcripts_dir):
        self.claude_transcripts_dir = os.path.abspath(claude_transcripts_dir)

    def read_turn_session_id(self, work_dir, turn_number):
        """`session_id` of `turns/NNN.json` when it has a safe format; otherwise None."""
        turn = read_json_object(os.path.join(loop_state.turns_dir(work_dir), "%03d.json" % turn_number))
        session_id = turn.get("session_id")
        return session_id if isinstance(session_id, str) and SESSION_ID_PATTERN.fullmatch(session_id) else None

    def locate_session_files(self, work_dir, session_id):
        """(orchestrator transcript or None, [(agent_id, jsonl, meta.json)]). session_id outside the format = (None, [])."""
        if not isinstance(session_id, str) or not SESSION_ID_PATTERN.fullmatch(session_id):
            return None, []
        for slug in list_project_slug_variants(work_dir):
            project_dir = os.path.join(self.claude_transcripts_dir, slug)
            main_path = os.path.join(project_dir, session_id + ".jsonl")
            subagents_dir = os.path.join(project_dir, session_id, "subagents")
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
                    agent_id = found.group(1)
                    subagents.append((agent_id, os.path.join(subagents_dir, name),
                                      os.path.join(subagents_dir, "agent-%s.meta.json" % agent_id)))
            return (main_path if has_main else None), subagents
        return None, []

    def read_agent_type(self, meta_path):
        agent_type = read_json_object(meta_path).get("agentType") if meta_path else None
        return agent_type if isinstance(agent_type, str) and agent_type else None

    def read_transcript_summary(self, path):
        """{"initial_prompt","actions":[{"type","text"}],"final_return","feature"} redacted; None = missing/unreadable.
        Tolerates a corrupted line and a missing field (same reading as the agent history)."""
        try:
            handle = open(path, encoding="utf-8", errors="replace")
        except OSError:
            return None
        prompt = handback_return = last_text = last_stop_reason = None
        actions = []
        try:
            with handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(record, dict):
                        continue
                    message = record.get("message") if isinstance(record.get("message"), dict) else None
                    record_type = record.get("type")
                    if message is None or record_type not in ("assistant", "user") or record.get("isMeta"):
                        continue
                    blocks = list_message_blocks(message.get("content"))
                    if record_type == "assistant":
                        last_stop_reason = message.get("stop_reason") or last_stop_reason
                        for block in blocks:
                            if not isinstance(block, dict):
                                continue
                            if block.get("type") == "text" and str(block.get("text") or "").strip():
                                last_text = str(block["text"])
                                self.append_action(actions, "ai", last_text)
                            elif block.get("type") == "tool_use":
                                name = str(block.get("name") or "tool")
                                tool_input = block.get("input") if isinstance(block.get("input"), dict) else {}
                                if name == SUBAGENT_HANDBACK_TOOL_NAME:
                                    handback_return = str(tool_input.get("message") or "")
                                    self.append_action(actions, "ai", handback_return)
                                    continue
                                self.append_action(actions, "tool", summarize_tool_call(name, tool_input))
                    elif prompt is None:
                        for block in blocks:
                            text = str(block.get("text") or "") if isinstance(block, dict) and block.get("type") == "text" else ""
                            if text.strip() and not text.lstrip().startswith(SYSTEM_REMINDER_START):
                                prompt = text
                                break
        except Exception:  # a strange transcript never takes the chat down
            return None
        finished = handback_return is not None or last_stop_reason == END_OF_TURN_REASON
        final_return = handback_return if handback_return is not None else (last_text if finished else None)
        found = None
        if prompt:
            found = (FEATURE_IN_PROMPT_PATTERN.search(prompt) or FEATURE_IN_PROMPT_CASELESS_PATTERN.search(prompt)
                     or FEATURE_IN_SPEC_PATH_PATTERN.search(prompt))
        return {"initial_prompt": redact_secrets(prompt, TRANSCRIPT_TEXT_LIMIT) if prompt else None,
                "actions": actions,
                "final_return": redact_secrets(final_return, TRANSCRIPT_TEXT_LIMIT) if final_return else None,
                "feature": found.group(1).rstrip(".") if found else None}

    @staticmethod
    def append_action(actions, action_type, text):
        if len(actions) < MAX_LOG_ITEMS:
            actions.append({"type": action_type, "text": redact_secrets(text, TRANSCRIPT_TEXT_LIMIT)})
