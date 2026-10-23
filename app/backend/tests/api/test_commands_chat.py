"""Read-only chat per project (`/p/<id>/api/chat/...`). Port of ChatTests of the legacy panel: exact argv, environment
without secrets, resume+fork only for the orchestrator with a session, subagent context from the transcript, persistence
without leaking the session_id, error/redaction, timeout, limits (1 per thread, 3 in total) and validation."""
import datetime
import json
import os
import re
import time
import unittest

from harness_backend.repositories.agent_transcript_repository import list_project_slug_variants
from tests.api.support_commands import CommandsTestCase, Project, is_alive

SESSION = "sess-turn-16"
# fake chat claude: records argv/stdin/env of every call in record.jsonl and answers according to mode.txt (next to the script)
FAKE_CHAT_CLAUDE = """import json, os, sys, time
folder = os.path.dirname(os.path.abspath(__file__))
mode = open(os.path.join(folder, "mode.txt"), encoding="utf-8").read().strip() if os.path.exists(os.path.join(folder, "mode.txt")) else "ok"
stdin = sys.stdin.read()
with open(os.path.join(folder, "record.jsonl"), "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": sys.argv[1:], "stdin": stdin, "cwd": os.getcwd(), "pid": os.getpid(),
                             "env": {k: v for k, v in os.environ.items() if k in ("MY_FAKE_SECRET_TOKEN", "LOOP_MONITOR_URL", "ANTHROPIC_FAKE")}}) + "\\n")
if mode == "error":
    sys.stderr.write("simulated failure\\n")
    sys.exit(3)
if mode in ("sleep", "wait"):
    release = os.path.join(folder, "release")
    deadline = time.time() + (60 if mode == "sleep" else 25)
    while time.time() < deadline and (mode == "sleep" or not os.path.exists(release)):
        time.sleep(0.1)
result = "test reply"
if mode == "secret":
    result = "found token=abc123secret in the code"
print(json.dumps({"result": result, "total_cost_usd": 0.25, "session_id": "sess-fake-%d" % len(open(os.path.join(folder, "record.jsonl")).read().splitlines()), "is_error": False}))
"""


def assistant_row(moment, message_id, blocks, usage=None, stop=None, **extra):
    return dict({"type": "assistant", "timestamp": moment, "message": {"id": message_id, "role": "assistant", "content": blocks, "stop_reason": stop,
                                                                       "usage": usage or {}}}, **extra)


def user_row(moment, content, **extra):
    return dict({"type": "user", "timestamp": moment, "message": {"role": "user", "content": content}}, **extra)


def tool_use(identifier, name, **tool_input):
    return {"type": "tool_use", "id": identifier, "name": name, "input": tool_input}


def tool_result(identifier, text, is_error=False):
    return {"type": "tool_result", "tool_use_id": identifier, "content": [{"type": "text", "text": text}], "is_error": is_error}


def shifted(start, seconds):
    moment = datetime.datetime.fromisoformat(start.replace("Z", "+00:00")) + datetime.timedelta(seconds=seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (moment.microsecond // 1000)


def agent_rows(prompt, start="2026-10-15T15:00:00.000Z"):
    def at(seconds):
        return shifted(start, seconds)

    return [
        user_row(start, prompt),
        user_row(at(0.1), "<system-reminder>ignore</system-reminder>", isMeta=True),
        assistant_row(at(1), "msg_1", [{"type": "thinking", "thinking": "hmm"}], {"input_tokens": 2, "output_tokens": 5}),
        assistant_row(at(1.5), "msg_1", [tool_use("tu_1", "Read", file_path="C:/x/spec.md")], {"input_tokens": 2, "output_tokens": 40}),
        user_row(at(2), [tool_result("tu_1", "# spec content")]),
        assistant_row(at(3), "msg_2", [{"type": "text", "text": "Read the spec."}, tool_use("tu_2", "Bash", command="dotnet build")]),
        user_row(at(4), [tool_result("tu_2", "Build ok")]),
        assistant_row(at(65), "msg_3", [tool_use("tu_3", "SubagentHandback", message="FEATURE: slug-a\nVERDICT: APPROVED\nmore")], stop="tool_use"),
        user_row(at(65.5), [tool_result("tu_3", "{\"success\":true}")]),
    ]


class ChatTests(CommandsTestCase):
    def write_lines(self, path, rows, extra_lines=()):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            for line in extra_lines:
                handle.write(line + "\n")

    def setup_projects(self, session=SESSION, number=16):
        fixture = self.fixture
        self.projects = os.path.join(fixture.tmp, "claude-projects")
        slug_dir = os.path.join(self.projects, re.sub(r"[^A-Za-z0-9]", "-", fixture.work))
        fixture.write_json("turns/%03d.json" % number, {"session_id": session, "total_cost_usd": 0.96, "duration_ms": 13000, "result": "turn ok"})
        main = [user_row("2026-10-15T15:00:00.000Z", "<command-name>/orchestrator</command-name>"),
                assistant_row("2026-10-15T15:00:01.000Z", "msg_a", [{"type": "text", "text": "Dispatching agents"}], stop="end_turn")]
        self.write_lines(os.path.join(slug_dir, session + ".jsonl"), main, extra_lines=["{corrupted line", "", "[1,2]"])
        subs = os.path.join(slug_dir, session, "subagents")
        self.write_lines(os.path.join(subs, "agent-aaa111.jsonl"), agent_rows("Root: x\nSCOPE_FEATURE: slug-a\nWrite the spec. password=hunter2", "2026-10-15T15:00:10.000Z"))
        self.write_lines(os.path.join(subs, "agent-bbb222.jsonl"), agent_rows("Review the spec FEATURE: slug-b", "2026-10-15T15:00:05.000Z"))
        fixture.write(os.path.join(subs, "agent-aaa111.meta.json"), json.dumps({"agentType": "spec-writer"}))
        fixture.write(os.path.join(subs, "agent-bbb222.meta.json"), json.dumps({"agentType": "spec-reviewer"}))

    def prepare(self, mode="ok", timeout=30):
        self.stub_dir = os.path.join(self.fixture.tmp, "stub")
        self.fixture.write(os.path.join(self.stub_dir, "fake_claude.py"), FAKE_CHAT_CLAUDE)
        self.fixture.write(os.path.join(self.stub_dir, "mode.txt"), mode)
        self.fixture.claude_binary = os.path.join(self.stub_dir, "fake_claude.py")
        module = self.fixture.start(transcripts=getattr(self, "projects", None))
        module.chat_service.timeout_seconds = timeout
        return module

    def mode(self, mode):
        self.fixture.write(os.path.join(self.stub_dir, "mode.txt"), mode)

    def records(self):
        path = os.path.join(self.stub_dir, "record.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle]

    def send(self, text="what is this?", thread=None, scope=None):
        body = {"thread": thread, "scope": scope or {"type": "general", "agent_id": None, "turn": None}, "text": text}
        return self.fixture.call("/api/chat/send", "POST", body)

    def wait_for_job(self, job, seconds=30):
        deadline = time.time() + seconds
        while time.time() < deadline:
            status, data = self.fixture.call("/api/chat/job/" + job)
            self.assertEqual(status, 200)
            if data["state"] != "running":
                return data
            time.sleep(0.1)
        self.fail("the job did not finish")

    def test_slug_replaces_non_alphanumeric_and_tries_a_lower_case_drive(self):
        variants = list_project_slug_variants("C:\\Users\\x\\proj-worktree-loop")
        self.assertEqual(variants[0], "C--Users-x-proj-worktree-loop")
        self.assertIn("c--Users-x-proj-worktree-loop", variants)

    def test_exact_argv_read_only_no_mcp_and_prompt_through_stdin(self):
        self.prepare()
        status, sent = self.send("how much did turn 3 cost? SECRET-IN-THE-QUESTION")
        self.assertEqual(status, 202)
        self.assertRegex(sent["job"], r"^j-")
        self.assertRegex(sent["thread"], r"^t-")
        job = self.wait_for_job(sent["job"])
        self.assertEqual((job["state"], job["reply"], job["cost_usd"], job["error"], job["thread"]), ("ok", "test reply", 0.25, None, sent["thread"]))
        record, = self.records()
        argv = record["argv"]
        self.assertEqual(argv[:5], ["-p", "--output-format", "stream-json", "--verbose", "--model"])
        self.assertEqual(argv[argv.index("--model") + 1], "sonnet")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "1")
        allowed = argv[argv.index("--allowedTools") + 1:argv.index("--disallowedTools")]
        denied = argv[argv.index("--disallowedTools") + 1:argv.index("--setting-sources")]
        self.assertEqual((allowed, denied), (["Read", "Grep", "Glob"], ["Bash", "Edit", "Write", "NotebookEdit"]))
        self.assertEqual(argv[argv.index("--setting-sources") + 1], "project,local")  # runtime isolation (default on) sits between the tools and the MCP flags
        self.assertLess(argv.index("--settings"), argv.index("--strict-mcp-config"))
        self.assertEqual(argv[argv.index("--strict-mcp-config") + 1:], ["--mcp-config", '{"mcpServers":{}}'])
        self.assertNotIn("--resume", argv)
        self.assertFalse(any("SECRET-IN-THE-QUESTION" in item for item in argv), "the user's text never goes on the command line")
        self.assertTrue(record["stdin"].startswith("You are in READ-ONLY mode. Answer questions; do not claim to have changed anything."))
        self.assertIn("SECRET-IN-THE-QUESTION", record["stdin"])
        self.assertIn("ROADMAP.md", record["stdin"])
        self.assertIn("VALIDATE.md", record["stdin"])
        self.assertEqual(os.path.normcase(os.path.realpath(record["cwd"])), os.path.normcase(os.path.realpath(self.fixture.work)))

    def test_env_without_secrets_and_without_the_app_token(self):
        os.environ["MY_FAKE_SECRET_TOKEN"] = "secret-value"
        os.environ["ANTHROPIC_FAKE"] = "stays"
        os.environ["LOOP_MONITOR_URL"] = "http://x"
        self.addCleanup(lambda: [os.environ.pop(name, None) for name in ("MY_FAKE_SECRET_TOKEN", "ANTHROPIC_FAKE", "LOOP_MONITOR_URL")])
        self.prepare()
        self.wait_for_job(self.send()[1]["job"])
        self.assertEqual(self.records()[0]["env"], {"ANTHROPIC_FAKE": "stays"})
        self.assertNotIn(self.fixture.server.token, json.dumps(self.records()))

    def test_resume_and_fork_only_for_orch_with_a_session_subagent_goes_by_context(self):
        self.setup_projects()
        self.prepare()
        orch = self.send("and the orchestrator?", scope={"type": "agent", "agent_id": "v016:orch", "turn": 16})
        self.wait_for_job(orch[1]["job"])
        argv = self.records()[0]["argv"]
        self.assertEqual(argv[argv.index("--resume") + 1], SESSION)
        self.assertIn("--fork-session", argv)
        sub = self.send("and the spec-writer?", scope={"type": "agent", "agent_id": "v016:aaa111", "turn": 16})
        self.wait_for_job(sub[1]["job"])
        record = self.records()[1]
        self.assertNotIn("--resume", record["argv"])
        self.assertNotIn("--fork-session", record["argv"])
        self.assertIn("Agent spec-writer of turn 16 (feature: slug-a).", record["stdin"])
        self.assertIn("SCOPE_FEATURE: slug-a", record["stdin"])
        self.assertIn("VERDICT: APPROVED", record["stdin"])
        self.assertIn("tool Bash dotnet build", record["stdin"])
        self.assertNotIn("hunter2", record["stdin"], "the agent prompt comes out redacted")
        self.assertIn(os.path.join(self.fixture.work, ".specs", "features", "slug-a"), record["stdin"])
        missing = self.send("and turn 99?", scope={"type": "agent", "agent_id": "v099:orch", "turn": 99})
        self.wait_for_job(missing[1]["job"])
        self.assertNotIn("--resume", self.records()[2]["argv"])
        by_feature = self.send("and the feature?", scope={"type": "agent", "agent_id": "service-a:executor-back", "turn": None})
        self.wait_for_job(by_feature[1]["job"])
        self.assertIn(os.path.join(self.fixture.work, ".specs", "features", "service-a"), self.records()[3]["stdin"])

    def test_thread_continues_the_session_persists_and_never_leaks_the_session_id(self):
        self.prepare()
        status, first = self.send("first question")
        self.wait_for_job(first["job"])
        status, second = self.send("second question", thread=first["thread"])
        self.assertEqual((status, second["thread"]), (202, first["thread"]))
        self.wait_for_job(second["job"])
        first_record, second_record = self.records()
        self.assertNotIn("--resume", first_record["argv"])
        self.assertEqual(second_record["argv"][second_record["argv"].index("--resume") + 1], "sess-fake-1")
        self.assertNotIn("--fork-session", second_record["argv"])
        status, thread = self.fixture.call("/api/chat/thread/" + first["thread"])
        self.assertEqual([m["role"] for m in thread["messages"]], ["user", "ai", "user", "ai"])
        self.assertEqual((thread["messages"][0]["text"], thread["messages"][0]["mode"], thread["messages"][1]["cost_usd"]), ("first question", "question", 0.25))
        status, threads = self.fixture.call("/api/chat/threads")
        self.assertEqual([(t["thread"], t["title"], t["message_count"], t["cost_usd"]) for t in threads["threads"]], [(first["thread"], "first question", 4, 0.5)])
        self.assertEqual(self.fixture.call("/api/chat/threads?agent_id=other")[1]["threads"], [])
        self.assertEqual(len(self.fixture.call("/api/chat/threads?turn=7")[1]["threads"]), 0)
        for _, payload in (self.fixture.call("/api/chat/thread/" + first["thread"]), self.fixture.call("/api/chat/threads"),
                           self.fixture.call("/api/chat/job/" + second["job"])):
            self.assertNotIn("sess-fake", json.dumps(payload))
            self.assertNotIn("project_id", json.dumps(payload))
        path = os.path.join(self.fixture.work, ".specs", "project", "loop", "internal", "chat", first["thread"] + ".json")
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["session_id"], "sess-fake-2")
        self.assertEqual(self.fixture.call("/api/chat/thread/t-000000000000"), (404, {"ok": False, "error": "thread not found"}))
        self.assertEqual(self.fixture.call("/api/chat/thread/..%2F..%2Fx")[0], 404)
        self.assertEqual(self.send(thread="t-000000000000")[0], 404)

    def test_job_of_another_project_is_404(self):
        module = self.prepare()
        second = Project("other-project", "other", self.fixture.tmp)
        self.fixture.project_service.projects[second.id] = second
        status, sent = self.send()
        self.wait_for_job(sent["job"])
        self.assertEqual(self.fixture.call("/api/chat/job/" + sent["job"], project_id=second.id), (404, {"ok": False, "error": "job not found"}))
        self.assertEqual(self.fixture.call("/api/action/job/" + sent["job"], project_id=second.id)[0], 404)
        self.assertEqual(self.fixture.call("/api/action/job/" + sent["job"])[0], 200, "the action/job alias serves the same job")
        self.assertFalse(module.chat_service.is_busy())

    def test_job_error_and_redacted_reply(self):
        self.prepare("error")
        job = self.wait_for_job(self.send()[1]["job"])
        self.assertEqual((job["state"], job["reply"]), ("error", None))
        self.assertIn("simulated failure", job["error"])
        self.mode("secret")
        job = self.wait_for_job(self.send()[1]["job"])
        self.assertEqual(job["state"], "ok")
        self.assertNotIn("abc123secret", job["reply"])
        self.assertIn("[redacted]", job["reply"])

    def test_timeout_kills_the_process_and_reports_timed_out(self):
        self.prepare("sleep", timeout=1.5)
        job = self.wait_for_job(self.send()[1]["job"])
        self.assertEqual((job["state"], job["error"]), ("error", "timed out"))
        pid = self.records()[0]["pid"]
        deadline = time.time() + 10
        while time.time() < deadline and is_alive(pid):
            time.sleep(0.1)
        self.assertFalse(is_alive(pid))

    def test_limits_one_job_per_thread_and_three_in_total_and_the_app_does_not_idle(self):
        module = self.prepare("wait")
        self.addCleanup(lambda: self.fixture.write(os.path.join(self.stub_dir, "release"), "1"))
        sent = [self.send("question %d" % n) for n in range(3)]
        self.assertEqual([code for code, _ in sent], [202, 202, 202])
        self.assertEqual(self.send("fourth")[0], 409)
        self.assertEqual(self.send("again", thread=sent[0][1]["thread"])[0], 409)
        self.assertEqual(self.fixture.call("/api/action/propose", "POST", {"text": "deliver"})[0], 409, "the router shares the same limit")
        self.assertTrue(module.chat_service.is_busy())
        self.assertTrue(self.fixture.activity_monitor.is_any_provider_busy(), "with a chat job running the app does not idle")
        self.fixture.write(os.path.join(self.stub_dir, "release"), "1")
        for _, data in sent:
            self.assertEqual(self.wait_for_job(data["job"])["state"], "ok")
        self.assertFalse(module.chat_service.is_busy())
        self.assertEqual(self.send("now it fits")[0], 202)

    def test_input_validation_and_token(self):
        self.prepare()
        self.assertEqual(self.send("   "), (400, {"ok": False, "error": "empty text"}))
        self.assertEqual(self.send("x" * 8001)[0], 413)
        self.assertEqual(self.send("x", scope={"type": "xyz"})[0], 400)
        self.assertEqual(self.send("x", scope={"type": "agent", "agent_id": "a b", "turn": None})[0], 400)
        self.assertEqual(self.send("x", scope={"type": "agent", "agent_id": None, "turn": -1})[0], 400)
        self.assertEqual(self.fixture.call("/api/chat/send", "POST", {"text": "x"}, token=None)[0], 403)
        self.assertEqual(self.fixture.call("/api/instruction", "POST", {"text": "x"}, token=None)[0], 403)
        self.assertEqual(self.fixture.call("/api/chat/job/j-missing")[0], 404)
        self.assertEqual(self.records(), [])


if __name__ == "__main__":
    unittest.main()
