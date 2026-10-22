"""Multi-repo grammar of the ROADMAP (`paths=<repo>:<path>`, `repo-order=`), collisions per repo, and the repo list of the engine
(`project_repos`: no file / unchecked = ONE primary repo, checked = the listed repos, primary first). Pure functions, temporary folders."""
import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import loop_state  # noqa: E402
import project_repos  # noqa: E402


def parse(text):
    features, errors = loop_state.parse_roadmap(text)
    assert not errors, errors
    return features


class TestRepoPathGrammar(unittest.TestCase):
    def test_split_repo_path(self):
        cases = {"api:src/a": ("api", "src/a"), "src/a": ("", "src/a"), "web.ui:pages": ("web.ui", "pages"), "C:/x/y": ("", "C:/x/y"),
                 "C:\\x": ("", "C:\\x"), " api:src ": ("api", "src")}
        for entry, expected in cases.items():
            with self.subTest(entry=entry):
                self.assertEqual(loop_state.split_repo_path(entry), expected)

    def test_feature_repo_paths_keys_unprefixed_paths_by_the_primary_name(self):
        feature = {"paths": "src/a,web:pages,app:src/b"}
        self.assertEqual(loop_state.feature_repo_paths(feature), [("", "src/a"), ("web", "pages"), ("app", "src/b")])
        self.assertEqual(loop_state.feature_repo_paths(feature, "app"), [("app", "src/a"), ("web", "pages"), ("app", "src/b")])

    def test_roadmap_line_parses_the_hyphenated_repo_order_field(self):
        features = parse("## Stage 1\n- [orders] status=pending scope=Medium kind=both paths=api:src/orders,web:src/orders repo-order=api,web\n")
        self.assertEqual(features["orders"]["repo-order"], "api,web")
        self.assertEqual(features["orders"]["paths"], "api:src/orders,web:src/orders")

    def test_unknown_repo_in_paths_or_repo_order_is_reported_only_when_the_names_are_given(self):
        features = parse("## Stage 1\n- [a] status=pending kind=back paths=ghost:src repo-order=api,phantom\n")
        self.assertEqual(loop_state.check_fields(features), [])  # single-repo callers pass no names: nothing is checked
        errors = loop_state.check_fields(features, ["app", "api"])
        self.assertEqual(len(errors), 2)
        self.assertTrue(any("paths=" in error and "'ghost'" in error for error in errors))
        self.assertTrue(any("repo-order=" in error and "'phantom'" in error for error in errors))
        self.assertEqual(loop_state.check_fields(parse("## Stage 1\n- [a] status=pending paths=api:src,src repo-order=app,api\n"), ["app", "api"]), [])


class TestCollisionsPerRepo(unittest.TestCase):
    def collide(self, first_paths, second_paths, primary="app", first_extra="", second_extra=""):
        first = {"paths": first_paths, "deps": first_extra}
        second = {"paths": second_paths, "deps": second_extra}
        return loop_state.edits_overlap(first, second, primary)

    def test_same_path_in_different_repos_does_not_collide(self):
        self.assertEqual(self.collide("api:src", "web:src"), "")

    def test_same_repo_overlapping_paths_collide(self):
        self.assertNotEqual(self.collide("api:src", "api:src/orders"), "")

    def test_unprefixed_path_belongs_to_the_primary_repo(self):
        self.assertNotEqual(self.collide("src/a", "app:src/a/b"), "")
        self.assertEqual(self.collide("src/a", "api:src/a"), "")

    def test_dir_dependency_collides_with_an_edit_of_the_same_repo_only(self):
        self.assertNotEqual(self.collide("api:src/shared", "web:src/x", second_extra="dir:api:src/shared"), "")
        self.assertEqual(self.collide("web:src/shared", "api:src/x", second_extra="dir:api:src/shared"), "")

    def test_conflicts_and_wave_picking_use_the_primary_name(self):
        features = parse("## Stage 1\n- [a] status=approved kind=back paths=api:src\n- [b] status=approved kind=front paths=web:src\n"
                         "- [c] status=approved kind=back paths=api:src/x\n")
        self.assertEqual(loop_state.conflicts(features, ["a", "b"], "app"), [])
        self.assertEqual(len(loop_state.conflicts(features, ["a", "c"], "app")), 1)
        work = loop_state.next_work(features, wave=3, primary_name="app")
        self.assertEqual(work["phase"], "execute")
        self.assertEqual(work["slugs"], ["a", "b"])
        self.assertEqual(work["deferred_by_collision"], ["c"])


class TestProjectRepos(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="project-repos-")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.primary = os.path.join(self.root, "Main App")
        os.makedirs(self.primary)

    def write_file(self, data):
        target = project_repos.repos_file_path(self.primary)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(data if isinstance(data, str) else json.dumps(data))

    def test_short_name_and_unique_name(self):
        self.assertEqual(project_repos.short_name_of(self.primary), "main-app")
        self.assertEqual(project_repos.short_name_of("/"), "repo")
        self.assertEqual(project_repos.unique_name("web", {"web", "web-2"}), "web-3")
        self.assertEqual(project_repos.unique_name("web", set()), "web")

    def test_no_file_is_one_primary_repo_with_the_legacy_worktree_path(self):
        repos = project_repos.load_repos(self.primary)
        self.assertEqual(len(repos), 1)
        self.assertTrue(repos[0].primary)
        self.assertEqual(repos[0].loop_worktree_path(), os.path.join(self.root, "Main App-worktree-loop"))

    def test_unchecked_broken_or_empty_files_are_single_repo(self):
        other = os.path.join(self.root, "api")
        self.write_file({"multi": False, "repos": [{"name": "api", "path": other}]})  # a listed repo is listed whatever `multi` says
        self.assertEqual([repo.name for repo in project_repos.load_repos(self.primary)], ["main-app", "api"])
        for data in ("{not json", {"multi": True, "repos": []}, {"multi": True}, [1]):
            with self.subTest(data=data):
                self.write_file(data)
                self.assertEqual([repo.name for repo in project_repos.load_repos(self.primary)], ["main-app"])

    def test_multi_lists_every_repo_primary_first_with_unique_names(self):
        api, web, web_two = (os.path.join(self.root, name) for name in ("api", "web", "other-web"))
        self.write_file({"multi": True, "repos": [{"name": "web", "path": web, "copy": [".env", 3], "setup": " npm ci "},
                                                  {"name": "web", "path": web_two, "worktree_path": "/w/web2"},
                                                  {"path": self.primary}, {"path": api}, {"name": "broken"}]})
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([repo.name for repo in repos], ["main-app", "web", "web-2", "api"])
        self.assertEqual([repo.primary for repo in repos], [True, False, False, False])
        self.assertEqual(repos[1].copy, [".env"])
        self.assertEqual(repos[1].setup, "npm ci")
        self.assertEqual(repos[2].loop_worktree_path(), "/w/web2")
        self.assertIs(project_repos.primary_of(repos), repos[0])
        self.assertEqual(project_repos.find_repo(repos, "api").path, api)
        self.assertIsNone(project_repos.find_repo(repos, "ghost"))

    def test_multi_file_that_forgot_the_primary_gets_it_first(self):
        self.write_file({"multi": True, "repos": [{"name": "api", "path": os.path.join(self.root, "api")}]})
        repos = project_repos.load_repos(self.primary)
        self.assertEqual([repo.name for repo in repos], ["main-app", "api"])
        self.assertTrue(repos[0].primary)

    def test_save_repos_round_trips_and_omits_empty_fields(self):
        api = project_repos.RepoRef(name="api", path=os.path.join(self.root, "api"), copy=[".env"], setup="make")
        project_repos.save_repos(self.primary, True, project_repos.single_repo(self.primary) + [api])
        raw = project_repos.read_repos_file(self.primary)
        self.assertTrue(raw["multi"])
        self.assertEqual(raw["repos"][0], {"name": "main-app", "path": self.primary, "primary": True, "role": "edit"})
        loaded = project_repos.load_repos(self.primary)
        self.assertEqual((loaded[1].copy, loaded[1].setup), ([".env"], "make"))


if __name__ == "__main__":
    unittest.main()
