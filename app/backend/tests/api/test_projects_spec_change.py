"""Spec change (`/p/<id>/api/spec-change*`) with a FAKE claude: the chat that edits a definition already written, the file
guard (only the scope's files), the ROADMAP protection, busy/loop-running refusals, reset and the CLI permission flags."""
import os
import time
import unittest

from .support_projects import (FAKE_DELAY_VARIABLE, JOB_WAIT_TIMEOUT_SECONDS, ProjectsTestCase, read_file, write_file)

FEATURE_SPEC = "# api-base spec\n\n- REQ-1: GET /items returns 200\n"
OTHER_SPEC = "# screen-base spec\n\n- REQ-1: the list shows\n"

# Simulated claude -p of the spec-change chat: edits the spec of the scope and, on request, steps outside it.
FAKE_SPEC_CLAUDE = r'''
import json, os, re, sys
argv = sys.argv[1:]
stdin_text = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as handle:
    handle.write(json.dumps({"argv": argv, "stdin": stdin_text, "cwd": os.getcwd()}) + "\n")
scope = re.search(r"SPEC-CHANGE-SCOPE: (\S+)", stdin_text).group(1)
def write(path, text, mode="w"):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, mode, encoding="utf-8", newline="\n") as handle:
        handle.write(text)
if scope == "_project":
    if "ADD-FEATURE" in stdin_text:
        write(os.path.join(".specs", "project", "ROADMAP.md"), "- [sneaky] status=pending scope=Small kind=back paths=src/x\n", "a")
    else:
        write(os.path.join(".specs", "project", "FLOWS.md"), "\n## note\nchanged by the spec chat\n", "a")
else:
    write(os.path.join(".specs", "features", scope, "spec.md"), "- REQ-2: new rule from the chat\n", "a")
if "TOUCH-OUTSIDE" in stdin_text:
    write(os.path.join("src", "evil.txt"), "outside\n")
    write(os.path.join(".specs", "features", "screen-base", "spec.md"), "hijacked\n")
    write("README.md", "changed\n")
print(json.dumps({"type": "result", "is_error": False, "result": "done: " + stdin_text.strip().splitlines()[-1],
                  "session_id": "fake-spec-session-1", "total_cost_usd": 0.1}))
'''


class SpecChangeTestCase(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        write_file(self.fake_claude_path, FAKE_SPEC_CLAUDE)
        self.repo, self.project_id = self.create_ready_project()
        write_file(os.path.join(self.repo, ".specs", "features", "api-base", "spec.md"), FEATURE_SPEC)
        write_file(os.path.join(self.repo, ".specs", "features", "api-base", "design.md"), "# design\n")
        write_file(os.path.join(self.repo, ".specs", "features", "screen-base", "spec.md"), OTHER_SPEC)

    def path(self, scope, suffix=""):
        return "/p/%s/api/spec-change/%s%s" % (self.project_id, scope, suffix)

    def wait_for_job(self, job_id):
        deadline = time.time() + JOB_WAIT_TIMEOUT_SECONDS
        while time.time() < deadline:
            status, job = self.request("GET", "/p/%s/api/spec-change-job/%s" % (self.project_id, job_id))
            self.assertEqual(status, 200, job)
            if job["state"] != "running":
                return job
            time.sleep(0.1)
        self.fail("spec-change job %s did not finish" % job_id)

    def send_and_wait(self, scope, text):
        status, body = self.request("POST", self.path(scope, "/send"), {"text": text})
        self.assertEqual(status, 202, body)
        return self.wait_for_job(body["job_id"])

    def last_message(self, scope):
        return self.request("GET", self.path(scope))[1]["thread"]["messages"][-1]


class TestSpecChangeRead(SpecChangeTestCase):
    def test_lists_the_files_of_the_scope(self):
        status, body = self.request("GET", self.path("api-base"))
        self.assertEqual(status, 200, body)
        self.assertEqual(body["files"], [".specs/features/api-base/design.md", ".specs/features/api-base/spec.md"])
        self.assertEqual(body["thread"], {"messages": [], "busy": False, "job_id": None, "session": False})
        self.assertFalse(body["loop_running"])
        status, body = self.request("GET", self.path("_project"))
        self.assertEqual(status, 200, body)
        self.assertEqual([path.split("/")[-1] for path in body["files"]], ["PROJECT.md", "ROADMAP.md", "FLOWS.md", "RULES.md"])

    def test_unknown_or_invalid_scope_is_a_400(self):
        for scope in ("nope", ".hidden", "a%20b"):
            self.assertEqual(self.request("GET", self.path(scope))[0], 400, scope)
        self.assertEqual(self.request("POST", self.path("nope", "/send"), {"text": "x"})[0], 400)

    def test_unknown_project_is_404_and_send_needs_the_token(self):
        self.assertEqual(self.request("GET", "/p/missing/api/spec-change/api-base")[0], 404)
        self.assertEqual(self.request("POST", self.path("api-base", "/send"), {"text": "x"}, with_token=False)[0], 403)
        self.assertEqual(self.request("GET", "/p/%s/api/spec-change-job/j-000000000000" % self.project_id)[0], 404)


class TestSpecChangeSend(SpecChangeTestCase):
    def test_edits_the_feature_spec_resumes_the_session_and_reports_the_changed_files(self):
        job = self.send_and_wait("api-base", "the list must also support paging")
        self.assertEqual(job["state"], "ok", job)
        self.assertIn("paging", job["reply"])
        self.assertIn("REQ-2", read_file(os.path.join(self.repo, ".specs", "features", "api-base", "spec.md")))
        reply = self.last_message("api-base")
        self.assertEqual((reply["role"], reply["changed"]), ("ai", [".specs/features/api-base/spec.md"]))
        first = self.read_claude_calls()[-1]
        self.assertIn("spec-writer of ONE feature", first["stdin"], "the rules go on the first message")
        self.assertEqual(os.path.normcase(first["cwd"]), os.path.normcase(self.repo))
        argv = first["argv"]
        self.assertIn("Edit(/.specs/features/api-base/**)", argv)
        self.assertNotIn("Edit(/.specs/project/ROADMAP.md)", argv)
        self.assertIn("dontAsk", argv)
        self.assertEqual(argv[argv.index("--disallowedTools") + 1:argv.index("--disallowedTools") + 4], ["Bash", "NotebookEdit", "Agent"])
        self.assertNotIn("--resume", argv)

        self.send_and_wait("api-base", "and cursors")
        second = self.read_claude_calls()[-1]
        self.assertIn("fake-spec-session-1", second["argv"], "the conversation of the scope is resumed")
        self.assertNotIn("spec-writer of ONE feature", second["stdin"])
        self.assertEqual(len(self.request("GET", self.path("api-base"))[1]["thread"]["messages"]), 4)
        self.assertEqual(self.request("GET", self.path("screen-base"))[1]["thread"]["messages"], [], "one thread per scope")

    def test_guard_undoes_changes_outside_the_scope(self):
        job = self.send_and_wait("api-base", "TOUCH-OUTSIDE please")
        self.assertEqual(job["state"], "ok", job)
        self.assertIn("undid changes outside this scope", job["reply"])
        self.assertFalse(os.path.exists(os.path.join(self.repo, "src", "evil.txt")))
        self.assertEqual(read_file(os.path.join(self.repo, "README.md")), "# test\n")
        self.assertEqual(read_file(os.path.join(self.repo, ".specs", "features", "screen-base", "spec.md")), OTHER_SPEC)
        self.assertIn("REQ-2", read_file(os.path.join(self.repo, ".specs", "features", "api-base", "spec.md")), "the scope's edit stays")
        self.assertIn("spec_change_guard_reverted", read_file(os.path.join(self.config.logs_dir, "app.jsonl")))

    def test_project_scope_edits_the_documents_and_restores_a_roadmap_with_new_features(self):
        job = self.send_and_wait("_project", "document the paging flow")
        self.assertEqual(job["state"], "ok", job)
        self.assertEqual(self.last_message("_project")["changed"], [".specs/project/FLOWS.md"])
        roadmap_path = os.path.join(self.repo, ".specs", "project", "ROADMAP.md")
        roadmap_before = read_file(roadmap_path)
        job = self.send_and_wait("_project", "ADD-FEATURE please")
        self.assertEqual(job["state"], "ok", job)
        self.assertIn("restored ROADMAP.md", job["reply"])
        self.assertEqual(read_file(roadmap_path), roadmap_before)
        self.assertNotIn("ROADMAP.md", " ".join(self.last_message("_project")["changed"]))
        approval = self.request("GET", "/p/%s/api/planning" % self.project_id)[1]
        self.assertTrue(approval["approved"], "the approval still holds after the restore")

    def test_validation_loop_running_and_busy_and_reset(self):
        for text in ("", "   ", None, "x" * 8001):
            self.assertEqual(self.request("POST", self.path("api-base", "/send"), {"text": text})[0], 400, text)
        self.write_live_loop_lock(self.repo)
        self.assertTrue(self.request("GET", self.path("api-base"))[1]["loop_running"])
        status, body = self.request("POST", self.path("api-base", "/send"), {"text": "change"})
        self.assertEqual((status, body["code"]), (409, "loop_running"), body)
        os.remove(os.path.join(self.repo, ".specs", "project", "loop", "internal", "LOOP.lock"))

        os.environ[FAKE_DELAY_VARIABLE] = "1.5"
        status, body = self.request("POST", self.path("api-base", "/send"), {"text": "first"})
        self.assertEqual(status, 202, body)
        scope = self.request("GET", self.path("api-base"))[1]
        self.assertEqual((scope["thread"]["busy"], scope["thread"]["job_id"]), (True, body["job_id"]))
        status, other = self.request("POST", self.path("screen-base", "/send"), {"text": "second"})
        self.assertEqual((status, other["code"]), (409, "spec_change_busy"), "one claude job per project")
        status, other = self.request("POST", self.path("api-base", "/reset"), {"confirm": True})
        self.assertEqual((status, other["code"]), (409, "spec_change_busy"))
        self.assertEqual(self.wait_for_job(body["job_id"])["state"], "ok")
        os.environ.pop(FAKE_DELAY_VARIABLE)

        self.assertEqual(self.request("POST", self.path("api-base", "/reset"), {})[0], 400)
        self.assertEqual(self.request("POST", self.path("api-base", "/reset"), {"confirm": True}), (200, {"ok": True}))
        self.assertEqual(self.request("GET", self.path("api-base"))[1]["thread"],
                         {"messages": [], "busy": False, "job_id": None, "session": False})
        self.assertIn("REQ-2", read_file(os.path.join(self.repo, ".specs", "features", "api-base", "spec.md")), "the spec stays after a reset")

    def test_a_failed_reply_records_no_ai_message(self):
        write_file(self.fake_claude_path, "import sys\nsys.stderr.write('simulated claude failure')\nsys.exit(1)\n")
        job = self.send_and_wait("api-base", "change")
        self.assertEqual(job["state"], "error")
        self.assertIn("simulated claude failure", job["error"])
        self.assertEqual([m["role"] for m in self.request("GET", self.path("api-base"))[1]["thread"]["messages"]], ["user"])


if __name__ == "__main__":
    unittest.main()
