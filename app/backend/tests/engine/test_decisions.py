#!/usr/bin/env python3
"""engine/decisions.py: tolerant parse (old, new and unparsed lines), append and the critical lines written after a mark."""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401  (engine on sys.path)
import decisions  # noqa: E402

OLD_LINE = "- [api] different button | chose: default | revert: swap"
NEW_LINE = "- [api] turn=3 level=critical drop the column? | chose: kept it | revert: run the migration"


class TestParse(unittest.TestCase):
    def test_old_line_has_no_turn_and_is_important(self):
        items, unparsed = decisions.parse(OLD_LINE + "\n")
        self.assertEqual(unparsed, [])
        self.assertEqual((items[0]["slug"], items[0]["turn"], items[0]["level"]), ("api", None, "important"))
        self.assertEqual((items[0]["doubt"], items[0]["chose"], items[0]["revert"]), ("different button", "default", "swap"))

    def test_new_line_carries_turn_and_level(self):
        items, _ = decisions.parse(NEW_LINE)
        self.assertEqual((items[0]["turn"], items[0]["level"], items[0]["doubt"]), (3, "critical", "drop the column?"))
        self.assertEqual(items[0]["line_no"], 1)

    def test_line_that_does_not_match_is_kept_as_unparsed(self):
        items, unparsed = decisions.parse("# heading\n\n- [api] no separators here\n" + OLD_LINE + "\n")
        self.assertEqual(len(items), 1)
        self.assertEqual(len(unparsed), 1)
        self.assertTrue(unparsed[0]["unparsed"])
        self.assertEqual(unparsed[0]["line_no"], 3)
        self.assertEqual(unparsed[0]["raw"], "- [api] no separators here")

    def test_prose_and_headings_are_not_decisions(self):
        self.assertEqual(decisions.parse("# VALIDATE\nWARNING contract merge (a): x\n"), ([], []))

    def test_line_id_ignores_the_whitespace_at_the_ends(self):
        self.assertEqual(decisions.line_id(OLD_LINE), decisions.line_id("  " + OLD_LINE + "\n"))


class TestAppend(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="decisions-")

    def test_append_creates_the_file_and_the_line_parses_back(self):
        line = decisions.append(self.root, "api", 4, "important", "which \n format | maybe", "json", "switch")
        self.assertTrue(os.path.isfile(decisions.validate_path(self.root)))
        items, unparsed = decisions.parse(decisions.read_file(self.root))
        self.assertEqual(unparsed, [])
        self.assertEqual((items[0]["turn"], items[0]["level"], items[0]["slug"]), (4, "important", "api"))
        self.assertEqual(items[0]["doubt"], "which format / maybe")  # the pipe of the text never breaks the grammar
        self.assertEqual(items[0]["raw"], line)

    def test_append_keeps_the_old_content_and_adds_a_newline_when_missing(self):
        path = decisions.validate_path(self.root)
        os.makedirs(os.path.dirname(path))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(OLD_LINE)  # no trailing newline
        decisions.append(self.root, "ui", None, "bogus", "d", "c", "r")
        items, _ = decisions.parse(decisions.read_file(self.root))
        self.assertEqual([item["slug"] for item in items], ["api", "ui"])
        self.assertEqual((items[1]["turn"], items[1]["level"]), (None, "important"))  # unknown turn, invalid level falls back

    def test_line_count_counts_lines(self):
        self.assertEqual(decisions.line_count(self.root), 0)
        decisions.append(self.root, "a", 1, "important", "d", "c", "r")
        decisions.append(self.root, "a", 1, "important", "d2", "c", "r")
        self.assertEqual(decisions.line_count(self.root), 2)


class TestNewCritical(unittest.TestCase):
    def test_only_critical_lines_after_the_mark(self):
        root = tempfile.mkdtemp(prefix="decisions-")
        decisions.append(root, "a", 1, "critical", "before the mark", "c", "r")
        mark = decisions.line_count(root)
        decisions.append(root, "a", 2, "important", "routine", "c", "r")
        decisions.append(root, "b", 2, "critical", "after the mark", "kept", "undo")
        found = decisions.new_critical(root, mark)
        self.assertEqual([item["doubt"] for item in found], ["after the mark"])
        self.assertEqual(decisions.new_critical(root, decisions.line_count(root)), [])
        self.assertEqual(len(decisions.new_critical(root, 0)), 2)


if __name__ == "__main__":
    unittest.main()
