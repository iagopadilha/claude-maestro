#!/usr/bin/env python3
"""Proof of every example in hooks/README.md ("Cookbook"): each project rule runs in the REAL guard.py (JSON on stdin, exit 2 =
blocked), against a temporary project whose .specs/project/loop/json/PERMISSIONS.json is exactly the one the README shows.

Run:  python -m unittest discover -s plugin/skills/orchestrator/hooks -p "test_guard*.py" -v

Must-block cases assert exit 2 AND that the reason is `project-deny` (the project rule, not a built-in one). Must-pass cases
assert exit 0. Git cases run in the `orchestrator` profile (subagents may not run write git at all: built-in `git-write`).
The last test checks that the README still contains every regex below, so the doc and the proof cannot drift apart.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.join(HERE, "guard.py")
README = os.path.join(HERE, "README.md")
BLOCKED = 2
PASSED = 0
ALL_PROFILES = ("orchestrator", "executor", "spec", "verifier")

# --- the cookbook rules, exactly as in README.md
SQL_DROP_TRUNCATE = r"""\b(?:DROP\s+(?:TABLE|DATABASE|SCHEMA)\b|TRUNCATE\s+(?:TABLE\s+)?[\w.`"\[])"""
SQL_DELETE_WITHOUT_WHERE = r"""\bDELETE\s+FROM\s+[\w.`"\[\]]+(?![^;\n]*\bWHERE\b)"""
GIT_ONTO_MAIN = r"""\bgit\b[^\n;&|]*?\s(?:checkout|switch)\s+(?:-[fqmB]\s+|--\S+\s+)*(?:main|master)(?![\w./-])"""
TERRAFORM_APPLY = r"""\bterraform\s+(?:-\S+\s+)*(?:apply|destroy)\b"""
KUBECTL_DELETE = r"""\bkubectl\b[^\n;&|]*\sdelete\b"""
PATH_MIGRATIONS = r"""(?:^|[\\/])migrations[\\/]"""
PATH_INFRA_PROD = r"""(?:^|[\\/])infra[\\/]prod[\\/]"""
ALLOW_INSTALL = {"rule": "install", "quote": "yes, you may run npm install in this project", "at": "2026-10-15"}

COOKBOOK_PERMISSIONS = {
    "deny_bash_regex": [SQL_DROP_TRUNCATE, SQL_DELETE_WITHOUT_WHERE, GIT_ONTO_MAIN, TERRAFORM_APPLY, KUBECTL_DELETE],
    "deny_path_regex": [PATH_MIGRATIONS, PATH_INFRA_PROD],
    "allow": [ALLOW_INSTALL],
}

# (tool, value): tool "bash" = a Bash command; "edit" = an Edit of a path relative to the project
CASES = {
    "destructive SQL": {
        "block": [("bash", 'psql -c "DROP TABLE users"'), ("bash", "mysql -e 'truncate table orders'"),
                  ("bash", 'sqlite3 app.db "TRUNCATE orders"'), ("bash", 'psql -c "drop database shop"'),
                  ("bash", 'psql -c "DELETE FROM users"'), ("bash", "mysql -e 'delete from orders;'"),
                  ("bash", 'psql -c "DELETE FROM users; SELECT 1 WHERE true"')],
        "pass": [("bash", 'psql -c "DELETE FROM users WHERE id = 42"'), ("bash", "truncate -s 0 app.log"),
                 ("bash", 'psql -c "SELECT * FROM dropped_tables"'), ("bash", 'grep -rn "DELETE FROM" src/')],
    },
    "git onto main": {
        "profiles": ("orchestrator",),
        "block": [("bash", "git checkout main"), ("bash", "git switch master"), ("bash", "git -C repo checkout -B main origin/main"),
                  ("bash", "git checkout main && git commit -m hotfix")],
        "pass": [("bash", "git checkout -b feature/main-menu"), ("bash", "git switch -c fix/login main"),
                 ("bash", "git diff main...HEAD"), ("bash", "git log --oneline main"), ("bash", "git commit -m 'feat: menu'")],
    },
    "infrastructure": {
        "block": [("bash", "terraform apply -auto-approve"), ("bash", "terraform -chdir=infra destroy"),
                  ("bash", "kubectl delete pod api-0"), ("bash", "kubectl -n prod delete deployment api")],
        "pass": [("bash", "terraform plan -out tf.plan"), ("bash", "terraform validate"),
                 ("bash", "kubectl get pods"), ("bash", "kubectl describe pod api-0")],
    },
    "protected folders": {
        "block": [("edit", "db/migrations/0001_init.sql"), ("edit", "migrations/0002_users.py"),
                  ("edit", "infra/prod/main.tf"), ("bash", 'echo "ALTER TABLE x" > db/migrations/0003.sql'),
                  ("bash", "sed -i s/a/b/ infra/prod/values.yaml")],
        "pass": [("edit", "src/migrations_helper.py"), ("edit", "docs/migrations.md"),
                 ("edit", "infra/staging/main.tf"), ("edit", "infra/production-notes.md")],
    },
}


class CookbookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="guard-cookbook-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.project = os.path.join(self.tmp, "project")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.project, ".specs", "project", "loop", "json"))
        os.makedirs(self.data_dir)
        self.env = dict(os.environ)
        for name in ("LOOP_APP_HOME", "LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "GUARD_PROD_HINTS", "LOOP_SPECS_DIR", "LOOP_TURN", "LOOP_HEADLESS"):  # LOOP_SPECS_DIR inherited from a loop turn would send these tests' blocks to the REAL guard.log
            self.env.pop(name, None)
        self.env["LOOP_DATA_DIR"] = self.data_dir  # no real app, no notification channel
        self.write_permissions(COOKBOOK_PERMISSIONS)

    def write_permissions(self, data):
        path = os.path.join(self.project, ".specs", "project", "loop", "json", "PERMISSIONS.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)

    def run_guard(self, tool, value, profile):
        if tool == "bash":
            payload = {"tool_name": "Bash", "tool_input": {"command": value}}
        else:
            payload = {"tool_name": "Edit", "tool_input": {"file_path": os.path.join(self.project, value)}}
        payload["cwd"] = self.project
        done = subprocess.run([sys.executable, GUARD, "--profile", profile], input=json.dumps(payload), capture_output=True,
                              text=True, env=self.env, timeout=60)
        return done.returncode, done.stderr


class TestCookbook(CookbookCase):
    def check_group(self, name):
        group = CASES[name]
        for profile in group.get("profiles", ALL_PROFILES):
            for tool, value in group["block"]:
                code, stderr = self.run_guard(tool, value, profile)
                self.assertEqual(code, BLOCKED, "%s / %s must block: %s" % (name, profile, value))
                self.assertIn("project-deny", stderr, "%s / %s blocked by another rule: %s -> %s" % (name, profile, value, stderr))
            for tool, value in group["pass"]:
                code, stderr = self.run_guard(tool, value, profile)
                if profile == "spec" and tool == "edit":
                    # the spec profile may not edit code at all (built-in `specs-only`): the project rule must simply NOT be the reason
                    self.assertNotIn("project-deny", stderr, "%s / %s: the project rule must not match: %s -> %s" % (name, profile, value, stderr))
                    continue
                self.assertEqual(code, PASSED, "%s / %s must pass: %s -> %s" % (name, profile, value, stderr))

    def test_destructive_sql(self):
        self.check_group("destructive SQL")

    def test_git_onto_main(self):
        self.check_group("git onto main")

    def test_terraform_and_kubectl(self):
        self.check_group("infrastructure")

    def test_protected_folders(self):
        self.check_group("protected folders")

    def test_pushing_main_needs_no_project_rule(self):
        """Built-in hard rule `push-protected`: blocked in every profile, and `allow` cannot lift it."""
        self.write_permissions(dict(COOKBOOK_PERMISSIONS, allow=[{"rule": "push-protected", "quote": "push it", "at": "2026-10-15"},
                                                                 {"rule": "git-push", "quote": "push it", "at": "2026-10-15"}]))
        for profile in ALL_PROFILES:
            code, stderr = self.run_guard("bash", "git push origin main", profile)
            self.assertEqual(code, BLOCKED, profile)
            self.assertIn("push-protected", stderr)


class TestAllow(CookbookCase):
    def test_allow_lifts_a_built_in_rule_with_the_users_quote(self):
        self.assertEqual(self.run_guard("bash", "npm install left-pad", "executor")[0], PASSED)
        self.write_permissions(dict(COOKBOOK_PERMISSIONS, allow=[]))
        code, stderr = self.run_guard("bash", "npm install left-pad", "executor")
        self.assertEqual(code, BLOCKED)
        self.assertIn("install", stderr)

    def test_allow_never_lifts_a_hard_rule(self):
        self.write_permissions(dict(COOKBOOK_PERMISSIONS, allow=["push-force", "git-push"]))
        code, stderr = self.run_guard("bash", "git push --force origin feature/x", "executor")
        self.assertEqual(code, BLOCKED)
        self.assertIn("push-force", stderr)

    def test_allowing_project_deny_switches_off_every_project_rule(self):
        self.write_permissions(dict(COOKBOOK_PERMISSIONS, allow=[{"rule": "project-deny", "quote": "x", "at": "2026-10-15"}]))
        self.assertEqual(self.run_guard("bash", "terraform apply -auto-approve", "executor")[0], PASSED)


class TestReadmeMatchesTheProof(unittest.TestCase):
    def test_every_cookbook_regex_is_in_the_readme(self):
        with open(README, encoding="utf-8") as handle:
            readme = handle.read()
        for pattern in COOKBOOK_PERMISSIONS["deny_bash_regex"] + COOKBOOK_PERMISSIONS["deny_path_regex"]:
            self.assertIn(json.dumps(pattern), readme, "README.md does not show the tested regex %s" % pattern)
        self.assertIn(json.dumps(ALLOW_INSTALL["quote"]), readme)


if __name__ == "__main__":
    unittest.main()
