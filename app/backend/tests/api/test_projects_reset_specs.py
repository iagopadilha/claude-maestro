"""Reset specs: moves the planning/loop state to .specs/.trash/<stamp>/ (never deletes), keeps the rules and the map."""
import json
import os
import re
import subprocess
import unittest

from harness_backend.services.specs_reset_service import RESET_MOVED_PATHS

from .support_projects import ProjectsTestCase, loop_file_path, read_file, write_file

TRASH_PATTERN = re.compile(r"^\.specs/\.trash/\d{8}-\d{6}(-\d+)?$")


def make_directory_link(link, target):
    """Symbolic link, or a junction on Windows without the symlink privilege. False when neither can be created."""
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        result = subprocess.run(["cmd", "/c", "mklink", "/J", link, target], capture_output=True)
        return result.returncode == 0
    return False


class TestResetSpecs(ProjectsTestCase):
    def build_project_with_state(self, name="resettable"):
        """Ready project (approved roadmap) plus a bit of everything the loop and the planning leave behind."""
        repo, project_id = self.create_ready_project(name=name)
        specs = os.path.join(repo, ".specs")
        write_file(os.path.join(specs, "features", "api-base", "spec.md"), "# spec\n")
        write_file(os.path.join(specs, "project", "phases", "stage-01.md"), "# stage\n")
        write_file(os.path.join(specs, "project", "archive", "old.md"), "# old\n")
        write_file(loop_file_path(repo, "ANSWERS.md"), "answers\n")
        write_file(loop_file_path(repo, "json", "LOOP.json"), "{}")
        write_file(loop_file_path(repo, "json", "rules.tests.json"), "{\"keep\": 1}")
        write_file(loop_file_path(repo, "json", "SCOPE.json"), "{\"keep\": 2}")
        write_file(loop_file_path(repo, "json", "PERMISSIONS.json"), "{\"keep\": 3}")
        write_file(loop_file_path(repo, "internal", "turns", "001.json"), "{}")
        write_file(loop_file_path(repo, "internal", "planning", "thread.json"), "{\"messages\": []}")
        write_file(loop_file_path(repo, "internal", "roadmap-goal.md"), "goal\n")
        write_file(os.path.join(repo, "docs", "codebase", "STACK.md"), "# stack\n")
        return repo, project_id

    def reset(self, project_id, confirm):
        return self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {"confirm": confirm})

    def snapshot_files(self, repo, relatives):
        return {relative: read_file(os.path.join(repo, *relative.split("/"))) for relative in relatives}

    def test_moves_state_to_the_trash_and_keeps_rules_and_map(self):
        repo, project_id = self.build_project_with_state()
        kept_before = self.snapshot_files(repo, [".specs/project/RULES.md", ".specs/project/PROJECT.md", ".specs/project/TEST-ENVIRONMENT.md",
                                                 ".specs/project/.env.test", ".specs/project/loop/json/rules.tests.json",
                                                 ".specs/project/loop/json/SCOPE.json", ".specs/project/loop/json/PERMISSIONS.json",
                                                 "docs/codebase/STACK.md", "CLAUDE.md"])
        roadmap_before = read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md"))
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 200, body)
        self.assertTrue(body["ok"])
        self.assertRegex(body["trash"], TRASH_PATTERN)
        moved = set(body["moved"])
        for relative in (".specs/features", ".specs/project/ROADMAP.md", ".specs/project/FLOWS.md", ".specs/project/phases",
                         ".specs/project/archive", ".specs/project/loop/ANSWERS.md", ".specs/project/loop/json/ROADMAP-APPROVED.json",
                         ".specs/project/loop/json/LOOP.json", ".specs/project/loop/internal"):
            self.assertIn(relative, moved)
        for relative in moved:
            self.assertTrue(any(relative == allowed or relative.startswith(allowed + "/") for allowed in RESET_MOVED_PATHS), relative)
        # kept: untouched, listed, and not in the moved list
        self.assertEqual(self.snapshot_files(repo, list(kept_before)), kept_before)
        for relative in kept_before:
            self.assertNotIn(relative, moved)
        self.assertIn(".specs/project/RULES.md", body["kept"])
        self.assertIn(".specs/project/loop/json/SCOPE.json", body["kept"])
        self.assertIn("docs/codebase", body["kept"])
        self.assertNotIn(".specs/codebase", body["kept"], "kept only lists what exists")
        # the monitor of this app is neither moved nor listed
        self.assertTrue(os.path.isfile(loop_file_path(repo, "json", "MONITOR.json")))
        self.assertFalse([path for path in moved if path.endswith("MONITOR.json")])
        # trash keeps the paths relative to .specs/
        trash = os.path.join(repo, *body["trash"].split("/"))
        self.assertEqual(read_file(os.path.join(trash, "project", "ROADMAP.md")), roadmap_before)
        self.assertTrue(os.path.isfile(os.path.join(trash, "features", "api-base", "spec.md")))
        self.assertTrue(os.path.isfile(os.path.join(trash, "project", "loop", "internal", "planning", "thread.json")))
        self.assertTrue(os.path.isfile(os.path.join(trash, "project", "loop", "json", "ROADMAP-APPROVED.json")))
        self.assertFalse(os.path.exists(os.path.join(trash, "project", "RULES.md")))
        # born clean again: templates and an empty features/
        self.assertEqual(os.listdir(os.path.join(repo, ".specs", "features")), [])
        for name in ("ROADMAP.md", "FLOWS.md", "STATE.md"):
            self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "project", name)), name)
        self.assertNotIn("[api-base]", read_file(os.path.join(repo, ".specs", "project", "ROADMAP.md")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs", "project", "phases")))
        self.assertTrue(os.path.isdir(loop_file_path(repo, "internal")))
        progress = self.get_progress(project_id)
        self.assertEqual(progress["mode"], "planning", "no approval and no loop ran: back to planning mode")

    def test_is_idempotent(self):
        repo, project_id = self.build_project_with_state()
        self.assertEqual(self.reset(project_id, "resettable")[0], 200)
        trash_root = os.path.join(repo, ".specs", ".trash")
        before = os.listdir(trash_root)
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 200, body)
        self.assertEqual(body["moved"], [])
        self.assertIsNone(body["trash"])
        self.assertEqual(os.listdir(trash_root), before, "nothing to clean: no new trash folder")
        self.assertIn(".specs/project/RULES.md", body["kept"])

    def test_project_without_specs_is_ok_and_creates_nothing(self):
        from .support_projects import create_git_repo
        repo = create_git_repo(os.path.join(self.projects_dir, "bare"))
        project_id = self.open_project(repo)
        status, body = self.reset(project_id, "bare")
        self.assertEqual((status, body["moved"], body["trash"]), (200, [], None), body)
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs")))

    def test_requires_the_project_name_as_confirmation(self):
        repo, project_id = self.build_project_with_state()
        for confirm in (None, "", "other", "RESETTABLE", True, 1):
            status, body = self.reset(project_id, confirm)
            self.assertEqual(status, 400, (confirm, body))
        status, body = self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {})
        self.assertEqual(status, 400, body)
        self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "features", "api-base", "spec.md")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs", ".trash")))

    def test_refused_while_the_loop_runs(self):
        repo, project_id = self.build_project_with_state()
        self.write_live_loop_lock(repo)
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "loop_running")
        self.assertIn("loop", body["error"])
        self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "features", "api-base", "spec.md")))
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs", ".trash")))

    def test_refused_while_a_claude_job_runs(self):
        repo, project_id = self.build_project_with_state()
        self.module.claude_job_service.get_running_job = lambda repo_path: {"id": "j-000000000000"}
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 409, body)
        self.assertEqual(body["code"], "planning_busy")
        self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", "features", "api-base", "spec.md")))

    def test_unknown_project_is_404(self):
        self.assertEqual(self.reset("0" * 12, "x")[0], 404)

    def test_requires_token(self):
        _, project_id = self.build_project_with_state()
        status, _ = self.request("POST", "/api/app/projects/%s/reset-specs" % project_id, {"confirm": "resettable"}, with_token=False)
        self.assertEqual(status, 403)

    def test_trash_is_git_ignored_and_never_listed_by_the_app(self):
        repo, project_id = self.build_project_with_state()
        write_file(os.path.join(repo, ".gitignore"), "node_modules/\n")  # .specs/ NOT ignored: the trash line must be added
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 200, body)
        self.assertIn(".specs/.trash/", read_file(os.path.join(repo, ".gitignore")).splitlines())
        self.reset(project_id, "resettable")
        self.assertEqual(read_file(os.path.join(repo, ".gitignore")).splitlines().count(".specs/.trash/"), 1)
        status, tree = self.request("GET", "/p/%s/api/specs/tree" % project_id)
        self.assertEqual(status, 200, tree)
        listed = json.dumps(tree)
        self.assertNotIn(".trash", listed)
        self.assertNotIn("api-base", listed)
        status, _ = self.request("GET", "/p/%s/api/specs/file?path=.trash/%s/features/api-base/spec.md" % (project_id, body["trash"].rsplit("/", 1)[-1]))
        self.assertIn(status, (400, 404))

    def test_specs_ignored_as_a_whole_adds_no_extra_line(self):
        repo, project_id = self.build_project_with_state()
        self.assertIn(".specs/", read_file(os.path.join(repo, ".gitignore")).splitlines())
        self.reset(project_id, "resettable")
        self.assertNotIn(".specs/.trash/", read_file(os.path.join(repo, ".gitignore")).splitlines())

    def test_a_symlink_is_never_followed_or_moved(self):
        repo, project_id = self.build_project_with_state()
        outside = os.path.join(self.temporary_dir, "outside")
        write_file(os.path.join(outside, "precious.txt"), "do not touch\n")
        archive = os.path.join(repo, ".specs", "project", "archive")
        for child in os.listdir(archive):
            os.remove(os.path.join(archive, child))
        os.rmdir(archive)
        if not make_directory_link(archive, outside):
            self.skipTest("links are not available here")
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 200, body)
        self.assertNotIn(".specs/project/archive", body["moved"])
        self.assertTrue(os.path.lexists(archive))
        self.assertEqual(read_file(os.path.join(outside, "precious.txt")), "do not touch\n")
        self.assertIn(".specs/features", body["moved"], "the rest of the reset still happens")

    def test_specs_folder_that_is_a_link_is_refused(self):
        repo, project_id = self.build_project_with_state()
        real_specs = os.path.join(self.temporary_dir, "real-specs")
        os.rename(os.path.join(repo, ".specs"), real_specs)
        if not make_directory_link(os.path.join(repo, ".specs"), real_specs):
            os.rename(real_specs, os.path.join(repo, ".specs"))
            self.skipTest("links are not available here")
        status, body = self.reset(project_id, "resettable")
        self.assertEqual(status, 400, body)
        self.assertTrue(os.path.isfile(os.path.join(real_specs, "features", "api-base", "spec.md")))


if __name__ == "__main__":
    unittest.main()
