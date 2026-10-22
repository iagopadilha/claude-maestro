"""Spec import into the planning chat (`planning/import`, `planning/imports`, `planning/import/<id>/send`) with the FAKE claude.
Only a tlc-spec-driven feature folder (with spec.md) is imported: a single file or pasted text is refused."""
import os
import unittest

from .support_projects import loop_file_path, read_file, write_file
from .test_projects_planning import PlanningTestCase

SPEC_TEXT = "# Feature\n\nGoal: a thing.\napi token = abcd1234secret\nFiles: src/app.py\n"


class PlanningImportTests(PlanningTestCase):
    def setUp(self):
        super().setUp()
        self.repo, self.project_id = self.create_planning_project()
        self.source_dir = os.path.join(self.temporary_dir, "source")

    def import_body(self, body):
        return self.request("POST", "/p/%s/api/planning/import" % self.project_id, body)

    def list_imports(self):
        status, body = self.request("GET", "/p/%s/api/planning/imports" % self.project_id)
        self.assertEqual(status, 200, body)
        return body["imports"]

    def imported_file(self, import_id, name):
        return loop_file_path(self.repo, "internal", "planning", "imported", import_id, name)

    def feature_folder(self, name, files=(("spec.md", SPEC_TEXT),)):
        for file_name, text in files:
            write_file(os.path.join(self.source_dir, name, file_name), text)
        return os.path.join(self.source_dir, name)

    def test_feature_folder_is_copied_redacted_and_listed(self):
        folder = self.feature_folder("my feature")
        status, body = self.import_body({"path": folder})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["files"], body["main"], body["source"]), (["spec.md", "IMPORT-LOG.md"], "spec.md", "folder"))  # the log is an attachment too
        self.assertRegex(body["import_id"], r"^\d{8}-\d{6}-my-feature$")
        copy = read_file(self.imported_file(body["import_id"], "spec.md"))
        self.assertIn("Goal: a thing.", copy)
        self.assertNotIn("abcd1234secret", copy, "the copy goes through the credential redaction")
        self.assertEqual(read_file(os.path.join(folder, "spec.md")), SPEC_TEXT, "the source is never changed")
        self.assertEqual([item["import_id"] for item in self.list_imports()], [body["import_id"]])
        self.assertEqual(self.read_claude_calls(), [], "importing sends nothing")

    def test_folder_with_spec_md_takes_every_md_and_ignores_the_rest(self):
        folder = self.feature_folder("feat", (("spec.md", SPEC_TEXT), ("design.md", "# Design\n"), ("tasks.md", "# Tasks\n"),
                                              ("notes.txt", "x"), ("a.png", "x")))
        status, body = self.import_body({"path": folder})
        self.assertEqual(status, 200, body)
        self.assertEqual((body["files"], body["main"]), (["design.md", "spec.md", "tasks.md", "IMPORT-LOG.md"], "spec.md"))
        self.assertTrue(body["import_id"].endswith("-feat"))

    def test_refusals_are_clear_and_nothing_is_copied_or_sent(self):
        single = self.feature_folder("single")
        self.feature_folder("empty", (("spec.md", "  \n"),))
        self.feature_folder("big", (("spec.md", "x" * (400 * 1024 + 1)),))
        self.feature_folder("many", [("spec.md", "# S\n")] + [("f%02d.md" % index, "# F\n") for index in range(30)])  # 31 files, limit 30
        self.feature_folder("docs", (("a.md", "# A\n"), ("b.md", "# B\n")))
        cases = (({"path": os.path.join(single, "spec.md")}, "not a file"),
                 ({"path": os.path.join(self.source_dir, "empty")}, "is empty"),
                 ({"path": os.path.join(self.source_dir, "big")}, "larger than 400 KB"),
                 ({"path": os.path.join(self.source_dir, "many")}, "limit is 30"),
                 ({"path": os.path.join(self.source_dir, "docs")}, "no spec.md"),
                 ({"path": os.path.join(self.source_dir, "missing")}, "folder not found"),
                 ({"text": SPEC_TEXT}, "send `path`"),
                 ({}, "send `path`"))
        for body, message in cases:
            status, answer = self.import_body(body)
            self.assertEqual(status, 400, (body, answer))
            self.assertIn(message, answer["error"])
        self.assertEqual(self.list_imports(), [])
        self.assertEqual(self.read_claude_calls(), [])

    def test_send_sends_one_fixed_message_through_the_planning_chat(self):
        folder = self.feature_folder("feat", (("spec.md", SPEC_TEXT), ("design.md", "# Design\n")))
        import_id = self.import_body({"path": folder})[1]["import_id"]
        status, body = self.request("POST", "/p/%s/api/planning/import/%s/send" % (self.project_id, import_id))
        self.assertEqual(status, 202, body)
        self.assertEqual(self.wait_for_planning_job(self.project_id, body["job_id"])["state"], "ok")
        calls = self.read_claude_calls()
        self.assertEqual(len(calls), 1)
        stdin = calls[0]["stdin"]
        imported = ".specs/project/loop/internal/planning/imported/%s" % import_id
        for expected in (imported + "/spec.md", imported + "/design.md", "ROADMAP.md", "FLOWS.md", "PROJECT.md", "REPOS-SUGGESTED.json",
                         'title="..."', "paths=<repo>", "deps=", "edit or reference", "Open questions", "Read tool",
                         "spec feature", "descriptive"):
            self.assertIn(expected, stdin)
        self.assertNotIn("abcd1234secret", stdin)
        self.assertIsNotNone(self.list_imports()[0]["sent_at"])

    def test_importing_again_adds_to_the_conversation_and_never_wipes_it(self):
        self.send_and_wait(self.project_id, "Start the plan")
        first = self.import_body({"path": self.feature_folder("one", (("spec.md", "# One\n"),))})[1]["import_id"]
        second = self.import_body({"path": self.feature_folder("two", (("spec.md", "# Two\n"),))})[1]["import_id"]
        for import_id in (first, second):
            status, body = self.request("POST", "/p/%s/api/planning/import/%s/send" % (self.project_id, import_id))
            self.assertEqual(status, 202, body)
            self.wait_for_planning_job(self.project_id, body["job_id"])
        messages = self.get_planning(self.project_id)["thread"]["messages"]
        self.assertEqual(len(messages), 6, "the first message and both imports stay in the same conversation")
        self.assertEqual(messages[0]["text"], "Start the plan")
        self.assertEqual({item["import_id"] for item in self.list_imports()}, {first, second})
        self.assertIn("--resume", self.read_claude_calls()[-1]["argv"])

    def test_unknown_or_unsafe_import_id_is_404(self):
        for import_id in ("20260101-000000-nope", "..%2F..%2Fx", "bad"):
            status, body = self.request("POST", "/p/%s/api/planning/import/%s/send" % (self.project_id, import_id))
            self.assertEqual(status, 404, (import_id, body))
        self.assertEqual(self.read_claude_calls(), [])


if __name__ == "__main__":
    unittest.main()
