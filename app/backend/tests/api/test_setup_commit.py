"""BR-03: POST workspace/repos/<name>/commit-setup. Scratch repos with a local bare `origin`; no claude, no network."""
import os
import subprocess
from types import SimpleNamespace

from harness_backend.api.controllers.setup_commit_controller import SetupCommitController
from harness_backend.services.setup_commit_service import SetupCommitService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import run_git, write_file

MESSAGE = "chore(setup): codebase maps and agent context"


class IdleJobs:
    def __init__(self):
        self.running = None

    def get_running_job(self, repo_path):
        return self.running


class TestSetupCommit(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        run_git(self.primary, "branch", "-M", "developer")
        self.origin = os.path.join(self.temporary_dir, "origin.git")
        subprocess.run(["git", "init", "-q", "--bare", self.origin], check=True)
        run_git(self.primary, "remote", "add", "origin", self.origin)
        run_git(self.primary, "push", "-q", "-u", "origin", "developer")
        self.context = self.make_context(self.primary)
        self.jobs = IdleJobs()
        self.service = SetupCommitService(self.registry, self.loop_status_service, self.jobs, self.git_client, self.log)

    def commit(self, branch="developer"):
        return self.service.commit_setup(self.context, "app", branch)

    def refusal(self, branch="developer"):
        with self.assertRaises(Exception) as caught:
            self.commit(branch)
        return caught.exception

    def origin_head(self, branch="developer"):
        return subprocess.run(["git", "-C", self.origin, "rev-parse", "--short", branch], capture_output=True, text=True).stdout.strip()

    def test_commits_only_setup_files_and_pushes_to_the_upstream(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        write_file(os.path.join(self.primary, ".specs", "project", "PROJECT.md"), "plan\n")
        write_file(os.path.join(self.primary, "README.md"), "changed by the user\n")
        run_git(self.primary, "add", "README.md")  # staged by the user: must stay out of the commit
        write_file(os.path.join(self.primary, "src", "new.py"), "x\n")
        result = self.commit()
        self.assertTrue(result["ok"])
        self.assertEqual(result["pushed_to"], "origin/developer")
        self.assertEqual(sorted(result["files"]), [".specs/project/PROJECT.md", "docs/codebase/STACK.md"])
        self.assertEqual(self.origin_head(), result["commit"])
        shown = run_git(self.primary, "show", "--name-only", "--format=%s", "HEAD").stdout.split()
        self.assertEqual(shown[0], "chore(setup):")
        self.assertEqual(run_git(self.primary, "log", "-1", "--format=%s").stdout.strip(), MESSAGE)
        self.assertEqual(sorted(shown[len(MESSAGE.split()):]), [".specs/project/PROJECT.md", "docs/codebase/STACK.md"])
        status = run_git(self.primary, "status", "--porcelain").stdout
        self.assertIn("M  README.md", status)
        self.assertIn("?? src/", status)

    def test_wrong_confirmation_is_a_400_and_changes_nothing(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        error = self.refusal("main")
        self.assertEqual(type(error).__name__, "InvalidRepoError")
        self.assertEqual(self.origin_head(), run_git(self.primary, "rev-parse", "--short", "HEAD").stdout.strip())
        self.assertIn("?? docs/", run_git(self.primary, "status", "--porcelain").stdout)

    def test_refused_on_main_and_master(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        for name in ("main", "master"):
            run_git(self.primary, "checkout", "-q", "-B", name)
            self.assertEqual(self.refusal(name).code, "protected_branch")

    def test_refused_on_a_detached_head(self):
        run_git(self.primary, "checkout", "-q", "--detach")
        self.assertEqual(self.refusal("developer").code, "detached_head")

    def test_refused_without_an_upstream(self):
        run_git(self.primary, "checkout", "-q", "-b", "local-only")
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        error = self.refusal("local-only")
        self.assertEqual(error.code, "no_upstream")
        self.assertIn("git push -u origin local-only", error.extras["reason"])

    def test_refused_while_a_loop_or_a_job_runs(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        self.jobs.running = {"id": "j1"}
        self.assertEqual(self.refusal().code, "busy")
        self.jobs.running = None
        self.write_live_loop_lock(self.context.work_dir)
        self.assertEqual(self.refusal().code, "busy")

    def test_refused_when_there_is_nothing_to_commit(self):
        write_file(os.path.join(self.primary, "README.md"), "only a non-setup change\n")
        self.assertEqual(self.refusal().code, "nothing_to_commit")

    def test_a_failed_push_keeps_the_commit_and_says_so(self):
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        run_git(self.primary, "remote", "set-url", "origin", os.path.join(self.temporary_dir, "gone.git"))
        error = self.refusal()
        self.assertEqual(error.code, "push_failed")
        self.assertEqual(error.extras["committed"], run_git(self.primary, "rev-parse", "--short", "HEAD").stdout.strip())

    def test_controller_answers_200_and_409_with_a_reason(self):
        controller = SetupCommitController(self.project_service, self.service)
        request = SimpleNamespace(route_params={"project_id": self.context.project.id, "name": "app"}, body={"confirm_branch": "developer"})
        refused = controller.commit_setup(request)
        self.assertEqual(refused.status, 409)
        self.assertIn("reason", refused.body)
        write_file(os.path.join(self.primary, "docs", "codebase", "STACK.md"), "map\n")
        ok = controller.commit_setup(request)
        self.assertEqual((ok.status, ok.body["ok"]), (200, True))
        request.body = {"confirm_branch": "nope"}
        self.assertEqual(controller.commit_setup(request).status, 400)
