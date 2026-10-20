"""Default `.specs/project/.env.test` (engine/test_env_fallback.py): born from the machine-level TEST values, idempotent, never
overwrites, never copies notification/SMTP keys and never prints a value. Temporary folders only."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import credential_check  # noqa: E402
import run_loop  # noqa: E402
import test_env_fallback  # noqa: E402

TEST_NAMES = ("TEST_USER_EMAILS", "TEST_USER_PASSWORDS", "LOCAL_URL", "ROUTE_PREFIX", "ADMIN_TOKEN", "NOTIFY_EMAIL_TO", "LOOP_NOTIFY_TO",
              "LOOP_SMTP_PASSWORD", "LOOP_DATA_DIR", "LOOP_USER_SHARED_DIR", "LOOP_ENV_FILE")
SECRET_FRAGMENT = "zz-secret-fragment-9"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)


def read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


class FallbackTestCase(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in TEST_NAMES}
        self.tmp = tempfile.mkdtemp(prefix="envfallback-")
        self.root = os.path.join(self.tmp, "project")
        self.data_dir = os.path.join(self.tmp, "data")
        os.makedirs(os.path.join(self.root, ".specs", "project"))
        os.makedirs(self.data_dir)
        os.environ["LOOP_DATA_DIR"] = self.data_dir
        os.environ["LOOP_USER_SHARED_DIR"] = os.path.join(self.tmp, "shared")
        self.env_test_path = os.path.join(self.root, ".specs", "project", ".env.test")

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def machine_env(self, text):
        write(os.path.join(self.data_dir, ".env"), text)


class TestEnsureEnvTest(FallbackTestCase):
    def test_copies_only_test_keys_from_the_machine_file_and_never_notification_credentials(self):
        self.machine_env("TEST_USER_EMAILS=qa@x.com\nTEST_USER_PASSWORDS=%s\nROUTE_PREFIX=/api\nLOOP_SMTP_PASSWORD=smtp-%s\nLOOP_SMTP_HOST=smtp.x.com\n"
                         % (SECRET_FRAGMENT, SECRET_FRAGMENT))
        result = test_env_fallback.ensure_env_test(self.root)
        text = read(self.env_test_path)
        self.assertTrue(result["created"])
        self.assertEqual(set(result["added"]), {"TEST_USER_EMAILS", "TEST_USER_PASSWORDS", "ROUTE_PREFIX", "LOCAL_URL"})
        self.assertNotIn("LOOP_SMTP", text)
        self.assertNotIn("smtp-", text)
        self.assertIn("TEST_USER_PASSWORDS=" + SECRET_FRAGMENT, text)  # the value goes into the gitignored file...
        self.assertTrue(all(SECRET_FRAGMENT not in line for line in text.splitlines() if line.startswith("#")))  # ...never into a comment
        self.assertIn("from ~/.loop-harness/.env", text)

    def test_idempotent_and_never_overwrites_a_defined_name(self):
        self.machine_env("ROUTE_PREFIX=/machine\nADMIN_TOKEN=machine-token-1\n")
        write(self.env_test_path, "ROUTE_PREFIX=/mine\n# ADMIN_TOKEN=commented\n")
        first = test_env_fallback.ensure_env_test(self.root)
        text_after_first = read(self.env_test_path)
        self.assertEqual(set(first["added"]), {"ADMIN_TOKEN", "LOCAL_URL"})
        self.assertIn("ROUTE_PREFIX=/mine", text_after_first)
        self.assertNotIn("/machine", text_after_first)
        self.assertEqual(test_env_fallback.ensure_env_test(self.root), {"created": False, "added": []})
        self.assertEqual(read(self.env_test_path), text_after_first)

    def test_local_url_defaults_to_the_panel_and_an_existing_one_is_kept(self):
        test_env_fallback.ensure_env_test(self.root)
        self.assertEqual(credential_check.read_project_values(self.root)["LOCAL_URL"], test_env_fallback.panel_local_url())
        write(self.env_test_path, "LOCAL_URL=https://localhost:5001\n")
        test_env_fallback.ensure_env_test(self.root)
        self.assertEqual(credential_check.read_project_values(self.root)["LOCAL_URL"], "https://localhost:5001")

    def test_notify_destination_comes_from_the_owner_destination_alias_only(self):
        self.machine_env("LOOP_NOTIFY_TO=owner@x.com\nLOOP_SMTP_USER=mailer\n")
        test_env_fallback.ensure_env_test(self.root)
        values = credential_check.read_project_values(self.root)
        self.assertEqual(values["NOTIFY_EMAIL_TO"], "owner@x.com")
        self.assertNotIn("LOOP_NOTIFY_TO", values)
        self.assertNotIn("LOOP_SMTP_USER", values)

    def test_gitignore_gets_the_env_test_line_once(self):
        test_env_fallback.ensure_env_test(self.root)
        write(self.env_test_path, "")
        test_env_fallback.ensure_env_test(self.root)
        lines = read(os.path.join(self.root, ".gitignore")).splitlines()
        self.assertEqual(lines.count(".specs/project/.env.test"), 1)


class TestCheckUsesTheGeneratedFile(FallbackTestCase):
    TEXT = "| App | `$LOCAL_URL` |\n| Notify | `$NOTIFY_EMAIL_TO` |\n| Other | `$QA_UNKNOWN_NAME` |\n| Data | `$SAMPLE_CNPJ` |\n"

    def prepare(self):
        write(os.path.join(self.root, ".specs", "project", "TEST-ENVIRONMENT.md"), self.TEXT)
        write(os.path.join(self.root, ".specs", "project", "ROADMAP.md"), "# R\n\n- [a] status=pending regression=yes\n")

    def test_the_check_reports_only_what_stays_undefined_with_an_action(self):
        self.machine_env("LOOP_NOTIFY_TO=owner@x.com\n")
        self.prepare()
        problems = run_loop.test_environment_problems(self.root, type("Opts", (), {"e2e": False})())
        joined = " | ".join(problems)
        self.assertIn("$QA_UNKNOWN_NAME", joined)
        self.assertNotIn("$LOCAL_URL", joined)
        self.assertNotIn("$NOTIFY_EMAIL_TO", joined)
        self.assertNotIn("$SAMPLE_CNPJ", joined)  # sample data is a warning, not a problem
        self.assertIn("Action:", joined)
        self.assertIn(".specs/project/.env.test", joined)

    def test_sample_data_without_a_value_is_a_warning(self):
        self.prepare()
        warnings = run_loop.test_environment_warnings(self.root, None)
        self.assertEqual(len(warnings), 1)
        self.assertIn("$SAMPLE_CNPJ", warnings[0])


if __name__ == "__main__":
    unittest.main()
