"""`GET codebase/tree` (A8) and `state.decisions_summary` / `state.validate` of the panel state."""
import json
import os
import unittest
import urllib.parse

from harness_backend.engine import loop_state

from .support_read import ReadTestCase


class CodebaseTreeTests(ReadTestCase):
    def test_empty_when_no_map(self):
        self.fixture.start()
        status, data = self.fixture.call("/api/codebase/tree")
        self.assertEqual((status, data), (200, {"groups": []}))

    def test_root_and_subproject_maps(self):
        work = self.fixture.work
        repo_name = os.path.basename(self.fixture.repo)
        self.fixture.write(os.path.join(work, "docs", "codebase", "STACK.md"), "# stack")
        self.fixture.write(os.path.join(work, "docs", "codebase", "ARCHITECTURE.md"), "# arch")
        self.fixture.write(os.path.join(work, "docs", "codebase", "notes.txt"), "ignored")
        self.fixture.write(os.path.join(work, "back", "docs", "codebase", "STACK.md"), "# back stack")
        self.fixture.write(os.path.join(work, "skipped", "docs", "codebase", "STACK.md"), "# skipped")
        entry = {"monorepo": True, "confirmed": True, "projects": [
            {"path": "back", "name": "back", "skip": False}, {"path": "front", "name": "front", "skip": False},
            {"path": "skipped", "name": "skipped", "skip": True}]}
        self.fixture.write(os.path.join(work, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"repos": {repo_name: entry}}))
        self.fixture.start()
        status, data = self.fixture.call("/api/codebase/tree")
        self.assertEqual(status, 200)
        self.assertEqual([group["folder"] for group in data["groups"]], [".", "back"])
        root, back = data["groups"]
        self.assertEqual(root["files"], [{"path": "docs/codebase/ARCHITECTURE.md", "name": "ARCHITECTURE.md"},
                                         {"path": "docs/codebase/STACK.md", "name": "STACK.md"}])
        self.assertEqual((back["label"], back["files"]), ("back", [{"path": "back/docs/codebase/STACK.md", "name": "STACK.md"}]))

    def test_unknown_project_is_404(self):
        self.fixture.start()
        self.assertEqual(self.fixture.call("/api/codebase/tree", project_id="nope")[0], 404)

    def test_state_keeps_validate_and_adds_decisions_summary(self):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "project", "loop", "VALIDATE.md"),
                           "# Validate\n- [a] turn=1 level=critical q | chose: x | revert: y\n- [b] q2 | chose: z | revert: w\n")
        self.fixture.start()
        state = self.state()
        self.assertEqual(len(state["validate"]), 2)
        self.assertEqual(state["decisions_summary"], {"open_critical": 1, "open_important": 1})


class CodebaseFileTests(ReadTestCase):
    def read(self, path):
        return self.fixture.call("/api/codebase/file?path=" + urllib.parse.quote(path, safe="/"))

    def test_reads_listed_files_and_refuses_the_rest(self):
        work = self.fixture.work
        self.fixture.write(os.path.join(work, "docs", "codebase", "STACK.md"), "# stack")
        self.fixture.write(os.path.join(work, "docs", "other.md"), "no")
        self.fixture.write(os.path.join(work, "docs", "codebase", "notes.txt"), "no")
        self.fixture.write(os.path.join(work, "secret.md"), "no")
        self.fixture.start()
        status, data = self.read("docs/codebase/STACK.md")
        self.assertEqual((status, data["path"], data["text"], data["editable"]), (200, "docs/codebase/STACK.md", "# stack", False))
        self.assertEqual(len(data["version"]), 40)
        for bad in ("../secret.md", "docs/codebase/../other.md", "/etc/passwd", "C:/Windows/win.ini", r"docs\codebase\..\other.md", ""):
            self.assertEqual(self.read(bad)[0], 400, bad)
        for missing in ("docs/other.md", "docs/codebase/notes.txt", "secret.md", "docs/codebase/NOPE.md"):
            self.assertEqual(self.read(missing)[0], 404, missing)
        self.assertEqual(self.fixture.call("/api/codebase/file?path=x", project_id="nope")[0], 404)


class EffectiveCostCapTests(ReadTestCase):
    def cost_limit(self, state):
        return next(row for row in state["limits"] if row["key"] == "cost")

    def test_default_is_50(self):
        self.fixture.start()
        state = self.state()
        self.assertEqual((state["meta"]["max_cost_usd"], self.cost_limit(state)["max"]), (50.0, 50.0))

    def test_loop_command_flag_then_meta_wins(self):
        self.fixture.write(loop_state.loop_path(self.fixture.work, "LOOP-CMD.json"), json.dumps({"argv": ["run_loop.py", "--max-cost", "12"]}))
        self.fixture.start()
        state = self.state()
        self.assertEqual((state["meta"]["max_cost_usd"], self.cost_limit(state)["max"]), (12.0, 12.0))
        self.fixture.write(loop_state.loop_path(self.fixture.work, "meta.json"), json.dumps({"turns": 1, "max_cost_usd": 75}))
        state = self.state()
        self.assertEqual((state["meta"]["max_cost_usd"], self.cost_limit(state)["max"]), (75, 75))


if __name__ == "__main__":
    unittest.main()
