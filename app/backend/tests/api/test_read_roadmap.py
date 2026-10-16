"""Dependency clarity, feature summary, wave and stage."""
import os
import unittest

from .support_read import ReadTestCase

CHAIN_ROADMAP = """# Chain

## Stage 1 - Chain

- [a] status=verified scope=Small kind=back paths=src/a
- [b] status=building scope=Small kind=back paths=src/b deps=a,dir:src/common
- [c] status=pending scope=Small kind=back paths=src/c deps=b
- [d] status=blocked tries=3 scope=Small kind=back paths=src/d deps=a
- [e] status=pending scope=Small kind=back paths=src/e deps=d,b
"""
FAKE_SPEC = """# slug-a: feature title

## Metadata

- **SCOPE:** Medium
- **KIND:** back

## Problem

The service mixes listing and creation in a single method.
That hurts readability. token=abc123secretvalue

Second paragraph that is left out.

## Requirements

| ID | Requirement |
|----|-------------|
| CT-01 | WHEN something THEN something |
| CT-02 | WHEN other THEN other |
| CT-02 | a repeat does not count twice |
| CT-10a | exception of CT-10 |
"""
WAVES_ROADMAP = """# Roadmap

## Stage 1 - Base

Goal: prepare the ground.
Criterion: the build passes.

- [base] status=verified scope=Small kind=back paths=src/base

## Stage 2 - Services

Goal: split into services.

- [slug-a] status=verified scope=Medium kind=back paths=src/a deps=base
- [slug-b] status=blocked tries=2 scope=Small kind=back paths=src/b deps=base
- [slug-c] status=building scope=Large kind=back paths=src/a/sub deps=base
- [slug-d] status=pending kind=back paths=src/d deps=slug-c
- [slug-e] status=pending scope=Bogus paths=src/e
"""
FAKE_STAGE_CLOSING = """# Stage 1 - Base

## Delivered
- base ready. password=hunter2

## User decisions
- none

## Lessons
""" + "- long lesson " * 200 + """

## Blocked
- None.
"""


class FeatureClarityTests(ReadTestCase):
    INITIAL_ROADMAP = CHAIN_ROADMAP

    def features(self):
        self.fixture.start()
        return {f["id"]: f for f in self.state()["features"]}

    def test_chain_a_b_c_with_dir_waiting_on_and_unblocks(self):
        f = self.features()
        self.assertEqual(f["a"]["depends_on"], [])
        self.assertEqual((f["a"]["unblocks"], f["a"]["waiting_on"], f["a"]["blocked_by"]), (["b", "d"], [], []))
        self.assertEqual(f["b"]["depends_on"], [{"id": "a", "status": "verified"}])
        self.assertEqual((f["b"]["dep_dirs"], f["b"]["unblocks"], f["b"]["waiting_on"]), (["src/common"], ["c", "e"], []))
        self.assertEqual(f["b"]["deps"], ["a", "dir:src/common"])
        self.assertEqual(f["c"]["depends_on"], [{"id": "b", "status": "building"}])
        self.assertEqual((f["c"]["waiting_on"], f["c"]["unblocks"], f["c"]["dep_dirs"], f["c"]["blocked_by"]), (["b"], [], [], []))

    def test_blocked_feature_blocks_the_dependent(self):
        f = self.features()
        self.assertEqual(f["d"]["waiting_on"], [])
        self.assertEqual(f["e"]["depends_on"], [{"id": "d", "status": "blocked"}, {"id": "b", "status": "building"}])
        self.assertEqual((f["e"]["waiting_on"], f["e"]["blocked_by"]), (["d", "b"], ["d"]))

    def test_turn_snapshot_also_brings_the_fields(self):
        self.fixture.write_json("turns/001.json", {"total_cost_usd": 0.1})
        self.fixture.snapshot(1, roadmap=CHAIN_ROADMAP)
        self.fixture.start()
        status, data = self.fixture.call("/api/turn/1")
        self.assertEqual(status, 200)
        e = {f["id"]: f for f in data["features"]}["e"]
        self.assertEqual((e["blocked_by"], e["waiting_on"]), (["d"], ["d", "b"]))

    def test_feature_name_comes_from_the_spec_title_and_commit_from_state(self):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", "a", "spec.md"), "# Spec: a - Customer signup\n")
        self.fixture.write(os.path.join(self.fixture.project_dir, "STATE.md"), "- a verified in commit 1c02e9abc\n")
        f = self.features()
        self.assertEqual((f["a"]["name"], f["a"]["commit"]), ("Customer signup", "1c02e9a"))
        self.assertEqual((f["b"]["name"], f["b"]["commit"]), ("b", None))


class WaveAndStageSummaryTests(ReadTestCase):
    INITIAL_ROADMAP = WAVES_ROADMAP

    def setUp(self):
        super().setUp()
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", "slug-a", "spec.md"), FAKE_SPEC)
        for number, feature in ((1, "base"), (2, "slug-a,slug-b"), (3, "slug-a,slug-b"), (4, "slug-c")):
            self.fixture.snapshot(number, roadmap=WAVES_ROADMAP, loop={"status": "continue", "feature": feature})
        self.fixture.write(os.path.join(self.fixture.project_dir, "phases", "stage-01.md"), FAKE_STAGE_CLOSING)

    def read_view(self):
        self.fixture.start()
        data = self.state()
        return {f["id"]: f for f in data["features"]}, {s["n"]: s for s in data["stages"]}

    def test_feature_summary_comes_from_the_spec_with_size_requirements_and_redacted_credential(self):
        features, _ = self.read_view()
        summary = features["slug-a"]["summary"]
        self.assertEqual(summary["source"], "spec.md")
        self.assertEqual((summary["size"], summary["requirements"]), ("Medium", 3))
        self.assertTrue(summary["goal"].startswith("The service mixes listing and creation in a single method. That hurts readability."))
        self.assertNotIn("abc123secretvalue", summary["goal"])
        self.assertNotIn("Second paragraph", summary["goal"])

    def test_summary_without_spec_falls_back_to_the_roadmap_or_none_without_inventing(self):
        features, _ = self.read_view()
        self.assertEqual(features["slug-c"]["summary"], {"goal": None, "size": "Large", "requirements": None, "source": "roadmap"})
        self.assertEqual(features["slug-e"]["summary"], {"goal": None, "size": None, "requirements": None, "source": "none"})

    def test_goal_is_clipped_at_600_characters(self):
        self.fixture.write(os.path.join(self.fixture.work, ".specs", "features", "slug-a", "spec.md"), "# a\n\n## Goal\n\n" + "word " * 200)
        features, _ = self.read_view()
        self.assertEqual(len(features["slug-a"]["summary"]["goal"]), 600)

    def test_wave_together_with_reason_and_upcoming(self):
        features, _ = self.read_view()
        self.assertEqual(features["slug-a"]["wave"], {"turn": 3, "together_with": ["slug-b"], "reason": "disjoint paths, no dependency between them",
                                                      "upcoming": False})
        self.assertEqual(features["slug-b"]["wave"]["together_with"], ["slug-a"])
        self.assertEqual(features["slug-c"]["wave"], {"turn": 4, "together_with": [], "reason": "alone in the turn", "upcoming": False})
        self.assertEqual(features["slug-d"]["wave"], {"turn": None, "together_with": [], "reason": "waiting on: slug-c", "upcoming": True})
        self.assertEqual(features["slug-e"]["wave"], {"turn": None, "together_with": [], "reason": "no open dependency", "upcoming": True})
        self.assertEqual(features["base"]["wave"]["turn"], 1)

    def test_wave_together_but_with_overlapping_paths_does_not_claim_disjoint(self):
        self.fixture.snapshot(5, loop={"feature": ["slug-a", "slug-c"]})
        features, _ = self.read_view()
        self.assertEqual(features["slug-a"]["wave"]["reason"], "dispatched together in the same turn")

    def test_stage_description_progress_current_feature_waves_and_closing(self):
        _, stages = self.read_view()
        one, two = stages[1]["summary"], stages[2]["summary"]
        self.assertEqual(stages[1]["title"], "Stage 1 - Base")
        self.assertEqual(one["description"], "Goal: prepare the ground. Criterion: the build passes.")
        self.assertEqual((one["progress"], one["current_feature"]), ({"verified": 1, "blocked": 0, "total": 1}, None))
        self.assertEqual(one["waves"], [{"turn": 1, "features": ["base"]}])
        self.assertEqual(two["progress"], {"verified": 1, "blocked": 1, "total": 5})
        self.assertEqual(two["current_feature"], "slug-c")
        self.assertEqual(two["waves"], [{"turn": 2, "features": ["slug-a", "slug-b"]}, {"turn": 3, "features": ["slug-a", "slug-b"]},
                                        {"turn": 4, "features": ["slug-c"]}])
        self.assertIsNone(two["closing"])
        closing = one["closing"]
        self.assertEqual(set(closing), {"delivered", "decisions", "lessons", "unverified", "blocked", "next_stage"})
        self.assertEqual((closing["decisions"], closing["blocked"], closing["unverified"], closing["next_stage"]),
                         ("- none", "- None.", None, None))
        self.assertNotIn("hunter2", closing["delivered"])
        self.assertEqual(len(closing["lessons"]), 1200)

    def test_turn_snapshot_brings_summary_and_wave_up_to_that_turn(self):
        self.fixture.write_json("turns/002.json", {"total_cost_usd": 0.1})
        self.fixture.start()
        status, data = self.fixture.call("/api/turn/2")
        self.assertEqual(status, 200)
        features = {f["id"]: f for f in data["features"]}
        self.assertEqual(features["slug-a"]["wave"]["turn"], 2)
        self.assertEqual(features["slug-a"]["summary"]["source"], "spec.md")
        self.assertTrue(features["slug-c"]["wave"]["upcoming"], "slug-c only enters on turn 4: up to turn 2 it is still upcoming")

    def test_turns_list_the_feature_from_the_snapshot(self):
        for number in (1, 2):
            self.fixture.write_json("turns/%03d.json" % number, {"total_cost_usd": 0.1, "duration_ms": 1500})
        self.fixture.start()
        turns = self.state()["turns"]
        self.assertEqual([(t["n"], t["feature"], t["status"], t["duration_s"]) for t in turns],
                         [(1, ["base"], "continue", 2), (2, ["slug-a", "slug-b"], "continue", 2)])
        self.assertTrue(all(t["commits"] is None for t in turns))  # snapshot without head: commits are not counted


if __name__ == "__main__":
    unittest.main()
