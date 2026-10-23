"""The stored `setup` command of REPOS.json (agent-writable) runs only when the user saved that exact text through the app
(`setup_approval`, kept in the app data folder). An edited or never approved text stops the worktree setup with an error line."""
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import project_repos  # noqa: E402
import run_loop  # noqa: E402
import setup_approval  # noqa: E402
from test_auto_prepare import delete_tree, make_repo, write  # noqa: E402

MARKER_NAME = "setup-ran.txt"


class SetupApprovalTests(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="setup-approval-"))
        self.addCleanup(delete_tree, self.root)
        patcher = mock.patch.dict(os.environ, {"LOOP_DATA_DIR": os.path.join(self.root, "app-data")})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.primary = os.path.join(self.root, "primary")
        self.api = os.path.join(self.root, "api")
        for folder in (self.primary, self.api):
            make_repo(folder, {"a.txt": "a\n"})
        self.opts = types.SimpleNamespace(branch="loop/setup", dry_run=False, setup="", repo_path=self.primary, push=False)
        self.command = "python -c \"open('%s','w').write('x')\"" % MARKER_NAME

    def item(self, setup):
        return project_repos.RepoRef(name="api", path=self.api, setup=setup, copy_configured=True)

    def run_extra(self, setup):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            try:
                run_loop.ensure_extra_worktree(self.item(setup), self.opts, "loop/setup", {})
            except SystemExit as stop:
                return str(stop.code), output.getvalue()
        return "", output.getvalue()

    def marker(self):
        return os.path.join(self.api + "-worktree-loop", MARKER_NAME)

    def test_an_approved_setup_runs(self):
        setup_approval.approve_setup(self.primary, self.api, self.command)
        stop, text = self.run_extra(self.command)
        self.assertEqual(stop, "")
        self.assertTrue(os.path.isfile(self.marker()))
        self.assertIn("[api] setup: " + self.command, text)

    def test_a_setup_never_approved_is_refused_before_the_worktree_exists(self):
        stop, text = self.run_extra(self.command)
        self.assertIn("was not saved through the app", stop)
        self.assertIn("[api] error: setup command of repo api was not saved through the app", text)
        self.assertFalse(os.path.exists(self.api + "-worktree-loop"), "nothing is created")

    def test_a_setup_changed_in_the_file_after_the_approval_is_refused(self):
        setup_approval.approve_setup(self.primary, self.api, "npm ci")
        stop, _text = self.run_extra(self.command)  # an agent rewrote REPOS.json
        self.assertIn("was NOT run", stop)
        self.assertFalse(os.path.exists(self.marker()))
        self.assertFalse(os.path.exists(self.api + "-worktree-loop"))

    def test_the_approval_is_per_project_and_per_repo(self):
        setup_approval.approve_setup(os.path.join(self.root, "other-primary"), self.api, self.command)
        self.assertFalse(setup_approval.is_setup_approved(self.primary, self.api, self.command))
        setup_approval.approve_setup(self.primary, os.path.join(self.root, "web"), self.command)
        self.assertFalse(setup_approval.is_setup_approved(self.primary, self.api, self.command))

    def test_no_setup_stored_needs_no_approval(self):
        stop, _text = self.run_extra("")
        self.assertEqual(stop, "")

    def test_the_approval_file_is_in_the_app_data_folder_not_in_a_repository(self):
        setup_approval.approve_setup(self.primary, self.api, self.command)
        self.assertTrue(setup_approval.approvals_file_path().startswith(os.path.join(self.root, "app-data")))
        with open(setup_approval.approvals_file_path(), encoding="utf-8") as handle:
            self.assertNotIn(self.command, handle.read(), "only the hash is stored")

    def test_save_repos_approves_only_the_names_the_user_typed(self):
        repos = [project_repos.RepoRef(name="primary", path=self.primary, primary=True), self.item(self.command)]
        project_repos.save_repos(self.primary, True, repos)
        self.assertFalse(setup_approval.is_setup_approved(self.primary, self.api, self.command), "a carried-over text is not approved")
        project_repos.save_repos(self.primary, True, repos, approve_setup_for=("api",))
        self.assertTrue(setup_approval.is_setup_approved(self.primary, self.api, self.command))
        with open(project_repos.repos_file_path(self.primary), encoding="utf-8") as handle:
            self.assertEqual(json.load(handle)["repos"][1]["setup"], self.command)
        self.assertFalse(setup_approval.is_setup_approved(self.primary, self.api, self.command + " --evil"))

    def test_the_primary_repos_stored_setup_is_checked_in_a_multi_repo_start(self):
        write(os.path.join(self.primary, ".specs", "project", "ROADMAP.md"),
              "- [one] status=pending scope=Small kind=back paths=a.txt\n- [two] status=pending scope=Small kind=back paths=api:a.txt\n")
        project_repos.save_repos(self.primary, True, [project_repos.RepoRef(name="primary", path=self.primary, primary=True, setup=self.command),
                                                      self.item("")])
        output = io.StringIO()
        with contextlib.redirect_stdout(output), self.assertRaises(SystemExit) as stop:
            run_loop.ensure_worktrees(self.primary, self.opts)
        self.assertIn("was not saved through the app", str(stop.exception))
        self.assertFalse(os.path.exists(self.primary + "-worktree-loop"))


if __name__ == "__main__":
    unittest.main()
