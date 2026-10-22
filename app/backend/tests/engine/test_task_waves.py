"""engine/task_waves.py (V2): parse tasks.md, waves, --max, shared files, cycles, done tasks, the Status mirror, legacy features and the CLI
(`harness.py tasks waves`). Everything in temporary folders."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: F401  (isolated LOOP_DATA_DIR; engine on sys.path)
import task_waves

HARNESS = os.path.join(engine_env.APP_ROOT_DIR, "harness.py")
SLUG = "export-data"


def block(number, title, files, deps="none", kind="impl", parallel=True, status="pending"):
    return ("### T%d: %s%s\n**Type**: %s\n**Files**: %s\n**Deps**: %s\n**Reqs**: R-%02d\n**Done when**: it works %d\n**Status**: %s\n\n"
            % (number, title, " [P]" if parallel else "", kind, files, deps, number, number, status))


BASIC = ("# tasks\n\n" + block(1, "One", "src/a.py") + block(2, "Two", "src/b.py") + block(3, "Three", "src/c.py")
         + block(4, "Wire", "src/app.py", deps="T1, T2, T3", kind="wiring", parallel=False))


class TaskWavesCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="waves-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.write_roadmap("")

    def write_roadmap(self, extra, features=""):
        path = os.path.join(self.root, ".specs", "project", "ROADMAP.md")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("# Roadmap\n\n%s\n## Stage 1 - Base\n\n%s" % (extra, features))

    def write_tasks(self, text, slug=SLUG):
        folder = os.path.join(self.root, ".specs", "features", slug)
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "tasks.md"), "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        return os.path.join(folder, "tasks.md")

    def waves(self, max_parallel=None):
        return task_waves.describe_waves(self.root, SLUG, max_parallel)


class ParseTests(TaskWavesCase):
    def test_parse_reads_every_field(self):
        parsed = task_waves.parse_tasks(BASIC)
        self.assertEqual(parsed["problems"], [])
        self.assertEqual([task["id"] for task in parsed["tasks"]], ["T1", "T2", "T3", "T4"])
        first, wire = parsed["tasks"][0], parsed["tasks"][3]
        self.assertEqual((first["title"], first["parallel"], first["type"], first["files"], first["deps"]),
                         ("One", True, "impl", ["src/a.py"], []))
        self.assertEqual((wire["type"], wire["parallel"], wire["deps"]), ("wiring", False, ["T1", "T2", "T3"]))
        self.assertEqual(wire["reqs"], ["R-04"])

    def test_parse_reports_missing_done_when_bad_type_and_duplicates(self):
        text = "### T1: A\n**Type**: test\n**Files**: a\n\n### T1: B\n**Type**: impl\n**Done when**: ok\n"
        problems = task_waves.parse_tasks(text)["problems"]
        self.assertTrue(any("duplicate task id T1" in item for item in problems), problems)
        self.assertTrue(any("Type must be impl or wiring" in item for item in problems), problems)
        self.assertTrue(any("no Done when" in item for item in problems), problems)


class WaveTests(TaskWavesCase):
    def test_three_parallel_tasks_then_the_wiring_task(self):
        self.write_tasks(BASIC)
        self.assertEqual(self.waves()["waves"], [["T1", "T2", "T3"], ["T4"]])

    def test_max_two_spreads_the_parallel_tasks(self):
        self.write_tasks(BASIC)
        self.assertEqual(self.waves(max_parallel=2)["waves"], [["T1", "T2"], ["T3"], ["T4"]])

    def test_default_max_comes_from_the_roadmap_header(self):
        self.write_roadmap("**Max parallel tasks:** 1\n")
        self.write_tasks(BASIC)
        described = self.waves()
        self.assertEqual(described["max_parallel"], 1)
        self.assertEqual(described["waves"], [["T1"], ["T2"], ["T3"], ["T4"]])

    def test_two_tasks_sharing_a_file_never_share_a_wave(self):
        text = "# t\n\n" + block(1, "A", "src/shared.py, src/a.py") + block(2, "B", "SRC/shared.py") + block(3, "C", "src/c.py")
        self.write_tasks(text)
        self.assertEqual(self.waves()["waves"], [["T1", "T3"], ["T2"]])

    def test_cycle_gives_a_problem_and_no_waves(self):
        self.write_tasks("# t\n\n" + block(1, "A", "a", deps="T2") + block(2, "B", "b", deps="T1"))
        described = self.waves()
        self.assertEqual(described["waves"], [])
        self.assertTrue(any("cycle" in problem for problem in described["problems"]), described["problems"])

    def test_unknown_and_self_dependencies_are_problems(self):
        tasks = task_waves.parse_tasks("# t\n\n" + block(1, "A", "a", deps="T9") + block(2, "B", "b", deps="T2"))["tasks"]
        built = task_waves.build_waves(tasks, 3)
        self.assertEqual(built["waves"], [])
        self.assertEqual(len(built["problems"]), 2)

    def test_done_tasks_are_skipped(self):
        self.write_tasks(BASIC)
        task_waves.set_task_status(self.root, SLUG, "T1", "done")
        task_waves.set_task_status(self.root, SLUG, "T2", "done")
        described = self.waves()
        self.assertEqual(described["waves"], [["T3"], ["T4"]])
        self.assertEqual(described["done"], ["T1", "T2"])
        self.assertEqual([task["id"] for task in described["tasks"]], ["T3", "T4"])

    def test_set_records_the_plan_wave_number_and_note(self):
        self.write_tasks(BASIC)
        document = task_waves.set_task_status(self.root, SLUG, "t4", "running", "going")
        self.assertEqual(document["tasks"]["T4"], {"status": "running", "wave": 2, "note": "going"})
        with self.assertRaises(ValueError):
            task_waves.write_status(self.root, SLUG, "T1", "bogus")

    def test_read_status_of_a_missing_or_broken_file_is_empty(self):
        self.assertEqual(task_waves.read_status(self.root, SLUG)["tasks"], {})
        path = task_waves.status_path(self.root, SLUG)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as handle:
            handle.write("{not json")
        self.assertEqual(task_waves.read_status(self.root, SLUG)["tasks"], {})

    def test_write_status_leaves_no_temp_file(self):
        task_waves.write_status(self.root, SLUG, "T1", "done")
        folder = os.path.dirname(task_waves.status_path(self.root, SLUG))
        self.assertEqual([name for name in os.listdir(folder) if name.endswith(".tmp")], [])


class LegacyTests(TaskWavesCase):
    def test_feature_without_tasks_md_gets_one_synthetic_task_and_a_note(self):
        self.write_roadmap("", '- [%s] status=pending scope=Small kind=back paths=src/x,src/y title="Export"\n' % SLUG)
        described = self.waves()
        self.assertEqual(described["waves"], [["T1"]])
        self.assertEqual(described["tasks"][0]["files"], ["src/x", "src/y"])
        self.assertTrue(any("no tasks.md" in problem for problem in described["problems"]), described["problems"])


class MirrorTests(TaskWavesCase):
    def test_mirror_updates_only_the_status_lines(self):
        path = self.write_tasks(BASIC)
        task_waves.set_task_status(self.root, SLUG, "T1", "done")
        task_waves.set_task_status(self.root, SLUG, "T3", "failed", "boom")
        feature_dir = os.path.dirname(path)
        self.assertEqual(task_waves.mirror_status_into_tasks_md(feature_dir, task_waves.read_status(self.root, SLUG)), 2)
        with open(path, encoding="utf-8", newline="") as handle:
            after = handle.read()
        before_lines, after_lines = BASIC.splitlines(), after.splitlines()
        self.assertEqual(len(before_lines), len(after_lines))
        differing = [(old, new) for old, new in zip(before_lines, after_lines) if old != new]
        self.assertEqual(differing, [("**Status**: pending", "**Status**: done"), ("**Status**: pending", "**Status**: failed")])
        self.assertEqual(task_waves.mirror_status_into_tasks_md(feature_dir, task_waves.read_status(self.root, SLUG)), 0, "idempotent")

    def test_mirror_keeps_crlf_line_endings(self):
        path = self.write_tasks(BASIC.replace("\n", "\r\n"))
        task_waves.set_task_status(self.root, SLUG, "T2", "done")
        task_waves.mirror_status_into_tasks_md(os.path.dirname(path), task_waves.read_status(self.root, SLUG))
        with open(path, "rb") as handle:
            raw = handle.read()
        self.assertNotIn(b"\n", raw.replace(b"\r\n", b""))
        self.assertIn(b"**Status**: done\r\n", raw)

    def test_mirror_without_tasks_md_changes_nothing(self):
        self.assertEqual(task_waves.mirror_status_into_tasks_md(os.path.join(self.root, "nowhere"), {"tasks": {}}), 0)


class CliTests(TaskWavesCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, HARNESS, "tasks"] + list(args) + ["--root", self.root], capture_output=True, text=True,
                              timeout=60, env=dict(os.environ))

    def test_harness_tasks_waves_prints_the_same_json_as_the_module(self):
        self.write_tasks(BASIC)
        done = self.run_cli("waves", SLUG)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["waves"], [["T1", "T2", "T3"], ["T4"]])
        self.assertEqual(json.loads(done.stdout), json.loads(json.dumps(self.waves())))
        limited = self.run_cli("waves", SLUG, "--max", "2")
        self.assertEqual(json.loads(limited.stdout)["waves"], [["T1", "T2"], ["T3"], ["T4"]])

    def test_set_then_waves_then_mirror(self):
        path = self.write_tasks(BASIC)
        for task in ("T1", "T2"):
            self.assertEqual(self.run_cli("set", SLUG, task, "done").returncode, 0)
        self.assertEqual(json.loads(self.run_cli("waves", SLUG).stdout)["waves"], [["T3"], ["T4"]])
        mirrored = self.run_cli("mirror", SLUG)
        self.assertEqual(json.loads(mirrored.stdout), {"slug": SLUG, "lines_changed": 2})
        with open(path, encoding="utf-8") as handle:
            self.assertEqual(handle.read().count("**Status**: done"), 2)

    def test_a_cycle_exits_with_a_problem(self):
        self.write_tasks("# t\n\n" + block(1, "A", "a", deps="T2") + block(2, "B", "b", deps="T1"))
        done = self.run_cli("waves", SLUG)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["waves"], [])


if __name__ == "__main__":
    unittest.main()
