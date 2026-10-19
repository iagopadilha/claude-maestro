"""`How to run` block of CLAUDE.md (engine/run_hints.py): .NET runs with the https launch profile, front ends with their dev
script, the block is rewritten in place and a hand-written `dotnet run` without the profile is reported. Temporary folders only."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_hints  # noqa: E402


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TestRunHints(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="runhints-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def add_dotnet(self, profiles):
        write(os.path.join(self.root, "BackX.Api", "BackX.Api.csproj"), "<Project />")
        write(os.path.join(self.root, "BackX.Api", "Properties", "launchSettings.json"), json.dumps({"profiles": profiles}))

    def test_dotnet_uses_the_https_profile_and_shows_the_urls(self):
        self.add_dotnet({"http": {"applicationUrl": "http://localhost:5000"},
                         "https": {"applicationUrl": "https://localhost:7000;http://localhost:5000"}})
        block = run_hints.build_block(self.root)
        self.assertIn("dotnet run --project BackX.Api --launch-profile https", block)
        self.assertIn("https://localhost:7000", block)

    def test_dotnet_without_https_profile_is_flagged_not_hidden(self):
        self.add_dotnet({"http": {"applicationUrl": "http://localhost:5000"}})
        block = run_hints.build_block(self.root)
        self.assertIn("no `https` profile", block)
        self.assertNotIn("--launch-profile", block)

    def test_front_end_reads_script_port_and_https_from_vite(self):
        write(os.path.join(self.root, "package.json"), json.dumps({"scripts": {"dev": "vite"}}))
        write(os.path.join(self.root, "vite.config.ts"), "export default { server: { port: 8000 } }")
        block = run_hints.build_block(self.root)
        self.assertIn("`npm run dev`, port 8000", block)

    def test_nothing_to_run_writes_nothing(self):
        self.assertIsNone(run_hints.build_block(self.root))
        self.assertEqual(run_hints.write_block(self.root), (False, []))
        self.assertFalse(os.path.exists(os.path.join(self.root, "CLAUDE.md")))

    def test_write_is_idempotent_and_keeps_the_rest_of_the_file(self):
        self.add_dotnet({"https": {"applicationUrl": "https://localhost:7000"}})
        path = os.path.join(self.root, "CLAUDE.md")
        write(path, "# Mine\n\nHand written part.\n")
        self.assertEqual(run_hints.write_block(self.root)[0], True)
        self.assertEqual(run_hints.write_block(self.root)[0], False)
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# Mine\n\nHand written part.\n"))
        self.assertEqual(text.count(run_hints.BEGIN), 1)

    def test_rewrites_only_inside_the_markers_when_the_project_changes(self):
        self.add_dotnet({"https": {"applicationUrl": "https://localhost:7000"}})
        run_hints.write_block(self.root)
        self.add_dotnet({"https": {"applicationUrl": "https://localhost:7777"}})
        self.assertTrue(run_hints.write_block(self.root)[0])
        with open(os.path.join(self.root, "CLAUDE.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("7777", text)
        self.assertNotIn("7000", text)

    def test_reports_a_dotnet_run_line_without_the_https_profile(self):
        self.add_dotnet({"https": {"applicationUrl": "https://localhost:7000"}})
        write(os.path.join(self.root, "CLAUDE.md"), "# X\n\n```bash\ndotnet run --project BackX.Api  # run locally\n```\n")
        _, conflicts = run_hints.write_block(self.root)
        self.assertEqual(conflicts, ["dotnet run --project BackX.Api  # run locally"])
        _, conflicts_again = run_hints.write_block(self.root)
        self.assertEqual(len(conflicts_again), 1, "the generated block itself is never reported")


class TestCodebaseBlock(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="codebaseblock-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_no_documents_writes_nothing(self):
        self.assertIsNone(run_hints.build_codebase_block(self.root))
        self.assertFalse(run_hints.write_codebase_block(self.root))
        self.assertFalse(os.path.exists(os.path.join(self.root, "CLAUDE.md")))

    def test_lists_the_documents_with_their_purpose_and_is_idempotent(self):
        for name in ("STACK.md", "CONCERNS.md", "EXTRA.md", "README.md"):
            write(os.path.join(self.root, "docs", "codebase", name), "# x\n")
        write(os.path.join(self.root, "CLAUDE.md"), "# Mine\n\nKeep this.\n")
        self.assertTrue(run_hints.write_codebase_block(self.root))
        self.assertFalse(run_hints.write_codebase_block(self.root))
        with open(os.path.join(self.root, "CLAUDE.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertTrue(text.startswith("# Mine\n\nKeep this.\n"))
        self.assertIn("`docs/codebase/STACK.md`: languages, frameworks", text)
        self.assertIn("`docs/codebase/CONCERNS.md`: tech debt", text)
        self.assertIn("`docs/codebase/EXTRA.md`: mapping document", text)
        self.assertNotIn("docs/codebase/README.md", text, "the app's own index is not a map document")
        self.assertIn("ONLY WHEN the task needs it", text)
        self.assertIn("the code wins", text)
        self.assertIn("update the document in the same change", text)
        self.assertEqual(text.count(run_hints.CODEBASE_BEGIN), 1)

    def test_legacy_map_in_specs_is_still_pointed_at_and_docs_wins_when_both_exist(self):
        write(os.path.join(self.root, ".specs", "codebase", "STACK.md"), "# old\n")
        self.assertIn("`.specs/codebase/STACK.md`", run_hints.build_codebase_block(self.root))
        write(os.path.join(self.root, "docs", "codebase", "STACK.md"), "# new\n")
        block = run_hints.build_codebase_block(self.root)
        self.assertIn("`docs/codebase/STACK.md`", block)
        self.assertNotIn(".specs/codebase", block)

    def test_map_index_is_idempotent_and_carries_the_commit(self):
        self.assertFalse(run_hints.write_map_index(self.root, "abc1234"), "no documents: nothing written")
        write(os.path.join(self.root, "docs", "codebase", "STACK.md"), "# x\n")
        self.assertTrue(run_hints.write_map_index(self.root, "abc1234", "2026-10-19"))
        self.assertFalse(run_hints.write_map_index(self.root, "abc1234", "2026-10-19"))
        folder = os.path.join(self.root, "docs", "codebase")
        self.assertEqual(run_hints.read_mapped_commit(folder), "abc1234")
        self.assertTrue(run_hints.write_map_index(self.root, "def5678", "2026-10-20"))
        self.assertEqual(run_hints.read_mapped_commit(folder), "def5678")
        with open(os.path.join(folder, "README.md"), encoding="utf-8") as handle:
            self.assertEqual(handle.read().count("Mapped at commit"), 1)
        run_hints.write_map_index(self.root, None, "2026-10-21")
        self.assertIsNone(run_hints.read_mapped_commit(folder), "no commit: the marker says unknown")

    def test_run_block_and_codebase_block_do_not_touch_each_other(self):
        write(os.path.join(self.root, "docs", "codebase", "STACK.md"), "# x\n")
        write(os.path.join(self.root, "package.json"), json.dumps({"scripts": {"dev": "vite"}}))
        run_hints.write_block(self.root)
        run_hints.write_codebase_block(self.root)
        run_hints.write_block(self.root)
        with open(os.path.join(self.root, "CLAUDE.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertEqual((text.count(run_hints.BEGIN), text.count(run_hints.CODEBASE_BEGIN)), (1, 1))


if __name__ == "__main__":
    unittest.main()
