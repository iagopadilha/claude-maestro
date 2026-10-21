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
    def test_it_is_always_three(self):
        for text in ("", "no features here", roadmap("- [a] status=approved paths=a"),
                     roadmap("- [a] status=approved paths=a", "- [b] status=approved paths=b deps=a"),
                     roadmap("- [a] status=approved paths=src", "- [b] status=approved paths=src/api"),
                     "## Stage 1\n- [a] status=verified paths=a\n"):
            result = compute_default_dispatch(text)
            self.assertEqual((result["wave"], result["auto"]), (3, True), text)
            self.assertIn("3 features per turn", result["reason"])

    def test_an_explicit_cap_is_kept_inside_the_loop_range(self):
        self.assertEqual(compute_default_dispatch("", cap=5)["wave"], 5)
        self.assertEqual(compute_default_dispatch("", cap=99)["wave"], 8)
        self.assertEqual(compute_default_dispatch("", cap=0)["wave"], 1)


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
        self.assertEqual((data["dispatch"]["wave"], data["dispatch"]["auto"]), (3, True))
        self.assertIn("3 features per turn", data["dispatch"]["reason"])
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "3"])

    def test_explicit_wave_wins(self):
        self.prepare_fake()
        self.write_roadmap("**Dispatch:** 1\n" + roadmap("- [a] status=approved paths=a", "- [b] status=approved paths=b"))
        data = self.fixture.call("/api/loop/check", "POST", {"wave": 5})[1]  # the body no longer decides it
        self.assertEqual((data["dispatch"]["wave"], data["dispatch"]["auto"]), (1, False))
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "1"])

    def test_no_roadmap_is_still_wave_three(self):
        self.prepare_fake()
        data = self.fixture.call("/api/loop/check", "POST", {})[1]
        self.assertEqual(data["dispatch"]["wave"], 3)
        self.assertEqual(self.read_record("check_argv.json"), [self.fixture.repo, "--check", "--wave", "3"])


if __name__ == "__main__":
    unittest.main()
