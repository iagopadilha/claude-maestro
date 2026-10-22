"""The git-ignored code inventory (`internal/inventory/*.json`) and SUBPROJECTS.json are visible from the loop worktree
(monorepo-subprojects-map-and-conformance step 13), also when the inventory is made after the worktree exists or the worktree already ran."""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_worktree_refresh import TestRefreshUnusedWorktree, loop_state, read, run_loop, write  # noqa: E402

INVENTORY = os.path.join(".specs", "project", "loop", "internal", "inventory")


class TestInventorySync(TestRefreshUnusedWorktree):
    def test_inventory_made_after_the_worktree_is_copied_on_reuse_even_when_a_loop_ran(self):
        write(os.path.join(self.repo, INVENTORY, "_root.json"), "{\"files\": 1}")
        write(os.path.join(self.repo, INVENTORY, "notes.txt"), "not json")
        write(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"), "{\"version\": 1}")
        write(loop_state.loop_path(self.work, "meta.json"), json.dumps({"branch": "loop/refresh", "turns": 3}))  # a loop ran: no refresh
        run_loop.ensure_worktree(self.repo, self.opts)
        self.assertEqual(read(os.path.join(self.work, INVENTORY, "_root.json")), "{\"files\": 1}")
        self.assertFalse(os.path.exists(os.path.join(self.work, INVENTORY, "notes.txt")))
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "SUBPROJECTS.json")), "{\"version\": 1}")

    def test_a_newer_inventory_replaces_an_older_copy_and_an_existing_subprojects_file_is_kept(self):
        target = os.path.join(self.work, INVENTORY, "_root.json")
        write(target, "old")
        os.utime(target, (1, 1))
        write(os.path.join(self.repo, INVENTORY, "_root.json"), "new")
        write(os.path.join(self.work, ".specs", "project", "SUBPROJECTS.json"), "worktree copy")
        write(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"), "repo copy")
        copied = run_loop.sync_untracked_map_files(self.repo, self.work)
        self.assertEqual(read(target), "new")
        self.assertEqual(copied, [os.path.join(INVENTORY, "_root.json")])
        self.assertEqual(read(os.path.join(self.work, ".specs", "project", "SUBPROJECTS.json")), "worktree copy")
        self.assertEqual(run_loop.sync_untracked_map_files(self.repo, self.work), [])  # up to date: nothing more


del TestRefreshUnusedWorktree  # only the subclass runs here

if __name__ == "__main__":
    unittest.main()
