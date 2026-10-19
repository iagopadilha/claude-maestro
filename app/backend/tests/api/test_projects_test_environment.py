"""Routes of the Test environment tab (`/p/<id>/api/test-environment*`): names only, values only into `.env.test`."""
import os
import subprocess

from .support_projects import ProjectsTestCase, create_git_repo, read_file, write_file

SECRET_VALUE = "s3cr3t-value-123"
DOCUMENT = ("# Test environment (DEV)\n\nSummary.\n\n| Item | Variable | Note |\n|---|---|---|\n| App | `$ZZ_TEST_URL` | FILL_IN: port |\n"
            "| Login | `$ZZ_TEST_TOKEN` | Infisical: zz-key |\n")


class TestEnvironmentRoutesTest(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        for name in ("ZZ_TEST_URL", "ZZ_TEST_TOKEN"):
            os.environ.pop(name, None)
        self.repo = create_git_repo(os.path.join(self.projects_dir, "envproj"))
        self.project_id = self.open_project(self.repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % self.project_id, {"conformance": "LEGACY"})
        self.assertEqual(status, 200, body)
        self.document_path = os.path.join(self.repo, ".specs", "project", "TEST-ENVIRONMENT.md")
        write_file(self.document_path, DOCUMENT)

    def route(self, suffix=""):
        return "/p/%s/api/test-environment%s" % (self.project_id, suffix)

    def test_get_lists_names_flags_and_open_fill_in_without_values(self):
        status, body = self.request("GET", self.route())
        self.assertEqual(status, 200, body)
        names = {item["name"]: item for item in body["names"]}
        self.assertEqual(sorted(names), ["ZZ_TEST_TOKEN", "ZZ_TEST_URL"])
        self.assertTrue(names["ZZ_TEST_TOKEN"]["secret"])
        self.assertFalse(names["ZZ_TEST_URL"]["secret"])
        self.assertEqual((names["ZZ_TEST_URL"]["set"], names["ZZ_TEST_URL"]["source"]), (False, "missing"))
        self.assertFalse(body["ready"])
        self.assertEqual(len(body["fill_in"]), 1)

    def test_put_values_writes_only_env_test_and_never_echoes_the_value(self):
        status, body = self.request("PUT", self.route("/values"), {"values": {"ZZ_TEST_TOKEN": SECRET_VALUE}})
        self.assertEqual(status, 200, body)
        self.assertNotIn(SECRET_VALUE, str(body))
        self.assertIn("ZZ_TEST_TOKEN=" + SECRET_VALUE, read_file(os.path.join(self.repo, ".specs", "project", ".env.test")))
        self.assertNotIn(SECRET_VALUE, read_file(self.document_path))
        _, listed = self.request("GET", self.route())
        item = next(entry for entry in listed["names"] if entry["name"] == "ZZ_TEST_TOKEN")
        self.assertEqual((item["set"], item["source"]), (True, "project"))
        self.assertNotIn(SECRET_VALUE, str(listed))
        ignored = subprocess.run(["git", "check-ignore", ".specs/project/.env.test"], cwd=self.repo, capture_output=True)
        self.assertEqual(ignored.returncode, 0, ".env.test must be git-ignored")

    def test_put_values_refuses_a_name_the_document_does_not_list(self):
        status, body = self.request("PUT", self.route("/values"), {"values": {"OTHER_NAME": SECRET_VALUE}})
        self.assertEqual(status, 400, body)
        self.assertNotIn(SECRET_VALUE, str(body))

    def test_put_access_is_stored_in_the_project(self):
        status, body = self.request("PUT", self.route("/access"), {"grafana_read": True})
        self.assertEqual(status, 200, body)
        self.assertTrue(body["access"]["grafana_read"])
        _, listed = self.request("GET", self.route())
        self.assertTrue(listed["access"]["grafana_read"])
