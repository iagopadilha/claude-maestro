"""The loop worktree (`../<repo>-worktree-loop`) reads the list stored under the repository name."""
import json
import os
import shutil
import sys
import tempfile
import unittest

ENGINE = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "harness_backend", "engine")
sys.path.insert(0, ENGINE)
import subprojects  # noqa: E402


class WorktreeKeyTests(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.worktree = os.path.join(self.base, "shop-worktree-loop")
        os.makedirs(os.path.join(self.worktree, ".specs", "project"))
        entry = {"monorepo": True, "confirmed": True, "projects": [{"path": "backend", "name": "backend", "stack": "dotnet",
                                                                    "skill": "backend-dev", "skip": False, "has_code": True}]}
        with open(os.path.join(self.worktree, ".specs", "project", "SUBPROJECTS.json"), "w", encoding="utf-8") as handle:
            json.dump({"version": 1, "repos": {"shop": entry}}, handle)

    def tearDown(self):
        shutil.rmtree(self.base, ignore_errors=True)

    def test_default_name_strips_the_worktree_suffix(self):
        self.assertEqual(subprojects.default_repo_name(self.worktree), "shop")
        self.assertEqual(subprojects.default_repo_name(os.path.join(self.base, "shop")), "shop")

    def test_worktree_reads_the_repository_entry(self):
        self.assertTrue(subprojects.monorepo_confirmed(self.worktree))


if __name__ == "__main__":
    unittest.main()
