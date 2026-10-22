"""Regression for the C1 refactor (named steps): the extracted gate builders keep the contract ids and order."""
import unittest

from harness_backend.services import planning_gates


def single_project_facts(**overrides):
    facts = {
        "is_git": True, "structure": {"ok": True, "missing": []}, "conformance": "STANDARD", "has_code": True,
        "map_files": ["STACK.md"], "map_folder": "docs/codebase", "map_commit": "abc1234", "running_job": None,
        "repos_unready": [], "project_text": "Vision: something", "features": {"a": {}}, "roadmap_exists": True,
        "roadmap_parse_errors": [], "roadmap_field_errors": [], "open_questions": [], "flows_exists": True,
        "flow_coverage": {"missing": [], "orphans": []}, "environment_all_problems": [], "regression_required": False,
    }
    facts.update(overrides)
    return facts


class PlanningGatesOrderTest(unittest.TestCase):
    def test_single_project_gate_ids_keep_the_contract_order(self):
        ids = [gate["id"] for gate in planning_gates.build_planning_gates(single_project_facts())]
        self.assertEqual(ids, ["git", "structure", "conformance", "map", "goal", "features", "fields", "open_questions", "flows", "test_environment"])

    def test_code_prerequisites_are_the_first_four_gates(self):
        facts = single_project_facts()
        prerequisites = planning_gates.build_code_prerequisite_gates(facts)
        self.assertEqual([gate["id"] for gate in prerequisites], [gate_id for gate_id in planning_gates.PREREQUISITE_GATES if gate_id != planning_gates.GATE_SUBPROJECTS])
        self.assertEqual(prerequisites, planning_gates.build_planning_gates(facts)[:4])

    def test_repos_gate_comes_right_after_the_map_and_readiness_is_last(self):
        facts = single_project_facts(repos_unready=[{"detail": "api: no map"}], readiness={"state": "ready", "at": "now"})
        ids = [gate["id"] for gate in planning_gates.build_planning_gates(facts)]
        self.assertEqual(ids[:5], ["git", "structure", "conformance", "map", "repos"])
        self.assertEqual(ids[-1], "ready")


if __name__ == "__main__":
    unittest.main()
