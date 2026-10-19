"""A multi-repo project on disk for the delivery / close loop tests: two real git repos (`app` primary, `api`), a LOCAL bare remote for
each, a loop worktree per repo on a `loop/*` branch with one commit ahead, ROADMAP in the primary worktree, REPOS.json and meta.json.

`gh` is a tiny Python script (`GH_SHIM`) that records its argv and body files and prints a fake PR url; `install_gh_shim` makes the
engine call it instead of a real `gh` (no network, no GitHub). Nothing here touches the user's git config or ~/.loop-harness.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import delivery  # noqa: E402
import loop_state  # noqa: E402
import project_repos  # noqa: E402

GH_SHIM = r'''
import json, os, sys
folder = os.environ["GH_SHIM_DIR"]
argv = sys.argv[1:]
calls_path = os.path.join(folder, "calls.jsonl")
record = {"argv": argv, "cwd": os.getcwd()}
if "--body-file" in argv:
    with open(argv[argv.index("--body-file") + 1], encoding="utf-8") as handle:
        record["body"] = handle.read()
previous = open(calls_path, encoding="utf-8").read().splitlines() if os.path.exists(calls_path) else []
with open(calls_path, "a", encoding="utf-8") as handle:
    handle.write(json.dumps(record) + "\n")
if argv[:2] == ["pr", "create"]:
    created = len([line for line in previous if json.loads(line)["argv"][:2] == ["pr", "create"]])
    print("https://example.test/pull/%d" % (created + 1))
sys.exit(0)
'''
ENVIRONMENT_VARIABLES = ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH", "LOOP_PR_MODE", "LOOP_PR_DRAFT", "LOOP_GIT_USER", "LOOP_HEADLESS")
ROADMAP_DONE = "# Roadmap\n\n## Stage 1 - Base\n\n- [feature-one] status=verified scope=Small kind=back paths=src\n"
IDENTITY_ARGUMENTS = ("-c", "user.name=test", "-c", "user.email=test@localhost")


def git(folder, *args, check=True):
    result = subprocess.run(["git", "-C", folder, *IDENTITY_ARGUMENTS, *args], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and result.returncode != 0:
        raise AssertionError("git %s failed in %s: %s" % (" ".join(args), folder, result.stderr))
    return result.stdout.strip()


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def delete_tree(path):
    import stat

    def clear_read_only(function, target, _):
        os.chmod(target, stat.S_IWRITE)
        function(target)
    shutil.rmtree(path, onerror=clear_read_only)


class MultiRepoWorldTestCase(unittest.TestCase):
    REPO_NAMES = ("app", "api")

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix="harness-world-"))
        self.addCleanup(delete_tree, self.root)
        self.shim_dir = os.path.join(self.root, "_gh")
        os.makedirs(self.shim_dir)
        self.shim_path = os.path.join(self.shim_dir, "gh_shim.py")
        write(self.shim_path, GH_SHIM)
        patcher = mock.patch.dict(os.environ, {"GH_SHIM_DIR": self.shim_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ENVIRONMENT_VARIABLES:
            os.environ.pop(name, None)
        self.repos, self.remotes, self.worktrees = {}, {}, {}
        for name in self.REPO_NAMES:
            self.build_repo(name)
        self.primary = self.repos["app"]
        self.primary_work = self.worktrees["app"]
        write(os.path.join(self.primary_work, ".specs", "project", "ROADMAP.md"), ROADMAP_DONE)
        write(os.path.join(self.primary_work, ".specs", "project", "FLOWS.md"), "# Flows\n")
        project_repos.save_repos(self.primary, True, [project_repos.RepoRef(name="app", path=self.repos["app"], primary=True),
                                                      project_repos.RepoRef(name="api", path=self.repos["api"])])
        self.meta = {"repos": {name: {"worktree": self.worktrees[name], "branch": "loop/" + name, "base": "developer", "primary": name == "app"}
                               for name in self.REPO_NAMES}}

    # ------------------------------------------------ building
    def build_repo(self, name):
        repo = os.path.join(self.root, name)
        remote = os.path.join(self.root, "remotes", name + ".git")
        os.makedirs(repo)
        os.makedirs(remote)
        git(remote, "init", "-q", "--bare")
        git(repo, "init", "-q")
        git(repo, "config", "user.name", "test")
        git(repo, "config", "user.email", "test@localhost")
        write(os.path.join(repo, "README.md"), "# %s\n" % name)
        write(os.path.join(repo, ".gitignore"), ".specs/\n")  # plan state is not part of the code (and does not block a worktree removal)
        git(repo, "add", "-A")
        git(repo, "commit", "-q", "-m", "start")
        git(repo, "branch", "-M", "developer")
        git(repo, "remote", "add", "origin", remote)
        git(repo, "push", "-q", "-u", "origin", "developer")
        worktree = repo + "-worktree-loop"
        git(repo, "worktree", "add", "-q", "-b", "loop/" + name, worktree)
        write(os.path.join(worktree, "feature.txt"), "work of %s\n" % name)
        git(worktree, "add", "-A")
        git(worktree, "commit", "-q", "-m", "work of " + name)
        self.repos[name], self.remotes[name], self.worktrees[name] = repo, remote, worktree

    def install_gh_shim(self):
        """The engine's `gh` calls run the recording shim; `shutil.which('gh')` finds it. Restored after the test."""
        original_run, original_which = delivery.run, shutil.which
        shim_python, shim_path = sys.executable, self.shim_path

        def run_with_shim(args, cwd=None, timeout=120):
            if args and args[0] == "gh":
                args = [shim_python, shim_path] + list(args[1:])
            return original_run(args, cwd=cwd, timeout=timeout)

        def which_with_shim(name, *rest, **named):
            return shim_path if name == "gh" else original_which(name, *rest, **named)

        for patcher in (mock.patch.object(delivery, "run", run_with_shim), mock.patch.object(shutil, "which", which_with_shim)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def read_gh_calls(self):
        path = os.path.join(self.shim_dir, "calls.jsonl")
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def write_meta(self, meta=None):
        write(loop_state.loop_path(self.primary_work, "meta.json"), json.dumps(meta or self.meta))

    def read_json(self, path):
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
