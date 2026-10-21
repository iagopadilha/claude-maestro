"""`parse_loop_settings`: the nine loop settings of the ROADMAP header (defaults, invalid values, missing keys, template)."""
import os
import unittest

from harness_backend.domain.roadmap_settings import LOOP_SETTING_DEFAULTS, parse_loop_settings

TEMPLATE = os.path.join(os.path.dirname(__file__), "..", "..", "..", "templates", "specs", "ROADMAP.md")


class ParseLoopSettingsTests(unittest.TestCase):
    def test_empty_text_gives_the_defaults_and_every_key_missing(self):
        result = parse_loop_settings("")
        self.assertEqual(result["settings"], LOOP_SETTING_DEFAULTS)
        self.assertEqual(result["problems"], [])
        self.assertEqual(sorted(result["missing"]), sorted(LOOP_SETTING_DEFAULTS))
        self.assertEqual((result["settings"]["budget_loop"], result["settings"]["budget_turn"]), (50.0, 5.0))

    def test_all_ten_lines(self):
        text = ("**E2E:** yes\n**Dispatch:** 4\n**Max spend per turn:** US$ 7.5\n**Max spend for the loop:** 120\n**Max minutes per turn:** 90\n"
                "**Branch:** loop/x\n**Branch base:** staging\n**Setup:** npm ci\n**When a feature gets stuck:** continue\n**Push and PR:** no\n")
        result = parse_loop_settings(text)
        self.assertEqual(result["settings"], {"dispatch": 4, "e2e": True, "budget_turn": 7.5, "budget_loop": 120.0, "minutes_turn": 90,
                                              "branch": "loop/x", "base_branch": "staging", "setup": "npm ci", "when_stuck": "continue", "push_pr": False})
        self.assertEqual((result["problems"], result["missing"]), ([], []))

    def test_invalid_values_keep_the_default_and_report_a_problem(self):
        text = "**Dispatch:** 12\n**Max spend per turn:** lots\n**Branch:** a..b\n**When a feature gets stuck:** maybe\n**Push and PR:** perhaps\n"
        result = parse_loop_settings(text)
        self.assertEqual([item["name"] for item in result["problems"]],
                         ["Dispatch", "Max spend per turn", "Branch", "When a feature gets stuck", "Push and PR"])
        self.assertEqual(result["problems"][0]["value"], "12")
        for key in ("dispatch", "budget_turn", "branch", "when_stuck", "push_pr"):
            self.assertEqual(result["settings"][key], LOOP_SETTING_DEFAULTS[key])
            self.assertNotIn(key, result["missing"])

    def test_automatic_branch_empty_setup_and_comments(self):
        result = parse_loop_settings("<!--\n**Dispatch:** 7\n-->\n**Branch:** Automatic\n**Setup:**\n**Dispatch:** auto\n")
        self.assertEqual((result["settings"]["branch"], result["settings"]["setup"], result["settings"]["dispatch"]), ("", "", "auto"))
        self.assertNotIn("setup", result["missing"])
        self.assertEqual(result["problems"], [])

    def test_the_template_yields_the_defaults_without_problems_or_missing_keys(self):
        with open(TEMPLATE, encoding="utf-8") as handle:
            result = parse_loop_settings(handle.read())
        self.assertEqual(result["settings"], LOOP_SETTING_DEFAULTS)
        self.assertEqual((result["problems"], result["missing"]), ([], []))


if __name__ == "__main__":
    unittest.main()
