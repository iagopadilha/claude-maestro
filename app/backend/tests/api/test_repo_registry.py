"""Repo registry of a project (REPOS.json): add / refuse / remove / copy list / setup / suggested repos / repo in use by another loop."""
import json
import os
import unittest

from harness_backend.domain.project_errors import (InvalidRepoError, InvalidRequestError, LoopRunningError, RepoConflictError,
                                                   RepoInUseError, RepoNotFoundError)
from harness_backend.services import repo_registry_service

from .support_multi_repo import MultiRepoTestCase
from .support_projects import read_file, run_git, write_file


class TestRepoRegistry(MultiRepoTestCase):
    def setUp(self):
        super().setUp()
        self.original_data_dir = os.environ.get("LOOP_DATA_DIR")
        os.environ["LOOP_DATA_DIR"] = os.path.join(self.temporary_dir, "data")  # the repo catalog lives here
        self.addCleanup(self.restore_data_dir)
        self.primary = self.make_repo("primary")
        self.context = self.make_context(self.primary)

    def restore_data_dir(self):
        if self.original_data_dir is None:
            os.environ.pop("LOOP_DATA_DIR", None)
        else:
            os.environ["LOOP_DATA_DIR"] = self.original_data_dir

    def repos_file(self):
        return json.loads(read_file(os.path.join(self.primary, ".specs", "project", "REPOS.json")))

    # ------------------------------------------------ list / add
    def test_project_without_file_lists_only_the_primary(self):
        listing = self.registry.list_repos(self.context)
        self.assertFalse(listing["multi"])
        self.assertEqual([repo["name"] for repo in listing["repos"]], ["primary"])
        self.assertTrue(listing["repos"][0]["primary"])
        self.assertTrue(listing["repos"][0]["is_git"])
        self.assertEqual(listing["suggested"], [])

    def test_add_repo_turns_multi_on_and_stores_name_copy_and_setup(self):
        api = self.make_repo("api")
        result = self.registry.add_repo(self.context, api, [".env", "./config/local.json"], "  npm ci  ")
        self.assertTrue(result["multi"])
        self.assertEqual(result["repo"]["name"], "api")
        self.assertEqual(result["repo"]["copy"], [".env", "config/local.json"])
        self.assertEqual(result["repo"]["setup"], "npm ci")
        stored = self.repos_file()
        self.assertTrue(stored["multi"])
        self.assertEqual([item["name"] for item in stored["repos"]], ["primary", "api"])
        self.assertEqual([repo["name"] for repo in self.registry.list_repos(self.context)["repos"]], ["primary", "api"])

    def test_two_folders_with_the_same_name_get_unique_names(self):
        first = self.make_repo(os.path.join("one", "web"))
        second = self.make_repo(os.path.join("two", "web"))
        self.registry.add_repo(self.context, first, [])
        added = self.registry.add_repo(self.context, second, [])
        self.assertEqual(added["repo"]["name"], "web-2")

    def test_refuses_a_folder_that_is_not_listable(self):
        not_git = os.path.join(self.temporary_dir, "plain")
        os.makedirs(not_git)
        detached = self.make_repo("detached")
        run_git(detached, "checkout", "-q", "--detach")
        cases = [("", "give the repo folder", None), (os.path.join(self.temporary_dir, "missing"), "not a folder", None),
                 (not_git, "not a git repository", None), (detached, "detached HEAD", repo_registry_service.DETACHED_HEAD_CODE),
                 (self.primary, "already listed", repo_registry_service.ALREADY_LISTED_CODE)]
        for folder, text, code in cases:
            with self.subTest(folder=folder):
                with self.assertRaises(InvalidRepoError) as failure:
                    self.registry.add_repo(self.context, folder, [])
                self.assertIn(text, str(failure.exception))
                self.assertEqual(failure.exception.code, code)
        self.assertFalse(os.path.exists(os.path.join(self.primary, ".specs", "project", "REPOS.json")))

    def test_refuses_a_second_add_of_the_same_folder(self):
        api = self.make_repo("api")
        self.registry.add_repo(self.context, api, [])
        with self.assertRaises(InvalidRepoError) as failure:
            self.registry.add_repo(self.context, api, [])
        self.assertEqual(failure.exception.code, repo_registry_service.ALREADY_LISTED_CODE)

    def test_copy_list_refuses_unsafe_entries(self):
        for entry in ("/etc/passwd", "../secret.env", "dir/", "node_modules/x/.env", "bin/app.json", "C:/x/.env"):
            with self.subTest(entry=entry):
                with self.assertRaises(InvalidRepoError):
                    self.registry.validate_copy_list([entry])
        with self.assertRaises(InvalidRepoError):
            self.registry.validate_copy_list(".env")
        self.assertEqual(self.registry.validate_copy_list([".env", "./.env", "a\\b.json"]), [".env", "a/b.json"])

    def test_setup_must_be_one_short_line(self):
        self.assertEqual(self.registry.validate_setup(None), "")
        for bad in ("a\nb", "x" * 201, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(InvalidRepoError):
                    self.registry.validate_setup(bad)

    # ------------------------------------------------ copy list / setup / suggestions
    def test_set_copy_list_and_set_setup_update_the_file(self):
        api = self.make_repo("api")
        self.registry.add_repo(self.context, api, [])
        self.assertEqual(self.registry.set_copy_list(self.context, "api", [".env.local"])["copy"], [".env.local"])
        self.assertEqual(self.registry.set_setup(self.context, "api", "dotnet restore")["setup"], "dotnet restore")
        self.assertIsNone(self.registry.set_setup(self.context, "api", "")["setup"])
        item = [entry for entry in self.repos_file()["repos"] if entry["name"] == "api"][0]
        self.assertEqual(item["copy"], [".env.local"])
        self.assertNotIn("setup", item)
        with self.assertRaises(RepoNotFoundError):
            self.registry.set_copy_list(self.context, "nope", [])
        with self.assertRaises(RepoNotFoundError):
            self.registry.set_setup(self.context, "nope", "x")

    def test_copy_suggestions_list_ignored_config_files_only(self):
        api = self.make_repo("api", {".gitignore": ".env\n.env.example\nnode_modules/\nbin/\nnotes.txt\n", "a.txt": "a\n"})
        write_file(os.path.join(api, ".env"), "SECRET=1\n")
        write_file(os.path.join(api, ".env.example"), "SECRET=\n")
        write_file(os.path.join(api, "node_modules", "pkg", ".env"), "x\n")
        write_file(os.path.join(api, "notes.txt"), "not a config\n")
        self.registry.add_repo(self.context, api, [".env"])
        suggestions = self.registry.list_copy_suggestions(self.context, "api")["suggestions"]
        self.assertEqual([item["path"] for item in suggestions], [".env"])
        self.assertTrue(suggestions[0]["secret"])
        self.assertTrue(suggestions[0]["ticked"])

    def test_copy_null_goes_back_to_automatic_and_empty_list_means_none(self):
        api = self.make_repo("api", {".gitignore": ".env\n", "go.mod": "module x\n"})
        write_file(os.path.join(api, ".env"), "S=1\n")
        self.registry.add_repo(self.context, api, None)
        item = lambda: [entry for entry in self.repos_file()["repos"] if entry["name"] == "api"][0]  # noqa: E731
        self.assertNotIn("copy", item())
        described = self.registry.list_repos(self.context)["repos"][1]
        self.assertEqual((described["copy_configured"], described["setup_configured"], described["detected_setup"]), (False, False, "go mod download"))
        self.assertTrue(self.registry.list_copy_suggestions(self.context, "api")["suggestions"][0]["ticked"])
        self.assertEqual(self.registry.set_copy_list(self.context, "api", [])["copy_configured"], True)
        self.assertEqual(item()["copy"], [])
        self.assertFalse(self.registry.list_copy_suggestions(self.context, "api")["suggestions"][0]["ticked"])
        self.assertEqual(self.registry.set_copy_list(self.context, "api", None), {"ok": True, "name": "api", "copy": [], "copy_configured": False})
        self.assertNotIn("copy", item())
        with self.assertRaises(InvalidRepoError):
            self.registry.set_copy_list(self.context, "api", "x")

    # ------------------------------------------------ multi / remove
    def test_unchecking_multi_hides_the_siblings_but_keeps_them_in_the_file(self):
        api = self.make_repo("api")
        self.registry.add_repo(self.context, api, [])
        listing = self.registry.set_multi(self.context, False)
        self.assertFalse(listing["multi"])
        self.assertEqual([repo["name"] for repo in listing["repos"]], ["primary"])
        self.assertEqual([item["name"] for item in self.repos_file()["repos"]], ["primary", "api"])
        self.assertEqual([repo.name for repo in self.registry.list_effective_repos(self.context)], ["primary"])
        self.assertEqual([repo["name"] for repo in self.registry.set_multi(self.context, True)["repos"]], ["primary", "api"])
        with self.assertRaises(InvalidRequestError):
            self.registry.set_multi(self.context, "yes")

    def test_remove_repo_only_edits_the_list(self):
        api = self.make_repo("api")
        self.registry.add_repo(self.context, api, [])
        result = self.registry.remove_repo(self.context, "api")
        self.assertEqual(result["removed"], "api")
        self.assertTrue(os.path.isdir(api))
        self.assertEqual([item["name"] for item in self.repos_file()["repos"]], ["primary"])
        with self.assertRaises(RepoNotFoundError):
            self.registry.remove_repo(self.context, "api")

    def test_primary_cannot_be_removed(self):
        with self.assertRaises(RepoConflictError) as failure:
            self.registry.remove_repo(self.context, "primary")
        self.assertEqual(failure.exception.code, repo_registry_service.PRIMARY_CODE)

    def test_multi_and_remove_are_refused_while_the_loop_runs(self):
        api = self.make_repo("api")
        self.registry.add_repo(self.context, api, [])
        self.write_live_loop_lock(self.primary)
        with self.assertRaises(LoopRunningError):
            self.registry.remove_repo(self.context, "api")
        with self.assertRaises(LoopRunningError):
            self.registry.set_multi(self.context, False)
        self.assertEqual([item["name"] for item in self.repos_file()["repos"]], ["primary", "api"])

    # ------------------------------------------------ suggested repos (planning)
    def write_catalog(self, entries):
        write_file(os.path.join(os.environ["LOOP_DATA_DIR"], "repo-catalog.json"),
                   json.dumps({"version": 1, "repos": {path: {"name": name, "path": path} for name, path in entries}}))

    def test_suggested_keeps_only_catalog_repos_not_yet_listed(self):
        api = self.make_repo("api")
        web_path = os.path.join(self.temporary_dir, "web")
        self.write_catalog([("api", api), ("web", web_path), ("primary", self.primary)])
        suggested = [{"name": "web", "reason": "front end", "evidence": "calls /items"}, {"name": "web"}, {"name": "ghost", "reason": "not in catalog"},
                     {"name": "primary"}, "junk", {"reason": "no name"}]
        write_file(os.path.join(self.primary, ".specs", "project", "REPOS-SUGGESTED.json"), json.dumps(suggested))
        listing = self.registry.list_repos(self.context)["suggested"]
        self.assertEqual([item["name"] for item in listing], ["web"])
        self.assertEqual(listing[0]["reason"], "front end")
        self.assertEqual(listing[0]["path"], web_path)

    def test_suggested_is_empty_for_a_broken_or_non_list_file(self):
        path = os.path.join(self.primary, ".specs", "project", "REPOS-SUGGESTED.json")
        for text in ("{not json", json.dumps({"name": "web"})):
            write_file(path, text)
            self.assertEqual(self.registry.read_suggested_repos(self.context), [])

    # ------------------------------------------------ repo in use by another project's running loop
    def test_repo_in_use_by_another_running_loop_is_refused(self):
        shared = self.make_repo("shared")
        other_primary = self.make_repo("other")
        other = self.make_context(other_primary)
        self.registry.add_repo(other, shared, [])
        mine = self.make_context(self.make_repo("mine"))
        self.registry.add_repo(mine, shared, [])
        self.loop_status_service.ensure_repos_free(mine)  # nobody runs: free
        self.write_live_loop_lock(other_primary)
        with self.assertRaises(RepoInUseError) as failure:
            self.loop_status_service.ensure_repos_free(mine)
        self.assertEqual(failure.exception.code, "repo_in_use")
        self.assertEqual(failure.exception.extras["repo"], "shared")
        self.assertEqual(failure.exception.extras["project"]["id"], other.project.id)

    def test_repo_is_free_when_the_other_project_does_not_share_it(self):
        other_primary = self.make_repo("other")
        self.make_context(other_primary)
        self.write_live_loop_lock(other_primary)
        self.loop_status_service.ensure_repos_free(self.context)


if __name__ == "__main__":
    unittest.main()
