"""last-run.json `base` is the run's base recorded in meta.json, not the branch delivery.json happened to record."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from multi_repo_world import MultiRepoWorldTestCase, delivery, loop_state  # noqa: E402
import close_loop  # noqa: E402


class RunBaseTestCase(MultiRepoWorldTestCase):
    def prepare(self, meta_base):
        self.install_gh_shim()
        repos = {name: dict(item, base=meta_base) for name, item in self.meta["repos"].items()}
        self.write_meta(dict(self.meta, repos=repos))
        delivery.finalize_project(self.primary_work, self.read_json(loop_state.loop_path(self.primary_work, "meta.json")))
        for name in ("app", "api"):  # delivery.json recorded the branch the repo was on, not the run's base
            path = loop_state.loop_path(self.worktrees[name], "delivery.json")
            record = self.read_json(path)
            record["base"] = "developer"
            close_loop.save_json(path, record)

    def close_and_read_bases(self):
        close_loop.close(self.primary, output_dir=os.path.join(self.root, "backups"))
        return {name: item["base"] for name, item in self.read_json(close_loop.last_run_path(self.primary))["repos"].items()}


class TestRunBase(RunBaseTestCase):
    def test_last_run_uses_the_base_of_meta(self):
        self.prepare("main")
        self.assertEqual(self.close_and_read_bases(), {"app": "main", "api": "main"})

    def test_last_run_falls_back_to_the_delivery_base_when_meta_has_none(self):
        self.prepare("")
        self.assertEqual(self.close_and_read_bases(), {"app": "developer", "api": "developer"})


if __name__ == "__main__":
    unittest.main()
