"""GET loop/settings `workspace.reattach_branch`: the branch the next Start would re-attach to (None after a finished run)."""
import json
import os
import sys
import tempfile
import types
import unittest

ENGINE_TESTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "engine")
sys.path.insert(0, ENGINE_TESTS_DIR)
from multi_repo_world import delete_tree, git, loop_state, write  # noqa: E402

from harness_backend.services.loop_control_service import LoopControlService  # noqa: E402


class ReattachBranchTests(unittest.TestCase):
    def setUp(self):
        self.repo = os.path.join(os.path.realpath(tempfile.mkdtemp(prefix="reattach-settings-")), "app")
        self.addCleanup(delete_tree, os.path.dirname(self.repo))
        os.makedirs(self.repo)
        git(self.repo, "init", "-q")
        write(os.path.join(self.repo, "README.md"), "x\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "branch", "loop/old")
        self.context = types.SimpleNamespace(repo_path=self.repo)

    def record(self, **fields):
        write(loop_state.loop_path(self.repo, "last-run.json"), json.dumps({"repos": {"app": dict({"branch": "loop/old"}, **fields)}}))

    def test_none_without_a_record(self):
        self.assertIsNone(LoopControlService.find_reattach_branch(self.context))

    def test_the_recorded_branch_when_not_finished(self):
        self.record()
        self.assertEqual(LoopControlService.find_reattach_branch(self.context), "loop/old")

    def test_none_when_finished(self):
        self.record(finished=True)
        self.assertIsNone(LoopControlService.find_reattach_branch(self.context))

    def test_none_when_the_branch_is_gone(self):
        self.record()
        git(self.repo, "branch", "-D", "loop/old")
        self.assertIsNone(LoopControlService.find_reattach_branch(self.context))


if __name__ == "__main__":
    unittest.main()
