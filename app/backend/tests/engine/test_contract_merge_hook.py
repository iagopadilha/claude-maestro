"""Contract merge hook of engine/run_loop.py (merge_verified_contracts): only a confirmed monorepo, one feature at a time,
result recorded in LOOP.json, conflict and missing-SYSTEM.md warnings, nothing at all for a single project. Temp folders only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import run_loop  # noqa: E402
import loop_state  # noqa: E402
import system_map  # noqa: E402

SYSTEM_TEXT = "# System\n\n<!-- CONTRACT:BEGIN -->\n<!-- CONTRACT:END -->\n\n## Run\n"
ROADMAP = "## Stage 1\n- [orders] status=verified kind=back paths=api\n- [pay] status=verified kind=back paths=api\n- [later] status=building kind=back paths=web\n"


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


class TestContractMergeHook(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="contract-hook-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.specs = os.path.join(self.root, ".specs", "project")
        write(os.path.join(self.specs, "ROADMAP.md"), ROADMAP)
        self.meta, self.state = {}, {"status": "continue", "turn": 1}

    def confirm_monorepo(self, confirmed=True, monorepo=True):
        write(os.path.join(self.root, ".specs", "project", "SUBPROJECTS.json"), json.dumps(
            {"version": 1, "repos": {os.path.basename(self.root): {"monorepo": monorepo, "confirmed": confirmed, "projects": [
                {"path": "api", "name": "api", "stack": "dotnet", "skill": "backend-dev", "skip": False}]}}}))

    def system(self, text=SYSTEM_TEXT):
        write(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), text)

    def contract(self, slug, text):
        write(system_map.contract_path(self.root, slug), text)

    def system_text(self):
        with open(os.path.join(self.root, system_map.SYSTEM_RELATIVE_PATH), encoding="utf-8") as handle:
            return handle.read()

    def run_hook(self):
        run_loop.merge_verified_contracts(self.root, self.meta, self.state, self.specs)
        with open(loop_state.state_file(self.root, "LOOP.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def test_verified_feature_updates_contract_and_records_changed(self):
        self.confirm_monorepo()
        self.system()
        self.contract("orders", "- [add] route GET /orders/{id} — get one order (Api/OrdersController.cs)\n")
        self.contract("later", "- [add] route GET /later — not verified yet (Api/L.cs)\n")
        saved = self.run_hook()
        self.assertIn("GET /orders/{id}", self.system_text())
        self.assertNotIn("/later", self.system_text())
        self.assertTrue(saved["contract_merge"]["orders"]["changed"])
        self.assertNotIn("later", saved["contract_merge"])
        self.assertTrue(self.meta["system_md_uncommitted"])
        self.assertEqual(self.meta["contract_merged_slugs"], ["orders", "pay"])

    def test_second_run_does_not_merge_again(self):
        self.confirm_monorepo()
        self.system()
        self.contract("orders", "- [add] route GET /orders — list (Api/O.cs)\n")
        self.run_hook()
        before = self.system_text()
        second_state = {"status": "continue", "turn": 2}
        run_loop.merge_verified_contracts(self.root, self.meta, second_state, self.specs)
        self.assertEqual(self.system_text(), before)
        self.assertNotIn("contract_merge", second_state)

    def test_conflict_warning_lands_in_status(self):
        self.confirm_monorepo()
        self.system()
        self.contract("orders", "- [add] route GET /items — list items (Api/A.cs)\n")
        self.contract("pay", "- [add] route GET /items — items for payments (Api/B.cs)\n")
        saved = self.run_hook()
        warnings = saved["contract_merge"]["pay"]["warnings"] + saved["contract_merge"]["orders"]["warnings"]
        self.assertTrue(warnings, saved)
        with open(os.path.join(self.specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md"), encoding="utf-8") as handle:
            self.assertIn("WARNING contract merge", handle.read())

    def test_contract_without_a_grammar_line_warns_in_status_validate_and_decisions(self):
        self.confirm_monorepo()
        self.system()
        self.contract("orders", "| Action | Route |\n|---|---|\n| add | GET /orders |\n")  # a table, not the grammar
        saved = self.run_hook()
        warning = "contract.md of orders has no line in the contract grammar: nothing merged"
        self.assertIn(warning, saved["contract_merge"]["orders"]["warnings"])
        self.assertFalse(saved["contract_merge"]["orders"]["changed"])
        with open(os.path.join(self.specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md"), encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("WARNING contract merge (orders): " + warning, text)
        self.assertIn("WARNING contract: " + warning, text)  # the decision line (important)

    def test_non_monorepo_is_untouched(self):
        self.system()
        self.contract("orders", "- [add] route GET /orders — list (Api/O.cs)\n")
        before = self.system_text()
        run_loop.merge_verified_contracts(self.root, self.meta, self.state, self.specs)
        self.assertEqual(self.system_text(), before)
        self.assertEqual(self.meta, {})
        self.assertFalse(os.path.exists(loop_state.state_file(self.root, "LOOP.json")))
        self.confirm_monorepo(confirmed=False)
        run_loop.merge_verified_contracts(self.root, self.meta, self.state, self.specs)
        self.assertEqual(self.meta, {})

    def test_missing_system_md_warns_and_keeps_deltas(self):
        self.confirm_monorepo()
        self.contract("orders", "- [add] route GET /orders — list (Api/O.cs)\n")
        saved = self.run_hook()
        self.assertFalse(saved["contract_merge"]["orders"]["changed"])
        self.assertIn("SYSTEM.md is missing", saved["contract_merge"]["orders"]["warnings"][0])
        self.assertTrue(os.path.isfile(system_map.contract_path(self.root, "orders")))
        self.assertEqual(system_map.pending_slugs(self.root, ["orders"]), ["orders"])

    def test_done_commits_system_md_itself(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        for key, value in (("user.name", "t"), ("user.email", "t@t")):
            subprocess.run(["git", "-C", self.root, "config", key, value], check=True)
        self.confirm_monorepo()
        self.system()
        subprocess.run(["git", "-C", self.root, "add", "-A"], check=True)
        subprocess.run(["git", "-C", self.root, "commit", "-qm", "base"], check=True)
        write(os.path.join(self.specs, "ROADMAP.md"), ROADMAP.replace("building", "verified"))
        self.contract("orders", "- [add] route GET /orders — list (Api/O.cs)\n")
        self.state = {"status": "done", "turn": 3}
        self.run_hook()
        status = subprocess.run(["git", "-C", self.root, "status", "--porcelain", "--", "docs"], capture_output=True, text=True).stdout
        self.assertEqual(status.strip(), "")
        self.assertFalse(self.meta["system_md_uncommitted"])

    def test_pending_system_md_is_not_a_dirty_leftover(self):
        subprocess.run(["git", "init", "-q", self.root], check=True)
        self.system()
        self.meta = {"system_md_uncommitted": True}
        self.assertEqual(run_loop.dirty_worktrees(self.root, self.meta), [])
        self.meta = {}
        self.assertTrue(run_loop.dirty_worktrees(self.root, self.meta))

    def test_backend_feature_verified_without_contract_warns_in_validate_and_status(self):
        self.confirm_monorepo()
        self.system()
        self.contract("orders", "- [add] route GET /orders - list (Api/O.cs)\n")
        saved = self.run_hook()
        self.assertEqual(saved["contract_merge"]["orders"]["warnings"], [])
        missing = saved["contract_merge"]["pay"]["warnings"]
        self.assertEqual(len(missing), 1, saved)
        self.assertIn("without .specs/features/pay/contract.md", missing[0])
        with open(os.path.join(self.specs, loop_state.LOOP_FOLDER_NAME, "VALIDATE.md"), encoding="utf-8") as handle:
            validate = handle.read()
        self.assertIn("[pay]", validate)
        self.assertIn("WARNING contract", validate)
        self.assertIn("level=important", validate)
        self.assertNotIn("[orders]", validate)

    def test_front_feature_or_single_project_never_warns_about_a_missing_contract(self):
        write(os.path.join(self.specs, "ROADMAP.md"), "## Stage 1\n- [screen] status=verified kind=front paths=api\n")
        self.confirm_monorepo()
        self.system()
        self.assertEqual(self.run_hook()["contract_merge"]["screen"]["warnings"], [])
        write(os.path.join(self.specs, "ROADMAP.md"), ROADMAP)
        self.meta, self.state = {}, {"status": "continue", "turn": 1}
        self.confirm_monorepo(confirmed=False)
        run_loop.merge_verified_contracts(self.root, self.meta, self.state, self.specs)
        self.assertEqual(self.meta, {})


if __name__ == "__main__":
    unittest.main()
