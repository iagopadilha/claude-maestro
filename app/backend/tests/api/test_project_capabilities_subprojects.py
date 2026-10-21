"""The `Subprojects` lines of the capabilities block of PROJECT.md (monorepo) and the untouched single-project output."""
import hashlib
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.project_capabilities_service import END_MARKER, START_MARKER, ProjectCapabilitiesService

from .support_projects import read_file, write_file


class FakeLog:
    def info(self, *args, **fields):
        pass


def digest(path):
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


class ProjectCapabilitiesSubprojectsTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="harness-capabilities-sub-")
        self.addCleanup(lambda: shutil.rmtree(self.folder, ignore_errors=True))
        self.repo = os.path.join(self.folder, "mono")
        self.context = SimpleNamespace(work_dir=self.repo, repo_path=self.repo, project=SimpleNamespace(id="p1"))
        self.project_md = os.path.join(self.repo, ".specs", "project", "PROJECT.md")
        self.root_claude = os.path.join(self.repo, "CLAUDE.md")
        write_file(self.project_md, "# Mono\n\nintro\n\n%s\nold\n%s\n\ntail\n" % (START_MARKER, END_MARKER))
        write_file(self.root_claude, "# root\nsome text\n")
        self.service = ProjectCapabilitiesService(SpecsFileRepository(), FakeLog(), lambda: [])

    def write_subprojects(self, confirmed=True, monorepo=True):
        projects = [{"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev", "skip": False},
                    {"path": "frontend", "name": "frontend", "stack": "react", "skill": "frontend-dev", "skip": False},
                    {"path": "tools", "name": "tools", "stack": "unknown", "skill": "", "skip": True, "reason": "scripts only"}]
        entry = {"monorepo": monorepo, "confirmed": confirmed, "projects": projects}
        write_file(os.path.join(self.repo, ".specs", "project", "SUBPROJECTS.json"),
                   json.dumps({"version": 1, "repos": {"mono": entry}}))

    def test_confirmed_monorepo_renders_one_line_per_subproject(self):
        self.write_subprojects()
        write_file(os.path.join(self.repo, "backend", "CLAUDE.md"), "# b\nConformance: STANDARD\n")
        write_file(os.path.join(self.repo, "backend", "docs", "codebase", "STACK.md"), "x")
        block = self.service.render(self.context)
        self.assertIn("**Subprojects**", block)
        self.assertIn("`mono` `backend`: stack dotnet, skill backend-dev, Conformance STANDARD, map `backend/docs/codebase` (has documents), "
                      "CLAUDE.md `backend/CLAUDE.md`", block)
        self.assertIn("`mono` `frontend`: stack react, skill frontend-dev, Conformance -, map `frontend/docs/codebase` (no documents yet)", block)

    def test_skipped_subproject_is_listed_as_skipped(self):
        self.write_subprojects()
        block = self.service.render(self.context)
        self.assertIn("`mono` `tools`: skipped (scripts only)", block)
        self.assertNotIn("`tools`: stack", block)

    def test_system_pointer_exists_or_missing(self):
        self.write_subprojects()
        self.assertIn("system map: `docs/codebase/SYSTEM.md` (does not exist yet)", self.service.render(self.context))
        write_file(os.path.join(self.repo, "docs", "codebase", "SYSTEM.md"), "# system")
        self.assertIn("system map: `docs/codebase/SYSTEM.md` (exists)", self.service.render(self.context))

    def test_single_project_and_unconfirmed_list_render_without_subprojects(self):
        baseline = self.service.render(self.context)
        self.assertNotIn("Subprojects", baseline)
        self.write_subprojects(confirmed=False)
        self.assertEqual(baseline, self.service.render(self.context))
        self.write_subprojects(monorepo=False)
        self.assertEqual(baseline, self.service.render(self.context))

    def test_hook_rewrites_only_the_block_and_never_the_root_claude(self):
        self.write_subprojects()
        before_claude = digest(self.root_claude)
        before_text = read_file(self.project_md)
        self.service.after_list_confirmed(self.context, "mono", self.repo, {})
        after_text = read_file(self.project_md)
        self.assertIn("**Subprojects**", after_text)
        self.assertEqual(before_text[:before_text.index(START_MARKER)], after_text[:after_text.index(START_MARKER)])
        self.assertEqual(before_text[before_text.index(END_MARKER):], after_text[after_text.index(END_MARKER):])
        self.assertEqual(before_claude, digest(self.root_claude))


if __name__ == "__main__":
    unittest.main()
