"""One generated block about the map per CLAUDE.md: a file with the `maestro:context` block never keeps the `CODEBASE` block (it lists the same
documents); a single repo root without the context block keeps `CODEBASE`; a confirmed monorepo root gets neither. Temporary folders only."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import context_block  # noqa: E402
import run_hints  # noqa: E402
from harness_backend.services.context_block_service import ContextBlockService  # noqa: E402

TEAM_TEXT = "# backend\n\nTeam notes: keep the services small.\n"
CONFORMANCE_BLOCK = "Conformance: STANDARD\nRule: new code follows the standard.\n"
DOCUMENTS = ("STACK.md", "TESTING.md")


def write_file(path, text, newline="\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline=newline) as handle:
        handle.write(text)


def read_file(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


class TestSingleMapBlock(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix="singlemap-")
        self.addCleanup(shutil.rmtree, self.repo, True)
        self.service = ContextBlockService()

    def make_monorepo(self, confirmed=True):
        name = os.path.basename(self.repo)
        entry = {"monorepo": True, "confirmed": confirmed, "projects": [{"path": "backend"}]}
        write_file(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"), json.dumps({"repos": {name: entry}}))
        write_file(os.path.join(self.repo, "CLAUDE.md"), "# root\n\n@.specs/project/RULES.md\n")
        for folder in ("backend",):
            for document in DOCUMENTS:
                write_file(os.path.join(self.repo, folder, "docs", "codebase", document), "# x\n")

    def claude_md(self, relative=""):
        return read_file(os.path.join(self.repo, *relative.split("/"), "CLAUDE.md"))

    def test_subproject_after_map_and_conformance_lists_the_documents_once(self):
        self.make_monorepo()
        write_file(os.path.join(self.repo, "backend", "CLAUDE.md"), TEAM_TEXT)
        self.service.refresh_repo(self.repo, None, subprojects_root=self.repo)  # after the map
        write_file(os.path.join(self.repo, "backend", "CLAUDE.md"), self.claude_md("backend") + "\n" + CONFORMANCE_BLOCK)
        self.service.refresh_folder(self.repo, None, "backend", subprojects_root=self.repo)  # after the Conformance
        text = self.claude_md("backend")
        self.assertEqual(text.count("`docs/codebase/STACK.md`"), 1)
        self.assertEqual(text.count("- `docs/codebase/TESTING.md`"), 1)
        self.assertEqual(text.count("the code wins"), 1)
        self.assertEqual(text.count(context_block.START_MARKER), 1)
        self.assertNotIn(run_hints.CODEBASE_BEGIN, text)
        self.assertIn("Conformance is STANDARD;", text)
        self.assertTrue(text.startswith(TEAM_TEXT))
        # a re-map after that never brings the duplicate back
        run_hints.write_codebase_block(os.path.join(self.repo, "backend"))
        self.assertEqual(self.claude_md("backend"), text)

    def test_file_with_both_blocks_loses_codebase_and_keeps_the_team_text(self):
        self.make_monorepo()
        folder = os.path.join(self.repo, "backend")
        team = TEAM_TEXT + "\nMore team text.\n"
        write_file(os.path.join(folder, "CLAUDE.md"), team)
        self.service.refresh_folder(self.repo, None, "backend", subprojects_root=self.repo)
        context_only = self.claude_md("backend")
        write_file(os.path.join(folder, "CLAUDE.md"),
                   context_only + "\n" + run_hints.build_codebase_block(folder) + "\n" + CONFORMANCE_BLOCK)
        self.assertIn(run_hints.CODEBASE_BEGIN, self.claude_md("backend"))
        self.service.refresh_folder(self.repo, None, "backend", subprojects_root=self.repo)
        text = self.claude_md("backend")
        self.assertNotIn(run_hints.CODEBASE_BEGIN, text)
        self.assertNotIn(run_hints.CODEBASE_END, text)
        self.assertTrue(text.startswith(team), "the team text is untouched")
        sentence = context_block.render_conformance_sentence
        expected = (context_only + "\n" + CONFORMANCE_BLOCK).replace(sentence(None), sentence("STANDARD"))  # the real state is read
        self.assertEqual(text, expected, "only the CODEBASE block went away (and the Conformance sentence follows the file)")
        self.assertEqual(text.count("`docs/codebase/STACK.md`"), 1)

    def test_write_codebase_block_removes_instead_of_writing_when_the_context_block_is_there(self):
        folder = os.path.join(self.repo, "single")
        for document in DOCUMENTS:
            write_file(os.path.join(folder, "docs", "codebase", document), "# x\n")
        write_file(os.path.join(folder, "CLAUDE.md"), "# single\r\n\r\nTeam.\r\n")
        context_block.write(folder, {"map_documents": list(DOCUMENTS)})
        with_context = read_file(os.path.join(folder, "CLAUDE.md"))
        both = with_context + "\r\n" + run_hints.build_codebase_block(folder).replace("\n", "\r\n")
        write_file(os.path.join(folder, "CLAUDE.md"), both, newline="")
        self.assertTrue(run_hints.write_codebase_block(folder))
        self.assertEqual(read_file(os.path.join(folder, "CLAUDE.md")), with_context, "CRLF file: back to the context block alone")
        self.assertFalse(run_hints.write_codebase_block(folder))

    def test_single_repo_root_without_context_block_keeps_codebase(self):
        for document in DOCUMENTS:
            write_file(os.path.join(self.repo, "docs", "codebase", document), "# x\n")
        write_file(os.path.join(self.repo, "CLAUDE.md"), "# single\n\nTeam.\n")
        self.assertTrue(run_hints.write_codebase_block(self.repo))
        text = self.claude_md()
        self.assertEqual(text.count(run_hints.CODEBASE_BEGIN), 1)
        self.assertNotIn(context_block.START_MARKER, text)
        self.assertFalse(run_hints.write_codebase_block(self.repo), "idempotent")

    def test_monorepo_root_gets_no_generated_block(self):
        self.make_monorepo()
        before = self.claude_md()
        write_file(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md"), "# System\n")
        self.assertFalse(run_hints.write_codebase_block(self.repo))
        self.assertFalse(self.service.refresh_folder(self.repo, None, "", subprojects_root=self.repo))
        self.assertEqual(self.claude_md(), before)

    def test_context_block_with_map_has_the_guide_sentence_and_without_map_does_not(self):
        with_map = context_block.render(self.repo, {"map_documents": list(DOCUMENTS)})
        self.assertIn("not the source of truth", with_map)
        self.assertIn("update the document in the same change", with_map)
        self.assertNotIn("source of truth", context_block.render(self.repo, {"map_documents": []}))


if __name__ == "__main__":
    unittest.main()
