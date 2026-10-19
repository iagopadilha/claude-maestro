"""Automatic worktree preparation: setup detected from manifests, local config files copied when REPOS.json has no `copy` key (absent =
automatic, `[]` = none), and the --check problem for a secondary repo without a Conformance line."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import detect_setup  # noqa: E402
import project_repos  # noqa: E402
import run_loop  # noqa: E402


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def delete_tree(path):
    import stat

    def clear_read_only(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=clear_read_only)


def make_repo(folder, files):
    os.makedirs(folder)
    for name, text in files.items():
        write(os.path.join(folder, name), text)
    for arguments in (["init", "-q"], ["add", "-A"], ["-c", "user.name=t", "-c", "user.email=t@l", "commit", "-q", "-m", "start"], ["branch", "-M", "main"]):
        subprocess.run(["git", "-C", folder] + arguments, capture_output=True, check=True)


class TestDetectSetup(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="detect-")
        self.addCleanup(delete_tree, self.folder)

    def detect_with(self, *names):
        for name in names:
            write(os.path.join(self.folder, name))
        return detect_setup.detect_setup(self.folder)

    def test_each_manifest_gives_its_command(self):
        cases = [(("pnpm-lock.yaml", "package.json"), "pnpm install --frozen-lockfile"), (("yarn.lock", "package.json"), "yarn install --frozen-lockfile"),
                 (("package-lock.json", "package.json"), "npm ci"), (("package.json",), "npm install"),
                 (("App.sln",), "dotnet restore"), (("src/Api.csproj",), "dotnet restore"),
                 (("requirements.txt",), "python -m pip install -r requirements.txt"), (("poetry.lock",), "poetry install"),
                 (("go.mod",), "go mod download")]
        for names, expected in cases:
            with self.subTest(names=names):
                self.setUp()
                self.assertEqual(self.detect_with(*names), expected)

    def test_nothing_recognised_or_missing_folder_is_none(self):
        self.assertIsNone(self.detect_with("README.md"))
        self.assertIsNone(detect_setup.detect_setup(os.path.join(self.folder, "missing")))

    def test_first_match_wins(self):
        self.assertEqual(self.detect_with("package-lock.json", "package.json", "go.mod"), "npm ci")


class TestAutoPrepareOfAnExtraRepo(unittest.TestCase):
    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="auto-prepare-"))
        self.addCleanup(delete_tree, self.root)
        self.api = os.path.join(self.root, "api")
        make_repo(self.api, {".gitignore": ".env\n.env.example\n*.local\nnode_modules/\n", "a.txt": "a\n", "go.mod": "module x\n"})
        write(os.path.join(self.api, ".env"), "SECRET=1\n")
        write(os.path.join(self.api, "settings.local"), "x\n")
        write(os.path.join(self.api, ".env.example"), "SECRET=\n")
        self.opts = types.SimpleNamespace(branch="loop/auto", dry_run=False, setup="", repo_path=self.root, push=False)

    def build(self, **fields):
        return project_repos.RepoRef(name="api", path=self.api, **fields)

    def run_extra(self, item):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), mock.patch.object(detect_setup, "detect_setup", return_value="echo auto"):
            entry = run_loop.ensure_extra_worktree(item, self.opts, "loop/auto", {})
        return entry, output.getvalue()

    def test_never_configured_copies_suggested_files_and_runs_detected_setup(self):
        entry, text = self.run_extra(self.build())
        self.assertEqual(sorted(entry["copied"]), [".env", "settings.local"])
        self.assertTrue(os.path.isfile(os.path.join(self.api + "-worktree-loop", ".env")))
        self.assertFalse(os.path.exists(os.path.join(self.api + "-worktree-loop", ".env.example")))
        self.assertIn("[api] setup: echo auto (auto-detected)", text)

    def test_configured_empty_copy_copies_nothing_and_stored_setup_wins(self):
        entry, text = self.run_extra(self.build(copy_configured=True, setup="echo stored"))
        self.assertEqual(entry["copied"], {})
        self.assertIn("[api] setup: echo stored", text)
        self.assertNotIn("auto-detected", text)

    def test_stored_copy_list_is_used_as_is(self):
        entry, _ = self.run_extra(self.build(copy=[".env"], copy_configured=True))
        self.assertEqual(list(entry["copied"]), [".env"])

    def test_absent_and_empty_copy_survive_a_save_and_a_load(self):
        primary = os.path.join(self.root, "primary")
        os.makedirs(primary)
        project_repos.save_repos(primary, True, [project_repos.RepoRef(name="primary", path=primary, primary=True),
                                                 self.build(), project_repos.RepoRef(name="web", path=os.path.join(self.root, "web"), copy_configured=True)])
        with open(project_repos.repos_file_path(primary), encoding="utf-8") as handle:
            items = {item["name"]: item for item in json.load(handle)["repos"]}
        self.assertNotIn("copy", items["api"])
        self.assertEqual(items["web"]["copy"], [])
        loaded = {repo.name: repo for repo in project_repos.load_repos(primary)}
        self.assertFalse(loaded["api"].copy_configured)
        self.assertTrue(loaded["web"].copy_configured)

    def test_check_flags_an_extra_repo_without_conformance(self):
        problems, _ = run_loop.extra_repos_preflight([self.build()], self.opts)
        self.assertTrue(any(item.startswith("repo api: its CLAUDE.md has no `Conformance: STANDARD|LEGACY`") for item in problems), problems)
        write(os.path.join(self.api, "CLAUDE.md"), "Conformance: LEGACY\n")
        problems, _ = run_loop.extra_repos_preflight([self.build()], self.opts)
        self.assertFalse([item for item in problems if "Conformance" in item])


if __name__ == "__main__":
    unittest.main()
