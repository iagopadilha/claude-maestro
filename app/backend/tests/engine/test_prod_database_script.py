"""engine/prod_database_script.py: the production database script made at delivery/close. Real temporary git repos (base branch + loop
branch), a FAKE dotnet runner (never real dotnet), a temporary output folder. Nothing touches ~/Downloads or a database."""
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import delivery  # noqa: E402
import prod_database_script  # noqa: E402

IDENTITY = ("-c", "user.name=test", "-c", "user.email=test@localhost")
TODAY = datetime.date(2026, 10, 20)
BASE_MIGRATION = "20260101000000_Init"
LOOP_MIGRATION = "20260201000000_AddOrders"
ROADMAP = ("# Roadmap\n\n## Stage 1 - Base\n\n- [second] status=verified scope=Small kind=back paths=src\n"
           "- [first] status=verified scope=Small kind=back paths=src\n")


def git(folder, *args):
    result = subprocess.run(["git", "-C", folder, *IDENTITY, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode != 0:
        raise AssertionError("git %s failed: %s" % (" ".join(args), result.stderr))
    return result.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FakeDotnet:
    def __init__(self, output="CREATE TABLE Orders (Id int);\n", code=0, error=""):
        self.output, self.code, self.error, self.calls = output, code, error, []

    def __call__(self, args, cwd):
        self.calls.append((list(args), cwd))
        return self.code, self.output, self.error


class ProdScriptTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix="proddb-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.output_dir = os.path.join(self.tmp, "out")
        self.repo = os.path.join(self.tmp, "orders-api")
        os.makedirs(self.repo)
        git(self.repo, "init", "-q", "-b", "developer")
        write(os.path.join(self.repo, "src", "Data", "Migrations", BASE_MIGRATION + ".cs"), "// init\n")
        write(os.path.join(self.repo, "src", "Data", "Migrations", BASE_MIGRATION + ".Designer.cs"), "// designer\n")
        write(os.path.join(self.repo, "src", "Data", "Migrations", "AppDbContextModelSnapshot.cs"), "// snapshot v1\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "loop/feature")

    def add_loop_migration(self):
        write(os.path.join(self.repo, "src", "Data", "Migrations", LOOP_MIGRATION + ".cs"), "// orders\n")
        write(os.path.join(self.repo, "src", "Data", "Migrations", LOOP_MIGRATION + ".Designer.cs"), "// designer\n")
        write(os.path.join(self.repo, "src", "Data", "Migrations", "AppDbContextModelSnapshot.cs"), "// snapshot v2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "migration")

    def add_sql(self, feature, name, text):
        write(os.path.join(self.repo, ".specs", "features", feature, "sql", name), text)

    def entry(self, **extra):
        values = {"name": "orders-api", "root": self.repo, "branch": "loop/feature", "base": "developer", "repo_path": self.repo}
        values.update(extra)
        return values

    def generate(self, runner, **extra):
        return prod_database_script.generate("orders", [self.entry()], self.repo, self.output_dir, runner, TODAY, **extra)


class TestMigrationScript(ProdScriptTestCase):
    def test_new_migration_runs_the_idempotent_script_between_the_last_base_and_the_last_loop_migration(self):
        self.add_loop_migration()
        runner = FakeDotnet()
        result = self.generate(runner)
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["repos"], ["orders-api"])
        self.assertEqual(result["path"], os.path.join(self.output_dir, "orders-prod-database-2026-10-20.sql"))
        self.assertEqual(len(runner.calls), 1)
        args, cwd = runner.calls[0]
        self.assertEqual(args, ["dotnet", "ef", "migrations", "script", "--idempotent", BASE_MIGRATION, LOOP_MIGRATION])
        self.assertEqual(os.path.normcase(cwd), os.path.normcase(os.path.join(self.repo, "src", "Data")))
        text = read(result["path"])
        self.assertIn("RUN ON PRODUCTION BEFORE DEPLOYING. REVIEW FIRST.", text)
        self.assertIn("CREATE TABLE Orders", text)
        self.assertIn("orders-api: migrations %s -> %s" % (BASE_MIGRATION, LOOP_MIGRATION), text)

    def test_designer_and_snapshot_files_alone_are_not_a_migration(self):
        write(os.path.join(self.repo, "src", "Data", "Migrations", "AppDbContextModelSnapshot.cs"), "// snapshot v2\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "snapshot only")
        runner = FakeDotnet()
        result = self.generate(runner)
        self.assertEqual((result["path"], result["repos"], runner.calls), ("", [], []))
        self.assertFalse(os.path.exists(self.output_dir))

    def test_a_repo_without_migrations_before_starts_from_zero(self):
        git(self.repo, "checkout", "-q", "developer")
        git(self.repo, "rm", "-q", "-r", "src")
        git(self.repo, "commit", "-q", "-m", "no migrations at base")
        git(self.repo, "checkout", "-q", "-B", "loop/feature")
        self.add_loop_migration()
        runner = FakeDotnet()
        self.generate(runner)
        self.assertEqual(runner.calls[0][0][-2:], ["0", LOOP_MIGRATION])

    def test_a_reference_repo_is_skipped(self):
        self.add_loop_migration()
        runner = FakeDotnet()
        result = self.generate(runner, roles={"orders-api": "reference"})
        self.assertEqual((result["path"], runner.calls), ("", []))

    def test_a_dotnet_failure_is_a_warning_not_an_error(self):
        self.add_loop_migration()
        self.add_sql("first", "001.sql", "UPDATE t SET a = 1;\n")
        result = self.generate(FakeDotnet(output="", code=1, error="build failed"))
        self.assertEqual(result["repos"], [])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertIn("build failed", result["warnings"][0])
        text = read(result["path"])  # the manual script is still delivered, with the warning inside the file
        self.assertIn("-- WARNING: orders-api", text)
        self.assertIn("UPDATE t SET a = 1;", text)

    def test_an_unknown_base_branch_is_a_warning(self):
        self.add_loop_migration()
        result = prod_database_script.generate("orders", [self.entry(base="nope")], self.repo, self.output_dir, FakeDotnet(), TODAY)
        self.assertEqual(result["path"], "")
        self.assertEqual(len(result["warnings"]), 1)


class TestManualScripts(ProdScriptTestCase):
    def test_scripts_follow_the_roadmap_order_then_file_name_and_come_after_the_migrations(self):
        write(os.path.join(self.repo, ".specs", "project", "ROADMAP.md"), ROADMAP)
        self.add_loop_migration()
        self.add_sql("first", "002-b.sql", "-- first b\nSELECT 2;\n")
        self.add_sql("first", "001-a.sql", "-- first a\nSELECT 1;\n")
        self.add_sql("second", "001.sql", "-- second\nSELECT 3;\n")
        self.add_sql("zzz-not-in-roadmap", "001.sql", "-- zzz\nSELECT 4;\n")
        result = self.generate(FakeDotnet())
        self.assertEqual(result["sql_files"], ["second/001.sql", "first/001-a.sql", "first/002-b.sql", "zzz-not-in-roadmap/001.sql"])
        text = read(result["path"])
        positions = [text.index(marker) for marker in ("CREATE TABLE Orders", "-- second", "-- first a", "-- first b", "-- zzz")]
        self.assertEqual(positions, sorted(positions))

    def test_only_sql_scripts_still_write_the_file_and_no_script_means_no_file(self):
        self.add_sql("first", "001.sql", "ALTER TABLE t ADD c int;\n")
        runner = FakeDotnet()
        result = self.generate(runner)
        self.assertEqual(runner.calls, [])
        self.assertIn("ALTER TABLE t ADD c int;", read(result["path"]))
        shutil.rmtree(os.path.join(self.repo, ".specs"))
        self.assertEqual(self.generate(runner)["path"], "")


class TestDeliveryAndRoles(ProdScriptTestCase):
    def test_roles_are_read_straight_from_repos_json_and_a_missing_role_is_edit(self):
        write(os.path.join(self.repo, ".specs", "project", "REPOS.json"), json.dumps({"repos": [
            {"name": "orders-api", "path": self.repo, "primary": True}, {"name": "lib", "path": os.path.join(self.tmp, "lib"), "role": "reference"}]}))
        roles = prod_database_script.read_roles(self.repo)
        self.assertEqual(prod_database_script.role_of(roles, "orders-api", self.repo), "edit")
        self.assertEqual(prod_database_script.role_of(roles, "lib"), "reference")
        self.assertEqual(prod_database_script.role_of({}, "anything"), "edit")

    def test_attach_to_delivery_records_the_script_and_never_runs_on_a_dry_run_or_an_unpushed_result(self):
        self.add_loop_migration()
        meta = {"branch": "loop/feature", "base": "developer"}
        runner = FakeDotnet()
        dry = delivery.attach_prod_database(self.repo, meta, {"push": "dry-run: git push"}, True, self.output_dir, runner)
        failed = delivery.attach_prod_database(self.repo, meta, {"push": "FAILED: x"}, False, self.output_dir, runner)
        self.assertNotIn("prod_database", dry)
        self.assertNotIn("prod_database", failed)
        self.assertEqual(runner.calls, [])
        done = delivery.attach_prod_database(self.repo, meta, {"push": "done: origin/loop/feature"}, False, self.output_dir, runner)
        self.assertEqual(done["prod_database"]["repos"], [os.path.basename(self.repo)])
        self.assertTrue(os.path.isfile(done["prod_database"]["path"]))
        self.assertTrue(done["prod_database"]["path"].startswith(self.output_dir))

    def test_a_failing_generator_becomes_a_delivery_warning(self):
        self.add_loop_migration()
        meta = {"branch": "loop/feature", "base": "developer"}
        done = delivery.attach_prod_database(self.repo, meta, {"push": "done: origin/loop/feature"}, False, self.output_dir,
                                             FakeDotnet(output="", code=1, error="no dotnet"))
        self.assertEqual(len(done["warnings"]), 1)
        self.assertIn("no dotnet", done["warnings"][0])
        self.assertEqual(done["push"], "done: origin/loop/feature")


class TestCloseLoopHook(ProdScriptTestCase):
    def test_close_writes_the_script_before_removal_and_a_failure_is_only_a_warning(self):
        self.add_loop_migration()
        entries = [{"name": "orders-api", "worktree": self.repo, "branch": "loop/feature", "base": "developer", "repo_path": self.repo}]
        checks = {"orders-api": {"branch": "loop/feature", "base": "developer"}}
        runner = FakeDotnet()
        original = prod_database_script.run_process
        prod_database_script.run_process = lambda args, cwd=None, timeout=0: (runner(args, cwd) if args[:1] == ["dotnet"] else original(args, cwd, 60))
        self.addCleanup(setattr, prod_database_script, "run_process", original)
        done = close_loop.write_prod_database_script(self.repo, self.repo, entries, checks, self.output_dir)
        self.assertEqual(done["repos"], ["orders-api"])
        self.assertTrue(os.path.isfile(done["path"]))
        runner.code, runner.output, runner.error = 1, "", "boom"
        failed = close_loop.write_prod_database_script(self.repo, self.repo, entries, checks, self.output_dir)
        self.assertEqual(failed["repos"], [])
        self.assertIn("boom", failed["warnings"][0])


class TestFailureMessage(unittest.TestCase):
    def test_the_exception_line_wins_over_the_build_warnings(self):
        text = "x.csproj : warning NU1903: vulnerable package\nBuild succeeded.\nSystem.NotSupportedException: idempotent not supported\n   at Foo()"
        self.assertEqual(prod_database_script.describe_failure(text), "System.NotSupportedException: idempotent not supported")

    def test_without_an_exception_the_tail_is_kept(self):
        self.assertEqual(prod_database_script.describe_failure("a\n" + "b" * 400)[-5:], "bbbbb")
        self.assertEqual(len(prod_database_script.describe_failure("a\n" + "b" * 400)), 300)

    def test_build_progress_lines_are_not_part_of_the_sql(self):
        out = "Build started...\nBuild succeeded.\nIF OBJECT_ID(N'x') IS NULL\nBEGIN\nEND;\n"
        self.assertEqual(prod_database_script.strip_build_progress(out), "IF OBJECT_ID(N'x') IS NULL\nBEGIN\nEND;")


if __name__ == "__main__":
    unittest.main()
