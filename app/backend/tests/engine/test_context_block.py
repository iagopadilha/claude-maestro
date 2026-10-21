"""`maestro:context` block of CLAUDE.md (engine/context_block.py): text outside the markers byte-identical (CRLF too), idempotent,
append, create with a title, damaged markers refused, no forbidden word and no `Conformance:` line inside. Temporary folders only."""
import os
import re
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import context_block  # noqa: E402

FACTS = {"conformance": "STANDARD", "map_documents": ["STACK.md", "TESTING.md"], "system_pointer": "../docs/codebase/SYSTEM.md",
         "commands": {"build": "npm run build", "run": "npm run dev", "test": "npm test"}, "giant_lines": 1500}
FORBIDDEN_WORDS = ("commit", "push", "pull request", "guard", "harness.py", "state file", "approval")


def read_bytes(path):
    with open(path, "rb") as handle:
        return handle.read()


class TestContextBlock(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="ctxblock-")
        self.addCleanup(shutil.rmtree, self.folder, True)
        self.path = os.path.join(self.folder, "CLAUDE.md")

    def put(self, data):
        with open(self.path, "wb") as handle:
            handle.write(data)

    def test_append_keeps_team_text_byte_identical(self):
        original = b"# Team\n\nKeep hooks small.\n"
        self.put(original)
        self.assertEqual(context_block.write(self.folder, FACTS), {"written": True, "error": ""})
        data = read_bytes(self.path)
        self.assertTrue(data.startswith(original))
        self.assertEqual(data.count(b"<!-- maestro:context -->"), 1)
        self.assertEqual(data.count(b"<!-- /maestro:context -->"), 1)
        self.assertNotIn(b"\r", data)

    def test_crlf_file_gets_crlf_block_and_text_outside_is_identical(self):
        before, after = b"# Team\r\n\r\nNotes\r\n", b"\r\n## Later\r\nMore\r\n"
        self.put(before)
        context_block.write(self.folder, FACTS)
        first = read_bytes(self.path)
        self.assertTrue(first.startswith(before))
        self.assertEqual(first.count(b"\n"), first.count(b"\r\n"))
        self.put(first.rstrip(b"\r\n") + after)  # text after the block
        context_block.write(self.folder, dict(FACTS, conformance="LEGACY"))
        second = read_bytes(self.path)
        self.assertTrue(second.startswith(before))
        self.assertTrue(second.endswith(after))
        self.assertIn(b"Conformance is LEGACY", second)
        self.assertEqual(second.count(b"\n"), second.count(b"\r\n"))

    def test_unchanged_block_does_not_touch_the_file(self):
        self.put(b"# Team\n")
        context_block.write(self.folder, FACTS)
        os.utime(self.path, (1_000_000_000, 1_000_000_000))
        result = context_block.write(self.folder, FACTS)
        self.assertEqual(result, {"written": False, "error": ""})
        self.assertEqual(os.stat(self.path).st_mtime, 1_000_000_000)

    def test_missing_file_is_created_with_title(self):
        result = context_block.write(self.folder, FACTS)
        self.assertTrue(result["written"])
        text = read_bytes(self.path).decode("utf-8")
        self.assertTrue(text.startswith("# %s\n\n<!-- maestro:context -->" % os.path.basename(self.folder)))
        os.remove(self.path)
        context_block.write(self.folder, FACTS, title="Catalogo")
        self.assertTrue(read_bytes(self.path).decode("utf-8").startswith("# Catalogo\n"))

    def test_two_blocks_or_unmatched_marker_refused(self):
        block = context_block.render(self.folder, FACTS)
        for text in (block + "\n" + block + "\n", "# T\n<!-- maestro:context -->\nhalf\n", "# T\n<!-- /maestro:context -->\n",
                     "<!-- /maestro:context -->\nx\n<!-- maestro:context -->\n"):
            self.put(text.encode("utf-8"))
            self.assertEqual(context_block.write(self.folder, FACTS), {"written": False, "error": "context block damaged"})
            self.assertEqual(read_bytes(self.path), text.encode("utf-8"))

    def test_block_has_no_forbidden_word_and_no_conformance_line(self):
        for facts in (FACTS, {}, dict(FACTS, conformance=None, commands={}, system_pointer="")):
            block = context_block.render(self.folder, facts)
            lowered = block.lower()
            for word in FORBIDDEN_WORDS:
                self.assertNotIn(word, lowered)
            self.assertIsNone(re.search(r"^(Conformance|Conformidade):", block, re.M))
        self.assertIn("This project's Conformance is STANDARD; the rules are in the Conformance block of this file.",
                      context_block.render(self.folder, FACTS))

    def test_unknown_commands_text_and_giant_rule(self):
        block = context_block.render(self.folder, {"conformance": "LEGACY", "map_documents": [], "giant_lines": 900})
        self.assertIn("Build/test command not known: see `docs/codebase/TESTING.md` when it exists.", block)
        self.assertIn("Files above 900 lines", block)
        self.assertIn("never make a file cross 900 lines: split it instead", block)
        with_testing = context_block.render(self.folder, {"map_documents": ["TESTING.md"]})
        self.assertIn("Build/test command not known: see `docs/codebase/TESTING.md`.", with_testing)
        self.assertIn("Conformance is not set yet", with_testing)

    def test_pointer_and_commands_rendered(self):
        block = context_block.render(self.folder, FACTS)
        self.assertIn("`../docs/codebase/SYSTEM.md`", block)
        self.assertIn("Test: `npm test`", block)
        self.assertIn("Files above 1500 lines", block)


if __name__ == "__main__":
    unittest.main()
