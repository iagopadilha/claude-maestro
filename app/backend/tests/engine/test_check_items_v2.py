"""T15 / T16 (RDY-01, RDY-02): `readiness_checks.uncommitted_items` (one item per repo, each file once) and `run_loop.check_items` / `--check --json`
(structured items with kind, level, repo, files, text and the fix sentence of design D2; the text lines stay the same)."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import project_repos  # noqa: E402
import readiness_checks  # noqa: E402
import run_loop  # noqa: E402
from test_check_planning import PlanningCheckCase, write  # noqa: E402

JSON_PREFIX = "[check] JSON: "


class CheckItemsTexts(unittest.TestCase):
    def items(self, problems, warns):
        return run_loop.check_items(problems, warns, [], os.getcwd())

    def test_every_kind_gets_its_fix_sentence_and_a_level(self):
        problems = ["missing .specs/project/ROADMAP.md: generate and approve the roadmap first"]
        warns = [".NET detected but no restore in Setup", "Node project without node_modules", "`auto` mode depends on the account",
                 "no test command known for repo-b"]
        by_kind = {item["kind"]: item for item in self.items(problems, warns)}
        self.assertEqual(set(by_kind), {"missing-roadmap", "dotnet-restore", "node-modules", "permission-mode", "other"})
        for kind in ("missing-roadmap", "dotnet-restore", "node-modules", "permission-mode"):
            self.assertTrue(by_kind[kind]["fix"], kind)
        self.assertEqual(by_kind["missing-roadmap"]["level"], "problem")
        self.assertEqual(by_kind["dotnet-restore"]["level"], "warning")
        self.assertIn("dotnet restore", by_kind["dotnet-restore"]["fix"])
        self.assertIn("npm ci", by_kind["node-modules"]["fix"])
        self.assertIn("acceptEdits", by_kind["permission-mode"]["fix"])
        self.assertEqual(by_kind["other"]["fix"], "")
        for item in by_kind.values():
            self.assertEqual(set(item), {"kind", "level", "repo", "files", "text", "fix"})

    def test_the_text_of_a_line_is_never_changed_and_one_item_per_warning(self):
        warns = ["Node project without node_modules", "something else"]
        items = self.items([], warns)
        self.assertEqual([item["text"] for item in items], warns)

    def test_uncommitted_text_lines_are_replaced_by_structured_items(self):
        items = self.items([], ["uncommitted changes in repo: 1 setup file not committed"])
        self.assertEqual(items, [])


class UncommittedItemsTests(PlanningCheckCase):
    def repos_of(self, project):
        return project_repos.load_repos(project.repo)

    def test_a_repo_with_uncommitted_files_yields_one_item_naming_the_repo_and_listing_each_file_once(self):
        project = self.make_planning()
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        write(os.path.join(project.repo, "other.txt"), "x\n")
        items = readiness_checks.uncommitted_items(self.repos_of(project), project.repo)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual((item["kind"], item["repo"]), ("uncommitted", os.path.basename(project.repo)))
        self.assertIn("docs/codebase/STACK.md", item["files"])
        self.assertEqual(len(item["files"]), len(set(item["files"])))
        self.assertIn("born from the last commit", item["fix"])

    def test_check_items_puts_the_uncommitted_item_first_with_a_level(self):
        project = self.make_planning()
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        items = run_loop.check_items([], ["Node project without node_modules"], self.repos_of(project), project.repo)
        self.assertEqual([item["kind"] for item in items], ["uncommitted", "node-modules"])
        self.assertEqual(items[0]["level"], "warning")

    def test_check_json_prints_the_same_text_lines_plus_one_json_line_with_items(self):
        project = self.make_planning()
        write(os.path.join(project.repo, "docs", "codebase", "STACK.md"), "map\n")
        plain = project.run("--check", "--planning")
        with_json = project.run("--check", "--planning", "--json")
        text_lines = lambda result: [line for line in result.stdout.splitlines() if line.startswith("[check]") and not line.startswith(JSON_PREFIX)]
        self.assertEqual(text_lines(with_json), text_lines(plain))
        payload_lines = [line for line in with_json.stdout.splitlines() if line.startswith(JSON_PREFIX)]
        self.assertEqual(len(payload_lines), 1)
        payload = json.loads(payload_lines[0][len(JSON_PREFIX):])
        self.assertEqual(set(payload), {"problems", "warnings", "items"} | (set(payload) - {"problems", "warnings", "items"}))
        self.assertTrue(any(item["kind"] == "uncommitted" and item["fix"] for item in payload["items"]), payload["items"])
        warning_count = len([item for item in payload["items"] if item["level"] == "warning"])
        self.assertGreaterEqual(warning_count, 1)


if __name__ == "__main__":
    unittest.main()
