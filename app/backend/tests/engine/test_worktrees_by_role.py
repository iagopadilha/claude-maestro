"""D4: worktrees only for the primary and the `edit` repos a ROADMAP feature changes; `reference` repos are read through `--add-dir`,
never get a worktree, and the close step ignores them. Real git repos in a temporary folder."""
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine_env  # noqa: E402,F401
import close_loop  # noqa: E402
import project_repos  # noqa: E402
import run_loop  # noqa: E402


def sh(args, cwd):
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", check=True)


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class WorldCase(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="by-role-"))
        self.addCleanup(self.clean)
        self.repos = {}
        for name in ("app", "api", "docs", "idle"):
            self.build(name)

    def clean(self):
        import stat

        def writable(function, target, _):
            os.chmod(target, stat.S_IWRITE)
            function(target)
        shutil.rmtree(self.root, onerror=writable)

    def build(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path)
        sh(["git", "init", "-q", "-b", "main"], path)
        for key, value in (("user.name", "t"), ("user.email", "t@t.t"), ("commit.gpgsign", "false")):
            sh(["git", "config", key, value], path)
        write(os.path.join(path, "README.md"), name + "\n")
        write(os.path.join(path, ".gitignore"), ".specs/\n")
        sh(["git", "add", "-A"], path)
        sh(["git", "commit", "-q", "-m", "base"], path)
        self.repos[name] = path

    def configure(self, roles, roadmap_paths):
        primary = self.repos["app"]
        entries = [project_repos.RepoRef(name="app", path=primary, primary=True)]
        entries += [project_repos.RepoRef(name=name, path=self.repos[name], role=role) for name, role in roles.items()]
        project_repos.save_repos(primary, True, entries)
        write(os.path.join(primary, ".specs", "project", "ROADMAP.md"),
              "# Roadmap\n## Stage 1 - Base\n- [one] status=approved scope=Small kind=back paths=%s\n" % roadmap_paths)

    def opts(self, **extra):
        values = dict(setup="", branch="", dry_run=False, only_repos=None, repo_path=self.repos["app"])
        values.update(extra)
        return types.SimpleNamespace(**values)

    def worktree(self, name):
        return self.repos[name] + "-worktree-loop"


class TestEnsureWorktreesByRole(WorldCase):
    def test_only_the_primary_and_the_edit_repos_a_feature_names_get_a_worktree(self):
        self.configure({"api": "edit", "docs": "reference", "idle": "edit"}, "src,api:handlers")
        work, meta = run_loop.ensure_worktrees(self.repos["app"], self.opts())
        self.assertEqual(work, self.worktree("app"))
        self.assertTrue(os.path.isdir(self.worktree("api")))
        self.assertFalse(os.path.exists(self.worktree("docs")), "reference repos never get a worktree")
        self.assertFalse(os.path.exists(self.worktree("idle")), "an edit repo nothing changes gets none")
        self.assertEqual(sorted(meta["repos"]), ["api", "app"])

    def test_a_feature_with_no_other_repo_keeps_the_single_repo_meta(self):
        self.configure({"api": "edit", "docs": "reference"}, "src")
        _, meta = run_loop.ensure_worktrees(self.repos["app"], self.opts())
        self.assertNotIn("repos", meta)
        self.assertFalse(os.path.exists(self.worktree("api")))

    def test_explicit_repos_flag_overrides_the_roadmap_filter_but_never_a_reference(self):
        self.configure({"api": "edit", "docs": "reference"}, "src")
        run_loop.ensure_worktrees(self.repos["app"], self.opts(only_repos=["api", "docs"]))
        self.assertTrue(os.path.isdir(self.worktree("api")))
        self.assertFalse(os.path.exists(self.worktree("docs")))

    def test_worktree_repos_helper(self):
        self.configure({"api": "edit", "docs": "reference"}, "api:x,docs:y")
        repos = project_repos.load_repos(self.repos["app"])
        features = run_loop.roadmap_features(self.repos["app"])
        self.assertEqual([item.name for item in run_loop.worktree_repos(repos, features)], ["app", "api"])


class TestReferenceRepoInTheTurn(WorldCase):
    def test_claude_command_adds_the_reference_folders_as_add_dir(self):
        opts = types.SimpleNamespace(claude="claude", permission_mode="acceptEdits", budget=3, extra_worktrees=["/w/api"], reference_dirs=["/r/docs"])
        argv = run_loop.claude_command(opts, "go")
        self.assertIn("--add-dir=/w/api", argv)
        self.assertIn("--add-dir=/r/docs", argv)

    def test_the_start_check_does_not_ask_a_reference_repo_for_a_worktree_or_a_conformance_line(self):
        self.configure({"api": "edit", "docs": "reference"}, "src")
        repos = project_repos.load_repos(self.repos["app"])
        features = run_loop.roadmap_features(self.repos["app"])
        self.assertEqual(run_loop.extra_repos_preflight(repos, self.opts(push=False), features),
                         ([], ["reference repo docs has no codebase map: agents will read the raw code; map it from Workspace"]))
        os.rename(self.repos["docs"], self.repos["docs"] + "-gone")
        problems, warns = run_loop.extra_repos_preflight(repos, self.opts(push=False), features)
        self.assertEqual(problems, [])
        self.assertTrue(any("reference folder" in item for item in warns))


class TestCloseIgnoresReposWithoutAWorktree(WorldCase):
    def test_close_entries_list_only_repos_that_had_a_worktree(self):
        self.configure({"api": "edit", "docs": "reference", "idle": "edit"}, "src,api:handlers")
        run_loop.ensure_worktrees(self.repos["app"], self.opts())
        entries, _ = close_loop.project_entries(self.repos["app"])
        self.assertEqual([entry["name"] for entry in entries], ["app", "api"])


if __name__ == "__main__":
    unittest.main()
