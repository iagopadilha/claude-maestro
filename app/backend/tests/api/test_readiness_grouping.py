"""T17 (RDY-01 / RDY-03): `group_items` merges the uncommitted items of one repo, `build_items` and the cached/`--json` payload carry `items`
with a level (problem/warning)."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.services.readiness_service import ReadinessService, build_items, group_items, parse_json_items

from .test_readiness_service import FakeLog, FakeRunner


def uncommitted(repo, *files):
    return {"kind": "uncommitted", "level": "warning", "repo": repo, "files": list(files), "text": "uncommitted changes in %s" % repo,
            "fix": "Commit these files (the worktree is born from the last commit)"}


class GroupItemsTests(unittest.TestCase):
    def test_three_uncommitted_items_of_one_repo_become_one_with_deduplicated_files(self):
        grouped = group_items([uncommitted("repo", "a.py", "b.py"), uncommitted("repo", "b.py", "docs/STACK.md"),
                               uncommitted("repo", "a.py", "CLAUDE.md")])
        self.assertEqual(len(grouped), 1)
        self.assertEqual(grouped[0]["files"], ["a.py", "b.py", "docs/STACK.md", "CLAUDE.md"])

    def test_one_item_per_repo_in_first_seen_order_and_other_items_pass_through(self):
        other = {"kind": "node-modules", "level": "warning", "repo": None, "files": [], "text": "node", "fix": "Add `npm ci`"}
        grouped = group_items([uncommitted("repo", "a"), other, uncommitted("catalogo", "x"), uncommitted("repo", "b")])
        self.assertEqual([(item["kind"], item.get("repo")) for item in grouped], [("uncommitted", "repo"), ("node-modules", None),
                                                                                  ("uncommitted", "catalogo")])
        self.assertEqual(grouped[0]["files"], ["a", "b"])

    def test_the_inputs_are_not_mutated_and_junk_is_tolerated(self):
        first, second = uncommitted("repo", "a"), uncommitted("repo", "b")
        group_items([first, second, "junk", None])
        self.assertEqual((first["files"], second["files"]), (["a"], ["b"]))
        self.assertEqual(group_items(None), [])

    def test_build_items_has_levels_problem_and_warning(self):
        items = build_items(["credential $X is not defined"], ["no test command"],
                            [{"repo": "r", "setup_files": ["a"], "other_changes": ["b"]}, {"repo": "r", "setup_files": ["b"], "other_changes": []}])
        self.assertEqual([(item["kind"], item["level"]) for item in items], [("uncommitted", "warning"), ("other", "problem"), ("other", "warning")])
        self.assertEqual(items[0]["files"], ["a", "b"])

    def test_parse_json_items_reads_the_last_json_line_and_falls_back_to_none(self):
        payload = json.dumps({"items": [{"kind": "other"}, "junk"]})
        self.assertEqual(parse_json_items("[check] READY\n[check] JSON: %s\n" % payload), [{"kind": "other"}])
        self.assertIsNone(parse_json_items("[check] READY\n"))
        self.assertIsNone(parse_json_items("[check] JSON: {broken"))


class ServiceItemsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="readiness-items-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.context = SimpleNamespace(work_dir=self.folder, repo_path=self.folder, project=SimpleNamespace(id="p1"))
        os.makedirs(os.path.join(self.folder, ".specs", "project"))

    def run_with(self, output):
        service = ReadinessService(FakeRunner((0, output, "")), lambda: "run_loop.py", FakeLog(), lambda: [], python="python")
        return service.run(self.context, wait=True)

    def test_the_json_items_of_the_check_are_grouped_and_carry_their_level(self):
        items = [uncommitted("repo", "a.py"), uncommitted("repo", "a.py", "b.py"),
                 {"kind": "other", "level": "problem", "repo": None, "files": [], "text": "p", "fix": ""}]
        output = "[check] READY. Command: x\n[check] JSON: %s\n" % json.dumps({"items": items})
        state = self.run_with(output)
        self.assertEqual([(item["kind"], item["level"]) for item in state["items"]], [("uncommitted", "warning"), ("other", "problem")])
        self.assertEqual(state["items"][0]["files"], ["a.py", "b.py"])

    def test_without_a_json_line_the_items_come_from_the_text(self):
        state = self.run_with("[check] WARNING: no test command known for repo-b\n[check] READY. Command: x\n")
        self.assertEqual([(item["level"], item["text"]) for item in state["items"]], [("warning", "no test command known for repo-b")])


if __name__ == "__main__":
    unittest.main()
