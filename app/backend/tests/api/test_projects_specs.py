"""View of .specs/: grouped tree, read with a version, write with a conflict check and refusal of paths outside .specs/."""
import os
import subprocess
import unittest
import urllib.parse

from .support_projects import ProjectsTestCase, loop_file_path, read_file, write_file


class TestTreeAndFiles(ProjectsTestCase):
    def setUp(self):
        super().setUp()
        self.repo, self.project_id = self.create_ready_project(approve=False)
        self.specs = os.path.join(self.repo, ".specs")
        write_file(os.path.join(self.specs, "features", "api-base", "spec.md"), "# Spec api-base\n")
        write_file(os.path.join(self.specs, "features", "loose", "spec.md"), "# Spec without a ROADMAP line\n")
        write_file(os.path.join(self.specs, "codebase", "STACK.md"), "# Stack\n")
        write_file(os.path.join(self.specs, "project", "phases", "stage-01.md"), "# Stage 1\n")
        write_file(loop_file_path(self.repo, "VALIDATE.md"), "- [general] x\n")
        write_file(loop_file_path(self.repo, "internal", "secret.md"), "do not list\n")
        write_file(loop_file_path(self.repo, "json", "note.md"), "do not list\n")

    def read(self, path):
        return self.request("GET", "/p/%s/api/specs/file?path=%s" % (self.project_id, urllib.parse.quote(path)))

    def write(self, path, content, version):
        return self.request("PUT", "/p/%s/api/specs/file" % self.project_id, {"path": path, "content": content, "version": version})

    def test_grouped_tree(self):
        status, tree = self.request("GET", "/p/%s/api/specs/tree" % self.project_id)
        self.assertEqual(status, 200, tree)
        self.assertTrue(tree["exists"])
        self.assertFalse(tree["loop_running"])
        project_paths = {item["path"] for item in tree["project"]}
        self.assertTrue({"project/PROJECT.md", "project/ROADMAP.md", "project/STATE.md", "project/RULES.md",
                         "project/phases/stage-01.md"} <= project_paths)
        self.assertEqual([item["path"] for item in tree["codebase"]], ["codebase/STACK.md"])
        self.assertEqual([item["path"] for item in tree["loop"]], ["project/loop/VALIDATE.md"])
        features = {feature["slug"]: feature for feature in tree["features"]}
        self.assertEqual([feature["slug"] for feature in tree["features"]], ["api-base", "screen-base", "loose"])
        self.assertEqual((features["api-base"]["status"], features["api-base"]["stage"]), ("pending", "1"))
        self.assertEqual([item["path"] for item in features["api-base"]["files"]], ["features/api-base/spec.md"])
        self.assertEqual(features["screen-base"]["files"], [])
        self.assertTrue(features["api-base"]["in_roadmap"])
        self.assertFalse(features["loose"]["in_roadmap"])
        everything = str(tree)
        self.assertNotIn("internal", everything)
        self.assertNotIn("note.md", everything)
        editable = {item["path"]: item["editable"] for item in tree["project"]}
        self.assertTrue(editable["project/PROJECT.md"])
        self.assertTrue(editable["project/ROADMAP.md"])
        self.assertFalse(editable["project/phases/stage-01.md"])
        item = next(item for item in tree["codebase"])
        self.assertEqual(set(item), {"path", "name", "editable", "not_editable_reason", "size", "modified_at"})

    def test_read_edit_and_version_conflict(self):
        status, document = self.read("features/api-base/spec.md")
        self.assertEqual(status, 200, document)
        self.assertEqual((document["text"], document["editable"]), ("# Spec api-base\n", True))
        status, written = self.write("features/api-base/spec.md", "# Spec api-base v2\n", document["version"])
        self.assertEqual(status, 200, written)
        self.assertTrue(written["ok"])
        self.assertNotEqual(written["version"], document["version"])
        self.assertEqual(read_file(os.path.join(self.specs, "features", "api-base", "spec.md")), "# Spec api-base v2\n")

        status, conflict = self.write("features/api-base/spec.md", "# another edit\n", document["version"])
        self.assertEqual(status, 409, conflict)
        self.assertEqual(conflict["current_version"], written["version"])
        self.assertEqual(read_file(os.path.join(self.specs, "features", "api-base", "spec.md")), "# Spec api-base v2\n")

    def test_creating_a_new_file_requires_empty_version(self):
        status, body = self.write("features/screen-base/spec.md", "# Spec screen\n", "")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["created"])
        self.assertEqual(self.write("codebase/NEW.md", "x\n", "any-version")[0], 409)

    def test_refuses_path_outside_specs(self):
        write_file(os.path.join(self.repo, "outside.md"), "outside\n")
        for path in ("../outside.md", "features/../../outside.md", os.path.join(self.repo, "outside.md"), "/etc/passwd.md",
                     "C:/Windows/win.md", "project/loop/internal/secret.md", "project/loop/json/note.md",
                     "Project/Loop/JSON/note.md", "project/ROADMAP.txt", ""):
            self.assertEqual(self.read(path)[0], 400, path)
            self.assertEqual(self.write(path, "x", "")[0], 400, path)
        self.assertEqual(read_file(os.path.join(self.repo, "outside.md")), "outside\n")

    def test_refuses_symbolic_link(self):
        external_target = os.path.join(self.temporary_dir, "external")
        write_file(os.path.join(external_target, "spec.md"), "external\n")
        link_path = os.path.join(self.specs, "features", "link")
        try:
            os.symlink(external_target, link_path, target_is_directory=True)
        except (OSError, NotImplementedError) as failure:
            if os.name != "nt":
                self.skipTest("no permission to create a symbolic link on this system: %s" % failure)
            # Windows without symbolic link rights: a junction (mklink /J) needs no administrator and also points outside
            result = subprocess.run(["cmd", "/c", "mklink", "/J", link_path, external_target], capture_output=True)
            if result.returncode != 0:
                self.skipTest("neither symbolic link nor junction on this system: %s" % failure)
        self.assertEqual(self.read("features/link/spec.md")[0], 400)
        self.assertEqual(self.write("features/link/spec.md", "invaded\n", "")[0], 400)
        self.assertEqual(read_file(os.path.join(external_target, "spec.md")), "external\n")
        self.assertNotIn("features/link/spec.md", str(self.request("GET", "/p/%s/api/specs/tree" % self.project_id)[1]))

    def test_read_only_and_missing(self):
        status, document = self.read("project/phases/stage-01.md")
        self.assertEqual((status, document["editable"]), (200, False))
        self.assertEqual(document["not_editable_reason"], "read-only: written by the loop/orchestrator")
        self.assertEqual(self.write("project/phases/stage-01.md", "x", document["version"])[0], 400)
        _, validate = self.read("project/loop/VALIDATE.md")
        self.assertEqual(self.write("project/loop/VALIDATE.md", "x", validate["version"])[0], 400)
        self.assertEqual(self.read("features/nothing/spec.md")[0], 404)

    def test_roadmap_and_state_only_with_the_loop_stopped(self):
        _, roadmap = self.read("project/ROADMAP.md")
        _, project = self.read("project/PROJECT.md")
        self.write_live_loop_lock(self.repo)
        status, document = self.read("project/ROADMAP.md")
        self.assertEqual((status, document["editable"]), (200, False))
        status, body = self.write("project/ROADMAP.md", "# changed\n", roadmap["version"])
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "loop running: ROADMAP.md and STATE.md can only be edited with the loop stopped")
        _, state = self.read("project/STATE.md")
        self.assertEqual(self.write("project/STATE.md", "# changed\n", state["version"])[0], 409)
        self.assertEqual(self.write("project/PROJECT.md", "# new PROJECT\n", project["version"])[0], 200)

    def test_put_requires_token(self):
        _, document = self.read("project/PROJECT.md")
        status, _ = self.request("PUT", "/p/%s/api/specs/file" % self.project_id,
                                 {"path": "project/PROJECT.md", "content": "x", "version": document["version"]}, with_token=False)
        self.assertEqual(status, 403)


class TestTreeWithoutSpecs(ProjectsTestCase):
    def test_project_without_specs_has_an_empty_tree(self):
        folder = os.path.join(self.projects_dir, "nothing")
        os.makedirs(folder)
        status, tree = self.request("GET", "/p/%s/api/specs/tree" % self.open_project(folder))
        self.assertEqual(status, 200)
        self.assertFalse(tree["exists"])
        self.assertEqual(tree["features"], [])


if __name__ == "__main__":
    unittest.main()
