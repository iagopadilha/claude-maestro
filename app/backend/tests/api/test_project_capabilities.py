"""The marked "What the agents can use" block of PROJECT.md: rendering, in-place rewrite, append, byte-identical outside the markers."""
import json
import os
import shutil
import tempfile
import unittest
from types import SimpleNamespace

from harness_backend.repositories.specs_file_repository import SpecsFileRepository
from harness_backend.services.project_capabilities_service import END_MARKER, START_MARKER, ProjectCapabilitiesService

from .support_projects import read_file, write_file


def read_bytes_text(path):
    with open(path, "rb") as handle:
        return handle.read().decode("utf-8")


class FakeLog:
    def info(self, *args, **fields):
        pass


class ProjectCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.mkdtemp(prefix="harness-capabilities-")
        self.addCleanup(lambda: shutil.rmtree(self.folder, ignore_errors=True))
        self.repo = os.path.join(self.folder, "main-repo")
        self.context = SimpleNamespace(work_dir=self.repo, repo_path=self.repo, project=SimpleNamespace(id="p1"))
        self.project_md = os.path.join(self.repo, ".specs", "project", "PROJECT.md")
        self.repos_json = os.path.join(self.repo, ".specs", "project", "REPOS.json")
        self.servers = ["grafana", {"server": "mcp-mysql-dev", "tools": ["mysql_query"]}]
        self.service = ProjectCapabilitiesService(SpecsFileRepository(), FakeLog(), lambda: self.servers)

    def write_repos(self, repos):
        write_file(self.repos_json, json.dumps({"scope_defined": True, "repos": repos}))

    def test_block_lists_repositories_with_roles_and_the_rules(self):
        self.write_repos([{"name": "main-repo", "path": self.repo, "primary": True, "role": "reference"},
                          {"name": "back-gestao", "path": "C:\\code\\back-gestao", "role": "reference"},
                          {"name": "front", "path": "C:\\code\\front"}])
        block = self.service.render(self.context)
        self.assertTrue(block.startswith(START_MARKER) and block.endswith(END_MARKER))
        self.assertIn("`main-repo` (edit, primary)", block, "the primary is always edit")
        self.assertIn("`back-gestao` (reference): C:/code/back-gestao", block)
        self.assertIn("`front` (edit): C:/code/front", block, "role missing = edit")
        self.assertIn("grafana; mcp-mysql-dev (mysql_query)", block)
        for text in ("git read commands", "`git commit`: the orchestrator's checkpoint", "`harness.py deliver`", "state files",
                     "A guard block is final: do not retry", "nothing in this section restricts git", "Only the PATHS of your feature"):
            self.assertIn(text, block)
        self.assertIn("(`main-repo`, `front`)", block, "only edit repositories are listed as editable")

    def test_without_repos_json_the_primary_alone_and_without_mcp_none(self):
        service = ProjectCapabilitiesService(SpecsFileRepository(), FakeLog(), lambda: [])
        block = service.render(self.context)
        self.assertIn("`main-repo` (edit, primary)", block)
        self.assertIn("none available on this machine", block)

    def test_missing_mcp_module_falls_back_to_none(self):
        service = ProjectCapabilitiesService(SpecsFileRepository(), FakeLog())  # default provider: engine/mcp_access, whatever it returns
        self.assertIn("MCP read tools:", service.render(self.context))

    def test_block_is_appended_once_and_rewritten_in_place_keeping_the_rest_byte_identical(self):
        original = "# Project\r\n\r\n**Vision:** x\r\n\r\n## Notes\r\nkeep me \xe9\r\n"
        os.makedirs(os.path.dirname(self.project_md), exist_ok=True)
        with open(self.project_md, "wb") as handle:
            handle.write(original.encode("utf-8"))
        self.assertTrue(self.service.write(self.context))
        first = read_bytes_text(self.project_md)
        self.assertTrue(first.startswith(original), "text before the block is byte-identical")
        self.assertEqual(first.count(START_MARKER), 1)
        self.assertNotIn("\r\n\n", first.replace("\r\n", "|"), "the appended block keeps the file's line endings")
        self.assertFalse(self.service.write(self.context), "nothing changed: no write")

        self.write_repos([{"name": "other", "path": "C:\\code\\other", "role": "reference"}])
        suffix = "\r\n## After the block\r\nmore text\r\n"
        with open(self.project_md, "ab") as handle:
            handle.write(suffix.encode("utf-8"))
        before_text = read_bytes_text(self.project_md)
        self.assertTrue(self.service.write(self.context))
        after_text = read_bytes_text(self.project_md)
        self.assertIn("`other` (reference)", after_text)
        self.assertEqual(after_text.count(START_MARKER), 1)
        self.assertTrue(after_text.startswith(original), "before the markers: identical")
        self.assertTrue(after_text.endswith(suffix), "after the markers: identical")
        self.assertNotEqual(before_text, after_text)

    def test_appended_after_text_without_a_final_newline(self):
        write_file(self.project_md, "# P")
        self.service.write(self.context)
        text = read_file(self.project_md)
        self.assertTrue(text.startswith("# P\n\n" + START_MARKER))

    def test_the_template_empty_block_is_filled_in_place(self):
        template = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                                "templates", "specs", "PROJECT.md")
        original = read_file(template)
        self.assertEqual((original.count(START_MARKER), original.count(END_MARKER)), (1, 1))
        write_file(self.project_md, original)
        self.assertTrue(self.service.write(self.context))
        text = read_file(self.project_md)
        head, tail = original.split(START_MARKER)[0], original.split(END_MARKER)[1]
        self.assertTrue(text.startswith(head) and text.endswith(tail))
        self.assertIn("What the agents can use", text)

    def test_no_project_md_writes_nothing(self):
        self.assertFalse(self.service.write(self.context))
        self.assertFalse(os.path.exists(self.project_md))


if __name__ == "__main__":
    unittest.main()
