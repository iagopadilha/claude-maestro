"""`**E2E:** yes|no` in the ROADMAP header: parser, template default and the planning gate that says the test environment is needed."""
import os
import unittest

from harness_backend.domain.roadmap_settings import parse_roadmap_e2e
from harness_backend.services.planning_gates import describe_test_environment

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "specs", "ROADMAP.md")


class RoadmapE2eParserTests(unittest.TestCase):
    def test_accepted_spellings(self):
        for text in ("**E2E:** yes", "**E2E**: yes", "E2E: yes", "- E2E: Yes", "  **e2e:** TRUE  ", "x\n\n**E2E:** yes\n- [a] status=pending"):
            self.assertIs(parse_roadmap_e2e(text), True, text)
        for text in ("**E2E:** no", "E2E: false", "**E2E:**   no (default)"):
            self.assertIs(parse_roadmap_e2e(text), False, text)

    def test_absent_or_unrelated_is_none(self):
        for text in ("", None, "**Goal:** E2E: yes later", "- [a] status=pending e2e=ok", "**E2E:** maybe", "NOE2E: yes"):
            self.assertIsNone(parse_roadmap_e2e(text), text)

    def test_a_line_inside_an_html_comment_is_ignored(self):
        self.assertIsNone(parse_roadmap_e2e("<!--\n**E2E:** yes\n-->\n"))
        self.assertIs(parse_roadmap_e2e("<!--\n**E2E:** yes\n-->\n**E2E:** no\n"), False)

    def test_the_template_defaults_to_no(self):
        with open(TEMPLATE, encoding="utf-8") as handle:
            self.assertIs(parse_roadmap_e2e(handle.read()), False)

    def test_gate_text_mentions_e2e(self):
        self.assertIn("E2E", describe_test_environment(["x"], True))
        self.assertIn("E2E", describe_test_environment(["x"], False))


if __name__ == "__main__":
    unittest.main()
