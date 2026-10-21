"""Subproject detection and lookup (engine/subprojects.py): scratch folders only, never a real repository."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine_env  # noqa: E402,F401
import subprojects  # noqa: E402

SCRIPT = os.path.join(engine_env.ENGINE_DIR, "subprojects.py")


def write(path, text=""):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)


def package_json(**dependencies):
    return json.dumps({"name": "x", "dependencies": dependencies})


class SubprojectsTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="subprojects-")
        self.addCleanup(shutil.rmtree, self.root, True)

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def make_monorepo(self):
        write(self.path("backend", "Shop.sln"))
        write(self.path("backend", "Shop.Api", "Shop.Api.csproj"), "<Project />")
        write(self.path("backend", "Shop.Api", "Program.cs"), "class P {}")
        write(self.path("frontend", "package.json"), package_json(react="18"))
        write(self.path("frontend", "src", "App.tsx"), "export default 1")

    def store(self, projects, confirmed=True, monorepo=True, repo=None):
        entry = {"monorepo": monorepo, "confirmed": confirmed, "projects": projects}
        write(self.path(".specs", "project", "SUBPROJECTS.json"),
              json.dumps({"version": 1, "repos": {repo or os.path.basename(self.root): entry}}))

    def run_cli(self, *arguments):
        return subprocess.run([sys.executable, SCRIPT] + list(arguments), capture_output=True, text=True, timeout=60)


class TestDetect(SubprojectsTestCase):
    def test_backend_and_frontend_are_two_candidates_with_skills(self):
        self.make_monorepo()
        result = subprojects.detect(self.root)
        self.assertTrue(result["monorepo"])
        by_path = {project["path"]: project for project in result["projects"]}
        self.assertEqual(set(by_path), {"backend", "frontend"})
        self.assertEqual((by_path["backend"]["stack"], by_path["backend"]["skill"]), ("dotnet", "backend-dev"))
        self.assertEqual((by_path["frontend"]["stack"], by_path["frontend"]["skill"]), ("react", "frontend-dev"))
        self.assertTrue(by_path["backend"]["has_code"] and by_path["frontend"]["has_code"])

    def test_single_project_has_no_candidates(self):
        for layer in ("Api", "Data", "Domain"):
            write(self.path(layer, layer + ".csproj"), "<Project />")
        write(self.path("Shop.sln"))
        result = subprojects.detect(self.root)
        self.assertFalse(result["monorepo"])
        self.assertEqual(result["projects"], [])

    def test_root_package_json_with_a_tests_package_is_not_a_monorepo(self):
        write(self.path("package.json"), package_json(react="18"))
        write(self.path("tests", "package.json"), package_json())
        write(self.path("tools", "package.json"), package_json())
        self.assertFalse(subprojects.detect(self.root)["monorepo"])

    def test_workspaces_globs_are_the_candidates(self):
        write(self.path("package.json"), json.dumps({"private": True, "workspaces": ["packages/*", "apps/web"]}))
        write(self.path("packages", "ui", "package.json"), package_json(react="18"))
        write(self.path("packages", "api", "package.json"), package_json(**{"@nestjs/core": "10"}))
        write(self.path("apps", "web", "package.json"), package_json(react="18"))
        result = subprojects.detect(self.root)
        self.assertTrue(result["monorepo"])
        stacks = {project["path"]: project["stack"] for project in result["projects"]}
        self.assertEqual(stacks, {"packages/ui": "react", "packages/api": "nestjs", "apps/web": "react"})
        self.assertIn("packages-ui", {project["name"] for project in result["projects"]})

    def test_workspaces_next_to_a_solution_is_a_single_project(self):
        write(self.path("package.json"), json.dumps({"workspaces": ["apps/*"]}))
        write(self.path("Shop.sln"))
        write(self.path("apps", "a", "package.json"), package_json())
        write(self.path("apps", "b", "package.json"), package_json())
        self.assertFalse(subprojects.detect(self.root)["monorepo"])

    def test_container_children_are_used_when_depth_one_has_none(self):
        write(self.path("apps", "web", "package.json"), package_json(react="18"))
        write(self.path("services", "billing", "Billing.csproj"), "<Project />")
        result = subprojects.detect(self.root)
        self.assertEqual({project["path"] for project in result["projects"]}, {"apps/web", "services/billing"})

    def test_ignored_names_never_count(self):
        write(self.path("backend", "Shop.sln"))
        write(self.path("node_modules", "lib", "package.json"), package_json())
        write(self.path("docs", "site", "package.json"), package_json())
        write(self.path(".hidden", "package.json"), package_json())
        write(self.path("dist", "package.json"), package_json())
        write(self.path("packages", "readme.txt"), "no project file")
        self.assertFalse(subprojects.detect(self.root)["monorepo"])
        write(self.path("frontend", "node_modules", "x", "package.json"), package_json(react="18"))
        write(self.path("frontend", "package.json"), package_json(react="18"))
        self.assertEqual({project["path"] for project in subprojects.detect(self.root)["projects"]}, {"backend", "frontend"})

    def test_nested_git_is_flagged(self):
        self.make_monorepo()
        os.makedirs(self.path("frontend", ".git"))
        projects = {project["path"]: project for project in subprojects.detect(self.root)["projects"]}
        self.assertIn("warning", projects["frontend"])
        self.assertNotIn("warning", projects["backend"])

    def test_cli_detect_json(self):
        self.make_monorepo()
        result = self.run_cli("detect", self.root, "--json")
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout)["monorepo"])


class TestStoredList(SubprojectsTestCase):
    PROJECTS = [{"path": "backend", "name": "backend", "stack": "dotnet", "skill": "backend-dev", "skip": False},
                {"path": "backend/tools", "name": "backend-tools", "stack": "unknown", "skill": "", "skip": True},
                {"path": "frontend", "name": "frontend", "stack": "react", "skill": "frontend-dev", "skip": False}]

    def test_owner_of_takes_the_longest_prefix(self):
        self.store(self.PROJECTS)
        self.assertEqual(subprojects.owner_of(self.root, "backend/Shop.Api/Program.cs")["name"], "backend")
        self.assertEqual(subprojects.owner_of(self.root, "backend/tools/seed.sh")["name"], "backend-tools")
        self.assertEqual(subprojects.owner_of(self.root, "frontend\\src\\App.tsx")["name"], "frontend")
        self.assertIsNone(subprojects.owner_of(self.root, "backendish/file.cs"))
        self.assertIsNone(subprojects.owner_of(self.root, "README.md"))

    def test_owner_of_with_repo_prefix_uses_that_repo_list(self):
        self.store(self.PROJECTS, repo="catalogo")
        self.assertIsNone(subprojects.owner_of(self.root, "frontend/x.tsx"))
        self.assertEqual(subprojects.owner_of(self.root, "catalogo:frontend/x.tsx")["name"], "frontend")
        self.assertEqual(subprojects.owner_of(self.root, "frontend/x.tsx", repo_name="catalogo")["name"], "frontend")

    def test_unreadable_list_reports_an_error_and_never_raises(self):
        write(self.path(".specs", "project", "SUBPROJECTS.json"), "{not json")
        loaded = subprojects.load(self.root)
        self.assertIsNone(loaded["entry"])
        self.assertTrue(loaded["error"])
        self.assertFalse(subprojects.monorepo_confirmed(self.root))
        self.assertIsNone(subprojects.owner_of(self.root, "backend/x.cs"))
        self.assertEqual(subprojects.for_paths_lines(self.root, ["backend/x.cs"]), [])

    def test_monorepo_confirmed_needs_both_flags(self):
        self.store(self.PROJECTS, confirmed=False)
        self.assertFalse(subprojects.monorepo_confirmed(self.root))
        self.store(self.PROJECTS, monorepo=False)
        self.assertFalse(subprojects.monorepo_confirmed(self.root))
        self.store(self.PROJECTS)
        self.assertTrue(subprojects.monorepo_confirmed(self.root))

    def test_for_paths_prints_a_line_per_touched_non_skipped_subproject(self):
        self.store(self.PROJECTS)
        write(self.path("backend", "CLAUDE.md"), "# backend\nConformance: STANDARD\n")
        result = self.run_cli("for-paths", self.root, "--paths",
                              "backend/Shop.Api/Program.cs,frontend/src/App.tsx,backend/tools/seed.sh,backend/Other.cs")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout.splitlines(), [
            "SUBPROJECT: backend | CLAUDE: backend/CLAUDE.md | MAP: backend/docs/codebase | SKILL: backend-dev | CONFORMANCE: STANDARD",
            "SUBPROJECT: frontend | CLAUDE: frontend/CLAUDE.md | MAP: frontend/docs/codebase | SKILL: frontend-dev | CONFORMANCE: -"])

    def test_for_paths_adds_system_only_with_a_subproject_line(self):
        self.store(self.PROJECTS)
        write(self.path("docs", "codebase", "SYSTEM.md"), "# system")
        with_line = self.run_cli("for-paths", self.root, "--paths", "frontend/a.tsx")
        self.assertEqual(with_line.stdout.splitlines()[-1], "SYSTEM: docs/codebase/SYSTEM.md")
        without = self.run_cli("for-paths", self.root, "--paths", "README.md")
        self.assertEqual(without.stdout, "")

    def test_for_paths_prints_nothing_for_an_unconfirmed_list(self):
        self.store(self.PROJECTS, confirmed=False)
        result = self.run_cli("for-paths", self.root, "--paths", "backend/a.cs")
        self.assertEqual((result.returncode, result.stdout), (0, ""))

    def test_for_paths_prints_nothing_for_a_single_project(self):
        write(self.path("Shop.sln"))
        write(self.path("Api", "Api.csproj"), "<Project />")
        result = self.run_cli("for-paths", self.root, "--paths", "Api/Program.cs,Shop.sln")
        self.assertEqual((result.returncode, result.stdout), (0, ""))


if __name__ == "__main__":
    unittest.main()
