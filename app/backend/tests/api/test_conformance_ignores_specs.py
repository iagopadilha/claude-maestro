"""Defining the Conformance always leaves `.specs/` out of git: the root project, each subproject of a monorepo (the repo root's .gitignore)
and an extra repository. The line is added once, after the user's own lines, and never repeated."""
import os

from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.repo_readiness_service import RepoReadinessService
from harness_backend.services.specs_structure_service import SpecsStructureService
from harness_backend.services.subprojects_service import SubprojectsService
from harness_backend.repositories.subprojects_file_repository import SubprojectsFileRepository

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, write_file
from .test_repo_readiness import StubJobs, StubRoadmapService
from .test_subprojects_service import MONOREPO_FILES

USER_IGNORE = "bin/\nnode_modules/\n"


class StoppedLoop:
    """Process runner of a project with no loop running."""
    @staticmethod
    def is_alive(pid):
        return False


def specs_lines(path):
    return [line.strip() for line in read_file(path).splitlines() if line.strip() == ".specs/"]


class TestConformanceIgnoresSpecs(MultiRepoTestCase):
    def structure(self):
        return SpecsStructureService(SpecsFileRepository(), os.path.join(self.temporary_dir, "templates"), StoppedLoop(), self.log)

    def test_root_conformance_adds_specs_after_the_user_lines_once(self):
        repo = self.make_repo("solo")
        write_file(os.path.join(repo, ".gitignore"), USER_IGNORE)
        context, structure = self.make_context(repo), self.structure()
        structure.write_project_conformance(context, "STANDARD")
        structure.write_project_conformance(context, "LEGACY")
        text = read_file(os.path.join(repo, ".gitignore"))
        self.assertTrue(text.startswith(USER_IGNORE))
        self.assertEqual(specs_lines(os.path.join(repo, ".gitignore")), [".specs/"])

    def test_subproject_conformance_adds_specs_to_the_repo_root(self):
        repo = self.make_repo("catalogo", MONOREPO_FILES)
        os.makedirs(os.path.join(repo, ".specs", "project"), exist_ok=True)
        write_file(os.path.join(repo, ".gitignore"), USER_IGNORE)
        context = self.make_context(repo)
        service = SubprojectsService(SubprojectsFileRepository(), self.structure(), self.registry, self.loop_status_service, self.log)
        service.describe(context)
        service.update_list(context, {"confirmed": True})
        service.set_conformance(context, "backend", "STANDARD")
        self.assertEqual(specs_lines(os.path.join(repo, ".gitignore")), [".specs/"])
        self.assertFalse(os.path.isfile(os.path.join(repo, "backend", ".gitignore")), "the subproject folder gets no .gitignore of its own")

    def test_extra_repo_conformance_adds_specs_to_that_repo(self):
        primary = self.make_repo("app")
        api = self.make_repo("api", {"service.py": "print('x')\n"})
        context = self.make_context(primary)
        self.registry.add_repo(context, api, [])
        readiness = RepoReadinessService(self.registry, self.structure(), StubRoadmapService(), StubJobs(), self.loop_status_service, self.log)
        readiness.set_conformance(context, "api", "LEGACY")
        self.assertEqual(specs_lines(os.path.join(api, ".gitignore")), [".specs/"])

    def test_an_already_ignored_specs_is_left_alone(self):
        repo = self.make_repo("solo")
        write_file(os.path.join(repo, ".gitignore"), ".specs\n")
        self.structure().write_project_conformance(self.make_context(repo), "STANDARD")
        self.assertEqual(read_file(os.path.join(repo, ".gitignore")), ".specs\n")
