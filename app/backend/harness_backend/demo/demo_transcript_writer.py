"""Writes the Claude Code transcripts of a finished demo turn, in the format the panel's history reads:
`<transcripts_dir>/<work slug>/<session>.jsonl` (orchestrator) and `<session>/subagents/agent-<id>.jsonl` + `.meta.json`.

A guard block is written the way Claude Code records a PreToolUse hook that exited 2: the tool call, then a `tool_result`
with `is_error: true` whose text is the hook's stderr (`BLOCKED by the orchestrator guard [<rule>]: <reason>. ...`).
"""
import json
import os

from ..repositories.claude_transcript_repository import list_project_slug_variants
from .demo_story_catalog import GUARD_ADVICE, GUARD_HOOK_COMMAND, GUARD_MARKER
from .demo_workspace import write_text_atomically

OUTPUT_SHARE = 0.08
CACHE_SHARE = 0.7
RETURN_TOOL = "SubagentHandback"


def format_transcript_time(moment):
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def split_usage(tokens):
    """Spreads a token count over the usage fields a real message has (they sum back to `tokens`)."""
    tokens = int(tokens or 0)
    output_tokens = int(tokens * OUTPUT_SHARE)
    cache_tokens = int(tokens * CACHE_SHARE)
    return {"input_tokens": tokens - output_tokens - cache_tokens, "cache_read_input_tokens": cache_tokens, "output_tokens": output_tokens}


def build_hook_block_text(action):
    return "PreToolUse:%s hook error: [%s]: %s [%s]: %s. %s\n" % (action["tool"], GUARD_HOOK_COMMAND, GUARD_MARKER, action["rule"],
                                                                 action["reason"], GUARD_ADVICE)


class TranscriptRowBuilder:
    """Turns recorded actions into transcript lines (one assistant message per action, tool results as user lines)."""

    def __init__(self, prefix):
        self.prefix = prefix
        self.rows = []
        self.counter = 0

    def next_id(self, kind):
        self.counter += 1
        return "%s_%s_%03d" % (kind, self.prefix, self.counter)

    def add_prompt(self, moment, text):
        self.rows.append({"type": "user", "timestamp": format_transcript_time(moment), "message": {"role": "user", "content": text}})

    def add_assistant(self, moment, blocks, tokens, stop_reason=None):
        self.rows.append({"type": "assistant", "timestamp": format_transcript_time(moment),
                          "message": {"id": self.next_id("msg"), "role": "assistant", "content": blocks, "stop_reason": stop_reason,
                                      "usage": split_usage(tokens)}})

    def add_tool_call(self, moment, tool, tool_input, result, tokens, is_error=False):
        tool_id = self.next_id("toolu")
        self.add_assistant(moment, [{"type": "tool_use", "id": tool_id, "name": tool, "input": tool_input}], tokens, "tool_use")
        self.rows.append({"type": "user", "timestamp": format_transcript_time(moment),
                          "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id,
                                                                   "content": [{"type": "text", "text": result}], "is_error": is_error}]}})

    def add_action(self, action):
        moment, tokens = action["at"], action["tokens"]
        if action["type"] == "ai":
            self.add_assistant(moment, [{"type": "text", "text": action["text"]}], tokens)
        elif action["type"] == "tool":
            self.add_tool_call(moment, action["tool"], action["input"], action["result"], tokens)
        elif action["type"] == "guard":
            self.add_tool_call(moment, action["tool"], action["input"], build_hook_block_text(action), tokens, is_error=True)
        elif action["type"] == "return":
            self.add_tool_call(moment, RETURN_TOOL, {"message": action["text"]}, '{"success":true}', tokens)


class DemoTranscriptWriter:
    def __init__(self, transcripts_dir, work_dir):
        self.transcripts_dir = transcripts_dir
        self.project_dir = os.path.join(transcripts_dir, list_project_slug_variants(work_dir)[0])

    def write_turn(self, session_id, orchestrator, agents):
        """orchestrator = {"prompt", "started_at", "actions", "result", "finished_at"}; agents = [{"agent_id", "role", "prompt",
        "started_at", "actions"}]. Each action = {"type": "ai|tool|guard|return", "at", "tokens", ...}."""
        main = TranscriptRowBuilder("orch")
        main.add_prompt(orchestrator["started_at"], orchestrator["prompt"])
        for action in orchestrator["actions"]:
            main.add_action(action)
        main.add_assistant(orchestrator["finished_at"], [{"type": "text", "text": orchestrator["result"]}], 0, stop_reason="end_turn")
        self.write_rows(os.path.join(self.project_dir, session_id + ".jsonl"), main.rows)
        subagents_dir = os.path.join(self.project_dir, session_id, "subagents")
        for agent in agents:
            builder = TranscriptRowBuilder(agent["agent_id"][-6:])
            builder.add_prompt(agent["started_at"], agent["prompt"])
            for action in agent["actions"]:
                builder.add_action(action)
            self.write_rows(os.path.join(subagents_dir, "agent-%s.jsonl" % agent["agent_id"]), builder.rows)
            write_text_atomically(os.path.join(subagents_dir, "agent-%s.meta.json" % agent["agent_id"]),
                                  json.dumps({"agentType": agent["role"]}))

    @staticmethod
    def write_rows(path, rows):
        write_text_atomically(path, "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows))
