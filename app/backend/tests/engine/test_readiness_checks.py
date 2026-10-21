"""engine/readiness_checks.py: subprojects, giant files, system map, uncommitted context, known commands. Scratch folders only."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import project_repos  # noqa: E402
import readiness_checks  # noqa: E402
import run_loop  # noqa: E402


def write(path, text="x\n"):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def git(folder, *args):
    return subprocess.run(["git", "-C", folder] + list(args), capture_output=True, text=True)


def feature(**fields):
    return dict({"status": "pending"}, **fields)


class ReadinessChecksCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="readiness-checks-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def store(self, projects, confirmed=True, monorepo=True):
        entry = {"monorepo": monorepo, "confirmed": confirmed, "projects": projects}
        write(self.path(".specs", "project", "SUBPROJECTS.json"), json.dumps({"version": 1, "repos": {os.path.basename(self.root): entry}}))

    def project(self, name, skip=False, has_code=True, conformance="", map_document=False):
        if conformance:
            write(self.path(name, "CLAUDE.md"), "Conformance: %s\n" % conformance)
        if map_document:
            write(self.path(name, "docs", "codebase", "STACK.md"))
        return {"path": name, "name": name, "stack": "x", "skill": "-", "skip": skip, "has_code": has_code}

    def inventory(self, name, giants):
        write(self.path(".specs", "project", "loop", "internal", "inventory", name + ".json"), json.dumps({"giant": giants}))


class TestSubprojectsPreflight(ReadinessChecksCase):
    def test_touched_subproject_without_conformance_or_map_is_a_problem_naming_it(self):
        self.store([self.project("frontend"), self.project("backend", conformance="STANDARD", map_document=True)])
        problems, warns = readiness_checks.subprojects_preflight(self.root, {"ui": feature(paths="frontend/src")})
        self.assertEqual(len(problems), 1)
        self.assertIn("`frontend`", problems[0])
        self.assertIn("Conformance", problems[0])
        self.assertIn("no map in frontend/docs/codebase", problems[0])
        self.assertEqual(warns, [])

    def test_ready_subproject_and_untouched_gaps_only_warn(self):
        self.store([self.project("frontend"), self.project("backend", conformance="LEGACY", map_document=True)])
        problems, warns = readiness_checks.subprojects_preflight(self.root, {"api": feature(paths="backend/Api", kind="both")})
        self.assertEqual(problems, [])
        self.assertEqual(len(warns), 1)
        self.assertIn("`frontend` is not touched", warns[0])

    def test_no_code_subproject_needs_only_conformance(self):
        self.store([self.project("docs-site", has_code=False, conformance="STANDARD")])
        self.assertEqual(readiness_checks.subprojects_preflight(self.root, {"x": feature(paths="docs-site/a")}), ([], []))

    def test_paths_outside_and_skipped_are_warnings(self):
        self.store([self.project("backend", conformance="STANDARD", map_document=True), self.project("tools", skip=True)])
        problems, warns = readiness_checks.subprojects_preflight(self.root, {"a": feature(paths="scripts/run.sh"), "b": feature(paths="tools/x")})
        self.assertEqual(problems, [])
        joined = "\n".join(warns)
        self.assertIn("paths outside any subproject (scripts/run.sh): the agents use the root rules only", joined)
        self.assertIn("subproject `tools` skipped: no map, no Conformance for its agents", joined)

    def test_longest_prefix_wins(self):
        self.store([self.project("apps", conformance="STANDARD", map_document=True), self.project("apps/web")])
        problems, _ = readiness_checks.subprojects_preflight(self.root, {"a": feature(paths="apps/web/src")})
        self.assertIn("`apps/web`", problems[0])

    def test_single_project_and_unconfirmed_list_give_nothing(self):
        self.assertEqual(readiness_checks.subprojects_preflight(self.root, {"a": feature(paths="src")}), ([], []))
        self.store([self.project("frontend")], confirmed=False)
        self.assertEqual(readiness_checks.subprojects_preflight(self.root, {"a": feature(paths="frontend")}), ([], []))

    def test_repo_prefixed_path_uses_that_repos_list_and_folder(self):
        other = os.path.join(self.root, "other-repo")
        write(os.path.join(other, "web", "CLAUDE.md"), "Conformance: STANDARD\n")
        entry = {"monorepo": True, "confirmed": True, "projects": [{"path": "web", "skip": False, "has_code": True}]}
        write(self.path(".specs", "project", "SUBPROJECTS.json"), json.dumps({"version": 1, "repos": {"other": entry}}))
        repos = [project_repos.RepoRef(name=os.path.basename(self.root), path=self.root, primary=True),
                 project_repos.RepoRef(name="other", path=other)]
        problems, _ = readiness_checks.subprojects_preflight(self.root, {"a": feature(paths="other:web/src")}, repos)
        self.assertEqual(len(problems), 1)
        self.assertIn("no map in web/docs/codebase", problems[0])
        self.assertNotIn("Conformance", problems[0])


class TestSystemMap(ReadinessChecksCase):
    def test_two_subprojects_without_system_md_warn(self):
        self.store([self.project("backend", conformance="STANDARD"), self.project("frontend", conformance="STANDARD")])
        features = {"a": feature(paths="backend"), "b": feature(paths="frontend")}
        self.assertIn("no system map: the agents have no description of how the subprojects talk", readiness_checks.system_map_warning(self.root, features)[0])
        write(self.path("docs", "codebase", "SYSTEM.md"))
        self.assertEqual(readiness_checks.system_map_warning(self.root, features), [])

    def test_one_subproject_does_not_warn(self):
        self.store([self.project("backend"), self.project("frontend")])
        self.assertEqual(readiness_checks.system_map_warning(self.root, {"a": feature(paths="backend")}), [])


class TestGiantPreflight(ReadinessChecksCase):
    def setUp(self):
        super().setUp()
        self.inventory("backend", [{"path": "backend/Big.cs", "lines": 3000, "bytes": 1}])

    def test_feature_touching_a_giant_file_warns_with_files_and_features(self):
        warns = readiness_checks.giant_preflight(self.root, {"a": feature(paths="backend/Big.cs"), "b": feature(paths="backend", title="split Big.cs"), "c": feature(paths="frontend"),
                                                          "d": feature(paths="backend")})
        self.assertEqual(len(warns), 1)
        self.assertIn("backend/Big.cs (3000 lines)", warns[0])
        self.assertIn("features: a, b.", warns[0])

    def test_refactor_in_deps_chain_clears_it_even_through_another_feature(self):
        features = {"refactor-big": feature(paths="backend/Big.cs"), "mid": feature(paths="docs", deps="refactor-big"),
                    "a": feature(paths="backend", deps="mid", title="touches Big.cs")}
        self.assertEqual(readiness_checks.giant_preflight(self.root, features), [])

    def test_refactor_not_in_the_chain_or_for_another_file_does_not_clear(self):
        features = {"refactor-big": feature(paths="backend/Big.cs"), "a": feature(paths="backend", title="edits Big.cs"),
                    "refactor-other": feature(paths="backend/Other.cs"), "b": feature(paths="backend/Big.cs", deps="refactor-other")}
        warns = readiness_checks.giant_preflight(self.root, features)
        self.assertEqual(len(warns), 1)
        self.assertIn("features: a, b.", warns[0])

    def test_note_giant_accepted_clears_it(self):
        self.assertEqual(readiness_checks.giant_preflight(self.root, {"a": feature(paths="backend", note="giant-accepted")}), [])

    def test_no_inventory_no_warning(self):
        shutil.rmtree(self.path(".specs"))
        self.assertEqual(readiness_checks.giant_preflight(self.root, {"a": feature(paths="backend")}), [])


class TestUncommittedWarnings(ReadinessChecksCase):
    def test_one_warning_with_the_exact_text_and_none_once_the_worktree_exists(self):
        git(self.root, "init", "-q")
        write(self.path("README.md"))
        write(self.path("CLAUDE.md"))
        write(self.path("docs", "codebase", "STACK.md"))
        repos = [project_repos.RepoRef(name="proj", path=self.root, primary=True)]
        warns = readiness_checks.uncommitted_warnings(repos)
        self.assertEqual(len(warns), 1)
        self.assertTrue(warns[0].startswith("uncommitted changes in proj: 1 setup file not committed: they will be carried into the worktree "
                                            "(or commit them from Workspace): docs/codebase/STACK.md"))
        self.assertIn("docs/codebase/STACK.md", warns[0])
        os.makedirs(repos[0].loop_worktree_path())
        self.addCleanup(shutil.rmtree, repos[0].loop_worktree_path(), True)
        self.assertEqual(readiness_checks.uncommitted_warnings(repos), [])


class TestKnownCommands(ReadinessChecksCase):
    def test_package_scripts(self):
        write(self.path("package.json"), json.dumps({"scripts": {"build": "x", "dev": "y", "test": "z"}}))
        self.assertEqual(readiness_checks.known_commands(self.root),
                         {"build": "npm run build", "run": "npm run dev", "test": "npm test", "summary": "npm test"})

    def test_testing_document_wins_the_summary_and_unknown_is_empty(self):
        self.assertEqual(readiness_checks.known_commands(self.root)["summary"], "")
        write(self.path("docs", "codebase", "TESTING.md"), "run it\n")
        self.assertEqual(readiness_checks.known_commands(self.root)["summary"], "docs/codebase/TESTING.md")

    def test_run_loop_delegates_and_looks_into_confirmed_subprojects(self):
        self.assertEqual(run_loop.known_test_command(self.root), "")
        write(self.path("backend", "docs", "codebase", "TESTING.md"), "dotnet test\n")
        self.assertEqual(run_loop.known_test_command(self.root), "")  # not a confirmed monorepo yet
        self.store([self.project("backend")])
        self.assertEqual(run_loop.known_test_command(self.root), "docs/codebase/TESTING.md")

    def test_root_conformance_is_not_required_for_a_confirmed_monorepo(self):
        self.assertEqual(len(run_loop.root_conformance_problems(self.root)), 1)
        self.store([self.project("backend")])
        self.assertEqual(run_loop.root_conformance_problems(self.root), [])


if __name__ == "__main__":
    unittest.main()
