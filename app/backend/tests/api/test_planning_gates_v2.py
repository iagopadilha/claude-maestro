"""T14 (base_branch_resolved + max_parallel_tasks in the AI text) and T34 (the import_coverage gate) of services/planning_gates.py."""
import os
import unittest
from unittest import mock

from harness_backend.domain.roadmap_settings import parse_loop_settings
from harness_backend.services import loop_worktree_service, planning_gates
from harness_backend.services.planning_gates import (build_import_coverage_gates, describe_loop_settings_for_ai, resolve_base_branch)

ENV_VARIABLES = ("LOOP_PR_BASE", "LOOP_INTEGRATION_BRANCH")


class BaseBranchResolvedTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, {}, clear=False)
        patcher.start()
        self.addCleanup(patcher.stop)
        for name in ENV_VARIABLES:
            os.environ.pop(name, None)
        no_app_env = mock.patch.object(loop_worktree_service, "read_env_file_value", lambda name: "")  # never read the real app .env
        no_app_env.start()
        self.addCleanup(no_app_env.stop)

    def resolve(self, roadmap_text):
        return resolve_base_branch(parse_loop_settings(roadmap_text))

    def test_without_a_line_or_variable_it_is_developer_default(self):
        self.assertEqual(self.resolve(""), {"name": "developer", "source": "default"})

    def test_the_roadmap_line_wins(self):
        os.environ["LOOP_INTEGRATION_BRANCH"] = "release/1"
        self.assertEqual(self.resolve("**Branch base:** main\n"), {"name": "main", "source": "roadmap"})

    def test_only_the_environment_variable_reports_env(self):
        os.environ["LOOP_INTEGRATION_BRANCH"] = "staging"
        self.assertEqual(self.resolve(""), {"name": "staging", "source": "env"})

    def test_an_invalid_roadmap_line_falls_back_to_the_environment_then_default(self):
        self.assertEqual(self.resolve("**Branch base:** a..b\n"), {"name": "developer", "source": "default"})
        os.environ["LOOP_PR_BASE"] = "qa"
        self.assertEqual(self.resolve("**Branch base:** a..b\n"), {"name": "qa", "source": "env"})

    def test_the_ai_text_names_both_lines(self):
        text = "\n".join(describe_loop_settings_for_ai(parse_loop_settings("**Branch base:** main\n**Max parallel tasks:** 4\n")))
        self.assertIn("base_branch_resolved: main (source: roadmap)", text)
        self.assertIn("Max parallel tasks: 4", text)
        self.assertIn("executors per wave inside one feature", text)
        self.assertIn("LOOP_INTEGRATION_BRANCH", text)


def facts(source_items, coverage_text, sent=True):
    return {"imports": [{"import_id": "20261021-101010-orig", "sent_at": "2026-10-21T10:11:00" if sent else None, "items": source_items}],
            "import_coverage_text": coverage_text}


ITEMS = {"requirements": ["ORIG-01", "ORIG-02", "ORIG-03"], "decisions": ["D-1"], "tasks": ["T1"], "sections": {}}
FULL = ("## orig\n\n| Item | Kind | Destination |\n| --- | --- | --- |\n| ORIG-01..03 | requirement | ROADMAP:[orig] reqs |\n"
        "| D-1 | decision | RULES.md#d1 |\n| T1 | task | ROADMAP:[orig] |\n")


class ImportCoverageGateTests(unittest.TestCase):
    def gate(self, fact_set):
        gates = build_import_coverage_gates(fact_set)
        return gates[0] if gates else None

    def test_no_import_or_an_unsent_import_means_no_gate(self):
        self.assertEqual(build_import_coverage_gates({}), [])
        self.assertEqual(build_import_coverage_gates(facts(ITEMS, "", sent=False)), [])

    def test_a_sent_import_without_the_coverage_file_fails_the_gate(self):
        gate = self.gate(facts(ITEMS, ""))
        self.assertFalse(gate["ok"])
        self.assertFalse(gate.get("optional"))
        self.assertEqual(gate["id"], "import_coverage")
        self.assertIn("IMPORT-COVERAGE.md missing", gate["detail"])

    def test_a_full_table_passes(self):
        gate = self.gate(facts(ITEMS, FULL))
        self.assertTrue(gate["ok"], gate)
        self.assertEqual(gate["detail"], "5 covered, 0 dropped, 0 missing")

    def test_deleting_one_row_fails_and_names_that_id(self):
        gate = self.gate(facts(ITEMS, FULL.replace("| D-1 | decision | RULES.md#d1 |\n", "")))
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["items"], ["missing: D-1"])

    def test_dropped_rows_are_listed_with_their_reason_and_do_not_block(self):
        gate = self.gate(facts(ITEMS, FULL.replace("RULES.md#d1", "dropped: replaced by D-9")))
        self.assertTrue(gate["ok"])
        self.assertEqual(len(gate["items"]), 1)
        self.assertIn("D-1", gate["items"][0])
        self.assertIn("replaced by D-9", gate["items"][0])

    def test_without_ids_the_coverage_is_by_section(self):
        items = {"requirements": [], "decisions": [], "tasks": [], "sections": {"Goals": True, "Edge Cases": True, "Out of Scope": False}}
        text = "## orig\n\n| Item | Kind | Destination |\n| --- | --- | --- |\n| Goals | goal | PROJECT.md |\n"
        gate = self.gate(facts(items, text))
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["items"], ["missing: Edge Cases"])

    def test_the_gate_is_part_of_the_planning_gates_in_order(self):
        self.assertEqual(planning_gates.GATE_IMPORT_COVERAGE, "import_coverage")


if __name__ == "__main__":
    unittest.main()
