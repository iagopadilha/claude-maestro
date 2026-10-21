"""Reset specs also DELETES the project's loop worktree (folder + local `loop/*` branch) with real git repos: no merge, no push, never the
primary checkout nor any non-loop worktree/branch. The same data is read BEFORE confirming by `GET .../reset-specs/preview`."""
import json
import os
import unittest

from .support_projects import ProjectsTestCase, create_git_repo, loop_file_path, read_file, run_git, write_file

LOOP_BRANCH = "loop/2026-10-20"


def list_branches(repo):
    return [line.strip().lstrip("* ") for line in run_git(repo, "branch", "--format=%(refname:short)").stdout.splitlines() if line.strip()]


def commit_in(folder, name, text="change\n", message="work"):
    write_file(os.path.join(folder, name), text)
    run_git(folder, "add", "-A")
    run_git(folder, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "-q", "-m", message)


class TestResetSpecsRemovesLoopWorktree(ProjectsTestCase):
    def build_project(self, name="wt"):
        repo, project_id = self.create_ready_project(name=name, approve=False)
        return repo, project_id

    def add_loop_worktree(self, repo, branch=LOOP_BRANCH):
        worktree = repo + "-worktree-loop"
        self.assertEqual(run_git(repo, "worktree", "add", "-q", "-b", branch, worktree).returncode, 0)
        return worktree

    def reset(self, project_id, name):
        return self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {"confirm": name})

    def preview(self, project_id):
        return self.request("GET", "/api/app/projects/%s/reset-specs/preview" % project_id)

    def test_preview_without_a_worktree_is_empty(self):
        _, project_id = self.build_project()
        status, body = self.preview(project_id)
        self.assertEqual((status, body), (200, {"ok": True, "worktrees": [], "skipped": []}))

    def test_preview_tells_path_branch_and_unpushed_commits(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo)
        commit_in(worktree, "feature.txt")
        status, body = self.preview(project_id)
        self.assertEqual(status, 200, body)
        self.assertEqual(len(body["worktrees"]), 1)
        item = body["worktrees"][0]
        self.assertEqual((item["repo"], item["primary"], item["branch"]), ("wt", True, LOOP_BRANCH))
        self.assertEqual(os.path.normcase(item["path"]), os.path.normcase(os.path.normpath(worktree)))
        self.assertEqual(item["unpushed_commits"], 1)
        self.assertTrue(os.path.isdir(worktree), "a preview deletes nothing")

    def test_preview_unpushed_commits_follow_the_upstream_and_are_unknown_without_a_reference(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo)
        remote = os.path.join(self.temporary_dir, "remote.git")
        run_git(self.temporary_dir, "init", "-q", "--bare", remote)
        run_git(repo, "remote", "add", "origin", remote)
        commit_in(worktree, "feature.txt")
        self.assertEqual(run_git(worktree, "push", "-q", "-u", "origin", LOOP_BRANCH).returncode, 0)
        self.assertEqual(self.preview(project_id)[1]["worktrees"][0]["unpushed_commits"], 0)
        commit_in(worktree, "more.txt", message="more")
        self.assertEqual(self.preview(project_id)[1]["worktrees"][0]["unpushed_commits"], 1)
        run_git(worktree, "branch", "-q", "--unset-upstream")
        run_git(repo, "branch", "-q", "-m", "trunk")  # no origin/<base> nor main/master/developer to compare with
        run_git(repo, "remote", "remove", "origin")
        self.assertIsNone(self.preview(project_id)[1]["worktrees"][0]["unpushed_commits"])

    def test_reset_deletes_the_worktree_and_its_loop_branch_and_keeps_the_primary(self):
        repo, project_id = self.build_project()
        primary_branch = run_git(repo, "branch", "--show-current").stdout.strip()
        worktree = self.add_loop_worktree(repo)
        commit_in(worktree, "feature.txt")  # never merged, never pushed: it is lost on purpose
        write_file(os.path.join(worktree, "uncommitted.txt"), "dirty\n")
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["worktree_failures"], [])
        self.assertEqual(len(body["worktrees_removed"]), 1)
        removed = body["worktrees_removed"][0]
        self.assertEqual((removed["repo"], removed["branch"], removed["branch_deleted"]), ("wt", LOOP_BRANCH, True))
        self.assertEqual(os.path.normcase(removed["path"]), os.path.normcase(os.path.normpath(worktree)))
        self.assertFalse(os.path.exists(worktree))
        self.assertNotIn(LOOP_BRANCH, list_branches(repo))
        self.assertNotIn(worktree.replace("\\", "/").lower(), run_git(repo, "worktree", "list", "--porcelain").stdout.replace("\\", "/").lower())
        self.assertEqual(run_git(repo, "branch", "--show-current").stdout.strip(), primary_branch)
        self.assertTrue(os.path.isfile(os.path.join(repo, "README.md")))
        self.assertIsNotNone(body["trash"])
        self.assertEqual(self.preview(project_id)[1]["worktrees"], [])

    def test_reset_after_a_loop_ran_resets_the_primary_specs_and_deletes_the_worktree(self):
        repo, project_id = self.create_ready_project(name="ran")  # approved roadmap in the primary
        worktree = self.add_loop_worktree(repo)
        write_file(os.path.join(worktree, ".specs", "project", "ROADMAP.md"), "# the loop's own copy\n")
        write_file(loop_file_path(worktree, "json", "LOOP.json"), json.dumps({"status": "continue", "turn": 1}))
        context = self.module.project_service.get_project_context(project_id)
        self.assertEqual(os.path.normcase(context.work_dir), os.path.normcase(worktree), "the loop worktree is the work dir")
        status, body = self.reset(project_id, "ran")
        self.assertEqual(status, 200, body)
        self.assertIn(".specs/project/ROADMAP.md", body["moved"])
        self.assertIn(".specs/project/loop/json/ROADMAP-APPROVED.json", body["moved"], "what is moved is the primary's state")
        trash = os.path.join(repo, *body["trash"].split("/"))
        self.assertTrue(os.path.isfile(os.path.join(trash, "project", "ROADMAP.md")))
        self.assertFalse(os.path.exists(worktree))
        self.assertIn(self.get_progress(project_id)["mode"], ("planning", "new"), "back to planning, not in a loop")
        context = self.module.project_service.get_project_context(project_id)
        self.assertEqual(os.path.normcase(context.work_dir), os.path.normcase(repo))

    def test_a_worktree_on_a_branch_that_is_not_a_loop_branch_is_never_touched(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo, branch="feature/mine")
        status, preview = self.preview(project_id)
        self.assertEqual(preview["worktrees"], [])
        self.assertEqual([item["repo"] for item in preview["skipped"]], ["wt"])
        self.assertIn("not a loop branch", preview["skipped"][0]["reason"])
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["worktrees_removed"], [])
        self.assertEqual(len(body["worktree_failures"]), 1)
        self.assertIn("feature/mine", list_branches(repo))
        self.assertTrue(os.path.isdir(worktree))
        self.assertIsNotNone(body["trash"], "the specs reset still happened")

    def test_a_plain_folder_at_the_worktree_path_is_never_deleted(self):
        repo, project_id = self.build_project()
        folder = repo + "-worktree-loop"
        write_file(os.path.join(folder, "precious.txt"), "mine\n")
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["worktrees_removed"], [])
        self.assertEqual(body["worktree_failures"][0]["error"], "not removed: the folder is not a git worktree of this repo")
        self.assertEqual(read_file(os.path.join(folder, "precious.txt")), "mine\n")

    def test_an_empty_folder_left_by_an_earlier_removal_is_deleted(self):
        repo, project_id = self.build_project()
        folder = repo + "-worktree-loop"
        os.makedirs(folder)  # what Windows leaves when a program held the folder open during an earlier `git worktree remove`
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["worktree_failures"], [])
        self.assertEqual([item["branch"] for item in body["worktrees_removed"]], [None])
        self.assertFalse(os.path.exists(folder))

    def test_the_branch_is_deleted_even_when_the_emptied_folder_comes_back(self):
        """Windows: `git worktree remove` empties the folder but cannot delete the folder itself. Git no longer registers the worktree,
        so the branch is deleted anyway and the empty folder is removed afterwards."""
        from harness_backend.infrastructure.git_client import GitClient
        from harness_backend.infrastructure.process_runner import ProcessRunner

        class FolderStaysRunner(ProcessRunner):
            def run_and_wait(self, argv, folder, timeout_seconds, input_text=None, extra_env=None, env=None):
                outcome = super().run_and_wait(argv, folder, timeout_seconds, input_text, extra_env, env)
                if "worktree" in argv and "remove" in argv:
                    os.makedirs(argv[-1], exist_ok=True)
                return outcome

        repo, _ = self.build_project()
        worktree = self.add_loop_worktree(repo)
        outcome = GitClient(FolderStaysRunner()).remove_worktree_and_branch(repo, worktree, LOOP_BRANCH)
        self.assertEqual((outcome["worktree_removed"], outcome["branch_deleted"], outcome["folder_left"], outcome["error"]), (True, True, False, ""))
        self.assertNotIn(LOOP_BRANCH, list_branches(repo))
        self.assertFalse(os.path.exists(worktree))

    def test_other_worktrees_and_branches_stay(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo)
        other = repo + "-review"
        run_git(repo, "worktree", "add", "-q", "-b", "loop/not-a-sibling-named-worktree-loop", other)
        run_git(repo, "branch", "feature/keep")
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(worktree))
        self.assertTrue(os.path.isdir(other))
        self.assertEqual(sorted(branch for branch in list_branches(repo) if branch != run_git(repo, "branch", "--show-current").stdout.strip()),
                         ["feature/keep", "loop/not-a-sibling-named-worktree-loop"])

    def test_multi_repo_removes_the_loop_worktree_of_every_repo(self):
        repo, project_id = self.build_project("multi")
        second = create_git_repo(os.path.join(self.projects_dir, "second"))
        write_file(os.path.join(repo, ".specs", "project", "REPOS.json"), json.dumps({"multi": True, "repos": [
            {"name": "multi", "path": repo, "primary": True}, {"name": "second", "path": second}]}))
        first_worktree = self.add_loop_worktree(repo)
        second_worktree = self.add_loop_worktree(second, branch="loop/2026-10-20-second")
        status, preview = self.preview(project_id)
        self.assertEqual(sorted(item["repo"] for item in preview["worktrees"]), ["multi", "second"])
        status, body = self.reset(project_id, "multi")
        self.assertEqual(status, 200, body)
        self.assertEqual(sorted(item["repo"] for item in body["worktrees_removed"]), ["multi", "second"])
        self.assertEqual(body["worktree_failures"], [])
        self.assertFalse(os.path.exists(first_worktree))
        self.assertFalse(os.path.exists(second_worktree))
        self.assertNotIn(LOOP_BRANCH, list_branches(repo))
        self.assertNotIn("loop/2026-10-20-second", list_branches(second))
        self.assertTrue(os.path.isfile(os.path.join(second, "README.md")))
        self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "project", "REPOS.json")), "the repo list is kept")

    def test_nothing_is_deleted_while_the_loop_runs(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo)
        write_file(loop_file_path(worktree, "json", "LOOP.json"), "{}")
        self.write_live_loop_lock(worktree)
        status, body = self.reset(project_id, "wt")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "loop_running")
        self.assertTrue(os.path.isdir(worktree))
        self.assertIn(LOOP_BRANCH, list_branches(repo))

    def test_nothing_is_deleted_without_the_exact_project_name(self):
        repo, project_id = self.build_project()
        worktree = self.add_loop_worktree(repo)
        self.assertEqual(self.reset(project_id, "other")[0], 400)
        self.assertTrue(os.path.isdir(worktree))
        self.assertIn(LOOP_BRANCH, list_branches(repo))

    def test_a_project_without_specs_still_deletes_the_worktree(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "nospecs"))
        project_id = self.open_project(repo)
        worktree = self.add_loop_worktree(repo)
        status, body = self.reset(project_id, "nospecs")
        self.assertEqual((status, body["moved"], body["trash"]), (200, [], None), body)
        self.assertEqual(len(body["worktrees_removed"]), 1)
        self.assertFalse(os.path.exists(worktree))

    def test_preview_of_an_unknown_project_is_404(self):
        self.assertEqual(self.preview("0" * 12)[0], 404)


if __name__ == "__main__":
    unittest.main()
