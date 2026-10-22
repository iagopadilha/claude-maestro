"""T9: `validate_round_stage` and `describe_stage_change` (domain/follow_up_stage.py), pure text rules."""
import unittest

from harness_backend.domain.follow_up_stage import describe_stage_change, list_follow_up_stages, validate_round_stage

BASE = """# Roadmap

## Stage 1 - Base

- [base] status=verified tries=0 scope=Small kind=back paths=src/base title="Base"
"""
STAGE1 = """
## Stage Follow-up-1 - Follow-up 1

- [extra-one] status=pending scope=Small kind=back paths=src/one deps=base title="Extra one"
"""
FLOWS = "# Flows\n\n- base: the base flow\n"


def feature(slug, extra=""):
    return '- [%s] status=pending scope=Small kind=back paths=src/%s deps=base title="T %s"%s\n' % (slug, slug, slug, extra)


class ValidateRoundStageTests(unittest.TestCase):
    def check(self, after, flows_after=FLOWS, baseline=BASE, flows_before=FLOWS, number=1, executed=("base",)):
        return validate_round_stage(baseline, after, flows_before, flows_after, number, list(executed))

    def test_first_round_with_the_stage_appended_is_valid(self):
        self.assertIsNone(self.check(BASE + STAGE1))

    def test_editing_a_line_above_the_stage_returns_a_reason(self):
        reason = self.check((BASE + STAGE1).replace("status=verified", "status=pending"))
        self.assertIn("above the Follow-up 1 stage", reason)

    def test_editing_inside_the_current_stage_is_valid(self):
        baseline = BASE + STAGE1
        after = baseline.replace('title="Extra one"', 'title="Extra one, reworded"') + feature("extra-two")
        self.assertIsNone(self.check(after, baseline=baseline))

    def test_removing_the_current_stage_is_reported_missing(self):
        baseline = BASE + STAGE1
        self.assertIn("is missing", self.check(BASE, baseline=baseline))

    def test_a_duplicated_stage_is_refused(self):
        reason = self.check(BASE + STAGE1 + STAGE1.replace("extra-one", "extra-dup"))
        self.assertIn("more than once", reason)

    def test_the_stage_must_be_the_last_one(self):
        after = BASE + STAGE1 + "\n## Stage 9 - Later\n\n" + feature("later")
        self.assertIn("must be the last stage", self.check(after))

    def test_a_slug_equal_to_an_executed_slug_names_it(self):
        # `base` of the executed stage re-declared as a feature of the round stage
        after = BASE + STAGE1 + feature("base")
        reason = self.check(after)
        self.assertIn("`base`", reason)
        self.assertIn("collides", reason)

    def test_flows_must_only_be_appended(self):
        self.assertIsNone(self.check(BASE + STAGE1, flows_after=FLOWS + "- extra-one: flow\n"))
        self.assertIn("FLOWS.md", self.check(BASE + STAGE1, flows_after="# Flows\n\n- base: CHANGED\n"))

    def test_second_round_freezes_stage_one(self):
        baseline = BASE + STAGE1
        stage2 = "\n## Stage Follow-up-2 - Follow-up 2\n\n" + feature("round-two")
        self.assertIsNone(self.check(baseline + stage2, baseline=baseline, number=2, executed=("base", "extra-one")))
        edited = (baseline + stage2).replace("Extra one", "Changed")
        self.assertIn("above the Follow-up 2 stage", self.check(edited, baseline=baseline, number=2, executed=("base", "extra-one")))

    def test_crlf_text_is_treated_like_lf(self):
        self.assertIsNone(self.check((BASE + STAGE1).replace("\n", "\r\n"), baseline=BASE.replace("\n", "\r\n")))


class DescribeStageChangeTests(unittest.TestCase):
    def stage(self, *lines):
        return list_follow_up_stages(BASE + "\n## Stage Follow-up-1 - Follow-up 1\n\n" + "".join(lines))[0]

    def test_added_removed_and_edited_are_listed(self):
        before = self.stage(feature("keep"), feature("drop"), feature("tweak"))
        after = self.stage(feature("keep"), feature("tweak", ' tries=1'), feature("fresh"))
        self.assertEqual(describe_stage_change(before, after), {"added": ["fresh"], "removed": ["drop"], "edited": ["tweak"]})

    def test_no_change_and_a_missing_stage(self):
        stage = self.stage(feature("a"))
        self.assertEqual(describe_stage_change(stage, stage), {"added": [], "removed": [], "edited": []})
        self.assertEqual(describe_stage_change(None, stage), {"added": ["a"], "removed": [], "edited": []})
        self.assertEqual(describe_stage_change(stage, None), {"added": [], "removed": ["a"], "edited": []})
        self.assertEqual(describe_stage_change(None, None), {"added": [], "removed": [], "edited": []})

    def test_without_the_lines_key_a_changed_field_is_still_edited(self):
        before = {"features": [{"slug": "a", "title": "A", "status": "pending"}]}
        after = {"features": [{"slug": "a", "title": "A2", "status": "pending"}]}
        self.assertEqual(describe_stage_change(before, after)["edited"], ["a"])


if __name__ == "__main__":
    unittest.main()
