"""T2 (WAVE-11): the `Max parallel tasks` loop setting of the ROADMAP header (1..5, default 3) and its place in the template."""
import os
import unittest

from harness_backend.domain.roadmap_settings import LOOP_SETTING_DEFAULTS, parse_loop_settings

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "specs", "ROADMAP.md")


class MaxParallelTasksTests(unittest.TestCase):
    def test_four_is_read(self):
        result = parse_loop_settings("**Max parallel tasks:** 4\n")
        self.assertEqual(result["settings"]["max_parallel_tasks"], 4)
        self.assertNotIn("max_parallel_tasks", result["missing"])
        self.assertEqual(result["problems"], [])

    def test_no_line_gives_three_and_the_key_is_missing(self):
        result = parse_loop_settings("**Dispatch:** 2\n")
        self.assertEqual(result["settings"]["max_parallel_tasks"], 3)
        self.assertIn("max_parallel_tasks", result["missing"])

    def test_nine_gives_three_and_a_problem_naming_the_range(self):
        result = parse_loop_settings("**Max parallel tasks:** 9\n")
        self.assertEqual(result["settings"]["max_parallel_tasks"], 3)
        self.assertEqual([item["name"] for item in result["problems"]], ["Max parallel tasks"])
        self.assertIn("1 to 5", str(result["problems"][0]))
        self.assertNotIn("max_parallel_tasks", result["missing"])

    def test_zero_and_text_are_problems_and_the_edges_are_accepted(self):
        for raw in ("0", "six", "-1"):
            self.assertEqual(len(parse_loop_settings("**Max parallel tasks:** %s\n" % raw)["problems"]), 1, raw)
        for raw, expected in (("1", 1), ("5", 5), ("2 (small repo)", 2)):
            self.assertEqual(parse_loop_settings("**Max parallel tasks:** %s\n" % raw)["settings"]["max_parallel_tasks"], expected)

    def test_the_default_and_the_template_agree(self):
        self.assertEqual(LOOP_SETTING_DEFAULTS["max_parallel_tasks"], 3)
        with open(TEMPLATE, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("Max parallel tasks", text, "the template documents the line inside its comment block")
        self.assertEqual(parse_loop_settings(text)["settings"]["max_parallel_tasks"], 3)


if __name__ == "__main__":
    unittest.main()
