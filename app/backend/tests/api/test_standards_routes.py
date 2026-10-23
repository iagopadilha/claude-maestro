"""Routes of the Standards slots (`## Standards` of a CLAUDE.md): the project's own repo, a secondary repo and one subproject of a
confirmed monorepo. Real app for the project and subproject routes (like test_subprojects_routes), service level for a secondary repo."""
import os

from harness_backend.api.controllers.repo_readiness_controller import RepoReadinessController
from harness_backend.domain.project_errors import InvalidRequestError, LoopRunningError, RepoNotFoundError
from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.standards_service import StandardsService
from harness_backend.services.subprojects_service import SubprojectsService

from .support_multi_repo import MultiRepoTestCase
from .support_projects import ProjectsTestCase, create_git_repo, read_file, write_file
from .test_subprojects_routes import MONOREPO_FILES

SLOTS = ["git", "backend", "frontend", "app-map", "observability"]
BINDINGS = {"git": "idata-git-padroes", "backend": "idata:idata-backend-padroes"}


class TestStandardsRoutes(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        # the repo routes belong to the multi-repo module, which this test case does not build: register only the controller
        RepoReadinessController(self.module.project_service, None, self.module.standards_service).register_routes(self.router)

    def open_scratch(self, name="solo", files=None):
        repo = create_git_repo(os.path.join(self.projects_dir, name), files)
        project_id = self.open_project(repo)
        status, body = self.request("POST", "/p/%s/api/structure/initialize" % project_id, {"conformance": "LEGACY"})
        self.assertEqual(status, 200, body)
        return repo, project_id

    @staticmethod
    def own(project_id):
        return "/p/%s/api/standards" % project_id

    @staticmethod
    def sub(project_id, suffix=""):
        return "/p/%s/api/subprojects/standards%s" % (project_id, suffix)

    def confirm(self, project_id):
        status, body = self.request("PUT", "/p/%s/api/subprojects" % project_id, {"confirmed": True})
        self.assertEqual(status, 200, body)

    def test_get_is_empty_when_nothing_is_bound(self):
        _, project_id = self.open_scratch()
        status, body = self.request("GET", self.own(project_id))
        self.assertEqual(status, 200, body)
        self.assertEqual(body, {"bindings": {}, "notes": [], "slots": SLOTS})

    def test_put_then_get_round_trip_writes_the_section_and_keeps_the_other_lines(self):
        repo, project_id = self.open_scratch()
        before = read_file(os.path.join(repo, "CLAUDE.md"))
        status, body = self.request("PUT", self.own(project_id), {"bindings": BINDINGS})
        self.assertEqual(status, 200, body)
        self.assertEqual(body["bindings"], BINDINGS)
        text = read_file(os.path.join(repo, "CLAUDE.md"))
        self.assertIn("## Standards\ngit: idata-git-padroes\nbackend: idata:idata-backend-padroes\n", text.replace("\r\n", "\n"))
        for line in before.splitlines():
            self.assertIn(line, text)
        status, body = self.request("GET", self.own(project_id))
        self.assertEqual((status, body["bindings"], body["slots"]), (200, BINDINGS, SLOTS))
        status, body = self.request("PUT", self.own(project_id), {"bindings": {"git": "", "frontend": "idata-guia-estilo"}})  # empty clears a slot
        self.assertEqual((status, body["bindings"]), (200, {"frontend": "idata-guia-estilo"}))
        status, body = self.request("PUT", self.own(project_id), {"bindings": {}})  # nothing bound: the section goes away
        self.assertEqual((status, body["bindings"]), (200, {}))
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "CLAUDE.md")))

    def test_invalid_value_is_400_invalid_standards_and_writes_nothing(self):
        repo, project_id = self.open_scratch()
        before = read_file(os.path.join(repo, "CLAUDE.md"))
        status, body = self.request("PUT", self.own(project_id), {"bindings": {"git": "not a skill name!"}})
        self.assertEqual((status, body.get("code")), (400, "invalid_standards"), body)
        self.assertEqual(read_file(os.path.join(repo, "CLAUDE.md")), before)
        status, body = self.request("PUT", self.own(project_id), {"bindings": ["git"]})
        self.assertEqual((status, body.get("code")), (400, "invalid_standards"), body)
        status, body = self.request("PUT", self.own(project_id), {})
        self.assertEqual((status, body.get("code")), (400, "invalid_standards"), body)

    def test_unknown_slot_is_400(self):
        repo, project_id = self.open_scratch()
        status, body = self.request("PUT", self.own(project_id), {"bindings": {"database": "some-skill"}})
        self.assertEqual((status, body.get("code")), (400, "invalid_standards"), body)
        self.assertIn("database", body["error"])
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "CLAUDE.md")))

    def test_put_is_refused_while_a_loop_runs_and_needs_the_token(self):
        repo, project_id = self.open_scratch()
        self.assertEqual(self.request("PUT", self.own(project_id), {"bindings": BINDINGS}, with_token=False)[0], 403)
        self.write_live_loop_lock(repo)
        status, body = self.request("PUT", self.own(project_id), {"bindings": BINDINGS})
        self.assertEqual((status, body.get("code")), (409, "loop_running"), body)
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "CLAUDE.md")))
        self.assertEqual(self.request("GET", self.own(project_id))[0], 200)  # reading is always allowed

    def test_repo_route_answers_for_the_own_repo_by_name(self):
        _, project_id = self.open_scratch()
        status, body = self.request("PUT", "/api/app/projects/%s/repos/solo/standards" % project_id, {"bindings": BINDINGS})
        self.assertEqual((status, body["bindings"]), (200, BINDINGS))

    def test_monorepo_root_put_is_refused_and_each_subproject_writes_its_own_claude_md(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        status, body = self.request("GET", self.sub(project_id, "?path=backend"))
        self.assertEqual((status, body.get("code")), (422, "subprojects_not_confirmed"), body)
        self.confirm(project_id)
        status, body = self.request("PUT", self.own(project_id), {"bindings": BINDINGS})
        self.assertEqual((status, body.get("code")), (409, "monorepo_root_standards"), body)
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "CLAUDE.md")))
        status, body = self.request("GET", self.sub(project_id, "?path=backend"))
        self.assertEqual((status, body["bindings"], body["slots"]), (200, {}, SLOTS))
        status, body = self.request("PUT", self.sub(project_id), {"path": "backend", "bindings": BINDINGS})
        self.assertEqual((status, body["bindings"]), (200, BINDINGS), body)
        self.assertIn("backend: idata:idata-backend-padroes", read_file(os.path.join(repo, "backend", "CLAUDE.md")))
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "frontend", "CLAUDE.md")))  # the other subproject is untouched
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "CLAUDE.md")))
        status, body = self.request("GET", self.sub(project_id, "?path=backend"))
        self.assertEqual(body["bindings"], BINDINGS)
        status, body = self.request("GET", self.sub(project_id, "?path=frontend"))
        self.assertEqual(body["bindings"], {})

    def test_subproject_route_errors(self):
        repo, project_id = self.open_scratch("mono", MONOREPO_FILES)
        self.confirm(project_id)
        self.assertEqual(self.request("PUT", self.sub(project_id), {"path": "nope", "bindings": BINDINGS})[0], 400)
        status, body = self.request("PUT", self.sub(project_id), {"path": "backend", "bindings": {"git": "bad value"}})
        self.assertEqual((status, body.get("code")), (400, "invalid_standards"), body)
        self.write_live_loop_lock(repo)
        status, body = self.request("PUT", self.sub(project_id), {"path": "backend", "bindings": BINDINGS})
        self.assertEqual((status, body.get("code")), (409, "loop_running"), body)
        self.assertNotIn("## Standards", read_file(os.path.join(repo, "backend", "CLAUDE.md")))


class TestStandardsServiceOfASecondaryRepo(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.primary = self.make_repo("app")
        self.api = self.make_repo("api", {"service.py": "print('x')\n"})
        self.context = self.make_context(self.primary)
        self.registry.add_repo(self.context, self.api, [])
        structure = SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), None, self.log)
        subprojects = SubprojectsService(SubprojectsFileRepository(SpecsFileRepository()), structure, self.registry, self.loop_status_service, self.log)
        self.standards = StandardsService(subprojects, self.log)

    def test_secondary_repo_claude_md_is_written_and_read_back(self):
        write_file(os.path.join(self.api, "CLAUDE.md"), "# api\n\nConformance: LEGACY\n")
        self.assertEqual(self.standards.get_repo(self.context, "api")["bindings"], {})
        result = self.standards.put_repo(self.context, "api", BINDINGS)
        self.assertEqual(result["bindings"], BINDINGS)
        text = read_file(os.path.join(self.api, "CLAUDE.md"))
        self.assertIn("Conformance: LEGACY", text)
        self.assertIn("## Standards", text)
        self.assertFalse(os.path.exists(os.path.join(self.primary, "CLAUDE.md")))

    def test_refusals(self):
        with self.assertRaises(RepoNotFoundError):
            self.standards.get_repo(self.context, "nope")
        with self.assertRaises(InvalidRequestError):
            self.standards.put_repo(self.context, "api", {"git": "bad value"})
        self.write_live_loop_lock(self.primary)
        with self.assertRaises(LoopRunningError):
            self.standards.put_repo(self.context, "api", BINDINGS)
        self.assertFalse(os.path.exists(os.path.join(self.api, "CLAUDE.md")))
