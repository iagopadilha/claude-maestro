"""Projects hub: create from scratch, open an existing folder, list, forget, folder browser and native dialog."""
import json
import os
import sys
import unittest

from .support_projects import ProjectsTestCase, create_git_repo, loop_file_path, read_file, run_git, write_file


class TestCreateProjectFromScratch(ProjectsTestCase):
    def test_creates_folder_git_and_specs_structure(self):
        status, body = self.request("POST", "/api/app/projects/create",
                                    {"parent_path": self.projects_dir, "name": "my-app", "init_git": True, "conformance": "STANDARD"})
        self.assertEqual(status, 200, body)
        repo = os.path.join(self.projects_dir, "my-app")
        self.assertEqual(body["project"]["repo_path"], repo)
        self.assertEqual(body["url"], "/p/%s/" % body["project"]["id"])
        for relative in ("project/PROJECT.md", "project/ROADMAP.md", "project/STATE.md", "project/RULES.md"):
            self.assertTrue(os.path.isfile(os.path.join(repo, ".specs", *relative.split("/"))), relative)
        for relative in ("features", "project/loop/json", "project/loop/internal"):
            self.assertTrue(os.path.isdir(os.path.join(repo, ".specs", *relative.split("/"))), relative)
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs", "codebase")), "the map lives in docs/codebase, not in .specs/")
        self.assertIn("my-app", read_file(os.path.join(repo, ".specs", "project", "PROJECT.md")))
        rules = read_file(os.path.join(repo, ".specs", "project", "RULES.md"))
        self.assertIn("| ID | Rule |", rules)
        self.assertNotIn("| R-02 |", rules)  # the template's example rules do not become project rules
        self.assertIn(".specs/", read_file(os.path.join(repo, ".gitignore")).splitlines())
        claude_md = read_file(os.path.join(repo, "CLAUDE.md"))
        self.assertIn("@.specs/project/RULES.md", claude_md)
        self.assertIn("Conformance: STANDARD", claude_md)
        self.assertTrue(os.path.exists(os.path.join(repo, ".specs", "project", "TEST-ENVIRONMENT.md")), "the template is created up front: the planning chat edits it")
        self.assertEqual(run_git(repo, "rev-parse", "--is-inside-work-tree").stdout.strip(), "true")
        self.assertEqual(run_git(repo, "log", "--oneline").stdout.count("\n"), 1)
        self.assertIn("chore: initial project structure (loop-harness)", run_git(repo, "log", "--format=%s").stdout)
        self.assertEqual(run_git(repo, "status", "--porcelain").stdout.strip(), "", "the initial commit takes everything (and .specs/ is ignored)")
        monitor = json.loads(read_file(loop_file_path(repo, "json", "MONITOR.json")))
        self.assertEqual(monitor["pid"], os.getpid())
        self.assertEqual(monitor["url"], self.server.url + "p/%s/" % body["project"]["id"])
        self.assertEqual(monitor["work"], repo)
        progress = self.get_progress(body["project"]["id"])
        self.assertEqual((progress["mode"], progress["next_action"]), ("new", "roadmap"))

    def test_without_git_does_not_init_a_repository(self):
        status, body = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": "no git", "init_git": False})
        self.assertEqual(status, 200, body)
        self.assertFalse(os.path.exists(os.path.join(self.projects_dir, "no git", ".git")))
        steps = {step["id"]: step for step in self.get_progress(body["project"]["id"])["steps"]}
        self.assertFalse(steps["git"]["ok"])

    def test_refuses_existing_folder_with_content(self):
        write_file(os.path.join(self.projects_dir, "taken", "a.txt"), "x")
        status, body = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": "taken"})
        self.assertEqual(status, 409, body)
        self.assertFalse(os.path.exists(os.path.join(self.projects_dir, "taken", ".specs")))

    def test_accepts_existing_empty_folder(self):
        os.makedirs(os.path.join(self.projects_dir, "empty"))
        status, body = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": "empty", "init_git": False})
        self.assertEqual(status, 200, body)

    def test_refuses_invalid_name_and_missing_parent(self):
        for name in ("", "../outside", "a/b", "a\\b", ".hidden", "con"):
            status, body = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": name})
            self.assertEqual(status, 400, (name, body))
        status, _ = self.request("POST", "/api/app/projects/create", {"parent_path": os.path.join(self.projects_dir, "missing"), "name": "x"})
        self.assertEqual(status, 400)
        self.assertEqual(os.listdir(self.projects_dir), [])

    def test_invalid_conformance_is_refused(self):
        status, _ = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": "x", "conformance": "OTHER"})
        self.assertEqual(status, 400)

    def test_requires_token(self):
        status, _ = self.request("POST", "/api/app/projects/create", {"parent_path": self.projects_dir, "name": "x"}, with_token=False)
        self.assertEqual(status, 403)
        self.assertEqual(os.listdir(self.projects_dir), [])


class TestOpenExistingProject(ProjectsTestCase):
    def test_opens_repo_without_specs_creating_nothing(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "legacy"), {"src/app.py": "print(1)\n"})
        project_id = self.open_project(repo)
        self.assertFalse(os.path.exists(os.path.join(repo, ".specs")), "opening creates neither .specs/ nor MONITOR.json")
        progress = self.get_progress(project_id)
        self.assertEqual((progress["mode"], progress["next_action"]), ("no_specs", "structure"))
        self.assertEqual(progress["loop_status"], "no_loop")

    def test_open_is_idempotent_and_updates_access(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "repeated"))
        first = self.open_project(repo)
        second = self.open_project(repo + os.sep)
        self.assertEqual(first, second)
        _, listing = self.request("GET", "/api/app/projects")
        self.assertEqual(len(listing["projects"]), 1)
        registry = json.loads(read_file(self.config.projects_registry_path))
        self.assertEqual(registry["version"], 1)
        self.assertEqual(len(registry["projects"]), 1)
        self.assertEqual(registry["projects"][0]["repo_path"], repo)

    def test_refuses_path_that_is_not_a_folder(self):
        write_file(os.path.join(self.projects_dir, "file.txt"), "x")
        for path in (os.path.join(self.projects_dir, "file.txt"), os.path.join(self.projects_dir, "nothing"), ""):
            status, body = self.request("POST", "/api/app/projects/open", {"path": path})
            self.assertEqual(status, 400, body)

    def test_list_with_progress_summary_and_forget(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "listed"))
        project_id = self.open_project(repo)
        status, listing = self.request("GET", "/api/app/projects")
        self.assertEqual(status, 200)
        item = listing["projects"][0]
        self.assertEqual((item["id"], item["name"], item["repo_path"], item["url"]), (project_id, "listed", repo, "/p/%s/" % project_id))
        self.assertTrue(item["folder_exists"])
        self.assertEqual(set(item["progress_summary"]), {"mode", "next_action", "loop_status", "counts"})
        self.assertEqual(self.request("POST", "/api/app/projects/%s/forget" % project_id, {})[0], 200)
        self.assertEqual(self.request("GET", "/api/app/projects")[1]["projects"], [])
        self.assertTrue(os.path.isdir(repo), "forget never deletes the folder")
        status, body = self.request("GET", "/p/%s/api/progress" % project_id)
        self.assertEqual((status, body), (404, {"ok": False, "error": "project not found"}))
        self.assertEqual(self.request("POST", "/api/app/projects/%s/forget" % project_id, {})[0], 404)

    def test_release_on_shutdown_deletes_only_this_process_monitor(self):
        repo, project_id = self.create_ready_project(approve=False)
        monitor_path = loop_file_path(repo, "json", "MONITOR.json")
        self.assertTrue(os.path.isfile(monitor_path))
        self.module.project_service.release_projects_on_shutdown()
        self.assertFalse(os.path.exists(monitor_path))

    def test_does_not_overwrite_monitor_of_another_live_process(self):
        repo = create_git_repo(os.path.join(self.projects_dir, "other-monitor"))
        monitor_path = loop_file_path(repo, "json", "MONITOR.json")
        foreign = {"pid": os.getppid(), "port": 1, "url": "http://127.0.0.1:1/", "repo": repo, "work": repo, "started_at": "x"}
        write_file(monitor_path, json.dumps(foreign))
        self.open_project(repo)
        self.assertEqual(json.loads(read_file(monitor_path)), foreign)
        self.module.project_service.release_projects_on_shutdown()
        self.assertTrue(os.path.isfile(monitor_path))


class TestFolderBrowser(ProjectsTestCase):
    def test_lists_only_subfolders_flagging_projects(self):
        create_git_repo(os.path.join(self.projects_dir, "with-git"))
        os.makedirs(os.path.join(self.projects_dir, "with-specs", ".specs"))
        os.makedirs(os.path.join(self.projects_dir, ".hidden"))
        write_file(os.path.join(self.projects_dir, "file.txt"), "x")
        status, body = self.request("GET", "/api/app/system/folders?path=" + self.projects_dir.replace("\\", "/"))
        self.assertEqual(status, 200, body)
        folders = {folder["name"]: folder for folder in body["folders"]}
        self.assertEqual(set(folders), {"with-git", "with-specs"})
        self.assertTrue(folders["with-git"]["has_git"])
        self.assertTrue(folders["with-specs"]["has_specs"])
        self.assertEqual(os.path.normcase(body["parent"]), os.path.normcase(self.temporary_dir))
        self.assertFalse(body["truncated"])

    def test_without_path_lists_roots_and_invalid_path_is_400(self):
        status, body = self.request("GET", "/api/app/system/folders")
        self.assertEqual(status, 200)
        self.assertTrue(body["folders"])
        status, _ = self.request("GET", "/api/app/system/folders?path=" + os.path.join(self.projects_dir, "missing"))
        self.assertEqual(status, 400)


class TestDialogWithoutTkinter(ProjectsTestCase):
    folder_dialog_argv = [sys.executable, "-c", "import sys; print('NO_TKINTER:ModuleNotFoundError: tkinter'); sys.exit(3)"]

    def test_without_tkinter_returns_null_with_reason(self):
        status, body = self.request("POST", "/api/app/projects/pick-folder", {})
        self.assertEqual(status, 200)
        self.assertIsNone(body["path"])
        self.assertIn("tkinter", body["reason"])


class TestDialogWithChoice(ProjectsTestCase):
    folder_dialog_argv = [sys.executable, "-c", "print('PATH:C:/chosen/folder')"]

    def test_returns_the_chosen_path(self):
        status, body = self.request("POST", "/api/app/projects/pick-folder", {})
        self.assertEqual(status, 200)
        self.assertEqual(body["path"], os.path.normpath("C:/chosen/folder"))


class TestDialogCancelled(ProjectsTestCase):
    folder_dialog_argv = [sys.executable, "-c", "print('PATH:')"]

    def test_cancel_returns_null(self):
        self.assertEqual(self.request("POST", "/api/app/projects/pick-folder", {})[1], {"path": None, "reason": "cancelled"})


if __name__ == "__main__":
    unittest.main()
