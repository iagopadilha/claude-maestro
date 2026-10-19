"""Auto dispatch of a new loop: the default `wave` computed from the approved ROADMAP, and its use by /api/loop/check."""
import json
import os
import time
import unittest

from harness_backend.services.dispatch_default import compute_default_dispatch
from tests.api.support_commands import CHECK_READY, FAKE_RUN_LOOP_CHECKABLE, CommandsTestCase


def roadmap(*lines):
    return "# Roadmap\n\n## Stage 1\n" + "\n".join(lines) + "\n"


class ComputeDefaultDispatchTests(unittest.TestCase):
    def test_independent_features_with_disjoint_paths(self):
        result = compute_default_dispatch(roadmap("- [a] status=approved paths=src/a", "- [b] status=approved paths=src/b"))
        self.assertEqual((result["wave"], result["auto"]), (2, True))
        self.assertIn("2 independent features in stage 1", result["reason"])

    def test_dependency_chain_runs_one_at_a_time(self):
        result = compute_default_dispatch(roadmap("- [a] status=approved paths=src/a", "- [b] status=approved paths=src/b deps=a",
                                                  "- [c] status=approved paths=src/c deps=b"))
        self.assertEqual(result["wave"], 1)

    def test_overlapping_paths_do_not_run_together(self):
        result = compute_default_dispatch(roadmap("- [a] status=approved paths=src", "- [b] status=approved paths=src/api"))
        self.assertEqual(result["wave"], 1)
        self.assertIn("share paths", result["reason"])

    def test_cap_is_three_by_default_and_never_above_independent(self):
        many = roadmap(*["- [f%d] status=approved paths=src/f%d" % (i, i) for i in range(6)])
        capped = compute_default_dispatch(many)
        self.assertEqual(capped["wave"], 3)
        self.assertIn("capped at 3", capped["reason"])
        self.assertEqual(compute_default_dispatch(many, cap=20)["wave"], 6)
        self.assertEqual(compute_default_dispatch(many, cap=99)["wave"], 6)  # range 1..8 and independent count
        self.assertEqual(compute_default_dispatch(roadmap("- [a] status=approved paths=x", "- [b] status=approved paths=y"), cap=5)["wave"], 2)

    def test_only_the_first_open_stage_counts(self):
        text = "## Stage 1\n- [a] status=verified paths=a\n- [b] status=approved paths=b\n## Stage 2\n- [c] status=approved paths=c\n- [d] status=approved paths=d\n"
        self.assertEqual(compute_default_dispatch(text)["wave"], 1)

    def test_verified_dependency_is_met(self):
        text = "## Stage 1\n- [a] status=verified paths=a\n- [b] status=approved paths=b deps=a\n- [c] status=approved paths=c deps=a\n"
        self.assertEqual(compute_default_dispatch(text)["wave"], 2)

    def test_unsure_is_one(self):
        for text in ("", "no features here", roadmap("- [a] status=approved paths=a", "- [b] status=approved"),
                     roadmap("- [a] no status here"), "## Stage 1\n- [a] status=verified paths=a\n"):
            self.assertEqual(compute_default_dispatch(text)["wave"], 1, text)

    def test_multi_repo_same_folder_in_the_same_repo_overlaps_but_other_repo_does_not(self):
        same = compute_default_dispatch(roadmap("- [a] status=approved paths=api:src", "- [b] status=approved paths=api:src/x"))
        other = compute_default_dispatch(roadmap("- [a] status=approved paths=api:src", "- [b] status=approved paths=web:src"))
        self.assertEqual((same["wave"], other["wave"]), (1, 2))


class CheckDispatchTests(CommandsTestCase):
    def prepare_fake(self, check_output=CHECK_READY):
        fake = os.path.join(self.fixture.tmp, "fake_run_loop_checkable.py")
        self.fixture.write(fake, FAKE_RUN_LOOP_CHECKABLE)
        self.fixture.write(os.path.join(self.fixture.tmp, "check_output.txt"), check_output)
        self.fixture.scripts["run_loop"] = fake
        self.fixture.start()

    def read_record(self, name, seconds=15):
        path = os.path.join(self.fixture.tmp, name)
        deadline = time.time() + seconds
        while time.time() < deadline:
            if os.path.exists(path) and os.path.getsize(path) > 0:
                with open(path, encoding="utf-8") as handle:
                    return json.load(handle)
            time.sleep(0.05)
        self.fail("record %s was not written" % name)

    def write_roadmap(self, text):
        self.fixture.write(os.path.join(self.fixture.project_dir, "ROADMAP.md"), text)

    def test_check_without_wave_returns_auto_dispatch_and_passes_it_to_run_loop(self):
        self.prepare_fake()
        self.write_roadmap(roadmap("- [a] status=approved paths=a", "- [b] status=approved paths=b"))
        status, data = self.fixture.call("/api/loop/check", "POST", {})
        self.assertEqual(status, 200)
        self.assertEqual((data["dispatch"]["wave"], data["dispatch"]["auto"]), (2, True))
        self.assertIn("2 independent", data["dispatch"]["reason"])
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "2"])

    def test_explicit_wave_wins(self):
        self.prepare_fake()
        self.write_roadmap(roadmap("- [a] status=approved paths=a", "- [b] status=approved paths=b"))
        data = self.fixture.call("/api/loop/check", "POST", {"wave": 1})[1]
        self.assertEqual((data["dispatch"]["wave"], data["dispatch"]["auto"]), (1, False))
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "1"])

    def test_no_roadmap_is_wave_one_without_flag(self):
        self.prepare_fake()
        data = self.fixture.call("/api/loop/check", "POST", {})[1]
        self.assertEqual(data["dispatch"]["wave"], 1)
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check"])


if __name__ == "__main__":
    unittest.main()
