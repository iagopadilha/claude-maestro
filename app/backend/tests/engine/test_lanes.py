"""Independent lanes: the scheduler (`loop_state.next_work`) gives every feature its own lane at its own step, so nothing waits for a
phase barrier, and `run_loop.plan_flags` writes them in the turn prompt. Also the `title="..."` field of the ROADMAP line. No claude."""
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import loop_state  # noqa: E402
import run_loop  # noqa: E402


def parse(text):
    features, errors = loop_state.parse_roadmap(text)
    assert not errors, errors
    return features


def lanes_of(text, wave=3, headless=True):
    plan = loop_state.next_work(parse(text), wave, headless=headless)
    return [(lane["slug"], lane["step"]) for lane in plan["lanes"]], plan


class TestLanes(unittest.TestCase):
    def test_every_feature_is_a_lane_at_its_own_step(self):
        lanes, plan = lanes_of("## Stage 1\n- [a] status=pending\n- [b] status=specced scope=Medium\n- [c] status=approved paths=p3\n")
        self.assertEqual(lanes, [("c", "execute"), ("b", "gate"), ("a", "spec")])
        self.assertEqual((plan["phase"], plan["slugs"]), ("lanes", ["c", "b", "a"]))

    def test_same_step_keeps_the_old_phase_name(self):
        for status, phase, step in (("pending", "specs", "spec"), ("specced", "gate", "gate"), ("approved", "execute", "execute")):
            with self.subTest(status=status):
                lanes, plan = lanes_of("## Stage 1\n- [a] status=%s paths=p1\n- [b] status=%s paths=p2\n" % (status, status))
                self.assertEqual((plan["phase"], lanes), (phase, [("a", step), ("b", step)]))

    def test_spec_and_gate_lanes_are_capped_by_the_wave_not_by_three(self):
        text = "## Stage 1\n" + "".join("- [f%d] status=pending\n" % n for n in range(6))
        self.assertEqual(len(lanes_of(text, wave=5)[0]), 5)
        self.assertEqual(len(lanes_of(text, wave=2)[0]), 2)
        self.assertEqual(lanes_of(text, wave=2)[1]["deferred_by_wave"], ["f2", "f3", "f4", "f5"])

    def test_most_advanced_lanes_take_the_places_first_when_the_wave_is_small(self):
        lanes, _ = lanes_of("## Stage 1\n- [a] status=pending\n- [b] status=specced\n- [c] status=approved paths=p\n", wave=2)
        self.assertEqual(lanes, [("c", "execute"), ("b", "gate")])

    def test_a_lost_dispatch_does_not_hold_the_other_features(self):
        lanes, plan = lanes_of("## Stage 1\n- [a] status=building paths=p1\n- [b] status=approved paths=p2\n- [c] status=specced\n- [d] status=pending\n")
        self.assertEqual(lanes, [("a", "recover"), ("b", "execute"), ("c", "gate")])
        self.assertEqual(plan["phase"], "lanes")

    def test_execution_lanes_keep_disjoint_paths_and_verified_deps(self):
        lanes, plan = lanes_of("## Stage 1\n- [a] status=approved paths=src\n- [b] status=approved paths=src/api\n"
                               "- [c] status=approved paths=web deps=a\n- [d] status=specced\n")
        self.assertEqual(lanes, [("a", "execute"), ("d", "gate")])
        self.assertEqual((plan["deferred_by_collision"], plan["waiting_deps"]), (["b"], ["c"]))

    def test_a_feature_that_depends_on_another_still_gets_its_spec_and_gate_in_parallel(self):
        lanes, _ = lanes_of("## Stage 1\n- [a] status=building paths=p1\n- [b] status=pending deps=a\n- [c] status=specced deps=a\n", wave=3)
        self.assertEqual(lanes, [("a", "recover"), ("c", "gate"), ("b", "spec")])

    def test_a_quarantined_feature_only_holds_its_dependents(self):
        lanes, plan = lanes_of("## Stage 1\n- [a] status=blocked\n- [b] status=approved paths=p deps=a\n- [c] status=approved paths=q\n- [d] status=pending\n")
        self.assertEqual(lanes, [("c", "execute"), ("d", "spec")])
        self.assertEqual(plan["stuck_by_blocked"], ["b"])

    def test_a_regression_feature_is_the_only_lane_of_its_turn(self):
        text = "## Stage 1\n- [refactor-a] status=approved paths=p1 regression=yes\n- [b] status=approved paths=p2\n- [c] status=pending\n"
        lanes, plan = lanes_of(text)
        self.assertEqual((lanes, plan["phase"]), ([("refactor-a", "execute")], "execute"))
        later, _ = lanes_of(text.replace("[refactor-a] status=approved", "[refactor-a] status=verified"))
        self.assertEqual(later, [("b", "execute"), ("c", "spec")])

    def test_a_worktree_feature_is_alone_in_headless_only(self):
        text = "## Stage 1\n- [a] status=approved paths=p1 isolation=worktree\n- [b] status=pending\n"
        self.assertEqual(lanes_of(text, headless=True)[0], [("a", "execute")])
        self.assertEqual(lanes_of(text, headless=False)[0], [("a", "execute"), ("b", "spec")])

    def test_an_alone_feature_waits_while_a_lane_is_recovering(self):
        lanes, _ = lanes_of("## Stage 1\n- [a] status=speccing\n- [refactor-b] status=approved paths=p regression=yes\n")
        self.assertEqual(lanes, [("a", "recover")])

    def test_only_the_current_stage_moves(self):
        lanes, plan = lanes_of("## Stage 1\n- [a] status=specced\n## Stage 2\n- [b] status=pending\n")
        self.assertEqual((lanes, plan["stage"]), ([("a", "gate")], "1"))

    def test_nothing_to_do_and_final(self):
        self.assertEqual(lanes_of("## Stage 1\n- [a] status=verified\n- [b] status=blocked\n")[1]["phase"], "final")
        lanes, plan = lanes_of("## Stage 1\n- [a] status=approved paths=p deps=b\n- [b] status=blocked\n")
        self.assertEqual((lanes, plan["phase"]), ([], "none"))


class TestPlanFlags(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="lanes-")
        self.addCleanup(shutil.rmtree, self.work, True)

    def write_roadmap(self, text):
        folder = os.path.join(self.work, ".specs", "project")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "ROADMAP.md"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_the_turn_prompt_carries_one_lane_per_feature_with_its_step(self):
        self.write_roadmap("## Stage 1\n- [a] status=pending\n- [b] status=specced\n- [c] status=approved paths=p3\n- [d] status=blocked\n- [e] status=pending deps=d\n")
        flags = run_loop.plan_flags(self.work, 3)
        self.assertEqual(flags, " --phase lanes --lanes c:execute,b:gate,a:spec --ready c,b,a --stuck e")

    def test_a_single_step_keeps_phase_and_ready(self):
        self.write_roadmap("## Stage 1\n- [a] status=approved paths=p1\n- [b] status=approved paths=p2\n")
        self.assertEqual(run_loop.plan_flags(self.work, 3), " --phase execute --lanes a:execute,b:execute --ready a,b")

    def test_without_a_roadmap_nothing_is_added(self):
        self.write_roadmap("no features here\n")
        self.assertEqual(run_loop.plan_flags(self.work, 3), "")


class TestFeatureTitle(unittest.TestCase):
    def test_title_is_read_with_its_spaces_and_does_not_leak_into_other_fields(self):
        features = parse('## Stage 1\n- [customer-registration-api] status=pending title="Register a customer with a tax id" scope=Small kind=back paths=api/customers deps=base\n- [base] status=verified\n')
        feature = features["customer-registration-api"]
        self.assertEqual(feature["title"], "Register a customer with a tax id")
        self.assertEqual((feature["status"], feature["scope"], feature["paths"], feature["deps"]), ("pending", "Small", "api/customers", "base"))

    def test_words_that_look_like_fields_inside_the_title_are_not_parsed(self):
        feature = parse('## Stage 1\n- [a] status=pending title="Fix status=blocked and tries=2 handling" paths=p\n')["a"]
        self.assertEqual((feature["status"], feature["title"], "tries" in feature), ("pending", "Fix status=blocked and tries=2 handling", False))

    def test_the_title_can_come_anywhere_in_the_line_and_is_optional(self):
        self.assertEqual(parse('## Stage 1\n- [a] status=pending paths=p title="Last field"\n')["a"]["title"], "Last field")
        self.assertNotIn("title", parse("## Stage 1\n- [a] status=pending paths=p\n")["a"])
        self.assertNotIn("title", parse('## Stage 1\n- [a] status=pending title=""\n')["a"])

    def test_a_title_without_closing_quote_is_a_grammar_error(self):
        _, errors = loop_state.parse_roadmap('## Stage 1\n- [a] status=pending title="never closed paths=p\n')
        self.assertTrue(any("title=" in error and "[a]" in error for error in errors), errors)

    def test_the_approval_signature_ignores_the_title(self):
        plain = "## Stage 1\n- [a] status=pending\n"
        titled = '## Stage 1\n- [a] status=pending title="Readable name"\n'
        self.assertEqual(loop_state.roadmap_signature(plain), loop_state.roadmap_signature(titled))

    def test_a_titled_line_keeps_the_scheduler_and_the_status_reader_working(self):
        lanes, _ = lanes_of('## Stage 1\n- [a] status=approved title="Ship status=ok" paths=p\n')
        self.assertEqual(lanes, [("a", "execute")])


if __name__ == "__main__":
    unittest.main()
