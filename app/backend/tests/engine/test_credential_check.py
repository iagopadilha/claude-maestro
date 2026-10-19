"""Preflight of the test credentials (engine/credential_check.py): TEST-ENVIRONMENT.md holds NAMES, never values; every $NAME is
defined in some .env; test users come from `;`-separated lists. Temporary folders only, never the real ~/.loop-harness."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import credential_check  # noqa: E402

ISOLATED = ("TEST_USER_EMAILS", "TEST_USER_PASSWORDS", "TEST_USER_ROLES", "ADMIN_TOKEN", "LOCAL_URL")


class TestCredentialCheck(unittest.TestCase):
    def setUp(self):
        self.saved = {name: os.environ.pop(name, None) for name in ISOLATED}
        self.root = tempfile.mkdtemp(prefix="credcheck-")
        os.makedirs(os.path.join(self.root, ".specs", "project"))

    def tearDown(self):
        for name, value in self.saved.items():
            os.environ.pop(name, None)
            if value is not None:
                os.environ[name] = value

    def write_env_test(self, text):
        with open(os.path.join(self.root, ".specs", "project", ".env.test"), "w", encoding="utf-8") as handle:
            handle.write(text)

    def test_split_keeps_alignment_and_honours_the_escaped_separator(self):
        self.assertEqual(credential_check.split_values("a;;c"), ["a", "", "c"])
        self.assertEqual(credential_check.split_values("pw\\;1;pw2"), ["pw;1", "pw2"])
        self.assertEqual(credential_check.split_values(""), [])

    def test_users_from_the_lists_become_numbered_names(self):
        self.write_env_test("TEST_USER_EMAILS=a@x.com;b@x.com\nTEST_USER_PASSWORDS=p1;p2\n")
        names = credential_check.defined_names(self.root)
        for expected in ("TEST_USER_1_EMAIL", "TEST_USER_2_PASSWORD", "TEST_USER_COUNT"):
            self.assertIn(expected, names)
        self.assertNotIn("TEST_USER_3_EMAIL", names)
        self.assertNotIn("TEST_USER_1_ROLE", names)

    def test_a_name_without_a_value_anywhere_is_reported_and_one_with_a_value_is_not(self):
        self.write_env_test("ADMIN_TOKEN=abc12345\nLOCAL_URL=\n")
        text = "| Admin | `$ADMIN_TOKEN` |\n| App | `$LOCAL_URL` |\n"
        self.assertEqual(credential_check.undefined_names(self.root, text), ["LOCAL_URL"])

    def test_plaintext_secret_is_found_by_line_without_echoing_the_value(self):
        text = "# Env\n| Admin | X-Admin-Token | `$ADMIN_TOKEN` |\npassword: hunter2hunter2\nAuthorization: Bearer eyJhbGciOiJIUzI1NiJ9abc\n"
        self.assertEqual(credential_check.plaintext_secret_lines(text), [3, 4])
        problems = credential_check.credential_problems(self.root, text)
        self.assertTrue(any("line(s) 3, 4" in problem for problem in problems))
        self.assertFalse(any("hunter2" in problem or "eyJhbGci" in problem for problem in problems))

    def test_fallback_chain_project_first_then_the_machine_default_test_user(self):
        data_dir = tempfile.mkdtemp(prefix="credcheck-data-")
        with open(os.path.join(data_dir, ".env"), "w", encoding="utf-8") as handle:
            handle.write("TEST_USER_EMAILS=machine@x.com\nTEST_USER_PASSWORDS=machine-pass\nADMIN_TOKEN=machine-token\n")
        saved = {name: os.environ.get(name) for name in ("LOOP_DATA_DIR", "LOOP_ENV_FILE")}
        os.environ["LOOP_DATA_DIR"] = data_dir
        os.environ.pop("LOOP_ENV_FILE", None)
        try:
            text = "| User | `$TEST_USER_1_EMAIL` | `$TEST_USER_1_PASSWORD` |\n| Admin | `$ADMIN_TOKEN` |\n"
            # the project defines nothing: the machine defaults (generic test user) answer
            self.assertEqual(credential_check.undefined_names(self.root, text), [])
            self.assertEqual(credential_check.credential_problems(self.root, text), [])
            # a name nobody defines is still reported, even with the fallback
            self.assertEqual(credential_check.undefined_names(self.root, text + "| App | `$LOCAL_URL` |\n"), ["LOCAL_URL"])
            # the project wins where it defines the same name
            self.write_env_test("TEST_USER_EMAILS=project@x.com;second@x.com\nTEST_USER_PASSWORDS=p1;p2\n")
            self.assertIn("TEST_USER_2_EMAIL", credential_check.defined_names(self.root))
        finally:
            for name, value in saved.items():
                os.environ.pop(name, None)
                if value is not None:
                    os.environ[name] = value

    def test_names_placeholders_and_masks_are_not_plaintext_secrets(self):
        text = "password: $TEST_USER_1_PASSWORD\ntoken = ***\n| Password | FILL_IN |\n| Token name | `$ADMIN_TOKEN` |\n"
        self.assertEqual(credential_check.plaintext_secret_lines(text), [])

    def test_table_cells_with_prose_next_to_a_secret_word_are_not_plaintext_secrets(self):
        text = "| Access token | obtained from the login response |\n| Bearer token | Authorization header |\n"
        self.assertEqual(credential_check.plaintext_secret_lines(text), [])
        self.assertEqual(credential_check.plaintext_secret_lines("| Password | hunter2hunter2 |\n"), [1])


if __name__ == "__main__":
    unittest.main()
