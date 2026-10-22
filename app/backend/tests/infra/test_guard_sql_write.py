"""PreToolUse guard rules `sql-write` (MySQL MCP is read only), `migration-target` (dotnet ef on DEV passes, production is blocked) and
`capabilities-block` (the planning chat never edits the server-written block of PROJECT.md). The hook is run as a process with a JSON
payload on stdin, like the real one; both copies of the guard (plugin and ~/.claude/skills) are tested when present."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.abspath(os.path.join(HERE, "..", "..", "..", ".."))
PLUGIN_GUARD = os.path.join(REPO_ROOT, "plugin", "skills", "orchestrator", "hooks", "guard.py")
USER_GUARD = os.path.join(os.path.expanduser("~"), ".claude", "skills", "orchestrator", "hooks", "guard.py")
GUARDS = [path for path in (PLUGIN_GUARD, USER_GUARD) if os.path.isfile(path)]
DEV = "mcp__mcp-mysql-dev__mysql_query"
PROD = "mcp__mcp-mysql-prod__mysql_query"
SQL_PROFILES = ("planning", "orchestrator", "executor", "spec", "verifier")
BLOCK = "<!-- maestro:capabilities -->\nRepos: a (edit)\n<!-- /maestro:capabilities -->"
CLEAN_ENVIRONMENT = ("GUARD_PROD_HINTS", "GUARD_PROD_HOSTS", "ASPNETCORE_ENVIRONMENT", "DOTNET_ENVIRONMENT", "LOOP_SPECS_DIR", "LOOP_HEADLESS",
                     "LOOP_TURN", "LOOP_APP_HOME")


class GuardTestCase(unittest.TestCase):
    guard = PLUGIN_GUARD

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="guard-sql-"))
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, ".specs", "project"))
        self.env = {name: value for name, value in os.environ.items() if name not in CLEAN_ENVIRONMENT}
        self.env["LOOP_DATA_DIR"] = os.path.join(self.root, "data")  # a guard failure alert never reaches the real app

    def run_guard(self, profile, tool, tool_input, env=None):
        payload = {"tool_name": tool, "tool_input": tool_input, "cwd": self.root}
        done = subprocess.run([sys.executable, self.guard, "--profile", profile], input=json.dumps(payload), capture_output=True, text=True,
                              env=env or self.env)
        self.assertNotIn("guard-failopen", self.guard_log())  # an internal guard error would let everything through silently
        return done.returncode, done.stderr

    def guard_log(self):
        for parts in (("loop", "internal", "guard.log"), ("guard.log",)):
            path = os.path.join(self.root, ".specs", "project", *parts)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    return handle.read()
        return ""

    def query(self, profile, sql, tool=DEV):
        return self.run_guard(profile, tool, {"query": sql})

    def bash(self, profile, command, env=None):
        return self.run_guard(profile, "Bash", {"command": command}, env)


class SqlWriteTests(GuardTestCase):
    def assertBlocked(self, result, rule):
        self.assertEqual(result[0], 2, result[1])
        self.assertIn("[%s]" % rule if rule else "", result[1])

    def test_every_write_keyword_is_blocked_for_every_profile_on_dev_and_for_the_executor_on_prod_too(self):
        statements = ["INSERT INTO t VALUES (1)", "update t set a = 1", "DELETE FROM t", "REPLACE INTO t VALUES (1)",
                      "MERGE INTO t USING s ON 1=1 WHEN MATCHED THEN DELETE", "CREATE TABLE x (a int)", "ALTER TABLE t ADD c int",
                      "DROP TABLE t", "TRUNCATE TABLE t", "GRANT ALL ON *.* TO u", "REVOKE ALL ON *.* FROM u", "CALL cleanup()",
                      "LOAD DATA INFILE 'f' INTO TABLE t", "SET GLOBAL max_connections = 1"]
        for profile in SQL_PROFILES:
            for statement in statements:
                for tool in ((DEV, PROD) if profile == "executor" else (DEV,)):
                    with self.subTest(profile=profile, statement=statement, tool=tool):
                        code, stderr = self.query(profile, statement, tool)
                        self.assertEqual(code, 2, stderr)
                        self.assertIn("sql-write", stderr)
                        self.assertIn("read only through MCP: write the change as a .sql script (or a migration) and ask the user", stderr)
                        self.assertIn("NEEDS_CONFIRM", stderr)

    def test_reads_pass(self):
        for profile in SQL_PROFILES:
            for statement in ("SELECT * FROM t", "  select a from t where note = 'please update and delete'", "SHOW TABLES", "DESCRIBE t",
                              "DESC t", "EXPLAIN SELECT 1", "SELECT 1; SELECT 2;", "SELECT * FROM t FOR UPDATE", "SET @a = 1", "SET SESSION sql_mode = ''",
                              "WITH c AS (SELECT 1) SELECT * FROM c", "SELECT updated_at, created_at FROM t", "(SELECT 1)"):
                with self.subTest(profile=profile, statement=statement):
                    self.assertEqual(self.query(profile, statement)[0], 0)

    def test_first_keyword_after_comments_and_whitespace_and_every_statement(self):
        for statement in ("-- a note\nDELETE FROM t", "/* x */ DROP TABLE t", "# c\n\n  UPDATE t SET a=1", "SELECT 1; DROP TABLE t",
                          "SELECT 1;\n-- next\nINSERT INTO t VALUES (1)", "/*! DROP TABLE t */", "/*!50000 DELETE FROM t */",
                          "WITH c AS (SELECT 1) DELETE FROM t", "SELECT * FROM t INTO OUTFILE '/tmp/x'", "SET PERSIST max_connections=1"):
            with self.subTest(statement=statement):
                self.assertEqual(self.query("executor", statement)[0], 2)

    def test_a_write_word_inside_a_string_or_a_comment_is_not_a_write(self):
        for statement in ("SELECT 'DROP TABLE t; DELETE FROM x'", "SELECT 1 -- DROP TABLE t", "SELECT /* DELETE FROM t */ 1",
                          "SELECT \"a;DELETE\" FROM t", "SELECT `update` FROM t"):
            with self.subTest(statement=statement):
                self.assertEqual(self.query("planning", statement)[0], 0)

    def test_other_tools_and_servers_are_not_inspected_by_this_rule(self):
        self.assertEqual(self.run_guard("planning", "mcp__grafana__query_loki_logs", {"query": "DELETE"})[0], 0)
        self.assertEqual(self.run_guard("executor", "Read", {"file_path": os.path.join(self.root, "a.sql")})[0], 0)

    def test_a_query_given_in_another_field_is_still_inspected(self):
        self.assertEqual(self.run_guard("planning", DEV, {"sql": "DROP TABLE t"})[0], 2)
        self.assertEqual(self.run_guard("planning", DEV, {"statement": "DROP TABLE t"})[0], 2)
        self.assertEqual(self.run_guard("planning", DEV, {"other": "DROP TABLE t"})[0], 2)

    def test_a_released_rule_in_permissions_lets_it_through(self):
        os.makedirs(os.path.join(self.root, ".specs", "project", "loop", "json"), exist_ok=True)
        with open(os.path.join(self.root, ".specs", "project", "loop", "json", "PERMISSIONS.json"), "w", encoding="utf-8") as handle:
            json.dump({"allow": ["sql-write"]}, handle)
        self.assertEqual(self.query("orchestrator", "DROP TABLE t")[0], 0)


class MigrationTargetTests(GuardTestCase):
    ALLOWED = ["dotnet ef migrations add AddOrders", "dotnet ef migrations remove", "dotnet ef migrations list",
               "dotnet ef migrations script --idempotent A B", "dotnet ef database update",
               "dotnet ef database update --project src/Data --startup-project src/Api",
               "dotnet run --project src/Api -- migrate", "ASPNETCORE_ENVIRONMENT=Development dotnet ef database update",
               "dotnet ef database update --connection \"Server=dev.example;Database=app\"",
               "$env:ASPNETCORE_ENVIRONMENT='Development'; dotnet ef database update"]
    BLOCKED = ["ASPNETCORE_ENVIRONMENT=Production dotnet ef database update", "DOTNET_ENVIRONMENT=Production dotnet ef database update",
               "dotnet ef database update --environment Production", "dotnet ef database update -- --environment=Production",
               "export ASPNETCORE_ENVIRONMENT=Production && dotnet ef database update",
               "$env:ASPNETCORE_ENVIRONMENT='Production'; dotnet ef database update",
               "dotnet ef database update --connection \"Server=prod-db.example;Database=app\"",
               "dotnet ef database update --connection 'Server=db.production.example;Database=app'",
               "ASPNETCORE_ENVIRONMENT=production dotnet run --project src/Api -- migrate",
               "dotnet ef migrations script --idempotent A B --environment Production"]

    def test_dev_migrations_pass_in_executor_and_verifier(self):
        for profile in ("executor", "verifier"):
            for command in self.ALLOWED:
                with self.subTest(profile=profile, command=command):
                    self.assertEqual(self.bash(profile, command), (0, ""))

    def test_production_targets_are_blocked(self):
        for profile in ("executor", "verifier"):
            for command in self.BLOCKED:
                with self.subTest(profile=profile, command=command):
                    code, stderr = self.bash(profile, command)
                    self.assertEqual(code, 2, stderr)
                    self.assertIn("migration-target", stderr)

    def test_an_inherited_production_environment_is_blocked_and_a_known_prod_host_too(self):
        env = dict(self.env, ASPNETCORE_ENVIRONMENT="Production")
        self.assertEqual(self.bash("executor", "dotnet ef database update", env)[0], 2)
        env = dict(self.env, GUARD_PROD_HOSTS="db-360.internal")
        self.assertEqual(self.bash("executor", "dotnet ef database update --connection \"Server=db-360.internal\"", env)[0], 2)
        self.assertEqual(self.bash("executor", "dotnet ef database update --connection \"Server=db-dev.internal\"", env)[0], 0)

    def test_a_command_that_is_not_a_migration_is_not_judged_by_this_rule(self):
        self.assertEqual(self.bash("executor", "echo ASPNETCORE_ENVIRONMENT=Production")[0], 0)
        self.assertEqual(self.bash("executor", "dotnet build --environment Production")[0], 0)
        self.assertEqual(self.bash("executor", "dotnet test")[0], 0)


class CapabilitiesBlockTests(GuardTestCase):
    def setUp(self):
        super().setUp()
        self.project = os.path.join(self.root, ".specs", "project", "PROJECT.md")
        self.original = "# Project\n\nVision text.\n\n" + BLOCK + "\n\nMore text.\n"
        with open(self.project, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(self.original)

    def edit(self, old, new, path=None):
        return self.run_guard("planning", "Edit", {"file_path": path or self.project, "old_string": old, "new_string": new})[0]

    def write(self, content, path=None):
        return self.run_guard("planning", "Write", {"file_path": path or self.project, "content": content})[0]

    def test_text_outside_the_markers_stays_editable(self):
        self.assertEqual(self.edit("Vision text.", "Vision text, revised."), 0)
        self.assertEqual(self.edit("More text.", "More text.\nA new line."), 0)
        self.assertEqual(self.write(self.original.replace("Vision text.", "Another vision.")), 0)

    def test_edits_inside_or_across_the_markers_are_blocked(self):
        self.assertEqual(self.edit("Repos: a (edit)", "Repos: a (edit), b (edit)"), 2)
        self.assertEqual(self.edit("<!-- maestro:capabilities -->", ""), 2)
        self.assertEqual(self.edit("<!-- /maestro:capabilities -->\n\nMore", "<!-- /maestro:capabilities -->\n\nLess"), 2)
        self.assertEqual(self.edit("Vision text.", "Vision.\n<!-- maestro:capabilities -->\nfake\n<!-- /maestro:capabilities -->"), 2)

    def test_a_whole_file_write_must_keep_the_block_byte_for_byte(self):
        self.assertEqual(self.write(self.original.replace("Repos: a (edit)", "Repos: hacked")), 2)
        self.assertEqual(self.write("# Project\n\nNo block any more.\n"), 2)
        self.assertEqual(self.write(self.original + "\n<!-- maestro:capabilities -->\nsecond\n<!-- /maestro:capabilities -->\n"), 2)

    def test_creating_a_project_md_with_a_block_is_blocked_but_without_one_is_fine(self):
        other = os.path.join(self.root, ".specs", "project", "sub", ".specs", "project", "PROJECT.md")
        self.assertEqual(self.write("# P\n", other), 0)
        self.assertEqual(self.write("# P\n" + BLOCK + "\n", other), 2)

    def test_other_files_and_other_profiles_are_not_affected(self):
        roadmap = os.path.join(self.root, ".specs", "project", "ROADMAP.md")
        self.assertEqual(self.write("<!-- maestro:capabilities -->x<!-- /maestro:capabilities -->", roadmap), 0)
        self.assertEqual(self.run_guard("spec", "Edit", {"file_path": self.project, "old_string": "Repos: a (edit)", "new_string": "x"})[0], 0)


def clone_for_user_guard(case):
    """Same tests against the user-level copy of the guard (~/.claude/skills/orchestrator/hooks/guard.py)."""
    name = case.__name__ + "UserCopy"
    return type(name, (case,), {"guard": USER_GUARD})


for _case in (SqlWriteTests, MigrationTargetTests, CapabilitiesBlockTests):
    if os.path.isfile(USER_GUARD):
        globals()[_case.__name__ + "UserCopy"] = clone_for_user_guard(_case)


if __name__ == "__main__":
    unittest.main()
