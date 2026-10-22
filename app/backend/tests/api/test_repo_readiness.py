"""Readiness guard of the extra repos (Conformance + map) and the planning gate "repos". Commit-and-push: test_workspace_commit_push_guard."""
import os

from harness_backend.domain.project_errors import InvalidRepoError, LoopRunningError, RepoConflictError
from harness_backend.repositories.loop_file_repository import LoopFileRepository
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.planning_gates import GATE_MAP, GATE_REPOS, build_planning_gates
from harness_backend.services.repo_readiness_service import RepoReadinessService
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.workspace_service import WorkspaceService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, run_git, write_file
from .test_workspace_service import FakeEngineAdapter, InlineJobService


class StubRoadmapService:
    def __init__(self):
        self.mapped = []

    def map_codebase(self, context, budget_usd=None):
        self.mapped.append(context.repo_path)
        return {"id": "j-map"}


class StubJobs:
    def __init__(self):
        self.running = {}

    def get_running_job(self, repo_path):
        return self.running.get(repo_path)


class TestRepoReadiness(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.api = self.make_repo("api", {"service.py": "print('x')\n"})
        self.context = self.make_context(self.primary)
        self.registry.add_repo(self.context, self.api, [])
        self.roadmap, self.jobs = StubRoadmapService(), StubJobs()
        structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)
        self.readiness = RepoReadinessService(self.registry, structure, self.roadmap, self.jobs, self.loop_status_service, self.log)

    def by_name(self):
        return {repo["name"]: repo for repo in self.readiness.describe_repos(self.context)["repos"]}

    def test_extra_repo_without_conformance_and_map_is_not_ready(self):
        api = self.by_name()["api"]
        self.assertFalse(api["ready"])
        self.assertEqual(api["missing"], ["conformance", "map"])
        self.assertIn(api["suggestion"]["suggestion"], ("STANDARD", "LEGACY"))
        self.assertEqual([item["name"] for item in self.readiness.list_unready(self.context)], ["api"])

    def test_reference_repo_needs_no_setup(self):
        self.registry.set_role(self.context, "api", "reference")
        self.assertEqual(self.by_name()["api"]["role"], "reference")
        self.assertEqual(self.readiness.list_unready(self.context), [])

    def test_conformance_written_in_the_repo_claude_md_then_only_the_map_is_missing(self):
        written = self.readiness.set_conformance(self.context, "api", "LEGACY")
        self.assertEqual(written["conformance"], "LEGACY")
        self.assertIn("Conformance: LEGACY", read_file(os.path.join(self.api, "CLAUDE.md")))
        self.assertEqual(self.by_name()["api"]["missing"], ["map"])

    def test_map_documents_make_it_ready(self):
        self.readiness.set_conformance(self.context, "api", "STANDARD")
        write_file(os.path.join(self.api, "docs", "codebase", "STACK.md"), "# stack\n")
        api = self.by_name()["api"]
        self.assertTrue(api["ready"])
        self.assertEqual(api["map"]["files"], ["STACK.md"])
        self.assertEqual(self.readiness.list_unready(self.context), [])

    def test_repo_without_code_needs_no_map(self):
        empty = self.make_repo("empty")
        self.registry.add_repo(self.context, empty, [])
        self.readiness.set_conformance(self.context, "empty", "STANDARD")
        self.assertTrue(self.by_name()["empty"]["ready"])

    def test_running_map_job_is_not_ready_and_is_reported(self):
        self.readiness.set_conformance(self.context, "api", "STANDARD")
        self.jobs.running[self.api] = {"id": "j-1", "type": "map"}
        api = self.by_name()["api"]
        self.assertFalse(api["ready"])
        self.assertEqual((api["map"]["running"], api["map"]["job"]), (True, "j-1"))

    def test_map_job_runs_in_the_extra_repo_and_keeps_git_clean_of_specs(self):
        result = self.readiness.map_repo(self.context, "api")
        self.assertEqual(result["job"], "j-map")
        self.assertEqual(self.roadmap.mapped, [self.api])
        self.assertIn(".specs", read_file(os.path.join(self.api, ".gitignore")))

    def test_primary_and_unknown_repos_are_refused_and_a_running_loop_blocks(self):
        with self.assertRaises(RepoConflictError):
            self.readiness.set_conformance(self.context, "app", "STANDARD")
        with self.assertRaises(LookupError):
            self.readiness.map_repo(self.context, "nope")
        self.write_live_loop_lock(self.primary)
        with self.assertRaises(LoopRunningError):
            self.readiness.set_conformance(self.context, "api", "STANDARD")

    def test_invalid_conformance_value_is_refused(self):
        with self.assertRaises(ValueError):
            self.readiness.set_conformance(self.context, "api", "WHATEVER")

    def test_single_repo_project_has_nothing_unready(self):
        solo = self.make_repo("solo")
        self.assertEqual(self.readiness.list_unready(self.make_context(solo)), [])

    def test_planning_gate_appears_after_the_map_only_for_unready_extra_repos(self):
        facts = self.gate_facts([{"name": "api", "missing": ["map"], "detail": "api: no codebase map"}])
        ids = [gate["id"] for gate in build_planning_gates(facts)]
        self.assertEqual(ids[ids.index(GATE_MAP) + 1], GATE_REPOS)
        gate = next(gate for gate in build_planning_gates(facts) if gate["id"] == GATE_REPOS)
        self.assertFalse(gate["ok"])
        self.assertEqual(gate["items"], ["api: no codebase map"])
        self.assertNotIn(GATE_REPOS, [gate["id"] for gate in build_planning_gates(self.gate_facts([]))])

    @staticmethod
    def gate_facts(unready):
        return {"structure": {"ok": True, "missing": []}, "has_code": True, "project_text": "x", "features": {}, "roadmap_parse_errors": [],
                "roadmap_field_errors": [], "open_questions": [], "flow_coverage": {"missing": [], "orphans": []}, "map_files": ["STACK.md"],
                "is_git": True, "conformance": "STANDARD", "roadmap_exists": False, "flows_exists": False, "repos_unready": unready}
