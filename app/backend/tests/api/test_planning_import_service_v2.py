"""T32 (services/planning_import_service): preview copies nothing, import creates both copies and IMPORT-LOG.md, re-import is 409
`already_imported`, `feature_folder_exists`, the sent message and record_coverage (appends once). The service is driven directly with a stub
planning service (no AI)."""
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.domain.project_errors import InvalidRequestError, RepoConflictError
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.planning_import_service import PlanningImportService

SPEC = "# Orig\n\n## Goals\n\n- ORIG-01 a\n- ORIG-02 b\n\n## Decisions\n\nD-1 chose x. token = abcd1234secret\n"
TASKS = "# Tasks\n\n### T1: one\n"


class FakeLog:
    def info(self, *args, **fields):
        pass

    warn = info


class StubPlanning:
    def __init__(self):
        self.messages = []

    def send_message(self, context, message, on_prepare_failed=None):
        self.messages.append(message)
        return {"ok": True, "job_id": "job-1"}


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def tree(folder):
    return sorted(os.path.relpath(os.path.join(root, name), folder) for root, _dirs, names in os.walk(folder) for name in names)


class ImportServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="import-svc-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.work = os.path.join(self.tmp, "work")
        os.makedirs(self.work)
        self.context = SimpleNamespace(work_dir=self.work, repo_path=self.work, project=SimpleNamespace(id="p1"))
        self.planning = StubPlanning()
        self.service = PlanningImportService(SpecsFileRepository(), self.planning, FakeLog())
        self.source = os.path.join(self.tmp, "produto-origem")
        write(os.path.join(self.source, "spec.md"), SPEC)
        write(os.path.join(self.source, "tasks.md"), TASKS)
        write(os.path.join(self.source, "extra", "notes.md"), "# Notes\n")

    def features_folder(self, slug="produto-origem"):
        return os.path.join(self.work, ".specs", "features", slug)

    def import_folder(self, import_id):
        return os.path.join(self.service.imported_root(self.context), import_id)


class PreviewTests(ImportServiceTests):
    def test_preview_copies_nothing_and_calls_no_ai(self):
        before = tree(self.work)
        preview = self.service.preview_import(self.context, {"path": self.source})
        self.assertEqual(tree(self.work), before)
        self.assertEqual(self.planning.messages, [])
        self.assertEqual(preview["slug"], "produto-origem")
        self.assertEqual([entry["relative_path"] for entry in preview["files"]], ["extra/notes.md", "spec.md", "tasks.md"])
        self.assertEqual((preview["counts"]["requirements"], preview["counts"]["decisions"], preview["counts"]["tasks"]), (2, 1, 1))
        self.assertIsNone(preview["refused"])
        self.assertIsNone(preview["already_imported"])
        self.assertNotIn("text", preview["files"][0])

    def test_preview_of_an_over_limit_folder_returns_the_refusal_payload(self):
        for index in range(30):
            write(os.path.join(self.source, "f%02d.md" % index), "# F\n")
        preview = self.service.preview_import(self.context, {"path": self.source})
        self.assertIn("limit is 30", preview["refused"]["reason"])
        self.assertEqual(preview["warnings"], [])

    def test_preview_warns_when_the_feature_folder_exists_without_a_record(self):
        write(os.path.join(self.features_folder(), "spec.md"), "# mine\n")
        warnings = self.service.preview_import(self.context, {"path": self.source})["warnings"]
        self.assertTrue(any("already exists without an import record" in item for item in warnings), warnings)


class ImportTests(ImportServiceTests):
    def test_import_creates_both_copies_redacted_and_the_log_with_one_row_per_file(self):
        result = self.service.import_spec(self.context, {"path": self.source})
        self.assertEqual(result["files"], ["extra/notes.md", "spec.md", "tasks.md", "IMPORT-LOG.md"])
        folder = self.import_folder(result["import_id"])
        for name in ("extra/notes.md", "spec.md", "tasks.md"):
            self.assertTrue(os.path.isfile(os.path.join(folder, *name.split("/"))), name)
            self.assertTrue(os.path.isfile(os.path.join(self.features_folder(), *name.split("/"))), name)
        self.assertNotIn("abcd1234secret", read(os.path.join(self.features_folder(), "spec.md")))
        self.assertNotIn("abcd1234secret", read(os.path.join(folder, "spec.md")))
        log = read(os.path.join(folder, "IMPORT-LOG.md"))
        for name in ("extra/notes.md", "spec.md", "tasks.md"):
            self.assertIn("| %s |" % name, log)
        self.assertIn("Detected: 2 requirements, 1 decisions, 1 tasks", log)
        self.assertEqual(read(os.path.join(self.source, "spec.md")), SPEC, "the source is never touched")

    def test_importing_the_same_folder_again_is_409_already_imported_with_the_log_path(self):
        first = self.service.import_spec(self.context, {"path": self.source})
        with self.assertRaises(RepoConflictError) as caught:
            self.service.import_spec(self.context, {"path": self.source})
        error = caught.exception
        self.assertEqual(error.code, "already_imported")
        self.assertEqual(error.extras["import_id"], first["import_id"])
        self.assertTrue(error.extras["log_path"].endswith("%s/IMPORT-LOG.md" % first["import_id"]))
        self.assertEqual(len(self.service.list_imports(self.context)["imports"]), 1)

    def test_the_same_slug_from_another_folder_is_also_refused(self):
        self.service.import_spec(self.context, {"path": self.source})
        other = os.path.join(self.tmp, "elsewhere", "produto-origem")
        write(os.path.join(other, "spec.md"), "# Other\n")
        with self.assertRaises(RepoConflictError) as caught:
            self.service.import_spec(self.context, {"path": other})
        self.assertEqual(caught.exception.code, "already_imported")

    def test_a_different_feature_still_imports_and_adds_to_the_plan(self):
        self.service.import_spec(self.context, {"path": self.source})
        other = os.path.join(self.tmp, "outra-feature")
        write(os.path.join(other, "spec.md"), "# Other\n\nOUT-01 x\n")
        self.service.import_spec(self.context, {"path": other})
        self.assertEqual(len(self.service.list_imports(self.context)["imports"]), 2)
        self.assertTrue(os.path.isfile(os.path.join(self.features_folder("outra-feature"), "spec.md")))

    def test_an_existing_feature_folder_without_an_import_is_feature_folder_exists_and_nothing_is_copied(self):
        write(os.path.join(self.features_folder(), "spec.md"), "# written by hand\n")
        with self.assertRaises(RepoConflictError) as caught:
            self.service.import_spec(self.context, {"path": self.source})
        self.assertEqual(caught.exception.code, "feature_folder_exists")
        self.assertEqual(read(os.path.join(self.features_folder(), "spec.md")), "# written by hand\n")
        self.assertEqual(self.service.list_imports(self.context)["imports"], [])

    def test_a_refused_folder_raises_a_400_and_copies_nothing(self):
        os.remove(os.path.join(self.source, "spec.md"))
        with self.assertRaises(InvalidRequestError):
            self.service.import_spec(self.context, {"path": self.source})
        self.assertFalse(os.path.exists(os.path.join(self.work, ".specs")))


class SendAndCoverageTests(ImportServiceTests):
    def import_and_send(self):
        import_id = self.service.import_spec(self.context, {"path": self.source})["import_id"]
        self.service.send_import(self.context, import_id)
        return import_id

    def test_the_sent_message_has_the_coverage_rules_and_the_detected_ids_and_the_log_gets_a_sent_line(self):
        import_id = self.import_and_send()
        message = self.planning.messages[0]
        for expected in ("IMPORT-COVERAGE.md", "reqs=", "dropped: <reason>", ".specs/features/produto-origem/", "ORIG-01, ORIG-02",
                         "D-1", "T1", "INDEX, not a copy", "Never summarize"):
            self.assertIn(expected, message)
        self.assertIn("Sent: ", read(os.path.join(self.import_folder(import_id), "IMPORT-LOG.md")))
        self.assertIsNotNone(self.service.list_imports(self.context)["imports"][0]["sent_at"])

    def test_record_coverage_appends_one_line_and_only_once_for_the_same_result(self):
        import_id = self.import_and_send()
        log_path = os.path.join(self.import_folder(import_id), "IMPORT-LOG.md")
        result = {"covered": ["ORIG-01"], "dropped": [], "missing": ["ORIG-02"]}
        self.assertTrue(self.service.record_coverage(self.context, result))
        self.assertFalse(self.service.record_coverage(self.context, result), "same result: nothing appended")
        lines = [line for line in read(log_path).splitlines() if line.startswith("Coverage (")]
        self.assertEqual(len(lines), 1)
        self.assertIn("1 covered, 0 dropped, 1 missing. Missing: ORIG-02.", lines[0])
        self.assertTrue(self.service.record_coverage(self.context, {"covered": ["ORIG-01", "ORIG-02"], "dropped": [], "missing": []}))
        self.assertEqual(len([line for line in read(log_path).splitlines() if line.startswith("Coverage (")]), 2)

    def test_record_coverage_without_an_import_does_nothing(self):
        self.assertFalse(self.service.record_coverage(self.context, {"covered": [], "dropped": [], "missing": []}))

    def test_send_of_an_unknown_import_is_not_found(self):
        from harness_backend.domain.project_errors import JobNotFoundError
        with self.assertRaises(JobNotFoundError):
            self.service.send_import(self.context, "20260101-000000-nope")


if __name__ == "__main__":
    unittest.main()
