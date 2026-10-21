"""DecisionsService and DecisionsController (A6): list with review state, per turn filter, counts, review, change request (merge-safe draft),
404 for an unknown id, and VALIDATE.md never modified."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.api.controllers.decisions_controller import DecisionsController
from harness_backend.api.router import Request, Router
from harness_backend.domain.project import ProjectNotFoundError
from harness_backend.engine import decisions as decisions_engine
from harness_backend.services.decisions_service import DecisionNotFoundError, DecisionsService

OLD_LINE = "- [alpha] picked sqlite | chose: sqlite | revert: switch the driver"


class FakeProjects:
    def __init__(self, context):
        self.context = context

    def get_project_context(self, project_id):
        if project_id != "p1":
            raise ProjectNotFoundError(project_id)
        return self.context


class DecisionsServiceTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="decisions-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.context = SimpleNamespace(work_dir=self.root)
        decisions_engine.append(self.root, "alpha", 1, "important", "cache?", "memory", "remove the cache")
        decisions_engine.append(self.root, "beta", 2, "critical", "drop column?", "kept it", "migration down")
        decisions_engine.append(self.root, "beta", 2, "important", "naming?", "short", "rename")
        with open(decisions_engine.validate_path(self.root), "a", encoding="utf-8") as handle:
            handle.write(OLD_LINE + "\n- [broken] not a decision with the pieces\n")
        self.validate_before = decisions_engine.read_file(self.root)
        self.service = DecisionsService()
        self.controller = DecisionsController(FakeProjects(self.context), self.service)

    def test_list_merges_fields_counts_and_unparsed(self):
        data = self.service.list_decisions(self.context)
        self.assertEqual([item["slug"] for item in data["items"]], ["alpha", "beta", "beta", "alpha"])
        first = data["items"][0]
        self.assertEqual((first["turn"], first["level"], first["doubt"], first["chose"], first["revert"], first["reviewed"], first["change_requested"]),
                         (1, "important", "cache?", "memory", "remove the cache", False, False))
        self.assertEqual(data["items"][3]["turn"], None)
        self.assertEqual(len(data["unparsed"]), 1)
        self.assertEqual(data["counts"], {"open_critical": 1, "open_important": 3})

    def test_turn_filter_keeps_global_counts(self):
        data = self.service.list_decisions(self.context, turn=2)
        self.assertEqual([item["turn"] for item in data["items"]], [2, 2])
        self.assertEqual(data["counts"]["open_important"], 3)

    def test_review_marks_and_lowers_counts_without_touching_validate(self):
        critical = self.service.unreviewed_critical(self.context)[0]
        self.assertEqual(self.service.mark_reviewed(self.context, critical["id"]), {"ok": True})
        self.assertEqual(self.service.unreviewed_critical(self.context), [])
        self.assertEqual(self.service.open_counts(self.context), {"open_critical": 0, "open_important": 3})
        item = next(row for row in self.service.list_decisions(self.context)["items"] if row["id"] == critical["id"])
        self.assertTrue(item["reviewed"])
        self.assertTrue(item["reviewed_at"])
        self.assertEqual(decisions_engine.read_file(self.root), self.validate_before)

    def test_change_request_merges_into_the_draft_and_keeps_text(self):
        draft_path = self.service.draft_path(self.root)
        os.makedirs(os.path.dirname(draft_path), exist_ok=True)
        with open(draft_path, "w", encoding="utf-8") as handle:
            json.dump({"text": "my notes", "decisions": ["old-id"]}, handle)
        first, second = [item["id"] for item in self.service.list_decisions(self.context)["items"][:2]]
        self.service.request_change(self.context, first)
        result = self.service.request_change(self.context, first)  # twice: no duplicate
        self.assertEqual(result["draft"], {"text": "my notes", "decisions": ["old-id", first]})
        self.assertEqual(self.service.request_change(self.context, second)["draft"]["decisions"], ["old-id", first, second])
        with open(draft_path, encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["text"], "my notes")
        flagged = self.service.list_decisions(self.context)["items"][0]
        self.assertTrue(flagged["change_requested"])
        self.assertFalse(flagged["reviewed"])
        self.assertEqual(decisions_engine.read_file(self.root), self.validate_before)

    def test_change_request_creates_the_draft_when_missing(self):
        identifier = self.service.list_decisions(self.context)["items"][0]["id"]
        self.assertEqual(self.service.request_change(self.context, identifier)["draft"], {"text": "", "decisions": [identifier]})

    def test_unknown_id_raises(self):
        with self.assertRaises(DecisionNotFoundError):
            self.service.mark_reviewed(self.context, "nope")
        with self.assertRaises(DecisionNotFoundError):
            self.service.request_change(self.context, "nope")

    def test_empty_work_folder(self):
        empty = SimpleNamespace(work_dir=tempfile.mkdtemp(prefix="decisions-empty-"))
        self.addCleanup(shutil.rmtree, empty.work_dir, True)
        self.assertEqual(self.service.list_decisions(empty), {"items": [], "unparsed": [], "counts": {"open_critical": 0, "open_important": 0}})

    def call(self, method, path, query=None, project_id="p1"):
        router = Router()
        self.controller.register_routes(router)
        route, params = router.find_route(method, "/p/%s/api%s" % (project_id, path))
        self.assertIsNotNone(route, path)
        response = route.handler(Request(method, path, params, query or {}, {}))
        return response.status, response.body

    def test_controller_routes(self):
        status, data = self.call("GET", "/decisions")
        self.assertEqual((status, len(data["items"])), (200, 4))
        status, data = self.call("GET", "/decisions", {"turn": ["1"]})
        self.assertEqual([item["slug"] for item in data["items"]], ["alpha"])
        self.assertEqual(self.call("GET", "/decisions", {"turn": ["x"]})[0], 400)
        identifier = self.service.list_decisions(self.context)["items"][0]["id"]
        self.assertEqual(self.call("POST", "/decisions/%s/review" % identifier), (200, {"ok": True}))
        status, data = self.call("POST", "/decisions/%s/change" % identifier)
        self.assertEqual((status, data["ok"], data["draft"]["decisions"]), (200, True, [identifier]))
        self.assertEqual(self.call("POST", "/decisions/zzz/review")[0], 404)
        self.assertEqual(self.call("POST", "/decisions/zzz/change")[0], 404)
        self.assertEqual(self.call("GET", "/decisions", project_id="other")[0], 404)


if __name__ == "__main__":
    unittest.main()
